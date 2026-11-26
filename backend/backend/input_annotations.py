"""
What annotations of inputs are, without the database (docs/design/annotations.md): validating what clients send
(group names, rectangles, labels, fields, methods), who sees and edits what, and what we answer. The SQL is in
backend/annotation_catalog.py, the API in api/input_annotations.py, the tables in models/Annotation.py.
Annotations of other apps (read-only sources a site installs) are backend/annotations.py's.
"""
import re
import json
from typing import Optional, List, Iterable, Tuple

from .input_collections import CollectionError, normalize_name, user_of, iso, MAX_NAME


# per input and group, of those not deleted: more answers 413
MAX_ANNOTATIONS = 10_000
MAX_LABELS = 50
MAX_LABEL = 100
# fields, as JSON
MAX_FIELDS_BYTES = 4096
MAX_MODEL_NAME = 100
MAX_DESCRIPTION = 20_000
# coordinates: images are never this big, and it stays far from integer overflows
MAX_COORDINATE = 1_000_000
METHODS = ('manual', 'script', 'import')
# labels autocomplete
DEFAULT_LABELS = 20
MAX_LABELS_LISTED = 200
MAX_LABEL_QUERY = 100
# the columns the history keeps, before and after each change
SNAPSHOT = ('x', 'y', 'w', 'h', 'image_width', 'image_height', 'labels', 'fields', 'method')


class AnnotationError(CollectionError):
  """What's wrong with what a client sent, and the HTTP status to answer"""


# Group names

def path_name(name: str) -> str:
  """Normalized like collections' names: input_collections.normalize_name"""
  if not name.strip(' /'):
    raise AnnotationError("Annotation groups need a name, e.g. af-team/train-patches", reason='invalid_name')
  try:
    return normalize_name(name)
  except CollectionError as e:
    raise AnnotationError(str(e), e.status, e.reason)


def normalize_group_name(name, user_name: Optional[str]) -> Tuple[str, bool]:
  """
  (name, personal): ' af-team / patches ' => 'af-team/patches', like collections' names. '~me/sandbox' is a personal
  group of the user named `user_name` (only theirs: '~someone-else/...' is refused, status 403).
  """
  if not isinstance(name, str):
    raise AnnotationError("`name` must be a string, e.g. af-team/train-patches", reason='invalid_name')
  stripped = name.strip()
  if not stripped.startswith('~'):
    return path_name(name), False
  owner, slash, rest = stripped[1:].partition('/')
  owner = owner.strip()
  if not owner or not slash or not rest.strip(' /'):
    raise AnnotationError("Personal groups are named ~<your user name>/<name>, e.g. ~alice/sandbox", reason='invalid_name')
  if not user_name or owner.lower() != str(user_name).lower():
    raise AnnotationError(f"Only {owner} makes groups named ~{owner}/...: yours are ~{user_name or '<you>'}/...", status=403, reason='not_your_namespace')
  normalized = f"~{user_name}/{path_name(rest)}"
  if len(normalized) > MAX_NAME:
    raise AnnotationError(f"The name is too long (at most {MAX_NAME} characters)", reason='invalid_name')
  return normalized, True


def clean_id(value, name: str) -> int:
  """A group's id (an integer column)"""
  if isinstance(value, str) and value.strip().isdigit():
    value = int(value.strip())
  if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > 2**31 - 1:
    raise AnnotationError(f"`{name}` must be an id (a positive integer)")
  return value


def clean_team(value) -> Optional[int]:
  """A team (a QA-Board group) id, None for no team"""
  if value is None or value == '':
    return None
  if isinstance(value, dict):
    value = value.get('id')
    if value is None:
      return None
  if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > 2**31 - 1:
    raise AnnotationError("`team` must be a group's id, or null for no team", reason='invalid_team')
  return value


def clean_ids(value, name: str = 'groups') -> Optional[List[int]]:
  """'1,2' or [1, 2] => [1, 2]; None when not given"""
  if value is None or value == '':
    return None
  items = value.split(',') if isinstance(value, str) else value
  if not isinstance(items, list):
    raise AnnotationError(f"`{name}` must be a list of ids, e.g. 1,2")
  ids = []
  for item in items:
    if isinstance(item, str):
      item = item.strip()
      if not item:
        continue
    item = clean_id(item, name)
    if item not in ids:
      ids.append(item)
  return ids[:1000]


# Rectangles

def integer(value, name: str, minimum: int) -> int:
  # True is an int in Python, not a coordinate; 3.0 is fine
  if isinstance(value, float) and value.is_integer():
    value = int(value)
  if isinstance(value, bool) or not isinstance(value, int):
    raise AnnotationError(f"`{name}` must be an integer, in the input's pixels", reason='invalid_rectangle')
  if value < minimum or value > MAX_COORDINATE:
    raise AnnotationError(f"`{name}` must be from {minimum} to {MAX_COORDINATE:,}", reason='invalid_rectangle')
  return value


