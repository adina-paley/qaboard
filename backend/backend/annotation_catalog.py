"""
The SQL of annotations of inputs (docs/design/annotations.md). The rules are in backend/input_annotations.py, the
API in api/input_annotations.py, the tables in models/Annotation.py. The Inputs page's filters by annotations are in
backend/input_catalog.py (InputFilters), with the other filters.

Functions take the projects in scope (a project and its ancestors, for groups) and the user: the callers check who
can read what. Every change of an annotation writes its row of annotation_changes in the same transaction. Nothing
here commits: the API does.
"""
import json
import datetime
from typing import Dict, List, Optional, Sequence

from . import input_catalog as catalog
from .input_annotations import SNAPSHOT
from .input_collections import like_escape


def utcnow() -> datetime.datetime:
  return catalog.utcnow()


# Groups

# Personal groups are seen by their creator only
VISIBLE = "(NOT g.personal OR g.created_by = :user_id)"

GROUP_COLUMNS = """
  g.id, g.project_id, g.name, g.description, g.team_id, t.name AS team_name, g.personal, g.created_at, g.updated_at,
  g.archived_at, u.id AS created_by_id, u.user_name AS created_by_user_name, u.full_name AS created_by_full_name
"""
GROUP_FROM = """
  FROM annotation_groups g
  LEFT JOIN groups t ON t.id = g.team_id
  LEFT JOIN users u ON u.id = g.created_by
"""


def groups_of(projects: Sequence[str], user_id: Optional[int] = None, archived: bool = False, with_counts: bool = False) -> List[dict]:
  """
  The groups of the projects (a project and its ancestors) the user sees, by name. `with_counts`: `count`, how many
  annotations (not deleted) each has.
  """
  count = """, (SELECT count(*) FROM annotations a WHERE a.group_id = g.id AND a.deleted_at IS NULL) AS count""" if with_counts else ""
  return catalog.rows_of(f"""
    SELECT {GROUP_COLUMNS}{count} {GROUP_FROM}
    WHERE g.project_id = ANY(:projects) AND {VISIBLE} {'' if archived else 'AND g.archived_at IS NULL'}
    ORDER BY g.name, g.project_id
  """, {"projects": list(projects), "user_id": user_id})


def group_by_id(group_id: int, for_update: bool = False) -> Optional[dict]:
  if for_update:
    catalog.execute("SELECT id FROM annotation_groups WHERE id = :id FOR UPDATE", {"id": group_id})
  return catalog.one_of(f"SELECT {GROUP_COLUMNS} {GROUP_FROM} WHERE g.id = :id", {"id": group_id})


def group_name_taken(project_id: str, name: str, except_id: Optional[int] = None) -> Optional[int]:
  row = catalog.one_of("SELECT id FROM annotation_groups WHERE project_id = :project AND name = :name AND id IS DISTINCT FROM :except_id",
                       {"project": project_id, "name": name, "except_id": except_id})
  return row['id'] if row else None


def create_group(project_id: str, name: str, description: str, team_id: Optional[int], personal: bool, user_id: Optional[int],
                 now: Optional[datetime.datetime] = None) -> int:
  now = now or utcnow()
  row = catalog.one_of("""
    INSERT INTO annotation_groups (project_id, name, description, team_id, personal, created_by, created_at, updated_at)
    VALUES (:project, :name, :description, CAST(:team_id AS integer), :personal, CAST(:user_id AS integer), :now, :now) RETURNING id
  """, {"project": project_id, "name": name, "description": description, "team_id": team_id, "personal": bool(personal), "user_id": user_id, "now": now})
  return row['id']


def update_group(group_id: int, fields: dict, now: Optional[datetime.datetime] = None):
  """`fields`: name, personal, description, team_id, archived (True/False)"""
  sets, params = [], {"id": group_id, "now": now or utcnow()}
  for column in ('name', 'personal', 'description'):
    if column in fields:
      sets.append(f"{column} = :{column}")
      params[column] = fields[column]
  if 'team_id' in fields:
    sets.append("team_id = CAST(:team_id AS integer)")
    params['team_id'] = fields['team_id']
  if 'archived' in fields:
    sets.append("archived_at = CASE WHEN :archived THEN coalesce(archived_at, :now) ELSE NULL END")
    params['archived'] = bool(fields['archived'])
  if not sets:
    return
  catalog.execute(f"UPDATE annotation_groups SET {', '.join(sets)}, updated_at = :now WHERE id = :id", params)


def team_exists(team_id: int) -> Optional[dict]:
  return catalog.one_of("SELECT id, name FROM groups WHERE id = :id", {"id": team_id})


