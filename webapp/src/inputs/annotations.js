// Annotations of inputs (docs/design/annotations.md, milestone 2): rectangles on inputs, in groups named like paths
// ("af-team/train-patches", personal ones "~alice/sandbox"), with labels, fields and their history.
// The API is backend/backend/api/input_annotations.py, documented in backend/backend/openapi.yaml.
// Edits send the version they saw: 409 `stale_version` answers the current annotation (`annotation`).
import { queryOptions } from "@tanstack/react-query";

import { http } from "../api/http";
import { max_name_length } from "./api";


// -- Queries

// {groups: [Group], can_create, can_share, my_teams}: the groups seen from the project (its own, its ancestors', the
// user's personal ones). `my_teams` ([{id, name}]) are the QA-Board groups the user can give a group to.
export const annotationGroupsQuery = (project, { archived = false } = {}) => queryOptions({
  queryKey: ['annotation-groups', project, { archived: !!archived }],
  queryFn: async ({ signal }) => {
    const data = (await http.get('/api/v1/annotation-groups', { params: { project, archived: archived ? 1 : 0 }, signal })).data;
    return { ...data, groups: data?.groups ?? [] };
  },
  enabled: !!project,
  staleTime: 60 * 1000,
});

// {annotations: [Annotation], groups: [Group]}: an input's annotations, in the groups seen from the project
export const inputAnnotationsKey = (input_id, project) => ['input-annotations', String(input_id), project ?? null];
export const inputAnnotationsQuery = (input_id, project) => queryOptions({
  queryKey: inputAnnotationsKey(input_id, project),
  queryFn: async ({ signal }) => {
    const data = (await http.get(`/api/v1/inputs/${input_id}/annotations`, { params: { project }, signal })).data;
    return { annotations: data?.annotations ?? [], groups: data?.groups ?? [] };
  },
  enabled: input_id !== undefined && input_id !== null && !!project,
  staleTime: 30 * 1000,
});

// {changes: [{version, action, changed_by, changed_at, before, after}]}, the latest first
// `version`: the annotation's, so that the history follows its changes
export const annotationHistoryQuery = (id, version) => queryOptions({
  queryKey: ['annotation-history', id, version ?? null],
  queryFn: async ({ signal }) => (await http.get(`/api/v1/annotations/${id}/history`, { signal })).data?.changes ?? [],
  // not for annotations being created
  enabled: is_saved_id(id),
  staleTime: 30 * 1000,
});

// [{label, count}]: the labels of a group, for the autocomplete
export const groupLabelsQuery = (group_id, q = '') => queryOptions({
  queryKey: ['annotation-labels', group_id, q.trim()],
  queryFn: async ({ signal }) => (await http.get(`/api/v1/annotation-groups/${group_id}/labels`, { params: { q: q.trim() || undefined }, signal })).data?.labels ?? [],
  enabled: group_id !== undefined && group_id !== null,
  staleTime: 60 * 1000,
});


// -- Changes: they answer what the server saved

export const createAnnotation = async (input_id, { group, x, y, w, h, labels, fields, method = 'manual', image_width, image_height }) =>
  (await http.post(`/api/v1/inputs/${input_id}/annotations`, {
    group, x, y, w, h, labels: labels ?? [], fields: fields ?? {}, method,
    image_width: image_width ?? undefined, image_height: image_height ?? undefined,
  })).data;
// changes: some of {x, y, w, h, labels, fields}
export const updateAnnotation = async (id, version, changes) => (await http.patch(`/api/v1/annotations/${id}`, { ...changes, version })).data;
// in the query string: proxies may drop the body of DELETEs
export const deleteAnnotation = async (id, version) => (await http.delete(`/api/v1/annotations/${id}`, { params: { version } })).data;
export const restoreAnnotation = async id => (await http.post(`/api/v1/annotations/${id}/restore`, {})).data;
export const createAnnotationGroup = async ({ project, name, description, team }) => (await http.post('/api/v1/annotation-groups', {
  project, name: normalize_group_name(name), description: description || undefined, team: team ?? null,
})).data;
// fields: some of {name, description, team, archived}
export const updateAnnotationGroup = async (id, fields) => (await http.patch(`/api/v1/annotation-groups/${id}`, fields)).data;

// 409 stale_version: someone changed the annotation meanwhile. The server answers the current one (`annotation`):
// null when it didn't (we reload the input's annotations), undefined when it's another error
export const stale_annotation = error => {
  if (error?.response?.status !== 409) return undefined;
  const data = error.response.data ?? {};
  if (data.reason && data.reason !== 'stale_version') return undefined;
  return data.annotation ?? data.current ?? null;
};


