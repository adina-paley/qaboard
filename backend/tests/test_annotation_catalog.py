"""
The SQL of annotations (backend/annotation_catalog.py), and the Inputs page's filters by annotations
(backend/input_catalog.py), on a real PostgreSQL: set QABOARD_TEST_DATABASE_URL. Same temporary tables as
test_input_catalog.py, in a transaction that is rolled back.
"""
import datetime
from types import SimpleNamespace

import pytest

from backend import annotation_catalog as annotations
from backend import input_catalog as catalog
from backend.input_catalog import InputFilters
from .test_input_catalog import db, inputs, people, one, add_input, add_run, NOW, paths


def rectangle(x=10, y=20, w=30, h=40, **values):
  return {'x': x, 'y': y, 'w': w, 'h': h, 'labels': [], 'fields': {}, 'method': 'manual', **values}


@pytest.fixture
def groups(db, inputs, people):
  """a's groups cars (alice's) and night (team 5), a/b's own, bob's personal one in a, and ab's (not a's ancestor)"""
  ids = SimpleNamespace(
    cars=annotations.create_group('a', 'isp/cars', 'Cars', None, False, 1, NOW),
    night=annotations.create_group('a', 'night', '', 5, False, 1, NOW),
    sub=annotations.create_group('a/b', 'b-only', '', None, False, 2, NOW),
    bobs=annotations.create_group('a', '~bob/sandbox', '', None, True, 2, NOW),
    ab=annotations.create_group('ab', 'elsewhere', '', None, False, 1, NOW),
  )
  return ids


def test_groups_seen_from_a_project(db, groups):
  names = lambda rows: [r['name'] for r in rows]
  # a project and its ancestors, personal ones their creator's only
  assert names(annotations.groups_of(['a/b', 'a'], user_id=1)) == ['b-only', 'isp/cars', 'night']
  assert names(annotations.groups_of(['a/b', 'a'], user_id=2)) == ['b-only', 'isp/cars', 'night', '~bob/sandbox']
  assert names(annotations.groups_of(['a'])) == ['isp/cars', 'night']
  night = annotations.group_by_id(groups.night)
  assert (night['team_id'], night['team_name'], night['created_by_user_name'], night['personal']) == (5, 'night-team', 'alice', False)
  assert annotations.group_by_id(groups.bobs, for_update=True)['personal'] is True
  assert annotations.group_by_id(999) is None


def test_rename_archive_and_names(db, groups):
  assert annotations.group_name_taken('a', 'night') == groups.night
  assert annotations.group_name_taken('a', 'night', except_id=groups.night) is None
  assert annotations.group_name_taken('a/b', 'night') is None
  db.cursor.execute("SAVEPOINT s")
  with pytest.raises(Exception):
    annotations.create_group('a', 'night', '', None, False, 1, NOW)
  db.cursor.execute("ROLLBACK TO SAVEPOINT s")
  later = NOW + datetime.timedelta(hours=1)
  annotations.update_group(groups.night, {'name': 'night/street', 'description': 'Lights', 'team_id': None, 'archived': True}, later)
  row = annotations.group_by_id(groups.night)
  assert (row['name'], row['description'], row['team_id'], row['archived_at'], row['updated_at']) == ('night/street', 'Lights', None, later, later)
  assert [r['name'] for r in annotations.groups_of(['a'])] == ['isp/cars']
  assert [r['name'] for r in annotations.groups_of(['a'], archived=True)] == ['isp/cars', 'night/street']
  # archiving again keeps the date
  annotations.update_group(groups.night, {'archived': True}, later + datetime.timedelta(hours=1))
  assert annotations.group_by_id(groups.night)['archived_at'] == later
  annotations.update_group(groups.night, {'archived': False, 'team_id': 6}, NOW)
  assert (annotations.group_by_id(groups.night)['archived_at'], annotations.group_by_id(groups.night)['team_name']) == (None, 'day-team')
  annotations.update_group(groups.night, {})
  assert annotations.team_exists(5) == {'id': 5, 'name': 'night-team'} and annotations.team_exists(7) is None


