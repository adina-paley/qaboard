"""
Collections: named, versioned lists of inputs saved in QA-Board (docs/design/inputs-and-collections.md).

    import qaboard
    collection = qaboard.collections.get("night/hard-cases@2")
    for item in collection:
      item.path           # relative to item.database
      item.absolute_path  # database / path

Batches run on collections with their `collections` key, next to `inputs` (qaboard/iterators.py):

    hard-cases:
      collections: [night/hard-cases@2]

The server resolves references (GET /api/v1/collections/resolve): `name` (the latest version), `name@3`,
`name@latest`, or `project:name@3` for a collection of another project. Without an explicit project, a name
resolves to the project's collection, else to the nearest ancestor project's.

Scripts save collections, or their next version, with save() (POST /api/v1/collections/save,
docs/design/annotations.md), and repositories with `qa collections save collections/night/hard-cases.yaml`:

    qaboard.collections.save("night/hard-cases", ["night/street/IMG_0042.raw"], database="/mnt/captures")

Requests go to the server `qa` sends results to, with the token it uses (qaboard/credentials.py). Importing this
module reads neither qaboard.yaml nor git: only get() and save() without `project` do, to know the current project,
and save() asks git where the script comes from.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple, Union

import click


class CollectionError(click.ClickException, ValueError):
  """We could not get a collection: the message says why, and what to do. `qa` prints "Error: <message>"."""
  def __init__(self, message: str, ref: Optional[str] = None, project: Optional[str] = None, status: Optional[int] = None):
    super().__init__(message)
    self.ref = ref
    self.project = project
    self.status = status


class CollectionNotFound(CollectionError):
  """The server doesn't know the collection, or the version, from this project."""


class CollectionConflict(CollectionError):
  """save(base_version=N) when N isn't the latest version: someone saved meanwhile. `latest_version` is the latest."""
  def __init__(self, message: str, ref: Optional[str] = None, project: Optional[str] = None, status: Optional[int] = 409,
               base_version: Optional[int] = None, latest_version: Optional[int] = None):
    super().__init__(message, ref=ref, project=project, status=status)
    self.base_version = base_version
    self.latest_version = latest_version


@dataclass(frozen=True)
class CollectionInput:
  """
  An input of a collection: `for item in collection: item.path`.
  An object, not a tuple, so that it can say more about the input as collections do (per-input options, regions).
  `data`: the input's data in the collection ({} when there is none), `region`: the region of the input it is about,
  if any (None: the whole input).
  """
  id: Optional[int]
  database: str
  path: str
  data: Dict[str, Any] = field(default_factory=dict, compare=False, hash=False)
  region: Optional[Any] = field(default=None, compare=False, hash=False)

  @property
  def absolute_path(self) -> Path:
    return Path(self.database) / self.path

  @property
  def sidecars(self) -> List[Path]:
    """The files that come with the input: `0001.metadata.yaml`, a site's `0001.imgprops`... (see sidecars())"""
    return sidecars(self.absolute_path)


# The files that come with inputs: patterns relative to the input's folder, by the input's suffix ("*": all inputs).
# `{stem}` is the input's name without its suffix, `{name}` its name. Glob characters (* ? [) are allowed, but each
# pattern with them lists a folder: prefer exact names, they cost one stat each.
DEFAULT_SIDECARS: Dict[str, List[str]] = {"*": ["{stem}.metadata.yaml", "{stem}.yaml"]}


def sidecar_rules() -> Dict[str, List[str]]:
  """
  DEFAULT_SIDECARS, with a site's rules: QABOARD_INPUT_SIDECARS (environment, or the site package's defaults,
  qaboard/site_config.py), as JSON or YAML, e.g. {".raw": ["{stem}.imgprops", "{stem}/{stem}.cde"]}. A suffix's
  patterns add to "*"'s; a site's "*" replaces the default one.
  The server and `qa` read the same setting: give it to both (the site package does).
  """
  from .site_config import site_config
  value = site_config('QABOARD_INPUT_SIDECARS')
  rules = {k: list(v) for k, v in DEFAULT_SIDECARS.items()}
  if not value:
    return rules
  try:
    import yaml
    site = yaml.safe_load(value) if isinstance(value, str) else value
    if not isinstance(site, dict):
      raise ValueError("expected a mapping of suffixes to lists of patterns")
    for suffix, patterns in site.items():
      patterns = [patterns] if isinstance(patterns, str) else list(patterns or [])
      key = '*' if suffix == '*' else ('.' + str(suffix).lstrip('.')).lower()
      rules[key] = [str(p) for p in patterns]
  except Exception as e:
    import sys
    print(f"WARNING: QABOARD_INPUT_SIDECARS is not usable ({e}): only the default sidecars are found.", file=sys.stderr)
  return rules


