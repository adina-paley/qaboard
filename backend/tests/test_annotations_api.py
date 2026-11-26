"""
The API of annotations of inputs (api/input_annotations.py), the Inputs page's filters by annotations (api/inputs.py)
and the rules (backend/input_annotations.py), with the SQL mocked (backend/annotation_catalog.py,
backend/input_catalog.py): permissions, validation, the answers. The SQL is tested in test_annotation_catalog.py.
Annotations of other apps (api/annotations.py) are tested in test_annotations.py. See tests/TESTING.md.
"""
import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from .test_collections_api import Permissions, call

DATE = datetime.datetime(2026, 11, 26, 10, 30)


def group_row(**kwargs):
  return {
    'id': 7, 'project_id': 'a', 'name': 'isp/cars', 'description': '', 'team_id': None, 'team_name': None, 'personal': False,
    'created_at': DATE, 'updated_at': DATE, 'archived_at': None,
    'created_by_id': 1, 'created_by_user_name': 'alice', 'created_by_full_name': 'Alice A', **kwargs,
  }


def annotation_row(**kwargs):
  return {
    'id': 40, 'group_id': 7, 'test_input_id': 5, 'x': 10, 'y': 20, 'w': 30, 'h': 40, 'image_width': 640, 'image_height': 480,
    'labels': ['car'], 'fields': {}, 'method': 'manual', 'output_id': None, 'created_at': DATE, 'updated_at': DATE,
    'deleted_at': None, 'version': 3,
    'created_by_id': 1, 'created_by_user_name': 'alice', 'created_by_full_name': 'Alice A',
    'updated_by_id': 2, 'updated_by_user_name': 'bob', 'updated_by_full_name': 'Bob B', **kwargs,
  }


@pytest.fixture
def apis(monkeypatch):
  from backend import input_catalog as catalog, annotation_catalog
  from backend.api import inputs, collections, input_annotations
  permissions = Permissions()
  # bob reads a and its subprojects, develops in a
  permissions.grant('a', 'project.read', 'collections.write', 'annotations.write')
  permissions.grant('a/b', 'project.read')
  user = {'is_authenticated': True, 'user_id': 2, 'user_name': 'bob'}
  modules = (inputs, collections, input_annotations)
  for module in modules:
    monkeypatch.setattr(module, 'require_project_access', permissions.require)
    monkeypatch.setattr(module, 'can', permissions.can)
    monkeypatch.setattr(module, 'get_current_user', lambda to_jsonify=True: user, raising=False)
  db_session = MagicMock()
  audited = []
  for module in modules:
    monkeypatch.setattr(module, 'db_session', db_session)
    monkeypatch.setattr(module, 'audit', lambda *args, **kwargs: audited.append((args, kwargs)))
  actor = SimpleNamespace(value=None)
  monkeypatch.setattr(input_annotations, 'current_actor', lambda: actor.value)
  fake = SimpleNamespace()
  for name in ('scope_projects', 'inputs_outside', 'in_collections_of', 'input_by_id', 'group_ids_of', 'groups_of', 'project_exists',
               'list_inputs', 'list_input_ids'):
    mock = MagicMock(name=name)
    monkeypatch.setattr(catalog, name, mock)
    setattr(fake, name, mock)
  annotations = SimpleNamespace()
  for name in ('groups_of', 'group_by_id', 'group_name_taken', 'create_group', 'update_group', 'team_exists', 'annotations_of_input',
               'counts_on_input', 'annotation_counts', 'count_live', 'annotation_by_id', 'create_annotation', 'change_annotation',
               'history', 'labels_of_group'):
    mock = MagicMock(name=name)
    monkeypatch.setattr(annotation_catalog, name, mock)
    setattr(annotations, name, mock)
  fake.scope_projects.side_effect = lambda project: [project, f"{project}/b", f"{project}/secret"]
  fake.inputs_outside.return_value = []
  fake.in_collections_of.return_value = False
  fake.input_by_id.return_value = {'id': 5, 'database': '/d', 'path': 'x.png', 'data': {}}
  fake.group_ids_of.return_value = []
  fake.groups_of.return_value = []
  fake.project_exists.return_value = True
  annotations.groups_of.return_value = [group_row()]
  annotations.group_by_id.return_value = group_row()
  annotations.group_name_taken.return_value = None
  annotations.annotation_by_id.return_value = annotation_row()
  annotations.annotation_counts.return_value = {}
  annotations.counts_on_input.return_value = {}
  annotations.count_live.return_value = 0
  annotations.team_exists.return_value = {'id': 5, 'name': 'night-team'}
  yield SimpleNamespace(api=input_annotations, inputs=inputs, sql=fake, annotations=annotations, permissions=permissions, user=user,
                        db=db_session, audited=audited, actor=actor)


