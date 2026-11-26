"""
Annotations of inputs kept by QA-Board: rectangles in groups, with their history (docs/design/annotations.md).
The rules are in backend/input_annotations.py, the SQL in backend/annotation_catalog.py. Annotations of other apps
(read-only sources a site installs) are api/annotations.py's.

GET          /api/v1/annotation-groups?project=&archived=0      the project's and its ancestors' groups, and the user's personal ones
POST         /api/v1/annotation-groups                           create one
PATCH        /api/v1/annotation-groups/<id>                      rename, describe, set the team, archive (POST works too)
GET          /api/v1/annotation-groups/<id>/labels?q=            its labels, for autocomplete
GET          /api/v1/inputs/<id>/annotations?project=&groups=    an input's annotations, in the groups seen from the project
POST         /api/v1/inputs/<id>/annotations                     add one
PATCH        /api/v1/annotations/<id>                            move, resize, relabel (POST works too): with the version seen
DELETE       /api/v1/annotations/<id>?version=                   hides it
POST         /api/v1/annotations/<id>/restore                    undoes a delete
GET          /api/v1/annotations/<id>/history                    its changes, the latest first

Seeing a group needs project.read on its project or a subproject (personal groups: their creator only), and an
input's annotations are only shown to who reads a project that used the input. Editing a group's annotations: its
creator, its team's members when it has one, else annotations.write on its project. Managing a group: its creator or
the project's owners. Every change is audited, and kept in the annotation's history.
"""
from typing import Optional

from flask import request, jsonify
from sqlalchemy.exc import IntegrityError

from backend import app, db_session
from .. import input_catalog as catalog
from .. import annotation_catalog as annotations
from ..input_collections import CollectionError, clean_project, clean_text, flag
from ..input_annotations import (
  AnnotationError, normalize_group_name, clean_team, clean_id, clean_ids, clean_rectangle, clean_labels, clean_fields, clean_method,
  clean_version, clean_label_query, is_visible_to, is_owner, can_edit, can_manage, serialize_group, serialize_annotation,
  serialize_change, stale_error, snapshot, MAX_ANNOTATIONS, MAX_DESCRIPTION, DEFAULT_LABELS, MAX_LABELS_LISTED,
)
from ..access import can, current_actor
from ..access.policy import prefixes
from ..audit import audit, capped, diff
from .auth import login_required, require_project_access
from .inputs import error_response, current_user_info, readable_scope


def not_found(message="This annotation group doesn't exist.", reason='annotation_group_not_found'):
  return jsonify({"error": message, "reason": reason}), 404


def sign_in_error():
  return jsonify({"error": "Sign in to do this: annotations record who changed them.", "reason": "sign_in_required"}), 401


def token_scope_error():
  """Tokens change annotations with the "act" scope, whoever's group it is (creator and team members included)"""
  actor = current_actor()
  try:
    if actor is not None and actor.token is not None and not actor.has_scope('act'):
      return jsonify({"error": "This token can't do this: it needs the \"act\" scope.", "reason": "missing_scope", "required": "act"}), 403
  except Exception:
    return None
  return None


def json_body() -> dict:
  body = request.get_json(silent=True)
  if not isinstance(body, dict):
    raise AnnotationError("Expected a JSON object")
  return body


class Viewer:
  """Who asks, and what they may do on the groups' projects, each computed once per request"""
  def __init__(self):
    self.user_info, self.user_id = current_user_info()
    self.user_name = self.user_info.get('user_name') if self.user_id else None
    self._teams = None
    self._writable = {}
    self._owned = {}

  @property
  def teams(self):
    if self._teams is None:
      self._teams = catalog.group_ids_of(self.user_id)
    return self._teams

  def writable(self, project_id: str) -> bool:
    if project_id not in self._writable:
      self._writable[project_id] = can('annotations.write', project_id)
    return self._writable[project_id]

  def owns(self, project_id: str) -> bool:
    if project_id not in self._owned:
      self._owned[project_id] = can('project.access', project_id)
    return self._owned[project_id]

  def can_edit(self, group) -> bool:
    # writable only matters for groups without a team: not computed for the others
    needs_role = not group.get('personal') and group.get('team_id') is None and not is_owner(group, self.user_id)
    return can_edit(group, self.user_id, self.teams if group.get('team_id') is not None else (), self.writable(group['project_id']) if needs_role else False)

  def can_manage(self, group) -> bool:
    return can_manage(group, self.user_id, not group.get('personal') and not is_owner(group, self.user_id) and self.owns(group['project_id']))

  def group(self, row, project_id: Optional[str] = None, count: int = 0) -> dict:
    return serialize_group(row, project_id, self.can_edit(row), self.can_manage(row), count)