// -- Ids: annotations being created have a temporary id until the server answers

let next_temporary = 1;
export const temporary_id = () => `new-${next_temporary++}`;
export const is_saved_id = id => typeof id === 'number' || (typeof id === 'string' && /^\d+$/.test(id));


// -- Group names: like collections' (segments of [A-Za-z0-9._ -] joined by "/"), personal ones start with ~<user>/

export const normalize_group_name = name => {
  const parts = String(name ?? '').split('/').map(part => part.trim().replace(/\s+/g, ' ')).filter(Boolean);
  return parts.join('/');
};
export const personal_prefix = user_name => `~${user_name}/`;
export const is_personal_name = name => String(name ?? '').trim().startsWith('~');
// Why a group can't have this name, else null. `user_name`: personal groups are ~<user_name>/...
export const group_name_problem = (name, user_name) => {
  const normalized = normalize_group_name(name);
  if (!normalized) return 'Give the group a name.';
  if (normalized.length > max_name_length) return `Names have at most ${max_name_length} characters.`;
  const parts = normalized.split('/');
  let rest = normalized;
  if (parts[0].startsWith('~')) {
    if (user_name && parts[0] !== `~${user_name}`) return `Your personal groups are named ~${user_name}/…`;
    if (parts.length < 2) return `Name it after your namespace: ~${user_name ?? 'you'}/sandbox.`;
    rest = parts.slice(1).join('/');
  }
  const wrong = [...new Set(rest.replace(/[A-Za-z0-9._ /-]/g, ''))];
  if (wrong.length > 0) return `Names can have letters, digits, spaces, “.”, “_” and “-”, and “/” for folders, not ${wrong.map(c => `“${c}”`).join(' ')}.`;
  return null;
};
// "af-team/train-patches" seen from a subproject of its project: "a/b:af-team/train-patches"
export const group_ref = group => group?.inherited ? `${group.project}:${group.name}` : group?.name ?? '';


// -- Groups in the viewer

// The groups users draw into: those they edit, not archived
export const drawable_groups = groups => (groups ?? []).filter(group => group.can_edit && !group.archived);

// The group to draw into: the one picked last for the project if users still edit it, else their personal ones first
export const default_draw_group = (groups, last) => {
  const drawable = drawable_groups(groups);
  return drawable.find(group => group.id === last)
    ?? drawable.find(group => group.personal)
    ?? drawable[0]
    ?? null;
};

// The groups listed: those with annotations of the input first, then the others users can draw into, by name
export const listed_groups = (groups, annotations) => {
  const counts = {};
  for (const a of annotations ?? []) counts[a.group] = (counts[a.group] ?? 0) + 1;
  return (groups ?? [])
    .map(group => ({ ...group, shown_count: counts[group.id] ?? 0 }))
    .filter(group => group.shown_count > 0 || (group.can_edit && !group.archived))
    .sort((a, b) => (b.shown_count > 0) - (a.shown_count > 0) || a.name.localeCompare(b.name));
};

// QA-Board's groups show unless users hid them
export const is_group_visible = (group, choices) => choices?.[group.id] ?? true;


// -- Annotations as the image viewer's editable regions

// The input's size, when the server knows it (its metadata), else null
export const input_size = input => {
  const width = Number(input?.width ?? input?.metadata?.width ?? input?.data?.metadata?.width);
  const height = Number(input?.height ?? input?.metadata?.height ?? input?.data?.metadata?.height);
  return width > 0 && height > 0 ? { width, height } : null;
};

// "night, street" or nothing
export const annotation_label = annotation => (annotation?.labels ?? []).join(', ');

// An annotation as the viewer draws it. `fallback_size`: its image's size when it doesn't say (the input's)
export const annotation_region = (annotation, { color, fallback_size, editable = true } = {}) => ({
  id: annotation.id,
  x: annotation.x,
  y: annotation.y,
  w: annotation.w,
  h: annotation.h,
  imageWidth: annotation.image_width ?? fallback_size?.width ?? undefined,
  imageHeight: annotation.image_height ?? fallback_size?.height ?? undefined,
  label: annotation_label(annotation),
  color,
  readOnly: !editable || annotation.can_edit === false,
});