def actions(apis):
  return [args[0] for args, _ in apis.audited]


# ==========================================
# Rules
# ==========================================

def test_group_names():
  from backend.input_annotations import normalize_group_name, AnnotationError
  assert normalize_group_name(' af-team / train patches ', 'bob') == ('af-team/train patches', False)
  assert normalize_group_name('~bob/ sandbox ', 'bob') == ('~bob/sandbox', True)
  # the user's name as the server knows it
  assert normalize_group_name('~BOB/x', 'bob') == ('~bob/x', True)
  with pytest.raises(AnnotationError) as e:
    normalize_group_name('~alice/x', 'bob')
  assert (e.value.status, e.value.reason) == (403, 'not_your_namespace')
  for name in ('~bob', '~bob/', '~/x', 'a:b', '', None, 'x' * 201, '~bob/' + 'x' * 200):
    with pytest.raises(AnnotationError) as e:
      normalize_group_name(name, 'bob')
    assert e.value.status == 400, name
  with pytest.raises(AnnotationError):
    normalize_group_name('~bob/x', None)


def test_rectangles():
  from backend.input_annotations import clean_rectangle, AnnotationError
  assert clean_rectangle({'x': 0, 'y': 1, 'w': 2, 'h': 3.0}) == {'x': 0, 'y': 1, 'w': 2, 'h': 3}
  assert clean_rectangle({'x': 0, 'y': 0, 'w': 640, 'h': 480, 'image_width': 640, 'image_height': 480})['image_width'] == 640
  for body in ({'x': 0, 'y': 0, 'w': 0, 'h': 1}, {'x': -1, 'y': 0, 'w': 1, 'h': 1}, {'x': 0.5, 'y': 0, 'w': 1, 'h': 1},
               {'x': True, 'y': 0, 'w': 1, 'h': 1}, {'x': '1', 'y': 0, 'w': 1, 'h': 1}, {'y': 0, 'w': 1, 'h': 1},
               {'x': 1, 'y': 0, 'w': 640, 'h': 1, 'image_width': 640, 'image_height': 480},
               {'x': 0, 'y': 0, 'w': 1, 'h': 1, 'image_width': 640}, {'x': 0, 'y': 0, 'w': 1, 'h': 1, 'image_width': 0, 'image_height': 1},
               {'x': 0, 'y': 0, 'w': 10**7, 'h': 1}):
    with pytest.raises(AnnotationError):
      clean_rectangle(body)
  # edits: what's sent, inside the image it was drawn on
  current = annotation_row()
  assert clean_rectangle({'x': 600}, current) == {'x': 600}
  with pytest.raises(AnnotationError, match='out of the image'):
    clean_rectangle({'x': 611}, current)
  # the size it was drawn on stays
  assert clean_rectangle({'w': 5, 'image_width': 10, 'image_height': 10}, current) == {'w': 5}
  assert clean_rectangle({'x': 10**6 - 10}, annotation_row(image_width=None, image_height=None)) == {'x': 10**6 - 10}


def test_labels_fields_methods():
  from backend.input_annotations import clean_labels, clean_fields, clean_method, clean_source, AnnotationError
  assert clean_labels([' car ', 'car', '', 'red  car', '\x00x']) == ['car', 'red car', 'x']
  assert clean_labels(None) == []
  assert len(clean_labels([f"l{i}" for i in range(50)] + ['l0'])) == 50
  for labels in ('car', [1], ['x' * 101], [f"l{i}" for i in range(51)]):
    with pytest.raises(AnnotationError):
      clean_labels(labels)
  assert clean_fields({'make': 'Zoé', 'n': [1, 2]}) == {'make': 'Zoé', 'n': [1, 2]} and clean_fields(None) == {}
  with pytest.raises(AnnotationError) as e:
    clean_fields({'k': 'x' * 4100})
  assert e.value.status == 413
  for fields in ([1], 'x', {'k': object()}):
    with pytest.raises(AnnotationError):
      clean_fields(fields)
  assert [clean_method(m) for m in (None, 'manual', 'script', 'import', ' model: yolo-v8 ')] == ['manual', 'manual', 'script', 'import', 'model:yolo-v8']
  for method in ('model:', 'model:' + 'x' * 101, 'human', 3):
    with pytest.raises(AnnotationError):
      clean_method(method)
  assert clean_source({'script': 'a.py'}) == {'script': 'a.py'} and clean_source(None) is None
  for source in ('a.py', {'argv': ['x' * 20_001]}):
    with pytest.raises(AnnotationError):
      clean_source(source)