def clean_image_size(body: dict) -> dict:
  """{image_width, image_height} when sent: both or none, positive integers"""
  sizes = {}
  for key in ('image_width', 'image_height'):
    if body.get(key) is not None:
      sizes[key] = integer(body[key], key, 1)
  if len(sizes) == 1:
    raise AnnotationError("Send both `image_width` and `image_height`, or neither", reason='invalid_rectangle')
  return sizes


def clean_rectangle(body: dict, current: Optional[dict] = None) -> dict:
  """
  x, y, w, h (integers, w, h > 0) from `body`: all of them and image_width, image_height if sent, or with `current`
  (the annotation edited) those sent. Inside the image when its size is known (when drawn: body's, edited: current's).
  """
  rectangle = {}
  for key, minimum in (('x', 0), ('y', 0), ('w', 1), ('h', 1)):
    if key in body and body[key] is not None:
      rectangle[key] = integer(body[key], key, minimum)
    elif current is None:
      raise AnnotationError(f"`{key}` is required: x, y, w, h in the input's pixels", reason='invalid_rectangle')
  # edits keep the size the annotation was drawn on
  sizes = clean_image_size(body) if current is None else {}
  rectangle.update(sizes)
  merged = {**({k: current.get(k) for k in ('x', 'y', 'w', 'h', 'image_width', 'image_height')} if current else {}), **rectangle}
  width, height = merged.get('image_width'), merged.get('image_height')
  if width is not None and height is not None and (merged['x'] + merged['w'] > width or merged['y'] + merged['h'] > height):
    raise AnnotationError(f"The rectangle goes out of the image ({width}x{height}): x + w and y + h are at most its width and height", reason='invalid_rectangle')
  return rectangle


# Labels, fields, method

def clean_labels(value) -> List[str]:
  """Trimmed, without empty ones nor duplicates, in their order: at most 50, of at most 100 characters"""
  if value is None:
    return []
  if not isinstance(value, list):
    raise AnnotationError("`labels` must be a list of strings", reason='invalid_labels')
  labels = []
  for item in value:
    if not isinstance(item, str):
      raise AnnotationError("`labels` must be a list of strings", reason='invalid_labels')
    label = re.sub(r'\s+', ' ', item.replace('\x00', '')).strip()
    if not label:
      continue
    if len(label) > MAX_LABEL:
      raise AnnotationError(f"Labels have at most {MAX_LABEL} characters: \"{label[:30]}...\"", reason='invalid_labels')
    if label not in labels:
      labels.append(label)
  if len(labels) > MAX_LABELS:
    raise AnnotationError(f"Annotations have at most {MAX_LABELS} labels: {len(labels)} were sent", reason='invalid_labels')
  return labels


def clean_fields(value) -> dict:
  """A JSON object of at most 4 KB"""
  if value is None:
    return {}
  if not isinstance(value, dict):
    raise AnnotationError("`fields` must be a JSON object: {key: value}", reason='invalid_fields')
  try:
    size = len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
  except (TypeError, ValueError):
    raise AnnotationError("`fields` must be a JSON object: {key: value}", reason='invalid_fields')
  if size > MAX_FIELDS_BYTES:
    raise AnnotationError(f"`fields` is too big: at most {MAX_FIELDS_BYTES:,} bytes of JSON, it has {size:,}", status=413, reason='too_long')
  if '\x00' in json.dumps(value):
    raise AnnotationError("`fields` can't contain NUL characters", reason='invalid_fields')
  return value


def clean_method(value) -> str:
  """manual (default), script, import, or model:<name>"""
  if value is None or value == '':
    return 'manual'
  if isinstance(value, str):
    value = value.strip()
    if value in METHODS:
      return value
    if value.startswith('model:'):
      name = value[len('model:'):].strip()
      if name and len(name) <= MAX_MODEL_NAME and name.isprintable():
        return f"model:{name}"
  raise AnnotationError(f"`method` is one of {', '.join(METHODS)}, or model:<name> (at most {MAX_MODEL_NAME} characters)", reason='invalid_method')


def clean_version(value, required: bool = True) -> Optional[int]:
  """The version an edit starts from"""
  if value is None or value == '':
    if required:
      raise AnnotationError("`version` is required: the version of the annotation your change starts from", reason='version_required')
    return None
  if isinstance(value, str) and value.strip().isdigit():
    value = int(value.strip())
  if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 2**31 - 1:
    raise AnnotationError("`version` must be a version number, from 1")
  return value


def clean_label_query(value) -> str:
  if not isinstance(value, str):
    return ''
  return re.sub(r'\s+', ' ', value.replace('\x00', '')).strip()[:MAX_LABEL_QUERY]