def get_annotation_group(group_id: int, viewer: Viewer, project_id: Optional[str] = None):
  """(group, None) or (None, an error response): a group the user sees, from `project_id` (by default its own)"""
  if project_id and (forbidden := require_project_access(None, project_id, 'project.read')):
    return None, forbidden
  group = annotations.group_by_id(group_id)
  if group is None or not is_visible_to(group, viewer.user_id):
    return None, not_found()
  if project_id and group['project_id'] not in prefixes(project_id):
    # seen from its project and the projects under it
    return None, not_found()
  if forbidden := require_project_access(None, project_id or group['project_id'], 'project.read'):
    return None, forbidden
  return group, None


def input_is_visible(input_id: int, project_id: str, user_id: Optional[int]) -> bool:
  """Whether a project the user can read (the project or its subprojects) used the input, or one of its collections has it"""
  if not catalog.inputs_outside([input_id], readable_scope(project_id)):
    return True
  return catalog.in_collections_of(input_id, prefixes(project_id), user_id)


def check_team(team_id: Optional[int], project_id: str, viewer: Viewer):
  """None if the user can give the group to this team: its members, or the project's owners. Else an error response."""
  if team_id is None:
    return None
  if annotations.team_exists(team_id) is None:
    return jsonify({"error": "This team (group) doesn't exist.", "reason": "team_not_found"}), 404
  if team_id not in viewer.teams and not viewer.owns(project_id):
    return jsonify({"error": "Give groups to your own teams: you aren't a member of this one.", "reason": "not_a_member"}), 403
  return None


def name_taken_response(project_id: str, name: str, taken_id: int, user_id: Optional[int]):
  """409: names are unique per project. Someone else's personal group: we don't say which (no id)."""
  taken = annotations.group_by_id(taken_id)
  if taken is not None and not is_visible_to(taken, user_id):
    return jsonify({"error": f"The name {name} is taken in {project_id}: pick another.", "reason": "name_taken"}), 409
  return jsonify({"error": f"{project_id} already has an annotation group named {name}.", "reason": "name_taken", "id": taken_id}), 409


# Groups

@app.route("/api/v1/annotation-groups", methods=['GET'])
@app.route("/api/v1/annotation-groups/", methods=['GET'])
def list_annotation_groups():
  """
  {groups: [Group], can_create, can_share, my_teams}: the groups of the project and of its ancestors, and the user's
  personal ones, by name, with how many annotations each has (`count`). ?archived=1 lists the archived ones too.
  """
  try:
    project_id = clean_project(request.args.get('project'))
  except CollectionError as e:
    return error_response(e)
  if forbidden := require_project_access(None, project_id, 'project.read'):
    return forbidden
  viewer = Viewer()
  rows = annotations.groups_of(prefixes(project_id), viewer.user_id, archived=flag(request.args.get('archived'), False), with_counts=True)
  return jsonify({
    "groups": [viewer.group(row, project_id, row.get('count') or 0) for row in rows],
    # anyone signed in makes personal groups (~me/...); developers make the project's
    "can_create": bool(viewer.user_id),
    "can_share": viewer.writable(project_id),
    # the teams the user can give a group to
    "my_teams": [{"id": g['id'], "name": g['name']} for g in catalog.groups_of(viewer.user_id)],
  })