def test_who_edits():
  from backend.input_annotations import can_edit, can_manage
  shared, team, personal = group_row(), group_row(team_id=5), group_row(personal=True, name='~alice/x')
  # its creator, always
  assert all(can_edit(g, 1, [], False) for g in (shared, team, personal))
  # developers, without a team
  assert can_edit(shared, 2, [], True) and not can_edit(shared, 2, [5], False)
  # its team's members, with one; developers not in it don't
  assert can_edit(team, 2, [5], False) and not can_edit(team, 2, [], True)
  assert not can_edit(personal, 2, [5], True) and not can_edit(shared, None, [], True)
  assert can_manage(shared, 1, False) and can_manage(shared, 2, True) and not can_manage(shared, 2, False)
  assert not can_manage(personal, 2, True) and not can_manage(shared, None, True)


# ==========================================
# Groups
# ==========================================

def test_list_groups(apis, dummy_app):
  apis.annotations.groups_of.return_value = [group_row(count=12), group_row(id=8, project_id='a/b', name='mine', personal=False, team_id=5, team_name='night-team', created_by_id=3)]
  apis.sql.groups_of.return_value = [{'id': 5, 'name': 'night-team'}]
  data = call(dummy_app, apis.api.list_annotation_groups, url='/api/v1/annotation-groups?project=a/b').get_json()
  assert apis.annotations.groups_of.call_args == ((['a/b', 'a'], 2), {'archived': False, 'with_counts': True})
  first, second = data['groups']
  assert first == {
    'id': 7, 'project': 'a', 'name': 'isp/cars', 'description': '', 'team': None, 'personal': False,
    'created_by': {'id': 1, 'user_name': 'alice', 'full_name': 'Alice A'}, 'created_at': '2026-11-26T10:30:00Z',
    'updated_at': '2026-11-26T10:30:00Z', 'archived': False, 'archived_at': None, 'can_edit': True, 'can_manage': False,
    'inherited': True, 'count': 12,
  }
  # bob isn't in its team (yet)
  assert (second['team'], second['can_edit'], second['inherited'], second['count']) == ({'id': 5, 'name': 'night-team'}, False, False, 0)
  # bob reads a/b: personal groups only there
  assert (data['can_create'], data['can_share'], data['my_teams']) == (True, False, [{'id': 5, 'name': 'night-team'}])
  call(dummy_app, apis.api.list_annotation_groups, url='/api/v1/annotation-groups?project=a&archived=1')
  assert apis.annotations.groups_of.call_args[1]['archived'] is True
  assert call(dummy_app, apis.api.list_annotation_groups, url='/api/v1/annotation-groups?project=other').status_code == 403
  assert call(dummy_app, apis.api.list_annotation_groups, url='/api/v1/annotation-groups').status_code == 400


def create_group(dummy_app, apis, body):
  return call(dummy_app, apis.api.create_annotation_group, url='/api/v1/annotation-groups', method='POST', json=body)


def test_create_groups(apis, dummy_app):
  apis.annotations.create_group.return_value = 7
  response = create_group(dummy_app, apis, {'project': 'a', 'name': ' isp / cars ', 'description': 'Cars', 'team': None})
  assert response.status_code == 201, response.get_json()
  assert apis.annotations.create_group.call_args[0] == ('a', 'isp/cars', 'Cars', None, False, 2)
  assert actions(apis) == ['annotation_group.create'] and apis.audited[0][1]['project_id'] == 'a'
  apis.db.commit.assert_called_once()
  # personal: anyone who reads the project, in their own namespace
  apis.annotations.group_by_id.return_value = group_row(name='~bob/try', personal=True, created_by_id=2)
  response = create_group(dummy_app, apis, {'project': 'a/b', 'name': '~bob/try'})
  assert response.status_code == 201 and response.get_json()['personal'] is True
  assert apis.annotations.create_group.call_args[0] == ('a/b', '~bob/try', '', None, True, 2)
  assert create_group(dummy_app, apis, {'project': 'a/b', 'name': 'shared'}).status_code == 403
  response = create_group(dummy_app, apis, {'project': 'a', 'name': '~alice/try'})
  assert (response.status_code, response.get_json()['reason']) == (403, 'not_your_namespace')
  response = create_group(dummy_app, apis, {'project': 'a', 'name': '~bob/try', 'team': 5})
  assert (response.status_code, response.get_json()['reason']) == (400, 'personal_group')