// A rectangle drawn on the image shown ({x, y, w, h, imageWidth, imageHeight}) in the input's pixels, when the image
// shown isn't the input itself (e.g. the server's preview) and its size is known. Else as it is.
export const to_input_pixels = (rect, size) => {
  if (!size?.width || !rect.imageWidth || (rect.imageWidth === size.width && rect.imageHeight === size.height)) {
    return { x: rect.x, y: rect.y, w: rect.w, h: rect.h, image_width: rect.imageWidth ?? null, image_height: rect.imageHeight ?? null };
  }
  const sx = size.width / rect.imageWidth;
  const sy = size.height / rect.imageHeight;
  const x0 = Math.round(rect.x * sx), y0 = Math.round(rect.y * sy);
  const x1 = Math.min(size.width, Math.round((rect.x + rect.w) * sx)), y1 = Math.min(size.height, Math.round((rect.y + rect.h) * sy));
  return { x: x0, y: y0, w: Math.max(1, x1 - x0), h: Math.max(1, y1 - y0), image_width: size.width, image_height: size.height };
};

// What a field's value is, from what users typed: numbers and booleans as such, else text
export const parse_field_value = text => {
  const value = String(text ?? '').trim();
  if (/^-?\d+(\.\d+)?$/.test(value) && value.length < 16) return Number(value);
  if (value === 'true' || value === 'false') return value === 'true';
  return value;
};

// Labels as users type them: trimmed, once each, in order
export const clean_labels = labels => [...new Set((labels ?? []).map(label => String(label).trim()).filter(Boolean))];


// -- How an annotation was made, and its history, in words

export const method_label = method => {
  if (!method || method === 'manual') return 'Drawn by hand';
  if (method === 'script') return 'From a script';
  if (method === 'import') return 'Imported';
  if (method.startsWith('model:')) return `Model ${method.slice('model:'.length)}`;
  return method;
};

export const person = user => user?.full_name || user?.user_name || (typeof user === 'string' ? user : null);

const rect_text = r => `${r.w}×${r.h} at (${r.x}, ${r.y})`;
// What a change did: ['Created 120×80 at (10, 20), labels night'] ['Moved to (12, 20)', 'Labels: +street −day']
export const describe_change = ({ action, before, after }) => {
  if (action === 'create') return [`Created ${after ? rect_text(after) : ''}${after?.labels?.length ? `, labels ${after.labels.join(', ')}` : ''}`.trim()];
  if (action === 'delete') return ['Deleted'];
  if (action === 'restore') return ['Restored'];
  if (!before || !after) return ['Changed'];
  const lines = [];
  const moved = before.x !== after.x || before.y !== after.y;
  const resized = before.w !== after.w || before.h !== after.h;
  if (resized) lines.push(`Resized to ${rect_text(after)}`);
  else if (moved) lines.push(`Moved to (${after.x}, ${after.y})`);
  const old_labels = new Set(before.labels ?? []);
  const new_labels = new Set(after.labels ?? []);
  const added = [...new_labels].filter(l => !old_labels.has(l));
  const removed = [...old_labels].filter(l => !new_labels.has(l));
  if (added.length || removed.length) lines.push(`Labels: ${[...added.map(l => `+${l}`), ...removed.map(l => `−${l}`)].join(' ')}`);
  const keys = [...new Set([...Object.keys(before.fields ?? {}), ...Object.keys(after.fields ?? {})])]
    .filter(key => JSON.stringify(before.fields?.[key]) !== JSON.stringify(after.fields?.[key]));
  if (keys.length) lines.push(`Fields: ${keys.join(', ')}`);
  return lines.length ? lines : ['Changed'];
};


// -- Where a collection's version saved from code came from: {script, argv, host, cwd, git: {repository, commit, dirty}}

// "Saved by a script: tools/triage.py at a1b2c3d on host"
export const describe_source = source => {
  if (!source || typeof source !== 'object') return null;
  const script = source.script ? String(source.script) : null;
  const commit = source.git?.commit ? `${String(source.git.commit).slice(0, 7)}${source.git.dirty ? ' (with local changes)' : ''}` : null;
  return [
    `Saved by a script${script ? `: ${script}` : ''}`,
    commit && `at ${commit}`,
    source.host && `on ${source.host}`,
  ].filter(Boolean).join(' ');
};
// On hover: the command line, the folder, the repository
export const source_details = source => {
  if (!source || typeof source !== 'object') return [];
  const argv = Array.isArray(source.argv) ? source.argv.map(String).map(a => /\s/.test(a) ? JSON.stringify(a) : a).join(' ') : source.argv ? String(source.argv) : null;
  return [
    argv && ['Command', argv],
    source.cwd && ['Folder', String(source.cwd)],
    source.git?.repository && ['Repository', String(source.git.repository)],
    source.git?.commit && ['Commit', `${source.git.commit}${source.git.dirty ? ', with local changes' : ''}`],
    source.host && ['Host', String(source.host)],
  ].filter(Boolean);
};
