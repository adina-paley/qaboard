"""
The inputs of a project, that runs used or that were added before any run, for a project's Inputs tab (docs/design/inputs-and-collections.md).
The rules are in backend/input_collections.py, the SQL in backend/input_catalog.py, thumbnails in backend/thumbnails.py.

GET /api/v1/inputs?project=&q=&folder=&database=&live=1&sort=last_run|path&limit=100&offset=0&facets=
                  &annotation_group=&label=&annotated=1     filters by annotations (docs/design/annotations.md)
GET /api/v1/inputs?...&fields=id         only the ids of every input that matches (up to MAX_INPUTS), for "Select all matching"
POST /api/v1/inputs {project, inputs: [{database, path, metadata?}]}   adds inputs before any run (`qa inputs add`)
GET /api/v1/inputs/<id>?project=         an input, with the project's latest runs on it, and its sidecar files
GET /api/v1/inputs/<id>/thumbnail?size=  a JPEG, 404 when no decoder reads the input
GET /api/v1/inputs/<id>/file             the input's file, when browsers read its format
"""
import hashlib
import os
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

from flask import request, jsonify, send_file, make_response

from qaboard.api import dir_to_url

from backend import app, db_session
from .. import input_catalog as catalog
from .. import annotation_catalog
from ..input_annotations import clean_id, clean_label_filter, is_visible_to
from ..input_collections import (
  CollectionError, clean_project, search_tokens, normalize_folder, clean_page, clean_sort, flag, thumbnail_size,
  serialize_input, input_file, iso, clean_new_inputs, MAX_INPUTS,
)
from ..access import can
from ..access.policy import prefixes
from .. import locations
from ..audit import audit
from .auth import get_current_user, require_project_access, login_required
from .files import readable_file


# Formats browsers show, served as they are to the image viewer. Not SVG: it can run scripts.
BROWSER_IMAGES = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.avif'}


def error_response(e: CollectionError):
  body = {"error": str(e)}
  if e.reason:
    body["reason"] = e.reason
  return jsonify(body), e.status


def current_user_info():
  """The user's info, and their id if they are logged in"""
  user_info = get_current_user(to_jsonify=False) or {}
  user_id = user_info.get('user_id') if user_info.get('is_authenticated') else None
  return user_info, user_id


def readable_scope(project_id: str) -> List[str]:
  """The project and its subprojects the user can read: subprojects may be more private than their parent"""
  return [p for p in catalog.scope_projects(project_id) if p == project_id or can('project.read', p)]


def shown_annotation_groups(project_id: str, user_id: Optional[int], selected: Optional[int] = None) -> Optional[List[dict]]:
  """
  The annotation groups whose annotations the Inputs page counts and filters by: those the user sees from the project
  (its and its ancestors', not archived, personal ones their creator's), and the group `selected` by a filter, even
  archived. None if `selected` isn't one the user sees from the project.
  """
  groups = annotation_catalog.groups_of(prefixes(project_id), user_id)
  if selected is not None and selected not in {g['id'] for g in groups}:
    group = annotation_catalog.group_by_id(selected)
    if group is None or group['project_id'] not in prefixes(project_id) or not is_visible_to(group, user_id):
      return None
    groups = [*groups, group]
  return groups


def with_annotation_counts(inputs: List[dict], group_ids: List[int]) -> List[dict]:
  """Adds `annotations` to each Input: how many it has in the groups, counted for the page in one query"""
  counts = annotation_catalog.annotation_counts([i['id'] for i in inputs], group_ids)
  for i in inputs:
    i['annotations'] = counts.get(i['id'], 0)
  return inputs