def test_create_groups_checks(apis, dummy_app):
  # a team bob isn't in, unless he owns the project
  response = create_group(dummy_app, apis, {'project': 'a', 'name': 'night', 'team': 5})
  assert (response.status_code, response.get_json()['reason']) == (403, 'not_a_member')
  apis.sql.group_ids_of.return_value = [5]
  apis.annotations.create_group.return_value = 9
  assert create_group(dummy_app, apis, {'project': 'a', 'name': 'night', 'team': 5}).status_code == 201
  assert apis.annotations.create_group.call_args[0][3] == 5
  apis.annotations.team_exists.return_value = None
  assert create_group(dummy_app, apis, {'project': 'a', 'name': 'night', 'team': 5}).get_json()['reason'] == 'team_not_found'
  assert create_group(dummy_app, apis, {'project': 'a', 'name': 'night', 'team': 'x'}).status_code == 400
  # taken: by a group bob sees (its id), or someone's personal one
  apis.annotations.group_name_taken.return_value = 7
  assert create_group(dummy_app, apis, {'project': 'a', 'name': 'isp/cars'}).get_json() == {'error': 'a already has an annotation group named isp/cars.', 'reason': 'name_taken', 'id': 7}
  apis.annotations.group_by_id.return_value = group_row(personal=True)
  assert 'id' not in create_group(dummy_app, apis, {'project': 'a', 'name': 'isp/cars'}).get_json()
  apis.annotations.group_name_taken.return_value = None
  apis.sql.project_exists.return_value = False
  assert create_group(dummy_app, apis, {'project': 'a', 'name': 'x'}).status_code == 404
  # tokens need the act scope
  apis.actor.value = SimpleNamespace(token=object(), has_scope=lambda scope: scope == 'read')
  assert create_group(dummy_app, apis, {'project': 'a', 'name': 'x'}).get_json()['reason'] == 'missing_scope'
  apis.actor.value = None
  apis.user['is_authenticated'] = False
  assert create_group(dummy_app, apis, {'project': 'a', 'name': 'x'}).status_code == 401
  assert len(actions(apis)) == 1


def edit_group(dummy_app, apis, body, group_id=7):
  return call(dummy_app, apis.api.edit_annotation_group, group_id, url=f'/api/v1/annotation-groups/{group_id}', method='PATCH', json=body)


def test_edit_groups(apis, dummy_app):
  # alice's group: bob isn't an owner of a
  response = edit_group(dummy_app, apis, {'description': 'x'})
  assert (response.status_code, response.get_json()['reason']) == (403, 'not_allowed')
  apis.permissions.grant('a', 'project.access')
  apis.sql.group_ids_of.return_value = []
  response = edit_group(dummy_app, apis, {'name': 'isp/vehicles', 'description': 'All', 'archived': True, 'team': 5})
  assert response.status_code == 200, response.get_json()
  assert apis.annotations.update_group.call_args[0] == (7, {'name': 'isp/vehicles', 'description': 'All', 'team_id': 5, 'archived': True})
  (action, *_), kwargs = apis.audited[-1]
  assert action == 'annotation_group.update' and kwargs['changes']['name'] == {'from': 'isp/cars', 'to': 'isp/vehicles'}
  assert edit_group(dummy_app, apis, {'archived': 'yes'}).status_code == 400
  apis.annotations.group_name_taken.return_value = 8
  assert edit_group(dummy_app, apis, {'name': 'taken'}).status_code == 409
  # someone else's group can't become bob's personal one
  apis.annotations.group_name_taken.return_value = None
  assert edit_group(dummy_app, apis, {'name': '~bob/cars'}).get_json()['reason'] == 'not_the_owner'