def sidecars(path: Path, limit: Optional[int] = None, rules: Optional[Dict[str, List[str]]] = None) -> List[Path]:
  """
  The files that come with an input, by the sidecar rules (sidecar_rules()): `0001.raw` has `0001.metadata.yaml` and,
  with a site's rules, `0001.imgprops` or `0001/0001.cde`. Existing files only, in the rules' order, at most `limit`.
  Nothing is stored: they're found where they are, when asked. The web app lists the same (GET /inputs/<id>).
  """
  import glob
  path = Path(path)
  rules = sidecar_rules() if rules is None else rules
  patterns = [*rules.get('*', []), *rules.get(path.suffix.lower(), [])] if path.suffix else list(rules.get('*', []))
  found: List[Path] = []
  for pattern in patterns:
    if limit is not None and len(found) >= limit:
      break
    try:
      if any(c in pattern for c in '*?['):
        relative = pattern.format(stem=glob.escape(path.stem), name=glob.escape(path.name))
        candidates = sorted(path.parent.glob(relative))
      else:
        candidates = [path.parent / pattern.format(stem=path.stem, name=path.name)]
      for candidate in candidates:
        if candidate != path and candidate not in found and candidate.is_file():
          found.append(candidate)
    except (OSError, KeyError, IndexError, ValueError):
      continue
  return found[:limit]


@dataclass
class Collection:
  """
  A version of a collection, as the server resolved a reference.
  `project` is the project that owns the collection (maybe an ancestor of the one asked from), `ref` the reference
  that was asked for, `name` and `version` what it resolved to.
  """
  ref: str
  project: Optional[str]
  name: str
  version: int
  inputs: List[CollectionInput] = field(default_factory=list)
  id: Optional[int] = None
  # save(): the inputs were those of the latest version already, nothing was saved (`version` is that version)
  unchanged: bool = False

  @property
  def pinned_ref(self) -> str:
    """A reference to this exact version, e.g. "night/hard-cases@2", for reproducible batches"""
    return f"{self.name}@{self.version}"

  def __len__(self) -> int:
    return len(self.inputs)

  def __iter__(self) -> Iterator[CollectionInput]:
    return iter(self.inputs)

  def to_dict(self) -> Dict[str, Any]:
    return {
      "id": self.id, "ref": self.ref, "project": self.project, "name": self.name, "version": self.version,
      "inputs": [_input_dict(i) for i in self.inputs],
    }


def _input_dict(i: CollectionInput) -> Dict[str, Any]:
  """{id, database, path}, with data and region only when they are set: `qa collections show --json` prints it"""
  return {"id": i.id, "database": i.database, "path": i.path, **({"data": i.data} if i.data else {}), **({"region": i.region} if i.region is not None else {})}


def _input_from(i: Dict[str, Any]) -> CollectionInput:
  """A CollectionInput from {id, database, path, data?, region?}, as the server answers (or Collection.to_dict wrote)"""
  data = i.get('data')
  return CollectionInput(id=_int_or_none(i.get('id')), database=str(i.get('database') or '.'), path=str(i['path']),
                         data=dict(data) if isinstance(data, dict) else {}, region=i.get('region'))


# The server lists the runs of batches with the project's code in a `qa batch --list` it starts, without a token to
# ask it for collections: it resolves those of the batch files first, and gives them in this file (backend/api/tuning.py).
# JSON: {"collections": {"<ref as written>": Collection.to_dict() | {"error": message, "status": 404}}}
RESOLVED_COLLECTIONS_ENV = 'QABOARD_RESOLVED_COLLECTIONS'


def resolver_from_file(path: str):
  """A resolver (ref, project) -> Collection for qaboard.iterators, that only reads the server's file: never the network"""
  import json
  problem: Optional[str] = None
  try:
    with open(path, encoding='utf-8') as f:
      resolved = json.load(f).get('collections') or {}
  except (OSError, ValueError, AttributeError) as e:
    resolved, problem = {}, str(e)

  def resolve(ref: str, project: Optional[str] = None) -> Collection:
    found = resolved.get(ref.strip())
    if found is None:
      raise CollectionError(
        f"QA-Board lists this batch with the project's code, and couldn't resolve the collection {ref!r} for it"
        f"{f' ({problem})' if problem else ''}: it only resolves the `collections` written in the batch files. "
        "Write it in the batch's `collections` in a batch file, or start the batch from the CLI.", ref=ref, project=project)
    if 'error' in found:
      status = found.get('status')
      raise (CollectionNotFound if status == 404 else CollectionError)(str(found['error']), ref=ref, project=project, status=status)
    return Collection(
      ref=ref, project=found.get('project'), name=str(found.get('name') or ''), version=int(found['version']), id=found.get('id'),
      inputs=[_input_from(i) for i in found.get('inputs') or [] if isinstance(i, dict) and 'path' in i],
    )
  return resolve