# Annotations

ANNOTATION_COLUMNS = """
  a.id, a.group_id, a.test_input_id, a.x, a.y, a.w, a.h, a.image_width, a.image_height, a.labels, a.fields, a.method,
  a.output_id, a.created_at, a.updated_at, a.deleted_at, a.version,
  cu.id AS created_by_id, cu.user_name AS created_by_user_name, cu.full_name AS created_by_full_name,
  uu.id AS updated_by_id, uu.user_name AS updated_by_user_name, uu.full_name AS updated_by_full_name
"""
ANNOTATION_FROM = """
  FROM annotations a
  LEFT JOIN users cu ON cu.id = a.created_by
  LEFT JOIN users uu ON uu.id = a.updated_by
"""


def annotations_of_input(input_id: int, group_ids: Sequence[int]) -> List[dict]:
  """The annotations of the input in the groups, not deleted, by group then as they were made"""
  if not group_ids:
    return []
  return catalog.rows_of(f"""
    SELECT {ANNOTATION_COLUMNS} {ANNOTATION_FROM}
    WHERE a.test_input_id = :input_id AND a.deleted_at IS NULL AND a.group_id = ANY(CAST(:group_ids AS integer[]))
    ORDER BY a.group_id, a.id
  """, {"input_id": input_id, "group_ids": list(group_ids)})


def counts_on_input(input_id: int, group_ids: Sequence[int]) -> Dict[int, int]:
  """{group id: how many annotations of the input it has}"""
  if not group_ids:
    return {}
  rows = catalog.rows_of("""
    SELECT a.group_id, count(*) AS count FROM annotations a
    WHERE a.test_input_id = :input_id AND a.deleted_at IS NULL AND a.group_id = ANY(CAST(:group_ids AS integer[]))
    GROUP BY a.group_id
  """, {"input_id": input_id, "group_ids": list(group_ids)})
  return {row['group_id']: int(row['count']) for row in rows}


def annotation_counts(input_ids: Sequence[int], group_ids: Sequence[int]) -> Dict[int, int]:
  """{input id: how many annotations it has in the groups}: for a page of inputs, in one query"""
  if not input_ids or not group_ids:
    return {}
  rows = catalog.rows_of("""
    SELECT a.test_input_id, count(*) AS count FROM annotations a
    WHERE a.test_input_id = ANY(CAST(:input_ids AS integer[])) AND a.deleted_at IS NULL AND a.group_id = ANY(CAST(:group_ids AS integer[]))
    GROUP BY a.test_input_id
  """, {"input_ids": list(input_ids), "group_ids": list(group_ids)})
  return {row['test_input_id']: int(row['count']) for row in rows}


def count_live(group_id: int, input_id: int) -> int:
  row = catalog.one_of("""
    SELECT count(*) AS count FROM annotations WHERE group_id = :group_id AND test_input_id = :input_id AND deleted_at IS NULL
  """, {"group_id": group_id, "input_id": input_id})
  return int((row or {}).get('count') or 0)


def annotation_by_id(annotation_id: int, for_update: bool = False) -> Optional[dict]:
  if for_update:
    # locks the annotation: one change at a time (not in the query below: its users are on the nullable side of joins)
    catalog.execute("SELECT id FROM annotations WHERE id = :id FOR UPDATE", {"id": annotation_id})
  return catalog.one_of(f"SELECT {ANNOTATION_COLUMNS} {ANNOTATION_FROM} WHERE a.id = :id", {"id": annotation_id})


def jsonb(value) -> Optional[str]:
  return None if value is None else json.dumps(value, ensure_ascii=False, default=str)


def record_change(annotation_id: int, version: int, action: str, before: Optional[dict], after: dict, user_id: Optional[int],
                  now: datetime.datetime):
  catalog.execute("""
    INSERT INTO annotation_changes (annotation_id, version, action, changed_by, changed_at, before, after)
    VALUES (:annotation_id, :version, :action, CAST(:user_id AS integer), :now, CAST(:before AS jsonb), CAST(:after AS jsonb))
  """, {"annotation_id": annotation_id, "version": version, "action": action, "user_id": user_id, "now": now,
        "before": jsonb(before), "after": jsonb(after)})