def test_personal_groups_are_their_creators(apis, dummy_app):
  apis.annotations.group_by_id.return_value = group_row(name='~alice/x', personal=True)
  apis.permissions.grant('a', 'project.access')
  # as if they didn't exist, even for owners
  assert edit_group(dummy_app, apis, {'description': 'x'}).status_code == 404
  assert call(dummy_app, apis.api.annotation_group_labels, 7, url='/api/v1/annotation-groups/7/labels').status_code == 404
  # bob's: he shares it, which needs annotations.write, and it loses nothing else
  apis.annotations.group_by_id.return_value = group_row(name='~bob/x', personal=True, created_by_id=2)
  response = edit_group(dummy_app, apis, {'name': 'shared/x'})
  assert response.status_code == 200
  assert apis.annotations.update_group.call_args[0] == (7, {'name': 'shared/x', 'personal': False})
  assert edit_group(dummy_app, apis, {'team': 5}).get_json()['reason'] == 'personal_group'
  # making his shared group personal drops its team
  apis.annotations.group_by_id.return_value = group_row(created_by_id=2, team_id=5)
  assert edit_group(dummy_app, apis, {'name': '~bob/x'}).status_code == 200
  assert apis.annotations.update_group.call_args[0] == (7, {'name': '~bob/x', 'personal': True, 'team_id': None})


def test_labels(apis, dummy_app):
  apis.annotations.labels_of_group.return_value = [{'label': 'car', 'count': 3}]
  data = call(dummy_app, apis.api.annotation_group_labels, 7, url='/api/v1/annotation-groups/7/labels?q=%20ca%20&limit=500').get_json()
  assert data == {'labels': [{'label': 'car', 'count': 3}]}
  assert apis.annotations.labels_of_group.call_args[0] == (7, 'ca', 200)
  # from a project that doesn't see it
  assert call(dummy_app, apis.api.annotation_group_labels, 7, url='/api/v1/annotation-groups/7/labels?project=ab').status_code == 403
  apis.permissions.grant('ab', 'project.read')
  assert call(dummy_app, apis.api.annotation_group_labels, 7, url='/api/v1/annotation-groups/7/labels?project=ab').status_code == 404
  assert call(dummy_app, apis.api.annotation_group_labels, 7, url='/api/v1/annotation-groups/7/labels?project=a/b').status_code == 200


# ==========================================
# Annotations
# ==========================================

def test_an_inputs_annotations(apis, dummy_app):
  apis.annotations.groups_of.return_value = [group_row(), group_row(id=8, name='old', archived_at=DATE), group_row(id=9, name='older', archived_at=DATE),
                                             group_row(id=10, name='team', team_id=5)]
  apis.annotations.counts_on_input.return_value = {7: 1, 8: 1}
  apis.annotations.annotations_of_input.return_value = [annotation_row(), annotation_row(id=41, group_id=8)]
  data = call(dummy_app, apis.api.input_annotations, 5, url='/api/v1/inputs/5/annotations?project=a/b').get_json()
  assert apis.annotations.groups_of.call_args == ((['a/b', 'a'], 2), {'archived': True})
  # archived groups only with annotations on it
  assert [(g['id'], g['count'], g['can_edit']) for g in data['groups']] == [(7, 1, True), (8, 1, True), (10, 0, False)]
  assert apis.annotations.annotations_of_input.call_args[0] == (5, [7, 8, 10])
  assert data['annotations'][0] == {
    'id': 40, 'group': 7, 'input': 5, 'x': 10, 'y': 20, 'w': 30, 'h': 40, 'image_width': 640, 'image_height': 480,
    'labels': ['car'], 'fields': {}, 'method': 'manual', 'output': None,
    'created_by': {'id': 1, 'user_name': 'alice', 'full_name': 'Alice A'}, 'created_at': '2026-11-26T10:30:00Z',
    'updated_by': {'id': 2, 'user_name': 'bob', 'full_name': 'Bob B'}, 'updated_at': '2026-11-26T10:30:00Z',
    'deleted_at': None, 'version': 3, 'can_edit': True,
  }
  # some groups
  call(dummy_app, apis.api.input_annotations, 5, url='/api/v1/inputs/5/annotations?project=a&groups=8,99')
  assert apis.annotations.annotations_of_input.call_args[0] == (5, [8])
  assert call(dummy_app, apis.api.input_annotations, 5, url='/api/v1/inputs/5/annotations?project=a&groups=x').status_code == 400