@app.route("/api/v1/annotation-groups", methods=['POST'])
@app.route("/api/v1/annotation-groups/", methods=['POST'])
@login_required
def create_annotation_group():
  """
  {project, name, description?, team?} => the Group, 201. 409 if the name is taken in the project. "~me/..." names
  are personal (anyone who reads the project), others need annotations.write.
  """
  viewer = Viewer()
  if not viewer.user_id:
    return sign_in_error()
  if error := token_scope_error():
    return error
  try:
    body = json_body()
    project_id = clean_project(body.get('project'))
    name, personal = normalize_group_name(body.get('name'), viewer.user_name)
    description = clean_text(body.get('description'), MAX_DESCRIPTION, 'description')
    team_id = clean_team(body.get('team'))
    if personal and team_id is not None:
      raise AnnotationError("Personal groups have no team: they're yours only", reason='personal_group')
  except CollectionError as e:
    return error_response(e)
  if forbidden := require_project_access(None, project_id, 'project.read' if personal else 'annotations.write'):
    return forbidden
  if not catalog.project_exists(project_id):
    return jsonify({"error": f"The project {project_id} doesn't exist.", "reason": "project_not_found"}), 404
  if error := check_team(team_id, project_id, viewer):
    return error
  if (taken := annotations.group_name_taken(project_id, name)) is not None:
    return name_taken_response(project_id, name, taken, viewer.user_id)
  try:
    group_id = annotations.create_group(project_id, name, description, team_id, personal, viewer.user_id)
    audit('annotation_group.create', 'annotation_group', group_id, name, project_id=project_id,
          meta={"personal": personal, "team_id": team_id})
    db_session.commit()
  except IntegrityError:
    db_session.rollback()
    taken = annotations.group_name_taken(project_id, name)
    return name_taken_response(project_id, name, taken, viewer.user_id) if taken is not None else (jsonify({"error": "Try again.", "reason": "conflict"}), 409)
  return jsonify(viewer.group(annotations.group_by_id(group_id), project_id)), 201


@app.route("/api/v1/annotation-groups/<int:group_id>", methods=['PATCH', 'POST'])
@app.route("/api/v1/annotation-groups/<int:group_id>/", methods=['PATCH', 'POST'])
@login_required
def edit_annotation_group(group_id):
  """{name?, description?, team?, archived?}: its creator or the project's owners. => the Group"""
  viewer = Viewer()
  if not viewer.user_id:
    return sign_in_error()
  if error := token_scope_error():
    return error
  group, error = get_annotation_group(group_id, viewer)
  if error:
    return error
  if not viewer.can_manage(group):
    return jsonify({"error": "Only who created this group, or the project's owners, can change it.", "reason": "not_allowed"}), 403
  fields = {}
  try:
    body = json_body()
    if 'name' in body:
      name, personal = normalize_group_name(body['name'], viewer.user_name)
      if name != group['name']:
        fields['name'] = name
      if personal != bool(group['personal']):
        fields['personal'] = personal
    if 'description' in body:
      fields['description'] = clean_text(body['description'], MAX_DESCRIPTION, 'description')
    if 'team' in body:
      team_id = clean_team(body['team'])
      if team_id != group.get('team_id'):
        fields['team_id'] = team_id
    if 'archived' in body:
      if not isinstance(body['archived'], bool):
        raise AnnotationError("`archived` must be true or false")
      fields['archived'] = body['archived']
    if fields.get('personal', bool(group['personal'])) and fields.get('team_id') is not None:
      raise AnnotationError("Personal groups have no team: they're yours only", reason='personal_group')
  except CollectionError as e:
    return error_response(e)
  if fields.get('personal') is True:
    # someone else's group can't become yours only
    if not is_owner(group, viewer.user_id):
      return jsonify({"error": "Only who created this group can make it personal.", "reason": "not_the_owner"}), 403
    fields['team_id'] = None
  if fields.get('personal') is False and (forbidden := require_project_access(None, group['project_id'], 'annotations.write')):
    return forbidden
  if fields.get('team_id') is not None and (error := check_team(fields['team_id'], group['project_id'], viewer)):
    return error
  if 'name' in fields and (taken := annotations.group_name_taken(group['project_id'], fields['name'], except_id=group_id)) is not None:
    return name_taken_response(group['project_id'], fields['name'], taken, viewer.user_id)
  try:
    annotations.update_group(group_id, fields)
    before = {k: (group.get('archived_at') is not None if k == 'archived' else group.get(k)) for k in fields}
    if changes := capped(diff(before, fields)):
      audit('annotation_group.update', 'annotation_group', group_id, fields.get('name') or group['name'], project_id=group['project_id'], changes=changes)
    db_session.commit()
  except IntegrityError:
    db_session.rollback()
    return jsonify({"error": f"{group['project_id']} already has an annotation group named {fields.get('name')}.", "reason": "name_taken"}), 409
  return jsonify(viewer.group(annotations.group_by_id(group_id)))