def create_annotation(group_id: int, input_id: int, values: dict, user_id: Optional[int], now: Optional[datetime.datetime] = None) -> int:
  """
  `values`: x, y, w, h, and image_width, image_height, labels, fields, method, output_id (cleaned). Its id, with its
  version 1 in the history.
  """
  now = now or utcnow()
  row = catalog.one_of("""
    INSERT INTO annotations (group_id, test_input_id, x, y, w, h, image_width, image_height, labels, fields, method, output_id,
                             created_by, created_at, updated_by, updated_at, version)
    VALUES (:group_id, :input_id, :x, :y, :w, :h, CAST(:image_width AS integer), CAST(:image_height AS integer),
            CAST(:labels AS text[]), CAST(:fields AS jsonb), :method, CAST(:output_id AS integer),
            CAST(:user_id AS integer), :now, CAST(:user_id AS integer), :now, 1)
    RETURNING id
  """, {
    "group_id": group_id, "input_id": input_id, "x": values['x'], "y": values['y'], "w": values['w'], "h": values['h'],
    "image_width": values.get('image_width'), "image_height": values.get('image_height'), "labels": list(values.get('labels') or []),
    "fields": jsonb(values.get('fields') or {}), "method": values.get('method') or 'manual', "output_id": values.get('output_id'),
    "user_id": user_id, "now": now,
  })
  after = {key: values.get(key) for key in SNAPSHOT}
  after.update(labels=list(values.get('labels') or []), fields=values.get('fields') or {}, method=values.get('method') or 'manual')
  record_change(row['id'], 1, 'create', None, after, user_id, now)
  return row['id']


# what an edit can change
EDITABLE = ('x', 'y', 'w', 'h', 'labels', 'fields')


def change_annotation(current: dict, action: str, values: dict, user_id: Optional[int], now: Optional[datetime.datetime] = None) -> int:
  """
  Applies a change to the annotation `current` (as read, locked by the caller), and records it in the history:
  `action` update (`values`: some of EDITABLE), delete or restore. Its new version.
  """
  now = now or utcnow()
  sets, params = [], {"id": current['id'], "user_id": user_id, "now": now, "version": current['version']}
  for column in EDITABLE:
    if column in values:
      if column == 'labels':
        sets.append("labels = CAST(:labels AS text[])")
        params['labels'] = list(values['labels'])
      elif column == 'fields':
        sets.append("fields = CAST(:fields AS jsonb)")
        params['fields'] = jsonb(values['fields'])
      else:
        sets.append(f"{column} = :{column}")
        params[column] = values[column]
  if action == 'delete':
    sets.append("deleted_at = :now")
  elif action == 'restore':
    sets.append("deleted_at = NULL")
  # the version it was read at: a change made meanwhile (without the caller's lock) would update nothing
  row = catalog.one_of(f"""
    UPDATE annotations SET {''.join(s + ', ' for s in sets)}updated_by = CAST(:user_id AS integer), updated_at = :now, version = version + 1
    WHERE id = :id AND version = :version RETURNING version
  """, params)
  if row is None:
    raise LookupError(current['id'])
  before = {key: current.get(key) for key in SNAPSHOT}
  before['labels'] = list(current.get('labels') or [])
  after = {**before, **{key: values[key] for key in EDITABLE if key in values}}
  record_change(current['id'], row['version'], action, before, after, user_id, now)
  return row['version']


def history(annotation_id: int) -> List[dict]:
  """Its changes, the latest first"""
  return catalog.rows_of("""
    SELECT c.version, c.action, c.changed_at, c.before, c.after,
           u.id AS changed_by_id, u.user_name AS changed_by_user_name, u.full_name AS changed_by_full_name
    FROM annotation_changes c LEFT JOIN users u ON u.id = c.changed_by
    WHERE c.annotation_id = :id ORDER BY c.version DESC, c.id DESC
  """, {"id": annotation_id})


def labels_of_group(group_id: int, q: str = '', limit: int = 20) -> List[dict]:
  """[{label, count}] of the group's annotations, for autocomplete: those that contain `q`, those that start with it first, then the most used"""
  params = {"group_id": group_id, "limit": limit, "contains": f"%{like_escape(q)}%", "prefix": f"{like_escape(q)}%"}
  search = "AND l.label ILIKE :contains ESCAPE '\\'" if q else ""
  prefix_first = "(l.label ILIKE :prefix ESCAPE '\\') DESC, " if q else ""
  rows = catalog.rows_of(f"""
    SELECT l.label, count(*) AS count
    FROM annotations a CROSS JOIN LATERAL unnest(a.labels) AS l(label)
    WHERE a.group_id = :group_id AND a.deleted_at IS NULL {search}
    GROUP BY l.label
    ORDER BY {prefix_first}count(*) DESC, l.label
    LIMIT :limit
  """, params)
  return [{"label": row['label'], "count": int(row['count'])} for row in rows]