def test_annotations_of_inputs_the_user_cant_see(apis, dummy_app):
  # no project bob reads used it, nor has it in a collection
  apis.sql.inputs_outside.return_value = [5]
  response = call(dummy_app, apis.api.input_annotations, 5, url='/api/v1/inputs/5/annotations?project=a')
  assert (response.status_code, response.get_json()['reason']) == (404, 'input_not_found')
  apis.annotations.annotations_of_input.assert_not_called()
  # a/secret isn't in bob's scope
  assert apis.sql.inputs_outside.call_args[0] == ([5], ['a', 'a/b'])
  apis.sql.in_collections_of.return_value = True
  assert call(dummy_app, apis.api.input_annotations, 5, url='/api/v1/inputs/5/annotations?project=a').status_code == 200
  assert apis.sql.in_collections_of.call_args[0] == (5, ['a'], 2)
  assert call(dummy_app, apis.api.input_annotations, 5, url='/api/v1/inputs/5/annotations?project=other').status_code == 403
  assert call(dummy_app, apis.api.input_annotations, 5, url='/api/v1/inputs/5/annotations').status_code == 400


def add(dummy_app, apis, body, input_id=5):
  return call(dummy_app, apis.api.create_annotation, input_id, url=f'/api/v1/inputs/{input_id}/annotations', method='POST', json=body)


def test_add_annotations(apis, dummy_app):
  apis.annotations.create_annotation.return_value = 40
  body = {'group': 7, 'x': 10, 'y': 20, 'w': 30, 'h': 40, 'labels': [' car ', 'car'], 'fields': {'make': 'x'}, 'method': 'model:yolo',
          'image_width': 640, 'image_height': 480}
  response = add(dummy_app, apis, body)
  assert response.status_code == 201, response.get_json()
  assert apis.annotations.create_annotation.call_args[0] == (7, 5, {
    'x': 10, 'y': 20, 'w': 30, 'h': 40, 'image_width': 640, 'image_height': 480, 'labels': ['car'], 'fields': {'make': 'x'}, 'method': 'model:yolo'}, 2)
  assert response.get_json()['id'] == 40 and response.get_json()['can_edit'] is True
  (action, kind, target, label), kwargs = apis.audited[-1]
  assert (action, kind, target, label, kwargs['project_id'], kwargs['meta']['input_id']) == ('annotation.create', 'annotation', 40, 'isp/cars', 'a', 5)
  apis.db.commit.assert_called_once()
  # the input must be seen from the group's project
  assert apis.sql.inputs_outside.call_args[0] == ([5], ['a', 'a/b'])


def test_add_annotations_checks(apis, dummy_app):
  body = {'group': 7, 'x': 10, 'y': 20, 'w': 30, 'h': 40}
  for bad in ({'w': 0}, {'group': None}, {'group': True}, {'labels': 'car'}, {'method': 'human'}, {'x': 700, 'image_width': 640, 'image_height': 480}):
    assert add(dummy_app, apis, {**body, **bad}).status_code == 400, bad
  assert add(dummy_app, apis, {**body, 'fields': {'k': 'x' * 5000}}).status_code == 413
  apis.annotations.count_live.return_value = 10_000
  assert add(dummy_app, apis, body).get_json()['reason'] == 'too_many_annotations'
  apis.annotations.count_live.return_value = 0
  apis.sql.inputs_outside.return_value = [5]
  assert add(dummy_app, apis, body).status_code == 404
  apis.sql.inputs_outside.return_value = []
  apis.annotations.group_by_id.return_value = group_row(archived_at=DATE)
  assert add(dummy_app, apis, body).get_json()['reason'] == 'archived_group'
  # a team's group, bob isn't in it
  apis.annotations.group_by_id.return_value = group_row(team_id=5)
  assert add(dummy_app, apis, body).get_json()['reason'] == 'not_allowed'
  apis.sql.group_ids_of.return_value = [5]
  apis.annotations.create_annotation.return_value = 40
  assert add(dummy_app, apis, body).status_code == 201
  # without a team, annotations.write on its project
  apis.annotations.group_by_id.return_value = group_row(project_id='a/b')
  assert add(dummy_app, apis, body).status_code == 403
  apis.annotations.group_by_id.return_value = None
  assert add(dummy_app, apis, body).status_code == 404
  assert actions(apis) == ['annotation.create']


def edit(dummy_app, apis, body, annotation_id=40):
  return call(dummy_app, apis.api.edit_annotation, annotation_id, url=f'/api/v1/annotations/{annotation_id}', method='PATCH', json=body)