def test_annotations_and_their_history(db, inputs, groups):
  first = annotations.create_annotation(groups.cars, inputs.street, rectangle(labels=['car', 'red'], fields={'make': 'Zoé'}, image_width=640, image_height=480), 1, NOW)
  second = annotations.create_annotation(groups.cars, inputs.street, rectangle(x=0, method='model:yolo'), 2, NOW)
  annotations.create_annotation(groups.night, inputs.street, rectangle(), 1, NOW)
  annotations.create_annotation(groups.cars, inputs.rain1, rectangle(), 1, NOW)

  found = annotations.annotations_of_input(inputs.street, [groups.cars])
  assert [a['id'] for a in found] == [first, second]
  a = found[0]
  assert (a['x'], a['y'], a['w'], a['h'], a['image_width'], a['labels'], a['fields'], a['method'], a['version']) == (10, 20, 30, 40, 640, ['car', 'red'], {'make': 'Zoé'}, 'manual', 1)
  assert (a['created_by_user_name'], a['updated_by_user_name'], a['created_at'], a['deleted_at']) == ('alice', 'alice', NOW, None)
  assert found[1]['method'] == 'model:yolo'
  assert annotations.annotations_of_input(inputs.street, []) == []
  assert annotations.counts_on_input(inputs.street, [groups.cars, groups.night, groups.sub]) == {groups.cars: 2, groups.night: 1}
  assert annotations.count_live(groups.cars, inputs.street) == 2

  # bob moves it and relabels it
  later = NOW + datetime.timedelta(minutes=5)
  current = annotations.annotation_by_id(first, for_update=True)
  assert annotations.change_annotation(current, 'update', {'x': 12, 'labels': ['car']}, 2, later) == 2
  a = annotations.annotation_by_id(first)
  assert (a['x'], a['labels'], a['version'], a['updated_by_user_name'], a['updated_at'], a['created_by_user_name']) == (12, ['car'], 2, 'bob', later, 'alice')
  # a change from a version read before: refused, nothing written
  with pytest.raises(LookupError):
    annotations.change_annotation(current, 'update', {'x': 99}, 2, later)
  assert annotations.annotation_by_id(first)['x'] == 12

  # deleted: hidden, kept
  annotations.change_annotation(a, 'delete', {}, 1, later)
  assert [x['id'] for x in annotations.annotations_of_input(inputs.street, [groups.cars])] == [second]
  assert annotations.annotation_by_id(first)['deleted_at'] == later
  assert annotations.count_live(groups.cars, inputs.street) == 1
  annotations.change_annotation(annotations.annotation_by_id(first), 'restore', {}, 1, later)
  assert annotations.annotation_by_id(first)['deleted_at'] is None

  changes = annotations.history(first)
  assert [(c['version'], c['action'], c['changed_by_user_name']) for c in changes] == [(4, 'restore', 'alice'), (3, 'delete', 'alice'), (2, 'update', 'bob'), (1, 'create', 'alice')]
  update, create = changes[2], changes[3]
  assert create['before'] is None
  assert create['after'] == {'x': 10, 'y': 20, 'w': 30, 'h': 40, 'image_width': 640, 'image_height': 480, 'labels': ['car', 'red'], 'fields': {'make': 'Zoé'}, 'method': 'manual'}
  assert update['before'] == create['after']
  assert update['after'] == {**create['after'], 'x': 12, 'labels': ['car']}
  assert annotations.history(999) == []


def test_sizes_are_positive(db, inputs, groups):
  db.cursor.execute("SAVEPOINT s")
  with pytest.raises(Exception, match='ck_annotations_size'):
    annotations.create_annotation(groups.cars, inputs.street, rectangle(w=0), 1, NOW)
  db.cursor.execute("ROLLBACK TO SAVEPOINT s")