@app.route("/api/v1/annotation-groups/<int:group_id>/labels", methods=['GET'])
def annotation_group_labels(group_id):
  """{labels: [{label, count}]}: the group's labels containing ?q= (those starting with it first), the most used first"""
  viewer = Viewer()
  try:
    project_id = clean_project(request.args.get('project')) if request.args.get('project') else None
    limit = int(request.args.get('limit') or DEFAULT_LABELS)
  except CollectionError as e:
    return error_response(e)
  except ValueError:
    return error_response(AnnotationError("`limit` must be a number"))
  group, error = get_annotation_group(group_id, viewer, project_id)
  if error:
    return error
  labels = annotations.labels_of_group(group_id, clean_label_query(request.args.get('q')), max(1, min(limit, MAX_LABELS_LISTED)))
  return jsonify({"labels": labels})


# Annotations of an input

@app.route("/api/v1/inputs/<int:input_id>/annotations", methods=['GET'])
@app.route("/api/v1/inputs/<int:input_id>/annotations/", methods=['GET'])
def input_annotations(input_id):
  """
  {annotations: [Annotation], groups: [Group]}: the input's annotations (not deleted) in the groups seen from the
  project (?groups=1,2: only those). `groups`: the groups seen from the project (not archived, and archived ones that
  have annotations of the input), each with `count`, its annotations of the input.
  """
  try:
    project_id = clean_project(request.args.get('project'))
    only = clean_ids(request.args.get('groups'))
  except CollectionError as e:
    return error_response(e)
  if forbidden := require_project_access(None, project_id, 'project.read'):
    return forbidden
  viewer = Viewer()
  if not input_is_visible(input_id, project_id, viewer.user_id):
    return jsonify({"error": f"No run of {project_id} used this input.", "reason": "input_not_found"}), 404
  rows = annotations.groups_of(prefixes(project_id), viewer.user_id, archived=True)
  counts = annotations.counts_on_input(input_id, [g['id'] for g in rows])
  # archived groups: hidden from pickers, their annotations stay readable
  groups = [g for g in rows if g.get('archived_at') is None or counts.get(g['id'])]
  shown = [g['id'] for g in groups if only is None or g['id'] in only]
  by_id = {g['id']: g for g in groups}
  editable = {g['id']: viewer.can_edit(g) for g in groups}
  found = annotations.annotations_of_input(input_id, shown)
  return jsonify({
    "annotations": [serialize_annotation(a, editable.get(a['group_id'], False)) for a in found if a['group_id'] in by_id],
    "groups": [serialize_group(g, project_id, editable[g['id']], viewer.can_manage(g), counts.get(g['id'], 0)) for g in groups],
  })


