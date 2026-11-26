"""
Tests for saving collections from code and files: `qaboard.collections.save()` and `qa collections save`
(qaboard/input_collections.py), with the server faked: requests get the answers of POST /api/v1/collections/save and
GET /api/v1/collections/resolve (docs/design/annotations.md, "CLI and SDK").
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from importlib import import_module, reload
from pathlib import Path
from unittest import mock

import click
import requests
from click.testing import CliRunner

ROOT = Path(__file__).resolve().parent.parent
SERVER = 'http://qaboard.example.com'


def response(status, body, path='collections/save'):
  r = requests.Response()
  r.status_code = status
  r._content = json.dumps(body).encode()
  r.headers['Content-Type'] = 'application/json'
  r.url = f"{SERVER}/api/v1/{path}"
  return r


class FakeServer:
  """
  Answers POST /collections/save and GET /collections/resolve like the real endpoints: input ids by (database, path),
  versions per (project, name), `unchanged` when the inputs are the latest version's, 409 stale_version. Keeps the requests.
  """
  def __init__(self, minimal=False, status=None):
    self.requests = []
    self.ids = {}
    self.versions = {}  # (project, name) => [[input ids], ...]
    self.minimal = minimal  # answers without inputs, a Collection with its version object
    self.status = status

  def input(self, item):
    if isinstance(item, int):
      key = next(k for k, v in self.ids.items() if v == item)
    else:
      key = (item['database'], item['path'])
      self.ids.setdefault(key, len(self.ids) + 1)
    return {"id": self.ids[key], "database": key[0], "path": key[1]}

  def __call__(self, method, url, params=None, headers=None, json=None, **kwargs):
    self.requests.append({"method": method, "url": url, "params": dict(params or {}), "json": json, "headers": dict(headers or {})})
    if self.status:
      return response(self.status, {"error": "Forbidden", "reason": "missing_role"})
    if url.endswith('/api/v1/collections/resolve'):
      project, name = params['ref'].split(':', 1) if ':' in params['ref'] else (params['project'], params['ref'])
      versions = self.versions.get((project, name))
      if not versions:
        return response(404, {"error": f"Unknown collection {params['ref']}"}, 'collections/resolve')
      inputs = [self.input(i) for i in versions[-1]]
      return response(200, {"collection": {"id": 7, "project": project, "name": name}, "version": len(versions), "project": project, "inputs": inputs})
    if not url.endswith('/api/v1/collections/save') or method != 'POST':
      return response(404, {"error": "not found"})
    if json['project'] == 'unknown/project':
      return response(404, {"error": "Unknown project unknown/project"})
    versions = self.versions.setdefault((json['project'], json['name']), [])
    if json.get('base_version') is not None and json['base_version'] != len(versions):
      return response(409, {"error": "Someone saved meanwhile", "reason": "stale_version", "base_version": json['base_version'], "latest_version": len(versions)})
    inputs = [self.input(i) for i in json['inputs']]
    ids = [i['id'] for i in inputs]
    unchanged = bool(versions) and versions[-1] == ids
    if not unchanged:
      versions.append(ids)
    collection = {"id": 7, "project": json['project'], "name": json['name'], "latest_version": len(versions)}
    if self.minimal:
      return response(200, {**collection, "version": {"version": len(versions), "notes": json.get('notes')}, "unchanged": unchanged})
    return response(200, {"collection": collection, "version": len(versions), "project": json['project'], "inputs": inputs, "unchanged": unchanged})

  def patch(self):
    environ = {k: v for k, v in os.environ.items() if not k.startswith(('QA_', 'QABOARD_'))}
    environ.update({'QABOARD_URL': SERVER, 'QABOARD_TOKEN': 'qab_secret'})
    patches = [mock.patch('requests.Session.request', side_effect=self), mock.patch.dict(os.environ, environ, clear=True)]
    for p in patches:
      p.start()
    return patches


SOURCE = {"script": "/repo/tools/triage.py", "argv": ["tools/triage.py"], "host": "h", "cwd": "/repo", "git": None}


class FakeServerTestCase(unittest.TestCase):
  def serve(self, **kwargs):
    server = FakeServer(**kwargs)
    for p in server.patch():
      self.addCleanup(p.stop)
    return server

  def saves(self, server):
    return [r['json'] for r in server.requests if r['url'].endswith('/collections/save')]


class TestSave(FakeServerTestCase):
  def test_saves_paths_of_a_database(self):
    import qaboard
    server = self.serve()
    collection = qaboard.collections.save("night/hard-cases", ["night/1.raw", Path("night/2.raw"), "/mnt/data/night/3.raw"],
                                          database="/mnt/data", project="group/repo", notes="from triage", source=SOURCE)
    request, = server.requests
    self.assertEqual((request['method'], request['url']), ('POST', f"{SERVER}/api/v1/collections/save"))
    self.assertIn('qab_secret', request['headers'].get('Authorization', ''))
    self.assertEqual(request['json'], {
      "project": "group/repo", "name": "night/hard-cases", "notes": "from triage", "source": SOURCE,
      "inputs": [{"database": "/mnt/data", "path": "night/1.raw"}, {"database": "/mnt/data", "path": "night/2.raw"},
                 {"database": "/mnt/data", "path": "night/3.raw"}],
    })
    self.assertEqual((collection.name, collection.version, collection.project, collection.id, collection.unchanged), ("night/hard-cases", 1, "group/repo", 7, False))
    self.assertEqual(collection.pinned_ref, "night/hard-cases@1")
    self.assertEqual([(i.id, i.path) for i in collection], [(1, "night/1.raw"), (2, "night/2.raw"), (3, "night/3.raw")])
    # again: unchanged, the same version
    again = qaboard.collections.save("night/hard-cases", ["night/1.raw", "night/2.raw", "night/3.raw"], database="/mnt/data", project="group/repo", source=SOURCE)
    self.assertEqual((again.version, again.unchanged), (1, True))
    # another list: the next version
    self.assertEqual(qaboard.collections.save("night/hard-cases", ["night/1.raw"], database="/mnt/data", project="group/repo", source=SOURCE).version, 2)

  def test_paths_are_resolved_like_runs(self):
    from qaboard.input_collections import to_save, CollectionInput
    with tempfile.TemporaryDirectory() as tmp:
      real = Path(tmp).resolve() / 'data'
      real.mkdir()
      (Path(tmp) / 'link').symlink_to(real)
      sent, items = to_save(['a.raw', f'{tmp}/link/b.raw', '/other/c.raw', ('/d', 'e.raw')], f'{tmp}/link/')
    self.assertEqual(sent, [
      {"database": real.as_posix(), "path": "a.raw"},
      # absolute paths are relative to the database when they're in it, else under /
      {"database": real.as_posix(), "path": "b.raw"},
      {"database": "/", "path": "other/c.raw"},
      {"database": "/d", "path": "e.raw"},
    ])
    self.assertEqual([i.id for i in items], [None] * 4)
    # inputs with an id are sent by id, once each
    sent, items = to_save([CollectionInput(3, '/d', 'a.raw'), CollectionInput(None, '/d', 'b.raw'), CollectionInput(3, '/d', 'a.raw'), ('/d', 'b.raw')])
    self.assertEqual(sent, [3, {"database": "/d", "path": "b.raw"}])

  def test_saves_a_collection(self):
    import qaboard
    from qaboard.input_collections import Collection, CollectionInput
    server = self.serve()
    previous = Collection(ref="a", project="group/repo", name="a", version=2, inputs=[CollectionInput(4, '/d', 'x.raw'), CollectionInput(5, '/d', 'y.raw')])
    server.ids = {('/d', 'x.raw'): 4, ('/d', 'y.raw'): 5}
    collection = qaboard.collections.save("b", previous, project="group/repo", source=SOURCE)
    self.assertEqual(self.saves(server)[0]['inputs'], [4, 5])
    self.assertEqual([i.path for i in collection], ['x.raw', 'y.raw'])

  def test_minimal_answer(self):
    """The server may answer the Collection with its version object, without the inputs: we know what we sent"""
    import qaboard
    self.serve(minimal=True)
    collection = qaboard.collections.save("a", ["x.raw"], database="/d", project="group/repo", description="about", source=SOURCE)
    self.assertEqual((collection.name, collection.version, collection.id, collection.project), ("a", 1, 7, "group/repo"))
    self.assertEqual([(i.database, i.path) for i in collection], [('/d', 'x.raw')])

  def test_options_sent_only_when_given(self):
    import qaboard
    server = self.serve()
    qaboard.collections.save("a", ["x.raw"], database="/d", project="group/repo", description="about", source=SOURCE)
    self.assertEqual(set(self.saves(server)[0]), {"project", "name", "inputs", "description", "source"})

  def test_conflict(self):
    import qaboard
    from qaboard.input_collections import CollectionConflict, CollectionError
    server = self.serve()
    qaboard.collections.save("a", ["x.raw"], database="/d", project="group/repo", source=SOURCE)
    qaboard.collections.save("a", ["y.raw"], database="/d", project="group/repo", source=SOURCE)
    with self.assertRaises(CollectionConflict) as e:
      qaboard.collections.save("a", ["z.raw"], database="/d", project="group/repo", base_version=1, source=SOURCE)
    self.assertIsInstance(e.exception, CollectionError)
    self.assertEqual((e.exception.status, e.exception.base_version, e.exception.latest_version), (409, 1, 2))
    self.assertIn('base_version=2', str(e.exception))
    self.assertEqual(self.saves(server)[-1]['base_version'], 1)
    self.assertEqual(qaboard.collections.save("a", ["z.raw"], database="/d", project="group/repo", base_version=2, source=SOURCE).version, 3)

  def test_errors(self):
    import qaboard
    from qaboard.input_collections import CollectionError, CollectionNotFound
    server = self.serve()
    with self.assertRaises(CollectionNotFound):
      qaboard.collections.save("a", ["x.raw"], database="/d", project="unknown/project", source=SOURCE)
    for name in ("a@2", "p:a", "", None):
      with self.assertRaises(CollectionError, msg=name):
        qaboard.collections.save(name, ["x.raw"], database="/d", project="group/repo", source=SOURCE)
    with self.assertRaises(CollectionError) as e:
      qaboard.collections.save("a", [], project="group/repo", source=SOURCE)
    self.assertIn('No inputs', str(e.exception))
    with self.assertRaises(CollectionError) as e:
      qaboard.collections.save("a", "x.raw", database="/d", project="group/repo", source=SOURCE)
    self.assertIn('list of paths', str(e.exception))
    with self.assertRaises(CollectionError) as e:
      qaboard.collections.save("a", ["relative.raw"], project="group/repo", source=SOURCE)
    self.assertIn('In which database', str(e.exception))
    self.assertEqual(len(self.saves(server)), 1)

  def test_refused(self):
    import qaboard
    from qaboard.input_collections import CollectionError
    self.serve(status=403)
    with self.assertRaises(CollectionError) as e:
      qaboard.collections.save("a", ["x.raw"], database="/d", project="group/repo", source=SOURCE)
    self.assertEqual(e.exception.status, 403)
    self.assertIn("could not save the collection 'a' in group/repo", str(e.exception))

  def test_unreachable(self):
    import qaboard
    from qaboard.input_collections import CollectionError
    self.serve()
    with mock.patch('requests.Session.request', side_effect=requests.ConnectionError('Connection refused')):
      with self.assertRaises(CollectionError) as e:
        qaboard.collections.save("a", ["x.raw"], database="/d", project="group/repo", source=SOURCE)
    self.assertIn('Could not reach QA-Board', str(e.exception))

  def test_default_project_and_source(self):
    import qaboard
    from qaboard import input_collections as collections
    server = self.serve()
    with mock.patch.object(collections, 'current_project', return_value='group/repo/sub'):
      qaboard.collections.save("a", ["x.raw"], database="/d")
    body, = self.saves(server)
    self.assertEqual(body['project'], 'group/repo/sub')
    self.assertEqual(set(body['source']), {"script", "argv", "host", "cwd", "git"})
    self.assertEqual(body['source']['argv'], sys.argv)
    with mock.patch.object(collections, 'current_project', return_value=None):
      with self.assertRaises(collections.CollectionError):
        qaboard.collections.save("a", ["x.raw"], database="/d")


def git(cwd, *args):
  subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.com', *args], cwd=cwd, check=True, capture_output=True)


class TestSource(unittest.TestCase):
  def setUp(self):
    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)
    self.tmp = Path(tmp.name).resolve()

  def test_script_in_git(self):
    from qaboard.input_collections import source_of
    repo = self.tmp / 'repo'
    (repo / 'tools').mkdir(parents=True)
    script = repo / 'tools' / 'triage.py'
    script.write_text('print(1)\n')
    git(repo, 'init', '-q')
    git(repo, 'add', '.')
    git(repo, 'commit', '-q', '-m', 'first')
    git(repo, 'remote', 'add', 'origin', 'https://oauth2:glpat-secret@gitlab.example.com/group/repo.git')
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repo, capture_output=True, text=True).stdout.strip()
    with mock.patch.object(sys, 'argv', [str(script), '--fast']):
      source = source_of()
    self.assertEqual(source['script'], script.as_posix())
    self.assertEqual(source['argv'], [str(script), '--fast'])
    self.assertEqual(source['cwd'], Path.cwd().as_posix())
    self.assertTrue(source['host'])
    self.assertEqual(source['git'], {"repository": "https://gitlab.example.com/group/repo.git", "commit": commit, "dirty": False})
    self.assertNotIn('secret', json.dumps(source))
    script.write_text('print(2)\n')
    with mock.patch.object(sys, 'argv', [str(script)]):
      self.assertTrue(source_of()['git']['dirty'])
    # untracked files don't make it dirty
    script.write_text('print(1)\n')
    (repo / 'notes.txt').write_text('')
    self.assertFalse(source_of(script)['git']['dirty'])

  def test_without_a_remote(self):
    from qaboard.input_collections import source_of
    git(self.tmp, 'init', '-q')
    (self.tmp / 'a.py').write_text('')
    git(self.tmp, 'add', '.')
    git(self.tmp, 'commit', '-q', '-m', 'first')
    self.assertEqual(source_of(self.tmp / 'a.py')['git']['repository'], None)

  def test_outside_git(self):
    from qaboard.input_collections import source_of
    script = self.tmp / 'a.py'
    script.write_text('')
    self.assertIsNone(source_of(script)['git'])

  def test_without_git(self):
    from qaboard.input_collections import source_of
    with mock.patch('subprocess.run', side_effect=FileNotFoundError('git')):
      source = source_of(self.tmp / 'a.py')
    self.assertIsNone(source['git'])
    self.assertEqual(source['script'], (self.tmp / 'a.py').as_posix())
    # whatever goes wrong
    with mock.patch('qaboard.local_changes._git', side_effect=RuntimeError('boom')):
      self.assertIsNone(source_of(self.tmp / 'a.py')['git'])

  def test_interactive_and_notebooks(self):
    from qaboard.input_collections import source_of
    for argv in ([''], ['-c']):
      with mock.patch.object(sys, 'argv', argv):
        self.assertEqual(source_of()['script'], '<interactive>')
    with mock.patch.object(sys, 'argv', ['/venv/lib/python3/site-packages/ipykernel_launcher.py', '-f', 'kernel.json']), \
         mock.patch.dict(sys.modules, {'ipykernel': mock.MagicMock()}), mock.patch.dict(os.environ, {'JPY_SESSION_NAME': ''}):
      self.assertEqual(source_of()['script'], '<notebook>')
      notebook = self.tmp / 'triage.ipynb'
      notebook.write_text('{}')
      with mock.patch.dict(os.environ, {'JPY_SESSION_NAME': str(notebook)}):
        self.assertEqual(source_of()['script'], notebook.as_posix())

  def test_credentials_are_removed(self):
    from qaboard.input_collections import _without_credentials
    self.assertEqual(_without_credentials('https://user:token@host:8443/a/b.git'), 'https://host:8443/a/b.git')
    self.assertEqual(_without_credentials('https://host/a/b.git?private_token=x'), 'https://host/a/b.git')
    self.assertEqual(_without_credentials('ssh://git@host/a/b.git'), 'ssh://host/a/b.git')
    self.assertEqual(_without_credentials('git@host:a/b.git\n'), 'git@host:a/b.git')


class TestFiles(unittest.TestCase):
  def setUp(self):
    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)
    self.tmp = Path(tmp.name).resolve()

  def write(self, name, text):
    path = self.tmp / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path

  def test_yaml(self):
    from qaboard.input_collections import read_collection_file, name_from_path
    path = self.write('collections/night/hard-cases.yaml', 'name: night/x\ndescription: Street lights\ndatabase: /mnt/captures\ninputs:\n- a.raw\n- " b.raw "\n')
    spec = read_collection_file(path)
    self.assertEqual((spec.name, spec.description, spec.database, spec.inputs), ('night/x', 'Street lights', '/mnt/captures', ['a.raw', 'b.raw']))
    self.assertEqual(name_from_path(path), 'night/hard-cases')
    self.assertEqual(name_from_path(self.tmp / 'collections' / 'collections' / 'a.yml'), 'a')
    self.assertIsNone(name_from_path(self.tmp / 'lists' / 'a.yaml'))
    self.assertEqual(read_collection_file(self.write('one.yml', 'inputs: a.raw\n')).inputs, ['a.raw'])

  def test_txt(self):
    from qaboard.input_collections import read_collection_file
    spec = read_collection_file(self.write('collections/a.txt', '# hard cases\n\nnight/1.raw\n  night/2.raw  \n# night/3.raw\n'))
    self.assertEqual((spec.name, spec.inputs), (None, ['night/1.raw', 'night/2.raw']))

  def test_invalid_files(self):
    from qaboard.input_collections import read_collection_file, CollectionError
    cases = {
      'a.yaml': ('inputs: [a.raw]\nglobs: "*.raw"\n', "unknown key 'globs'"),
      'b.yaml': ('name: b\n', 'has no inputs'),
      'c.yaml': ('inputs: []\n', 'has no inputs'),
      'd.yaml': ('- a.raw\n', 'expected a mapping'),
      'e.yaml': ('', 'an empty file'),
      'f.yaml': ('inputs: [{a.raw: [base]}]\n', 'is a list of paths'),
      'g.yaml': ('inputs: [a.raw\n', 'not valid YAML'),
      'h.txt': ('# nothing\n\n', 'lists no inputs'),
      'i.json': ('{}', 'is YAML'),
      'j.yaml': ('name: [a]\ninputs: [a.raw]\n', '`name` is text'),
    }
    for name, (text, message) in cases.items():
      with self.assertRaises(CollectionError, msg=name) as e:
        read_collection_file(self.write(name, text))
      self.assertIn(message, str(e.exception), name)
    with self.assertRaises(CollectionError) as e:
      read_collection_file(self.tmp / 'missing.yaml')
    self.assertIn('Could not read', str(e.exception))

  def test_globs(self):
    from qaboard.input_collections import expand_inputs, CollectionError
    database = self.tmp / 'db'
    for name in ('night/1.raw', 'night/1.imgprops', 'night/2.raw', 'day/3.raw', 'day/sub/4.raw'):
      self.write(f'db/{name}', '')
    self.assertEqual(expand_inputs(['night/*.raw', 'x/literal.raw'], database), [(database, Path('night/1.raw')), (database, Path('night/2.raw')), 'x/literal.raw'])
    # folders are searched with the input type's globs, like batch files
    self.assertEqual(expand_inputs(['day'], database, {"globs": "*.raw"}), [(database, Path('day/3.raw')), (database, Path('day/sub/4.raw'))])
    self.assertEqual(expand_inputs(['day'], database), ['day'])
    # absolute globs
    self.assertEqual(expand_inputs([f'{database}/night/2.*'], None), [f'{database.as_posix()}/night/2.raw'])
    with self.assertRaises(CollectionError) as e:
      expand_inputs(['nights/*.raw'], database)
    self.assertIn('No inputs match "nights/*.raw"', str(e.exception))
    with self.assertRaises(CollectionError):
      expand_inputs(['night/*.raw'], None)

  def test_changes(self):
    from qaboard.input_collections import changes, Collection, CollectionInput
    latest = Collection(ref='a', project='p', name='a', version=3, inputs=[CollectionInput(1, '/d', 'a'), CollectionInput(2, '/d', 'b')])
    added, removed = changes(latest, [CollectionInput(None, '/d', 'a'), CollectionInput(None, '/d', 'c'), CollectionInput(2, '/x', 'renamed')])
    self.assertEqual(([i.path for i in added], [i.path for i in removed]), (['c'], []))
    added, removed = changes(latest, [CollectionInput(None, '/d', 'c')])
    self.assertEqual(([i.path for i in added], [i.path for i in removed]), (['c'], ['a', 'b']))
    self.assertEqual(changes(None, [CollectionInput(None, '/d', 'c')])[0][0].path, 'c')


class TestCommand(FakeServerTestCase):
  """`qa collections save` in the sample project"""
  def setUp(self):
    previous_cwd = os.getcwd()
    os.chdir(ROOT / 'qaboard/sample_project')
    self.addCleanup(os.chdir, previous_cwd)
    for name in ('storage', 'database', 'files'):
      tmp = tempfile.TemporaryDirectory()
      self.addCleanup(tmp.cleanup)
      setattr(self, name, Path(tmp.name).resolve())
    for name in ('night/1.raw', 'night/2.raw', 'day/3.raw'):
      (self.database / name).parent.mkdir(parents=True, exist_ok=True)
      (self.database / name).write_text('')
    self.server = self.serve()
    patch = mock.patch.dict(os.environ, {'QA_STORAGE': str(self.storage), 'QA_TESTING': '1'})
    patch.start()
    self.addCleanup(patch.stop)
    for module in ('qaboard.api', 'qaboard.config', 'qaboard.qa'):
      reload(import_module(module))
    from qaboard._testing import reset_process_state
    reset_process_state()
    self.addCleanup(reset_process_state)

  def qa(self, *args):
    import qaboard
    return CliRunner().invoke(qaboard.qa, ['--database', str(self.database), *args], obj={}, auto_envvar_prefix='QA', color=False)

  def write(self, name, text):
    path = self.files / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path

  def test_saves_a_yaml_file(self):
    path = self.write('collections/night/hard-cases.yaml', 'description: Street lights\ninputs:\n- night/*.raw\n- day/3.raw\n')
    result = self.qa('collections', 'save', str(path), '--notes', 'first')
    self.assertEqual(result.exit_code, 0, result.output)
    body, = self.saves(self.server)
    self.assertEqual({k: v for k, v in body.items() if k != 'source'}, {
      "project": "user/sample_project", "name": "night/hard-cases", "description": "Street lights", "notes": "first",
      "inputs": [{"database": self.database.as_posix(), "path": p} for p in ('night/1.raw', 'night/2.raw', 'day/3.raw')],
    })
    # the source is the file, in its repository
    self.assertEqual(body['source']['script'], path.as_posix())
    self.assertEqual(body['source']['argv'], sys.argv)
    self.assertEqual(result.stdout, "night/hard-cases@1\n")
    self.assertIn("Saved night/hard-cases@1 in user/sample_project: 3 inputs.", click.unstyle(result.stderr))
    # unchanged
    result = self.qa('collections', 'save', str(path))
    self.assertEqual(result.exit_code, 0, result.output)
    self.assertIn("night/hard-cases@1 already has these 3 inputs: nothing saved.", click.unstyle(result.stderr))

  def test_txt_with_options(self):
    other = self.files / 'other'
    path = self.write('list.txt', '# a list\n1.raw\n/abs/2.raw\n')
    result = self.qa('collections', 'save', str(path), '--name', 'my/list', '--database', str(other), '--description', 'about')
    self.assertEqual(result.exit_code, 0, result.output)
    body, = self.saves(self.server)
    self.assertEqual((body['name'], body['description']), ('my/list', 'about'))
    self.assertEqual(body['inputs'], [{"database": other.as_posix(), "path": "1.raw"}, {"database": "/", "path": "abs/2.raw"}])

  def test_database_of_the_file(self):
    path = self.write('collections/a.yaml', f'database: {self.files}\ninputs: [x.raw]\n')
    result = self.qa('collections', 'save', str(path))
    self.assertEqual(result.exit_code, 0, result.output)
    self.assertEqual(self.saves(self.server)[0]['inputs'], [{"database": self.files.as_posix(), "path": "x.raw"}])

  def test_relative_to_where_qa_started(self):
    self.write('collections/a.yaml', 'inputs: [x.raw]\n')
    os.chdir(self.files)
    result = self.qa('collections', 'save', 'collections/a.yaml', '--project', 'user/sample_project')
    self.assertEqual(result.exit_code, 0, result.output)
    self.assertEqual(self.saves(self.server)[0]['name'], 'a')

  def test_dryrun(self):
    path = self.write('collections/night.yaml', 'inputs: ["night/*.raw"]\n')
    for args in (['collections', 'save', str(path), '--dryrun'], ['--dryrun', 'collections', 'save', str(path)]):
      result = self.qa(*args)
      self.assertEqual(result.exit_code, 0, result.output)
      self.assertIn("night is new in user/sample_project: saving would create it with 2 inputs.", click.unstyle(result.stderr))
      self.assertIn(f"+ {self.database}\tnight/1.raw", result.stdout)
      self.assertIn("Dry run: nothing saved.", result.stderr)
    self.assertEqual(self.saves(self.server), [])
    resolve = [r for r in self.server.requests if r['url'].endswith('/collections/resolve')][0]
    self.assertEqual(resolve['params'], {"project": "user/sample_project", "ref": "user/sample_project:night"})

    self.assertEqual(self.qa('collections', 'save', str(path)).exit_code, 0)
    path.write_text('inputs: ["night/2.raw", "day/*.raw"]\n')
    result = self.qa('collections', 'save', str(path), '--dryrun')
    self.assertEqual(result.exit_code, 0, result.output)
    self.assertIn("night@1 has 2 inputs. Saving would add 1 and remove 1: version 2 with 2 inputs.", click.unstyle(result.stderr))
    self.assertEqual(result.stdout, f"+ {self.database}\tday/3.raw\n- {self.database}\tnight/1.raw\n")
    self.assertEqual(len(self.saves(self.server)), 1)

    path.write_text('inputs: ["night/*.raw"]\n')
    result = self.qa('collections', 'save', str(path), '--dryrun')
    self.assertIn("night@1 already has these 2 inputs: nothing to save.", click.unstyle(result.stderr))
    self.assertEqual(result.stdout, '')

  def test_dryrun_lists_the_first_changes(self):
    path = self.write('a.txt', ''.join(f'{i}.raw\n' for i in range(8)))
    with mock.patch('qaboard.input_collections.SHOWN_CHANGES', 3):
      result = self.qa('collections', 'save', str(path), '--name', 'a', '--dryrun')
    self.assertEqual(result.exit_code, 0, result.output)
    self.assertEqual(result.stdout.splitlines()[-1], "+ ... and 5 more")
    self.assertEqual(len(result.stdout.splitlines()), 4)

  def test_errors(self):
    result = self.qa('collections', 'save', str(self.files / 'missing.yaml'))
    self.assertEqual(result.exit_code, 1)
    self.assertIn('Could not find the file', result.output)
    result = self.qa('collections', 'save', str(self.write('lists/a.yaml', 'inputs: [a.raw]\n')))
    self.assertEqual(result.exit_code, 2)
    self.assertIn("isn't in a `collections/` folder", result.output)
    result = self.qa('collections', 'save', str(self.write('collections/b.yaml', 'inputs: [a.raw]\nconfigs: [x]\n')))
    self.assertEqual(result.exit_code, 1)
    self.assertIn("unknown key 'configs'", result.output)
    result = self.qa('collections', 'save', str(self.write('collections/c.yaml', 'inputs: ["nights/*.raw"]\n')))
    self.assertEqual(result.exit_code, 1)
    self.assertIn('No inputs match "nights/*.raw"', result.output)
    self.assertEqual(self.saves(self.server), [])

  def test_refused(self):
    self.server.status = 403
    result = self.qa('collections', 'save', str(self.write('collections/a.yaml', 'inputs: [a.raw]\n')))
    self.assertEqual(result.exit_code, 1)
    self.assertIn("Error: QA-Board could not save the collection 'a' in user/sample_project", result.output)


if __name__ == '__main__':
  unittest.main()