@app.route("/api/v1/inputs", methods=['GET'])
@app.route("/api/v1/inputs/", methods=['GET'])
def list_inputs():
  """
  {inputs, total, databases, folders, annotation_groups, labels, limit, offset}, see input_catalog.list_inputs: the
  facets are null after the first page, unless facets=1. Each Input has `annotations`: how many it has in the
  annotation groups shown (the one filtered by, else those the user sees from the project).
  """
  try:
    project_id = clean_project(request.args.get('project'))
    database = request.args.get('database') or None
    if database is not None and len(database) > 2000:
      raise CollectionError("`database` is too long")
    filters = catalog.InputFilters(
      tokens=search_tokens(request.args.get('q')),
      folder=normalize_folder(request.args.get('folder')),
      database=database,
      live=flag(request.args.get('live'), True),
      annotation_group=clean_id(request.args.get('annotation_group'), 'annotation_group') if request.args.get('annotation_group') else None,
      label=clean_label_filter(request.args.get('label')),
      annotated=flag(request.args.get('annotated'), False),
    )
    sort = clean_sort(request.args.get('sort'))
    fields = request.args.get('fields') or None
    if fields not in (None, 'id'):
      raise CollectionError("`fields` can only be `id`")
    limit, offset = clean_page(request.args.get('limit'), request.args.get('offset'))
    # total, databases and folders: by default on the first page only (offset=0), null on the next ones
    with_facets = flag(request.args.get('facets'), offset == 0)
  except CollectionError as e:
    return error_response(e)
  if forbidden := require_project_access(None, project_id, 'project.read'):
    return forbidden
  _, user_id = current_user_info()
  groups = shown_annotation_groups(project_id, user_id, filters.annotation_group)
  if groups is None:
    return jsonify({"error": f"No annotation group {filters.annotation_group} in {project_id}.", "reason": "annotation_group_not_found"}), 404
  filters.annotation_groups = [g['id'] for g in groups]
  if fields == 'id':
    # Every match, not a page: as many as a collection's version can have. More: the web app asks to narrow the filters.
    found = catalog.list_input_ids(readable_scope(project_id), filters, sort, cap=MAX_INPUTS)
    return jsonify({**found, "max": MAX_INPUTS})
  found = catalog.list_inputs(readable_scope(project_id), filters, sort, limit, offset, with_facets=with_facets)
  if found.get('annotation_groups') is not None:
    names = {g['id']: g['name'] for g in groups}
    found['annotation_groups'] = sorted(
      ({"id": g['id'], "name": names.get(g['id']), "count": g['count']} for g in found['annotation_groups'] if g['id'] in names),
      key=lambda g: (g['name'] or '', g['id']))
  inputs = with_annotation_counts([serialize_input(row) for row in found['inputs']], filters.counted_groups())
  return jsonify({**found, "inputs": inputs, "limit": limit, "offset": offset})


@app.route("/api/v1/inputs", methods=['POST'])
@app.route("/api/v1/inputs/", methods=['POST'])
@login_required
def add_inputs():
  """
  {project, inputs: [{database, path, metadata?}]} => {inputs: [{id, database, path, new}], added, known}: the
  project has the inputs before any run, to browse them and save collections of them. `path` is relative to
  `database`, like runs send them. Metadata is merged into the input's (data.metadata, where runs write theirs).
  At most 10,000 inputs per request. Needs collections.write, like saving a collection.
  """
  body = request.get_json(silent=True)
  try:
    if not isinstance(body, dict):
      raise CollectionError("Expected a JSON object: {project, inputs: [{database, path, metadata?}]}")
    project_id = clean_project(body.get('project'))
    inputs = clean_new_inputs(body.get('inputs'))
  except CollectionError as e:
    return error_response(e)
  if forbidden := require_project_access(None, project_id, 'collections.write'):
    return forbidden
  if not catalog.project_exists(project_id):
    return jsonify({"error": f"The project {project_id} doesn't exist yet: it does once it has runs.", "reason": "project_not_found"}), 404
  added = catalog.add_inputs(project_id, inputs)
  new = [row for row in added if row['new']]
  with_metadata = sum(1 for i in inputs if i.get('metadata'))
  if new or with_metadata:
    audit('input.add', 'project', project_id, project_id, project_id=project_id, meta={
      "added": len(new), "known": len(added) - len(new), "with_metadata": with_metadata,
      "databases": sorted({row['database'] for row in new})[:10], "paths": [row['path'] for row in new[:20]],
    })
  db_session.commit()
  # their grid thumbnails, in the background: the first visit of their folder doesn't wait for decodes
  warm_thumbnails(new)
  return jsonify({
    "inputs": [{"id": row['id'], "database": row['database'], "path": row['path'], "new": row['new']} for row in added],
    "added": len(new), "known": len(added) - len(new),
  })