def clean_label_filter(value) -> Optional[str]:
  """The Inputs page's label filter"""
  label = clean_label_query(value)
  return label or None


# Who sees and edits what

def is_owner(row, user_id: Optional[int]) -> bool:
  return user_id is not None and row.get('created_by_id', row.get('created_by')) == user_id


def is_visible_to(group, user_id: Optional[int]) -> bool:
  """Personal groups are their creator's only"""
  return not group.get('personal') or is_owner(group, user_id)


def can_edit(group, user_id: Optional[int], team_ids: Iterable[int], writable: bool) -> bool:
  """
  Editing a group's annotations: its creator; the members of its team when it has one; else anyone with
  annotations.write on its project (`writable`). Personal groups: their creator only.
  """
  if user_id is None:
    return False
  if is_owner(group, user_id):
    return True
  if group.get('personal'):
    return False
  if group.get('team_id') is not None:
    return group['team_id'] in set(team_ids)
  return bool(writable)


def can_manage(group, user_id: Optional[int], owns_project: bool) -> bool:
  """Renaming, describing, setting the team, archiving: its creator or the project's owners (not for personal groups)"""
  if user_id is None:
    return False
  if is_owner(group, user_id):
    return True
  return not group.get('personal') and bool(owns_project)


# What we answer

def serialize_group(row, project_id: Optional[str] = None, can_edit: bool = False, can_manage: bool = False, count: int = 0) -> dict:
  """`row`: annotation_catalog.GROUP_COLUMNS. `project_id`: where it's seen from (`inherited`). `count`: its annotations (where they're counted)"""
  return {
    'id': row['id'],
    'project': row['project_id'],
    'name': row['name'],
    'description': row.get('description') or '',
    'team': {'id': row['team_id'], 'name': row.get('team_name')} if row.get('team_id') is not None else None,
    'personal': bool(row.get('personal')),
    'created_by': user_of(row),
    'created_at': iso(row.get('created_at')),
    'updated_at': iso(row.get('updated_at')),
    'archived': row.get('archived_at') is not None,
    'archived_at': iso(row.get('archived_at')),
    'can_edit': bool(can_edit),
    # rename, describe, set the team, archive
    'can_manage': bool(can_manage),
    'inherited': bool(project_id) and project_id != row['project_id'],
    'count': int(count or 0),
  }


def serialize_annotation(row, can_edit: bool = False) -> dict:
  """`row`: annotation_catalog.ANNOTATION_COLUMNS"""
  return {
    'id': row['id'],
    'group': row['group_id'],
    'input': row['test_input_id'],
    'x': row['x'], 'y': row['y'], 'w': row['w'], 'h': row['h'],
    'image_width': row.get('image_width'),
    'image_height': row.get('image_height'),
    'labels': list(row.get('labels') or []),
    'fields': row.get('fields') or {},
    'method': row.get('method') or 'manual',
    'output': row.get('output_id'),
    'created_by': user_of(row),
    'created_at': iso(row.get('created_at')),
    'updated_by': user_of(row, 'updated_by_'),
    'updated_at': iso(row.get('updated_at')),
    'deleted_at': iso(row.get('deleted_at')),
    'version': row['version'],
    'can_edit': bool(can_edit),
  }


def snapshot(row) -> dict:
  """What the history keeps of an annotation, before and after a change"""
  return {key: (list(row.get(key) or []) if key == 'labels' else row.get(key)) for key in SNAPSHOT}


def serialize_change(row) -> dict:
  return {
    'version': row['version'],
    'action': row['action'],
    'changed_by': user_of(row, 'changed_by_'),
    'changed_at': iso(row.get('changed_at')),
    'before': row.get('before'),
    'after': row.get('after'),
  }


def stale_error(row, can_edit: bool = False) -> dict:
  return {
    "error": f"Someone changed this annotation meanwhile (version {row['version']}): reload it, or apply your change to it.",
    "reason": "stale_version",
    "version": row['version'],
    "annotation": serialize_annotation(row, can_edit),
  }


# Collections saved from code (POST /collections/save): where they came from

MAX_SOURCE = 20_000


def clean_source(value) -> Optional[dict]:
  """{script, argv, host, cwd, git: {repository, commit, dirty}}, as the SDK sends it: a JSON object of at most 20 KB"""
  if value is None:
    return None
  if not isinstance(value, dict):
    raise AnnotationError("`source` must be a JSON object: {script, argv, host, cwd, git: {repository, commit, dirty}}", reason='invalid_source')
  try:
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
  except (TypeError, ValueError):
    raise AnnotationError("`source` must be a JSON object", reason='invalid_source')
  if len(text) > MAX_SOURCE or '\x00' in text:
    raise AnnotationError(f"`source` is too big: at most {MAX_SOURCE:,} characters of JSON", status=413, reason='too_long')
  return value or None