@app.route("/api/v1/inputs/<int:input_id>/annotations", methods=['POST'])
@app.route("/api/v1/inputs/<int:input_id>/annotations/", methods=['POST'])
@login_required
def create_annotation(input_id):
  """
  {group, x, y, w, h, labels?, fields?, method?, image_width?, image_height?} => the Annotation, 201.
  In the input's pixels; inside the image when its size is sent.
  """
  viewer = Viewer()
  if not viewer.user_id:
    return sign_in_error()
  if error := token_scope_error():
    return error
  try:
    body = json_body()
    group_id = body.get('group')
    if isinstance(group_id, dict):
      group_id = group_id.get('id')
    if group_id is None:
      raise AnnotationError("`group` is required: the id of the annotation group to add it to", reason='group_required')
    group_id = clean_id(group_id, 'group')
    values = {
      **clean_rectangle(body),
      'labels': clean_labels(body.get('labels')),
      'fields': clean_fields(body.get('fields')),
      'method': clean_method(body.get('method')),
    }
  except CollectionError as e:
    return error_response(e)
  group, error = get_annotation_group(group_id, viewer)
  if error:
    return error
  if not viewer.can_edit(group):
    return jsonify({"error": f"You can't edit the annotations of {group['name']}.", "reason": "not_allowed"}), 403
  if group.get('archived_at') is not None:
    return jsonify({"error": f"{group['name']} is archived: unarchive it to add annotations.", "reason": "archived_group"}), 409
  if catalog.input_by_id(input_id) is None or not input_is_visible(input_id, group['project_id'], viewer.user_id):
    return jsonify({"error": f"No run of {group['project_id']} used this input.", "reason": "input_not_found"}), 404
  if annotations.count_live(group_id, input_id) >= MAX_ANNOTATIONS:
    return jsonify({"error": f"An input has at most {MAX_ANNOTATIONS:,} annotations per group.", "reason": "too_many_annotations"}), 413
  annotation_id = annotations.create_annotation(group_id, input_id, values, viewer.user_id)
  audit('annotation.create', 'annotation', annotation_id, group['name'], project_id=group['project_id'],
        meta=capped({"group_id": group_id, "input_id": input_id, **{k: values[k] for k in ('x', 'y', 'w', 'h', 'labels', 'method')}}))
  db_session.commit()
  return jsonify(serialize_annotation(annotations.annotation_by_id(annotation_id), True)), 201


# One annotation

def get_annotation(annotation_id: int, viewer: Viewer, for_update: bool = False):
  """(annotation, group, None) or (None, None, an error response): an annotation the user sees"""
  annotation = annotations.annotation_by_id(annotation_id, for_update=for_update)
  if annotation is None:
    return None, None, not_found("This annotation doesn't exist.", 'annotation_not_found')
  group, error = get_annotation_group(annotation['group_id'], viewer)
  if error:
    status = error[1] if isinstance(error, tuple) else 404
    return None, None, (not_found("This annotation doesn't exist.", 'annotation_not_found') if status == 404 else error)
  if not input_is_visible(annotation['test_input_id'], group['project_id'], viewer.user_id):
    return None, None, not_found("This annotation doesn't exist.", 'annotation_not_found')
  return annotation, group, None


def editable_annotation(annotation_id: int, viewer: Viewer):
  """(annotation locked, group, None) or (None, None, an error response): an annotation the user edits"""
  if not viewer.user_id:
    return None, None, sign_in_error()
  if error := token_scope_error():
    return None, None, error
  annotation, group, error = get_annotation(annotation_id, viewer, for_update=True)
  if error:
    return None, None, error
  if not viewer.can_edit(group):
    return None, None, (jsonify({"error": f"You can't edit the annotations of {group['name']}.", "reason": "not_allowed"}), 403)
  return annotation, group, None


@app.route("/api/v1/annotations/<int:annotation_id>", methods=['PATCH', 'POST'])
@app.route("/api/v1/annotations/<int:annotation_id>/", methods=['PATCH', 'POST'])
@login_required
def edit_annotation(annotation_id):
  """
  {version, x?, y?, w?, h?, labels?, fields?} => the Annotation. `version`: the one the change starts from, else 409
  `stale_version` with the current `annotation`.
  """
  viewer = Viewer()
  try:
    body = json_body()
    version = clean_version(body.get('version'))
  except CollectionError as e:
    return error_response(e)
  annotation, group, error = editable_annotation(annotation_id, viewer)
  if error:
    return error
  if annotation.get('deleted_at') is not None:
    return jsonify({"error": "This annotation was deleted: restore it first.", "reason": "deleted"}), 409
  if annotation['version'] != version:
    return jsonify(stale_error(annotation, True)), 409
  try:
    values = clean_rectangle(body, annotation)
    if 'labels' in body:
      values['labels'] = clean_labels(body['labels'])
    if 'fields' in body:
      values['fields'] = clean_fields(body['fields'])
  except CollectionError as e:
    return error_response(e)
  before = snapshot(annotation)
  values = {k: v for k, v in values.items() if before.get(k) != v}
  if values:
    annotations.change_annotation(annotation, 'update', values, viewer.user_id)
    audit('annotation.update', 'annotation', annotation_id, group['name'], project_id=group['project_id'],
          changes=capped(diff({k: before[k] for k in values}, values)),
          meta={"group_id": group['id'], "input_id": annotation['test_input_id'], "version": annotation['version'] + 1})
    db_session.commit()
  return jsonify(serialize_annotation(annotations.annotation_by_id(annotation_id), True))