def warm_thumbnails(rows) -> int:
  """Queues the thumbnails of these inputs, those in the storage (like the file rules): no user sees them yet"""
  from ..thumbnails import warm
  from ..files import is_listable
  paths = [p for p in (input_file(row['database'], row['path']) for row in rows) if p and is_listable(p)]
  try:
    return warm(paths)
  except Exception as e:
    print(f"WARNING: could not queue thumbnails: {e}")
    return 0


def serialize_run(row) -> dict:
  output_dir_url = None
  if row.get('output_dir_override'):
    try:
      output_dir_url = dir_to_url(locations.local_path(row['output_dir_override']))
    except Exception:
      output_dir_url = None
  return {
    'id': row['id'],
    'project': row['project'],
    'batch': row['batch'],
    'commit': row['commit'],
    'branch': row['branch'],
    'user': row['user'],
    'origin': row['origin'],
    'platform': row.get('platform'),
    'configurations': row.get('configurations') or [],
    'created_date': iso(row['created_date']),
    'deleted': bool(row['deleted']),
    'is_failed': bool(row['is_failed']),
    'is_pending': bool(row.get('is_pending')),
    'is_running': bool(row.get('is_running')),
    'output_dir_url': output_dir_url,
  }


@app.route("/api/v1/inputs/<int:input_id>", methods=['GET'])
@app.route("/api/v1/inputs/<int:input_id>/", methods=['GET'])
def get_input(input_id):
  """
  An input (`Input`) and the latest runs on it (`runs`) of the project and of its subprojects.
  Seen from projects that ran on it, or that see a collection with it (their own or an ancestor's).
  """
  try:
    project_id = clean_project(request.args.get('project'))
  except CollectionError as e:
    return error_response(e)
  if forbidden := require_project_access(None, project_id, 'project.read'):
    return forbidden
  scope = readable_scope(project_id)
  row = catalog.input_in_scope(input_id, scope)
  if row is None:
    _, user_id = current_user_info()
    row = catalog.input_by_id(input_id) if catalog.in_collections_of(input_id, prefixes(project_id), user_id) else None
    if row is None:
      return jsonify({"error": f"No run of {project_id} used this input.", "reason": "input_not_found"}), 404
    row = {**row, **catalog.input_stats(input_id, scope)}
  runs = catalog.input_runs(input_id, scope)
  return jsonify({**serialize_input(row), "runs": [serialize_run(r) for r in runs], "sidecars": sidecars_of(row)})


# sidecars listed with an input
MAX_SIDECARS = 50


def sidecars_of(row) -> Optional[List[dict]]:
  """
  The files that come with the input (qaboard.input_collections.sidecars: `0001.metadata.yaml`, a site's `0001.imgprops`),
  [{name, size, url}], when the user can read the input's folder. None when the server can't tell.
  """
  from qaboard.input_collections import sidecars
  absolute = input_file(row['database'], row['path'])
  if absolute is None:
    return None
  path, denied = readable_file(f"/s{quote(absolute)}")
  if denied:
    return None
  answer = []
  for sidecar in sidecars(Path(path), limit=MAX_SIDECARS):
    # the site's rules may point elsewhere (../calibration/{stem}.cal): the file rules apply to each
    if readable_file(f"/s{quote(str(sidecar))}")[1] is not None:
      continue
    try:
      size = sidecar.stat().st_size
    except OSError:
      continue
    # `name`: its path from the input's folder, e.g. 0001/0001.cde
    answer.append({"name": os.path.relpath(sidecar, Path(path).parent), "size": size, "url": quote(f"/s{sidecar}")})
  return answer