def test_labels_for_autocomplete(db, inputs, groups):
  for labels in (['car', 'red car'], ['car'], ['truck', 'scar'], ['bus']):
    annotations.create_annotation(groups.cars, inputs.street, rectangle(labels=labels), 1, NOW)
  deleted = annotations.create_annotation(groups.cars, inputs.street, rectangle(labels=['bus']), 1, NOW)
  annotations.change_annotation(annotations.annotation_by_id(deleted), 'delete', {}, 1, NOW)
  annotations.create_annotation(groups.night, inputs.street, rectangle(labels=['car']), 1, NOW)
  assert annotations.labels_of_group(groups.cars) == [
    {'label': 'car', 'count': 2}, {'label': 'bus', 'count': 1}, {'label': 'red car', 'count': 1}, {'label': 'scar', 'count': 1}, {'label': 'truck', 'count': 1}]
  # those starting with it first, case-insensitively
  assert [l['label'] for l in annotations.labels_of_group(groups.cars, 'CAR')] == ['car', 'red car', 'scar']
  assert annotations.labels_of_group(groups.cars, 'c', limit=1) == [{'label': 'car', 'count': 2}]
  # LIKE's wildcards are searched literally
  assert annotations.labels_of_group(groups.cars, '%') == []
  assert annotations.labels_of_group(groups.sub) == []


def test_deleting_inputs_groups_and_users(db, inputs, groups):
  first = annotations.create_annotation(groups.cars, inputs.ab, rectangle(), 2, NOW)
  annotations.change_annotation(annotations.annotation_by_id(first), 'update', {'x': 1}, 2, NOW)
  db.cursor.execute("UPDATE annotation_groups SET created_by = NULL WHERE created_by = 2")
  db.cursor.execute("DELETE FROM users WHERE id = 2")
  a = annotations.annotation_by_id(first)
  assert (a['created_by_id'], a['updated_by_id']) == (None, None)
  assert annotations.history(first)[0]['changed_by_id'] is None
  # an input deleted: its annotations and their history go
  db.cursor.execute("DELETE FROM input_projects WHERE test_input_id = %s", (inputs.ab,))
  db.cursor.execute("DELETE FROM outputs WHERE test_input_id = %s", (inputs.ab,))
  db.cursor.execute("DELETE FROM test_inputs WHERE id = %s", (inputs.ab,))
  assert annotations.annotation_by_id(first) is None
  assert one(db.cursor, "SELECT count(*) AS n FROM annotation_changes")['n'] == 0
  # a group deleted with its project
  annotations.create_annotation(groups.sub, inputs.b_only, rectangle(), 1, NOW)
  db.cursor.execute("DELETE FROM input_projects WHERE project_id = 'a/b'")
  db.cursor.execute("DELETE FROM ci_commits WHERE project_id = 'a/b'")
  db.cursor.execute("DELETE FROM projects WHERE id = 'a/b'")
  assert annotations.group_by_id(groups.sub) is None
  assert annotations.counts_on_input(inputs.b_only, [groups.sub]) == {}


# The Inputs page

@pytest.fixture
def annotated(db, inputs, groups):
  """street: 2 cars (car, red) + 1 night (car); rain1: 1 car (bus); rain2: 1 deleted car; b_only: 1 in a/b's group; other: bob's"""
  annotations.create_annotation(groups.cars, inputs.street, rectangle(labels=['car', 'red']), 1, NOW)
  annotations.create_annotation(groups.cars, inputs.street, rectangle(labels=['car']), 1, NOW)
  annotations.create_annotation(groups.night, inputs.street, rectangle(labels=['car']), 1, NOW)
  annotations.create_annotation(groups.cars, inputs.rain1, rectangle(labels=['bus']), 1, NOW)
  deleted = annotations.create_annotation(groups.cars, inputs.rain2, rectangle(labels=['car']), 1, NOW)
  annotations.change_annotation(annotations.annotation_by_id(deleted), 'delete', {}, 1, NOW)
  annotations.create_annotation(groups.sub, inputs.b_only, rectangle(labels=['car']), 2, NOW)
  annotations.create_annotation(groups.bobs, inputs.other, rectangle(labels=['mine']), 2, NOW)
  return groups