# project:name@version, the project and the version optional
_REF = re.compile(r'^(?:(?P<project>[^:@]+):)?(?P<name>[^:@]+)(?:@(?P<version>[^:@]*))?$')


def parse_ref(ref: str) -> Tuple[Optional[str], str, Optional[int]]:
  """
  ("project" or None, "name", version or None for the latest) from a reference like `a/b:night/hard@2`.
  Only checks the shape: the server normalizes names.
  """
  match = _REF.match(ref.strip()) if isinstance(ref, str) else None
  if not match or not match.group('name').strip():
    raise CollectionError(f'Invalid collection reference {ref!r}: write "name", "name@3", "name@latest" or "project:name@3".', ref=str(ref))
  version_str = match.group('version')
  version: Optional[int] = None
  if version_str is not None and version_str != 'latest':
    if not version_str.isdigit() or int(version_str) < 1:
      raise CollectionError(f'Invalid version in the collection reference {ref!r}: write "@3" for version 3, or "@latest".', ref=ref)
    version = int(version_str)
  project = match.group('project').strip() if match.group('project') else None
  return project, match.group('name').strip(), version


def current_project() -> Optional[str]:
  """The id of the (sub)project of the current directory's qaboard.yaml, e.g. "group/repo/subproject", like `qa` uses."""
  from .config import project
  return Path(project).as_posix() if project else None


def get(ref: str, project: Optional[str] = None, *, server: Optional[str] = None, token: Optional[str] = None) -> Collection:
  """
  The inputs of a collection, from QA-Board: `get("night/hard-cases@2")`.
  `ref`: `name` (the latest version), `name@3`, `name@latest`, or `project:name@3`.
  `project`: the project to resolve the name from. By default the project the reference names (`a/b:night@2`), else
  the project of the current directory's qaboard.yaml.
  `server`/`token`: by default those `qa` uses.
  Raises CollectionNotFound when there is no such collection or version, CollectionError for other problems.
  """
  ref_project, name, pinned_version = parse_ref(ref)
  ref = ref.strip()
  if project is None:
    # `a/b:night@2` says where it is: no need for a qaboard.yaml (nor to read it)
    project = ref_project or current_project()
  if not project:
    raise CollectionError(f"To find the collection {ref!r}, give the project: qaboard.collections.get({ref!r}, project=\"group/repo\"), "
                          "or run from the folder of a project's qaboard.yaml.", ref=ref)
  from .credentials import default_server
  from .results import _request, ResultsError
  server = (server or default_server()[0]).rstrip('/')
  try:
    data = _request(server, 'collections/resolve', {"project": project, "ref": ref}, token)
  except ResultsError as e:
    if e.status == 404:
      error = e.body.get('error') if isinstance(e.body, dict) else None
      raise CollectionNotFound(
        f"No collection {ref!r} for the project {project}{f' ({error})' if error else ''}. "
        f"Check its name and version on the project's Collections in QA-Board ({server}): "
        "collections belong to a project or to one of its ancestors.", ref=ref, project=project, status=404) from None
    if e.status is None:
      from .credentials import describe_network_error
      reason = describe_network_error(e.__cause__) if isinstance(e.__cause__, Exception) else str(e)
      raise CollectionError(
        f"Could not reach QA-Board at {server} to resolve the collection {ref!r}: {reason}. Collections are resolved by "
        "the server: check that it is up and that QABOARD_URL (or QABOARD_HOST) points to it.", ref=ref, project=project) from None
    raise CollectionError(f"QA-Board could not resolve the collection {ref!r} for the project {project}: {e}", ref=ref, project=project, status=e.status) from None
  collection = from_api(data, ref=ref, project=project)
  if pinned_version is not None and collection.version != pinned_version:
    raise CollectionError(f"Asked for {ref!r} but QA-Board answered version {collection.version}.", ref=ref, project=project)
  if not collection.name:
    collection.name = name
  return collection


def _int_or_none(value: Any) -> Optional[int]:
  try:
    return int(value)
  except (TypeError, ValueError):
    return None