def input_path(input_id):
  """(Path, row, None) of an input's file the user can read, else (None, None, an error response)"""
  project_id = request.args.get('project')
  if project_id:
    try:
      project_id = clean_project(project_id)
    except CollectionError as e:
      return None, None, error_response(e)
    if forbidden := require_project_access(None, project_id, 'project.read'):
      return None, None, forbidden
  row = catalog.input_by_id(input_id)
  if row is None:
    return None, None, (jsonify({"error": "This input doesn't exist.", "reason": "input_not_found"}), 404)
  absolute = input_file(row['database'], row['path'])
  if absolute is None:
    return None, None, (jsonify({"error": "This input isn't a file the server reads: its database isn't an absolute path.", "reason": "not_a_file"}), 404)
  # the file browser's rules: only files in the storage, that the user can read
  path, denied = readable_file(f"/s{quote(absolute)}")
  if denied:
    return None, None, denied
  return path, row, None


def busy_response():
  """Too many thumbnails are being decoded (thumbnails.Busy): the grid's images try again"""
  response = make_response(jsonify({"error": "The server is busy making thumbnails: try again in a few seconds.", "reason": "busy"}), 503)
  response.headers['Retry-After'] = '2'
  response.headers['Cache-Control'] = 'no-store'
  return response


@app.route("/api/v1/inputs/<int:input_id>/thumbnail", methods=['GET'])
def input_thumbnail(input_id):
  from ..thumbnails import thumbnail_of, is_cached, Busy
  path, row, error = input_path(input_id)
  if error:
    return error
  try:
    stat = Path(path).stat()
  except OSError:
    response = make_response(jsonify({"error": "The input's file doesn't exist (anymore?).", "reason": "missing_file"}), 404)
    response.headers['Cache-Control'] = 'private, max-age=300'
    return response
  size = thumbnail_size(request.args.get('size'))
  etag = hashlib.sha1(f"{path}:{stat.st_mtime_ns}:{size}".encode()).hexdigest()
  if etag in request.if_none_match:
    response = make_response('', 304)
  else:
    try:
      data = thumbnail_of(str(path), stat.st_mtime_ns, size)
    except Busy:
      return busy_response()
    if data is None:
      response = make_response(jsonify({"error": "QA-Board can't make a thumbnail of this file's format.", "reason": "no_thumbnail"}), 404)
      response.headers['Cache-Control'] = 'private, max-age=3600'
      return response
    response = make_response(data)
    response.mimetype = 'image/jpeg'
  response.set_etag(etag)
  # The server doesn't cache the big ones: browsers keep them longer. The ETag changes with the file.
  response.headers['Cache-Control'] = 'private, max-age=86400' if is_cached(size) else 'private, max-age=604800'
  response.headers['X-Content-Type-Options'] = 'nosniff'
  return response


@app.route("/api/v1/inputs/<int:input_id>/file", methods=['GET'])
def input_file_content(input_id):
  """The file, for the image viewer, when browsers read its format: else 415 (show the thumbnail instead)"""
  path, row, error = input_path(input_id)
  if error:
    return error
  suffix = Path(path).suffix.lower()
  if suffix not in BROWSER_IMAGES:
    return jsonify({
      "error": f"Browsers can't show {suffix or 'these'} files: use the thumbnail.", "reason": "unsupported_format",
      "thumbnail_url": f"/api/v1/inputs/{input_id}/thumbnail?size=1024",
    }), 415
  if not Path(path).is_file():
    return jsonify({"error": "The input's file doesn't exist (anymore?).", "reason": "missing_file"}), 404
  response = send_file(str(path), conditional=True, max_age=3600)
  response.headers['Cache-Control'] = 'private, max-age=3600'
  response.headers['X-Content-Type-Options'] = 'nosniff'
  return response