def test_annotation_counts_of_a_page(db, inputs, annotated):
  shown = [annotated.cars, annotated.night]
  assert annotations.annotation_counts([inputs.street, inputs.rain1, inputs.rain2, inputs.other], shown) == {inputs.street: 3, inputs.rain1: 1}
  assert annotations.annotation_counts([inputs.street], [annotated.night]) == {inputs.street: 1}
  assert annotations.annotation_counts([], shown) == {} and annotations.annotation_counts([inputs.street], []) == {}


def test_filter_inputs_by_annotations(db, inputs, annotated):
  shown = [annotated.cars, annotated.night, annotated.sub]
  def listed(**kwargs):
    return paths(catalog.list_inputs(['a', 'a/b'], InputFilters(annotation_groups=shown, **kwargs), sort='path'))
  assert listed(annotated=True) == [('/data', 'b-only.png'), ('/data', 'night/rain/1.png'), ('/data', 'night/street.png')]
  assert listed(annotation_group=annotated.night) == [('/data', 'night/street.png')]
  assert listed(label='car') == [('/data', 'b-only.png'), ('/data', 'night/street.png')]
  assert listed(label='bus', annotation_group=annotated.night) == []
  assert listed(label='red', annotated=True) == [('/data', 'night/street.png')]
  # bob's personal group isn't shown: its annotations don't count
  assert listed(label='mine') == []
  # nothing shown: nothing annotated
  assert paths(catalog.list_inputs(['a'], InputFilters(annotated=True))) == []
  # "Select all matching" has the same filters
  found = catalog.list_input_ids(['a', 'a/b'], InputFilters(annotation_groups=shown, label='car'), sort='path')
  assert found['ids'] == [inputs.b_only, inputs.street]


def test_annotation_facets(db, inputs, annotated):
  shown = [annotated.cars, annotated.night, annotated.sub]
  found = catalog.list_inputs(['a', 'a/b'], InputFilters(annotation_groups=shown))
  assert found['total'] == 5
  assert sorted((g['id'], g['count']) for g in found['annotation_groups']) == sorted([(annotated.cars, 2), (annotated.night, 1), (annotated.sub, 1)])
  assert found['labels'] == [{'label': 'car', 'count': 2}, {'label': 'bus', 'count': 1}, {'label': 'red', 'count': 1}]

  # each facet ignores its own filter, not the others
  found = catalog.list_inputs(['a', 'a/b'], InputFilters(annotation_groups=shown, annotation_group=annotated.cars, label='car'))
  assert found['total'] == 1
  # the groups with car annotations
  assert sorted((g['id'], g['count']) for g in found['annotation_groups']) == sorted([(annotated.cars, 1), (annotated.night, 1), (annotated.sub, 1)])
  # the labels of cars' annotations
  assert found['labels'] == [{'label': 'bus', 'count': 1}, {'label': 'car', 'count': 1}, {'label': 'red', 'count': 1}]
  # the other facets follow the annotation filters
  assert found['databases'] == [{'database': '/data', 'count': 1}] and found['folders'] == [{'folder': 'night', 'name': 'night', 'count': 1}]
  found = catalog.list_inputs(['a', 'a/b'], InputFilters(annotation_groups=shown, folder='night/rain', annotated=True))
  assert (found['total'], found['labels']) == (1, [{'label': 'bus', 'count': 1}])

  # not counted after the first page, nor without groups
  found = catalog.list_inputs(['a', 'a/b'], InputFilters(annotation_groups=shown), offset=1)
  assert (found['annotation_groups'], found['labels']) == (None, None)
  found = catalog.list_inputs(['a'], InputFilters())
  assert (found['annotation_groups'], found['labels']) == ([], [])