def test_edit_annotations(apis, dummy_app):
  response = edit(dummy_app, apis, {'version': 3, 'x': 12, 'y': 20, 'labels': ['car', 'red'], 'fields': {}})
  assert response.status_code == 200, response.get_json()
  current, action, values, user_id = apis.annotations.change_annotation.call_args[0]
  # only what changed
  assert (current['id'], action, values, user_id) == (40, 'update', {'x': 12, 'labels': ['car', 'red']}, 2)
  assert apis.annotations.annotation_by_id.call_args_list[0][1] == {'for_update': True}
  (name, *_), kwargs = apis.audited[-1]
  assert name == 'annotation.update' and kwargs['changes'] == {'labels': {'from': ['car'], 'to': ['car', 'red']}, 'x': {'from': 10, 'to': 12}}
  # nothing changed: no new version
  apis.annotations.change_annotation.reset_mock()
  assert edit(dummy_app, apis, {'version': 3, 'x': 10}).status_code == 200
  apis.annotations.change_annotation.assert_not_called()
  assert len(apis.audited) == 1


def test_stale_and_deleted_annotations(apis, dummy_app):
  response = edit(dummy_app, apis, {'version': 2, 'x': 12})
  assert (response.status_code, response.get_json()['reason'], response.get_json()['version']) == (409, 'stale_version', 3)
  assert response.get_json()['annotation']['x'] == 10
  assert edit(dummy_app, apis, {'x': 12}).get_json()['reason'] == 'version_required'
  assert edit(dummy_app, apis, {'version': 3, 'x': 700}).status_code == 400
  apis.annotations.annotation_by_id.return_value = annotation_row(deleted_at=DATE)
  assert edit(dummy_app, apis, {'version': 3, 'x': 12}).get_json()['reason'] == 'deleted'
  apis.annotations.change_annotation.assert_not_called()
  # someone else's personal group
  apis.annotations.annotation_by_id.return_value = annotation_row()
  apis.annotations.group_by_id.return_value = group_row(personal=True)
  assert edit(dummy_app, apis, {'version': 3, 'x': 12}).status_code == 404
  # the input isn't seen from the group's project anymore (its subproject is private)
  apis.annotations.group_by_id.return_value = group_row()
  apis.sql.inputs_outside.return_value = [5]
  assert edit(dummy_app, apis, {'version': 3, 'x': 12}).status_code == 404


def test_delete_and_restore(apis, dummy_app):
  delete = lambda url: call(dummy_app, apis.api.delete_annotation, 40, url=url, method='DELETE')
  assert delete('/api/v1/annotations/40').get_json()['reason'] == 'version_required'
  assert delete('/api/v1/annotations/40?version=2').get_json()['reason'] == 'stale_version'
  assert delete('/api/v1/annotations/40?version=3').status_code == 200
  assert apis.annotations.change_annotation.call_args[0][1:] == ('delete', {}, 2)
  apis.annotations.annotation_by_id.return_value = annotation_row(deleted_at=DATE, version=4)
  assert delete('/api/v1/annotations/40?version=4').get_json()['reason'] == 'already_deleted'
  restore = lambda body: call(dummy_app, apis.api.restore_annotation, 40, url='/api/v1/annotations/40/restore', method='POST', json=body)
  assert restore({'version': 3}).get_json()['reason'] == 'stale_version'
  assert restore(None).status_code == 200
  assert apis.annotations.change_annotation.call_args[0][1:] == ('restore', {}, 2)
  assert actions(apis) == ['annotation.delete', 'annotation.restore']
  apis.annotations.annotation_by_id.return_value = annotation_row()
  assert restore({}).get_json()['reason'] == 'not_deleted'
  # viewers don't
  apis.annotations.group_by_id.return_value = group_row(project_id='a/b')
  assert delete('/api/v1/annotations/40?version=3').status_code == 403