def from_api(data: Dict[str, Any], ref: str, project: Optional[str] = None) -> Collection:
  """
  A Collection from GET /collections/resolve: {collection, version, project, inputs: [{id, database, path, data?, region?}]}.
  `collection` is the Collection object (or its name), `version` the version number (or its object).
  """
  if not isinstance(data, dict):
    raise CollectionError(f"Unexpected answer from QA-Board when resolving the collection {ref!r}: {str(data)[:200]}", ref=ref, project=project)
  described = data.get('collection')
  collection = described if isinstance(described, dict) else {}
  name = collection.get('name') or (described if isinstance(described, str) else None) or data.get('name') or ''
  version = data.get('version')
  if isinstance(version, dict):
    version = version.get('version')
  version_number = _int_or_none(version)
  if version_number is None:
    raise CollectionError(f"Unexpected answer from QA-Board when resolving the collection {ref!r}: no version.", ref=ref, project=project)
  inputs = []
  for i in data.get('inputs') or []:
    if not isinstance(i, dict) or 'path' not in i:
      continue
    inputs.append(_input_from(i))
  return Collection(
    ref=ref,
    project=data.get('project') or collection.get('project') or project,
    name=str(name),
    version=version_number,
    inputs=inputs,
    id=_int_or_none(collection.get('id')),
  )


def _without_credentials(url: str) -> str:
  """A git remote's URL as others can read it: https://user:token@host/a/b.git => https://host/a/b.git"""
  from urllib.parse import urlsplit, urlunsplit
  url = url.strip()
  if '://' not in url:
    return url  # a path, or git@host:a/b.git: its user is no secret
  try:
    parts = urlsplit(url)
  except ValueError:
    return ''
  netloc = parts.netloc.rsplit('@', 1)[-1]
  return urlunsplit((parts.scheme, netloc, parts.path, '', ''))


def _git_source(folder: Path) -> Optional[Dict[str, Any]]:
  """
  {repository, commit, dirty} of the git repository of a folder: None outside git or without git. What git doesn't
  say in time stays None. Never raises: a save doesn't fail for it.
  """
  source: Optional[Dict[str, Any]] = None
  try:
    from .local_changes import _Budget, _git, _Timeout
  except Exception:  # pragma: no cover
    return None
  try:
    budget = _Budget(10)
    commit = _git(folder, ['rev-parse', '--verify', '--quiet', 'HEAD'], budget)
    if not commit:
      return None
    source = {"repository": None, "commit": commit.decode().strip(), "dirty": None}
    url = _git(folder, ['config', '--get', 'remote.origin.url'], budget)
    if not url:
      remotes = (_git(folder, ['remote'], budget) or b'').decode().split()
      url = _git(folder, ['remote', 'get-url', remotes[0]], budget) if remotes else None
    if url:
      source['repository'] = _without_credentials(url.decode()) or None
    # changes to tracked files: what the commit doesn't say about the script
    status = _git(folder, ['status', '--porcelain', '--untracked-files=no', '--ignore-submodules=dirty'], budget)
    if status is not None:
      source['dirty'] = bool(status.strip())
  except _Timeout:
    pass
  except Exception as e:
    import logging
    logging.getLogger(__name__).debug("collections: no git information: %r", e)
  return source


def _script() -> Tuple[str, Optional[Path]]:
  """(what started this process, the folder whose git repository it comes from): a script, a notebook, or "<interactive>" """
  import os
  import sys
  argv0 = sys.argv[0] if sys.argv else ''
  if 'ipykernel' in sys.modules and ('ipykernel' in argv0 or 'colab' in argv0):
    main = sys.modules.get('__main__')
    # VS Code says which notebook runs, Jupyter (JPY_SESSION_NAME) too
    notebook = getattr(main, '__vsc_ipynb_file__', None) or os.environ.get('JPY_SESSION_NAME')
    if notebook and Path(notebook).is_file():
      path = Path(notebook).resolve()
      return path.as_posix(), path.parent
    return "<notebook>", None
  if argv0 in ('', '-c', '-') or hasattr(sys, 'ps1'):
    return "<interactive>", None
  path = Path(argv0)
  try:
    path = path.resolve()
  except OSError:
    path = path.absolute()
  return path.as_posix(), path.parent if path.is_file() else None