@app.route("/api/v1/annotations/<int:annotation_id>", methods=['DELETE'])
@app.route("/api/v1/annotations/<int:annotation_id>/", methods=['DELETE'])
@login_required
def delete_annotation(annotation_id):
  """?version= (or {version}): hides it, with its history. POST /annotations/<id>/restore undoes it. => the Annotation"""
  viewer = Viewer()
  try:
    body = request.get_json(silent=True)
    version = request.args.get('version') or (body.get('version') if isinstance(body, dict) else None)
    version = clean_version(version)
  except CollectionError as e:
    return error_response(e)
  annotation, group, error = editable_annotation(annotation_id, viewer)
  if error:
    return error
  if annotation.get('deleted_at') is not None:
    return jsonify({"error": "This annotation is already deleted.", "reason": "already_deleted", "annotation": serialize_annotation(annotation, True)}), 409
  if annotation['version'] != version:
    return jsonify(stale_error(annotation, True)), 409
  annotations.change_annotation(annotation, 'delete', {}, viewer.user_id)
  audit('annotation.delete', 'annotation', annotation_id, group['name'], project_id=group['project_id'],
        meta=capped({"group_id": group['id'], "input_id": annotation['test_input_id'], "version": annotation['version'] + 1,
                     **{k: annotation[k] for k in ('x', 'y', 'w', 'h')}, "labels": list(annotation.get('labels') or [])}))
  db_session.commit()
  return jsonify(serialize_annotation(annotations.annotation_by_id(annotation_id), True))


@app.route("/api/v1/annotations/<int:annotation_id>/restore", methods=['POST'])
@app.route("/api/v1/annotations/<int:annotation_id>/restore/", methods=['POST'])
@login_required
def restore_annotation(annotation_id):
  """{version?}: undoes a delete. => the Annotation"""
  viewer = Viewer()
  try:
    body = request.get_json(silent=True)
    version = clean_version(body.get('version') if isinstance(body, dict) else request.args.get('version'), required=False)
  except CollectionError as e:
    return error_response(e)
  annotation, group, error = editable_annotation(annotation_id, viewer)
  if error:
    return error
  if annotation.get('deleted_at') is None:
    return jsonify({"error": "This annotation isn't deleted.", "reason": "not_deleted", "annotation": serialize_annotation(annotation, True)}), 409
  if version is not None and annotation['version'] != version:
    return jsonify(stale_error(annotation, True)), 409
  if annotations.count_live(group['id'], annotation['test_input_id']) >= MAX_ANNOTATIONS:
    return jsonify({"error": f"An input has at most {MAX_ANNOTATIONS:,} annotations per group.", "reason": "too_many_annotations"}), 413
  annotations.change_annotation(annotation, 'restore', {}, viewer.user_id)
  audit('annotation.restore', 'annotation', annotation_id, group['name'], project_id=group['project_id'],
        meta={"group_id": group['id'], "input_id": annotation['test_input_id'], "version": annotation['version'] + 1})
  db_session.commit()
  return jsonify(serialize_annotation(annotations.annotation_by_id(annotation_id), True))


@app.route("/api/v1/annotations/<int:annotation_id>/history", methods=['GET'])
def annotation_history(annotation_id):
  """{changes: [{version, action, changed_by, changed_at, before, after}]}, the latest first. Deleted annotations too."""
  viewer = Viewer()
  annotation, group, error = get_annotation(annotation_id, viewer)
  if error:
    return error
  return jsonify({"changes": [serialize_change(row) for row in annotations.history(annotation_id)]})