def test_history(apis, dummy_app):
  apis.annotations.history.return_value = [
    {'version': 2, 'action': 'update', 'changed_at': DATE, 'before': {'x': 10}, 'after': {'x': 12},
     'changed_by_id': 2, 'changed_by_user_name': 'bob', 'changed_by_full_name': 'Bob B'},
    {'version': 1, 'action': 'create', 'changed_at': DATE, 'before': None, 'after': {'x': 10},
     'changed_by_id': None, 'changed_by_user_name': None, 'changed_by_full_name': None},
  ]
  data = call(dummy_app, apis.api.annotation_history, 40, url='/api/v1/annotations/40/history').get_json()
  assert data['changes'] == [
    {'version': 2, 'action': 'update', 'changed_by': {'id': 2, 'user_name': 'bob', 'full_name': 'Bob B'}, 'changed_at': '2026-11-26T10:30:00Z', 'before': {'x': 10}, 'after': {'x': 12}},
    {'version': 1, 'action': 'create', 'changed_by': None, 'changed_at': '2026-11-26T10:30:00Z', 'before': None, 'after': {'x': 10}},
  ]
  apis.annotations.annotation_by_id.return_value = None
  assert call(dummy_app, apis.api.annotation_history, 40, url='/api/v1/annotations/40/history').status_code == 404
  apis.annotations.annotation_by_id.return_value = annotation_row()
  apis.annotations.group_by_id.return_value = group_row(project_id='other')
  assert call(dummy_app, apis.api.annotation_history, 40, url='/api/v1/annotations/40/history').status_code == 403


# ==========================================
# The Inputs page
# ==========================================

def test_inputs_filtered_and_counted_by_annotations(apis, dummy_app):
  apis.annotations.groups_of.return_value = [group_row(), group_row(id=8, name='a-first')]
  apis.sql.list_inputs.return_value = {
    "inputs": [{'id': 5, 'database': '/d', 'path': 'x.png', 'data': {}}, {'id': 6, 'database': '/d', 'path': 'y.png', 'data': {}}],
    "total": 2, "databases": [], "folders": [], "annotation_groups": [{"id": 7, "count": 2}, {"id": 8, "count": 1}], "labels": [{"label": "car", "count": 2}],
  }
  apis.annotations.annotation_counts.return_value = {5: 3}
  data = call(dummy_app, apis.inputs.list_inputs, url='/api/v1/inputs?project=a&label=%20car%20&annotated=1').get_json()
  filters = apis.sql.list_inputs.call_args[0][1]
  assert (filters.annotation_groups, filters.annotation_group, filters.label, filters.annotated) == ([7, 8], None, 'car', True)
  assert apis.annotations.groups_of.call_args[0] == (['a'], 2)
  assert [i['annotations'] for i in data['inputs']] == [3, 0]
  assert apis.annotations.annotation_counts.call_args[0] == ([5, 6], [7, 8])
  # by name
  assert data['annotation_groups'] == [{'id': 8, 'name': 'a-first', 'count': 1}, {'id': 7, 'name': 'isp/cars', 'count': 2}]
  assert data['labels'] == [{'label': 'car', 'count': 2}]

  # one group: its annotations are counted
  call(dummy_app, apis.inputs.list_inputs, url='/api/v1/inputs?project=a&annotation_group=8')
  assert apis.sql.list_inputs.call_args[0][1].annotation_group == 8
  assert apis.annotations.annotation_counts.call_args[0] == ([5, 6], [8])
  # an archived one, seen from the project: counted too
  apis.annotations.group_by_id.return_value = group_row(id=9, archived_at=DATE)
  call(dummy_app, apis.inputs.list_inputs, url='/api/v1/inputs?project=a&annotation_group=9&fields=id')
  assert apis.sql.list_input_ids.call_args[0][1].annotation_groups == [7, 8, 9]
  # not seen from the project
  apis.annotations.group_by_id.return_value = group_row(id=9, project_id='a/b')
  response = call(dummy_app, apis.inputs.list_inputs, url='/api/v1/inputs?project=a&annotation_group=9')
  assert (response.status_code, response.get_json()['reason']) == (404, 'annotation_group_not_found')
  apis.annotations.group_by_id.return_value = group_row(id=9, personal=True)
  assert call(dummy_app, apis.inputs.list_inputs, url='/api/v1/inputs?project=a&annotation_group=9').status_code == 404
  assert call(dummy_app, apis.inputs.list_inputs, url='/api/v1/inputs?project=a&annotation_group=x').status_code == 400


def test_runs_say_which_input_they_used():
  """The run viewers ask for the annotations of a run's input by its id (GET /inputs/<test_input_id>/annotations)"""
  import pathlib
  source = (pathlib.Path(__file__).resolve().parent.parent / 'backend' / 'models' / 'Output.py').read_text()
  assert "'test_input_id': columns.get('test_input_id')" in source and "columns['test_input_id'] = self.test_input_id" in source