def source_of(script: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
  """
  Where a version saved from code comes from: {script, argv, host, cwd, git: {repository, commit, dirty} | None}.
  `script`: by default the script that runs (sys.argv[0]), "<notebook>" or "<interactive>". git describes the
  repository of the script's folder, else of the current directory; None outside git. Never raises.
  """
  import socket
  import sys
  if script is None:
    script_name, folder = _script()
  else:
    path = Path(script).absolute()
    script_name, folder = path.as_posix(), path.parent
  try:
    cwd: Optional[str] = Path.cwd().as_posix()
  except OSError:
    cwd = None
  try:
    host: Optional[str] = socket.gethostname()
  except OSError:
    host = None
  git_folder = folder if folder is not None and folder.is_dir() else (Path(cwd) if cwd else None)
  return {
    "script": script_name,
    "argv": [str(a) for a in sys.argv],
    "host": host,
    "cwd": cwd,
    "git": _git_source(git_folder) if git_folder is not None else None,
  }


InputItem = Union[str, Path, CollectionInput, Tuple[Any, Any], Dict[str, Any]]


def _input_key(item: Dict[str, Any]) -> Tuple[Any, ...]:
  return ('id', item['id']) if 'id' in item else ('path', item['database'], item['path'])


def to_save(inputs: Union['Collection', Iterable[InputItem]], database: Optional[Union[str, Path]] = None) -> Tuple[List[Any], List[CollectionInput]]:
  """
  (what POST /collections/save takes: ids and {database, path}, the same as CollectionInputs), once each, in order.
  Paths are resolved like qaboard.inputs.add() and runs do (inputs.as_input).
  """
  from .inputs import as_input
  if isinstance(inputs, (str, Path)):
    raise CollectionError(f"`inputs` is a list of paths: [{str(inputs)!r}], not one path.")
  sent: List[Any] = []
  items: List[CollectionInput] = []
  seen = set()

  def add(item: Any) -> None:
    if isinstance(item, Collection):
      for i in item.inputs:
        add(i)
      return
    if isinstance(item, CollectionInput):
      body: Dict[str, Any] = {"id": item.id} if item.id is not None else {"database": item.database, "path": item.path}
      as_item = item
    else:
      found = as_input(item, database)
      body = {"database": found['database'], "path": found['path']}
      as_item = CollectionInput(id=None, database=found['database'], path=found['path'])
    key = _input_key(body)
    if key in seen:
      return
    seen.add(key)
    sent.append(body['id'] if 'id' in body else body)
    items.append(as_item)

  for item in inputs:
    add(item)
  return sent, items


def _check_name(name: Any) -> str:
  if not isinstance(name, str) or not name.strip():
    raise CollectionError(f"A collection needs a name, like \"night/hard-cases\", not {name!r}.")
  ref_project, _, version = parse_ref(name)
  if ref_project is not None or version is not None or '@' in name:
    raise CollectionError(f"Save {name!r} with its name only, e.g. \"night/hard-cases\": save() makes the next version, "
                          "and `project=` says in which project.", ref=name)
  return name.strip()


def save(name: str, inputs: Union['Collection', Iterable[InputItem]], *, database: Optional[Union[str, Path]] = None,
         project: Optional[str] = None, notes: Optional[str] = None, description: Optional[str] = None,
         base_version: Optional[int] = None, source: Optional[Dict[str, Any]] = None,
         server: Optional[str] = None, token: Optional[str] = None) -> Collection:
  """
  Saves a collection, or its next version: `save("night/hard-cases", ["night/street/IMG_0042.raw"], database="/mnt/captures")`.
  `inputs`: paths relative to `database` (or absolute), `CollectionInput`s (e.g. from get()), (database, path) pairs,
  or a `Collection`. Inputs the project doesn't have yet are added, like qaboard.inputs.add().
  `notes`: about this version; `description`: of the collection. `base_version`: the version your changes start
  from: when someone saved another meanwhile, raises CollectionConflict instead of saving.
  `project`: by default the project of the current directory's qaboard.yaml. `server`/`token`: by default those `qa` uses.
  `source`: where the version comes from, by default this script, its command line, host and git commit (source_of()).
  Returns the Collection saved: `.version`, and `.unchanged` when the inputs were already the latest version's (then
  nothing is saved). Raises CollectionError when it can't.
  """
  name = _check_name(name)
  project = project or current_project()
  if not project:
    raise CollectionError(f"To save the collection {name!r}, give the project: qaboard.collections.save({name!r}, inputs, project=\"group/repo\"), "
                          "or run from the folder of a project's qaboard.yaml.", ref=name)
  sent, items = to_save(inputs, database)
  if not sent:
    raise CollectionError(f"No inputs to save in the collection {name!r}.", ref=name, project=project)
  if base_version is not None and (isinstance(base_version, bool) or not isinstance(base_version, int) or base_version < 1):
    raise CollectionError(f"`base_version` is a version number, like 3, not {base_version!r}.", ref=name, project=project)
  body: Dict[str, Any] = {"project": project, "name": name, "inputs": sent}
  for key, value in (("notes", notes), ("description", description), ("base_version", base_version)):
    if value is not None:
      body[key] = value
  body["source"] = source_of() if source is None else source
  from .credentials import default_server
  from .results import _request, ResultsError
  server = (server or default_server()[0]).rstrip('/')
  try:
    data = _request(server, 'collections/save', None, token, method='POST', json=body)
  except ResultsError as e:
    answer = e.body if isinstance(e.body, dict) else {}
    error = answer.get('error')
    if e.status == 409 and (e.reason or answer.get('reason')) == 'stale_version':
      latest = _int_or_none(answer.get('latest_version'))
      raise CollectionConflict(
        f"Version {latest or '?'} of {name!r} was saved after version {base_version}: nothing was saved. Look at it "
        f"(qaboard.collections.get({name!r})), then save with base_version={latest or 'its version'}.",
        ref=name, project=project, base_version=base_version, latest_version=latest) from None
    if e.status == 404:
      raise CollectionNotFound(f"QA-Board could not save the collection {name!r}: {error or e}. Check the project's name ({project}).",
                               ref=name, project=project, status=404) from None
    if e.status is None:
      from .credentials import describe_network_error
      reason = describe_network_error(e.__cause__) if isinstance(e.__cause__, Exception) else str(e)
      raise CollectionError(f"Could not reach QA-Board at {server} to save the collection {name!r}: {reason}. Nothing was saved.",
                            ref=name, project=project) from None
    raise CollectionError(f"QA-Board could not save the collection {name!r} in {project}: {error or e}", ref=name, project=project, status=e.status) from None
  collection = from_api(data, ref=name, project=project)
  if collection.id is None:
    collection.id = _int_or_none(data.get('id'))
  if not collection.name:
    collection.name = name
  if not collection.inputs:
    collection.inputs = items
  collection.unchanged = bool(data.get('unchanged'))
  return collection


@click.group(name='collections')
def collections():
  """Collections of inputs saved in QA-Board."""
  pass


@collections.command(name='show')
@click.argument('ref')
@click.option('--project', default=None, help="The project to resolve the name from. Default: the project of the current qaboard.yaml.")
@click.option('--json', 'as_json', is_flag=True, help="Print the collection as JSON: {id, ref, project, name, version, inputs: [{id, database, path}]}")
def show(ref, project, as_json):
  """
  Prints the inputs of a collection, one "database<TAB>path" per line, e.g. `qa collections show night/hard-cases@2`.
  REF: name (the latest version), name@3, name@latest, or project:name@3.
  """
  collection = get(ref, project=project)
  if as_json:
    import json
    click.echo(json.dumps(collection.to_dict(), indent=2))
    return
  owner = f"{collection.project}:" if collection.project and collection.project != (project or current_project()) else ""
  click.secho(f"{owner}{collection.pinned_ref}: {len(collection.inputs)} input{'s' if len(collection.inputs) != 1 else ''}", fg='cyan', err=True)
  for i in collection.inputs:
    click.echo(f"{i.database}\t{i.path}")


# `qa collections save FILE`: the keys of a collection's YAML file, and the suffixes of lists of paths
COLLECTION_FILE_KEYS = ('name', 'description', 'database', 'inputs')
YAML_SUFFIXES = ('.yaml', '.yml')
LIST_SUFFIXES = ('.txt', '.lst', '.list')
GLOB_CHARACTERS = '*?['
# how many added and removed inputs `qa collections save --dryrun` lists
SHOWN_CHANGES = 5


@dataclass
class CollectionFile:
  """What a collection's file says: a YAML file {name?, description?, database?, inputs}, or a list of paths"""
  path: Path
  inputs: List[str]
  name: Optional[str] = None
  description: Optional[str] = None
  database: Optional[Any] = None


def read_collection_file(path: Path) -> CollectionFile:
  """
  A collection's file: YAML (.yaml, .yml) with `inputs` and optionally `name`, `description`, `database`, or a list
  of paths, one per line (.txt), where `#` starts a comment line. Raises CollectionError when it isn't one.
  """
  path = Path(path)
  suffix = path.suffix.lower()
  if suffix not in (*YAML_SUFFIXES, *LIST_SUFFIXES):
    raise CollectionError(f"{path}: a collection's file is YAML (.yaml, .yml) or a list of paths, one per line (.txt).")
  try:
    text = path.read_text(encoding='utf-8')
  except (OSError, UnicodeDecodeError) as e:
    raise CollectionError(f"Could not read {path}: {e}") from None
  if suffix in LIST_SUFFIXES:
    lines = [line.strip() for line in text.splitlines()]
    inputs = [line for line in lines if line and not line.startswith('#')]
    if not inputs:
      raise CollectionError(f"{path} lists no inputs: write one path per line, relative to the database or absolute.")
    return CollectionFile(path=path, inputs=inputs)

  import yaml
  try:
    data = yaml.safe_load(text)
  except yaml.YAMLError as e:
    raise CollectionError(f"{path} is not valid YAML: {e}") from None
  if not isinstance(data, dict):
    raise CollectionError(f"{path}: expected a mapping with `inputs` (and optionally {', '.join(k for k in COLLECTION_FILE_KEYS if k != 'inputs')}), "
                          f"not {type(data).__name__ if data is not None else 'an empty file'}.")
  unknown = [str(k) for k in data if k not in COLLECTION_FILE_KEYS]
  if unknown:
    raise CollectionError(f"{path}: unknown key{'s' if len(unknown) > 1 else ''} {', '.join(map(repr, unknown))}. "
                          f"A collection's file has {', '.join(COLLECTION_FILE_KEYS)}.")
  listed: Any = data.get('inputs')
  if isinstance(listed, str):
    listed = [listed]
  if not listed:
    raise CollectionError(f"{path} has no inputs: list them under `inputs`, relative to the database or absolute.")
  if not isinstance(listed, list) or not all(isinstance(i, str) and i.strip() for i in listed):
    wrong = next((i for i in listed if not isinstance(i, str) or not i.strip()), listed) if isinstance(listed, list) else listed
    raise CollectionError(f"{path}: `inputs` is a list of paths (globs work), not {wrong!r}.")
  for key in ('name', 'description'):
    if data.get(key) is not None and not isinstance(data[key], str):
      raise CollectionError(f"{path}: `{key}` is text, not {data[key]!r}.")
  return CollectionFile(path=path, inputs=[i.strip() for i in listed], name=data.get('name') or None,
                        description=data.get('description'), database=data.get('database'))


def name_from_path(path: Path) -> Optional[str]:
  """The default name of a collection's file: its path under a `collections/` folder, without its suffix. None outside one."""
  parts = Path(path).absolute().parts
  folders = [index for index, part in enumerate(parts[:-1]) if part == 'collections']
  if not folders:
    return None
  return '/'.join([*parts[folders[-1] + 1:-1], Path(parts[-1]).stem])


def expand_inputs(entries: Iterable[str], database: Optional[Path], inputs_settings: Optional[Dict[str, Any]] = None,
                  qatools_config: Optional[Dict[str, Any]] = None) -> List[InputItem]:
  """
  The inputs of a collection's file, for save(): paths as they are, and globs expanded like batch files do
  (qaboard/iterators.py): `night/*.raw`, or folders searched with the input type's globs (inputs.globs in qaboard.yaml).
  Raises CollectionError when a glob matches nothing: a typo would otherwise drop inputs silently.
  """
  from .iterators import iter_inputs_at_path
  settings = inputs_settings or {}
  globs = settings.get('globs', settings.get('glob'))
  if globs and not isinstance(globs, (list, tuple)):
    globs = [globs]
  use_parent_folder = settings.get('use_parent_folder', False)
  items: List[InputItem] = []
  for entry in entries:
    path = Path(entry)
    absolute = path.is_absolute()
    base, pattern = (Path(path.anchor), path.relative_to(path.anchor)) if absolute else (database, path)
    has_glob = any(c in entry for c in GLOB_CHARACTERS)
    is_folder = bool(not has_glob and globs and base is not None and (base / pattern).is_dir())
    if not has_glob and not is_folder:
      items.append(entry)
      continue
    if base is None:
      raise CollectionError(f"In which database is {entry}? Give `database` in the file, or --database.")
    if not sorted(base.glob(pattern.as_posix())):
      raise CollectionError(f'No inputs match "{entry}" in {base}.')
    if qatools_config is None:
      from .config import config as qatools_config  # what metadata() gets, for `qa batch`'s QA_BATCH_ALWAYS_GET_METADATA
    found = [i for i, _ in iter_inputs_at_path(pattern, base, globs, use_parent_folder, qatools_config)]
    if not found:
      raise CollectionError(f'No inputs match "{entry}" in {base} with the globs {globs}.')
    for i in found:
      items.append(i.as_posix() if absolute else (database, i.relative_to(base)))
  return items


def changes(latest: Optional[Collection], items: List[CollectionInput]) -> Tuple[List[CollectionInput], List[CollectionInput]]:
  """(added, removed): what saving `items` changes in the `latest` version (None: a new collection)"""
  if latest is None:
    return list(items), []
  old_ids = {i.id for i in latest.inputs if i.id is not None}
  old_paths = {(i.database, i.path) for i in latest.inputs}
  new_ids = {i.id for i in items if i.id is not None}
  new_paths = {(i.database, i.path) for i in items}
  added = [i for i in items if not ((i.id is not None and i.id in old_ids) or (i.database, i.path) in old_paths)]
  removed = [i for i in latest.inputs if not ((i.id is not None and i.id in new_ids) or (i.database, i.path) in new_paths)]
  return added, removed


def _plural(n: int, word: str = 'input') -> str:
  return f"{n:,} {word}{'s' if n != 1 else ''}"


def _show_changes(sign: str, inputs: List[CollectionInput]) -> None:
  for i in inputs[:SHOWN_CHANGES]:
    click.echo(f"{sign} {i.database}\t{i.path}")
  if len(inputs) > SHOWN_CHANGES:
    click.echo(f"{sign} ... and {len(inputs) - SHOWN_CHANGES:,} more")


def _find_file(path: Path, previous_cwd: Optional[str]) -> Path:
  """A file users give: relative to where they started `qa` (it moves to the qaboard.yaml's folder), else to the project"""
  if path.is_absolute():
    candidates = [path]
  else:
    candidates = [*([Path(previous_cwd) / path] if previous_cwd else []), Path.cwd() / path]
  found = next((c for c in candidates if c.is_file()), None)
  if found is None:
    raise CollectionError(f"Could not find the file {path}" + (f" (in {previous_cwd} nor {Path.cwd()})" if previous_cwd and not path.is_absolute() else "") + ".")
  return found


@collections.command(name='save')
@click.argument('file', type=click.Path(dir_okay=False, path_type=Path))
@click.option('--name', default=None, help="The collection's name. Default: `name` in the file, else its path under a `collections/` folder, without its suffix.")
@click.option('--database', default=None, help="Where relative paths are. Default: `database` in the file, else the project's (inputs.database), like batch files.")
@click.option('--notes', default=None, help="About this version, shown with it.")
@click.option('--description', default=None, help="The collection's description. Default: `description` in the file.")
@click.option('--project', default=None, help="Default: the project of the current qaboard.yaml.")
@click.option('--dryrun', is_flag=True, help="Only print what would change against the latest version (like `qa --dryrun`).")
@click.pass_context
def save_command(ctx, file, name, database, notes, description, project, dryrun):
  """
  Saves a collection, or its next version, from a file: `qa collections save collections/night/hard-cases.yaml`.

  \b
  FILE: YAML, or a list of paths (.txt, one per line, # for comments):
    name: night/hard-cases       # optional: by default the file's path under a `collections/` folder
    description: Street lights that fool contrast AF
    database: /mnt/captures      # optional, like batch files
    inputs:
      - night/street/IMG_0042.raw
      - night/street/*.raw       # globs work like in batch files

  An unchanged file saves nothing. Prints the version saved, e.g. night/hard-cases@3.
  """
  from .conventions import location_from_spec
  obj = ctx.obj or {}
  dryrun = dryrun or bool(obj.get('dryrun'))
  location = _find_file(file, obj.get('previous_cwd'))
  spec = read_collection_file(location)
  name = name or spec.name or name_from_path(location)
  if not name:
    raise click.UsageError(f"Which collection? {file} isn't in a `collections/` folder: give `name:` in it, or --name.")
  name = _check_name(name)
  try:
    if database:
      db: Optional[Path] = location_from_spec(database)
    elif spec.database is not None:
      db = location_from_spec(spec.database)
    else:
      db = Path(obj['database']) if obj.get('database') else None
  except ValueError as e:
    raise CollectionError(f"{location}: invalid `database`: {e}") from None
  description = description if description is not None else spec.description
  project = project or current_project()
  if not project:
    raise CollectionError("In which project? Give --project, or run from the folder of a project's qaboard.yaml.", ref=name)

  items = expand_inputs(spec.inputs, db, obj.get('inputs_settings'))

  if dryrun:
    _, inputs = to_save(items, db)
    try:
      latest: Optional[Collection] = get(f"{project}:{name}", project=project)
    except CollectionNotFound:
      latest = None
    added, removed = changes(latest, inputs)
    if latest is None:
      click.secho(f"{name} is new in {project}: saving would create it with {_plural(len(inputs))}.", fg='cyan', err=True)
    elif not added and not removed:
      click.secho(f"{latest.pinned_ref} already has these {_plural(len(inputs))}: nothing to save.", fg='cyan', err=True)
      return
    else:
      click.secho(f"{latest.pinned_ref} has {_plural(len(latest.inputs))}. Saving would add {len(added):,} and remove {len(removed):,}: "
                  f"version {latest.version + 1} with {_plural(len(inputs))}.", fg='cyan', err=True)
    _show_changes('+', added)
    _show_changes('-', removed)
    click.secho("Dry run: nothing saved.", fg='cyan', err=True)
    return

  collection = save(name, items, database=db, project=project, notes=notes, description=description, source=source_of(location))
  if collection.unchanged:
    click.secho(f"{collection.pinned_ref} already has these {_plural(len(collection.inputs))}: nothing saved.", fg='green', err=True)
  else:
    click.secho(f"Saved {collection.pinned_ref} in {collection.project or project}: {_plural(len(collection.inputs))}.", fg='green', err=True)
  click.echo(collection.pinned_ref)
