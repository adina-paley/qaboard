/**
 * Annotations (docs/design/annotations.md): the API calls, the helpers, the Inputs page's filters and the run viewers'
 * "QA-Board annotations" regions. No React here: the panel's tests are in annotations.test.jsx.
 * Run with: cd webapp && npm test -- annotations
 */
import { describe, test, expect, vi, afterEach } from 'vitest';

import {
  annotationGroupsQuery, inputAnnotationsQuery, annotationHistoryQuery, groupLabelsQuery, createAnnotation, updateAnnotation,
  deleteAnnotation, restoreAnnotation, createAnnotationGroup, stale_annotation, group_name_problem, normalize_group_name,
  default_draw_group, drawable_groups, listed_groups, annotation_region, to_input_pixels, parse_field_value, clean_labels,
  describe_change, describe_source, source_details, method_label, input_size, is_saved_id, temporary_id, is_group_visible,
} from '../annotations';
import { inputs_filters_from_query, inputs_params, describe_filters, filters_key } from '../api';
import { qaboardAnnotationsToRegions, regionProviders, isGroupShown } from '../../viewers/images/regionProviders';


// Answers the API calls (the longest matching prefix; a function answers [status, body]), and records them
const stubApi = (answers = {}) => {
  const calls = [];
  vi.stubGlobal('fetch', vi.fn(async (url, init = {}) => {
    const method = init.method ?? 'GET';
    calls.push({ url, method, body: init.body ? JSON.parse(init.body) : undefined });
    const key = Object.keys(answers).sort((a, b) => b.length - a.length).find(k => `${method} ${url}`.startsWith(k));
    const answer = key ? answers[key] : {};
    const [status, body] = typeof answer === 'function' ? answer(url, init) : [200, answer];
    return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
  }));
  return calls;
};
afterEach(() => vi.unstubAllGlobals());

const group = (id, fields = {}) => ({ id, project: 'p', name: `g${id}`, description: '', team: null, personal: false, can_edit: true, can_manage: false, archived: false, inherited: false, count: 0, ...fields });
const annotation = (id, fields = {}) => ({ id, group: 1, input: 5, x: 10, y: 20, w: 30, h: 40, image_width: 400, image_height: 300, labels: [], fields: {}, method: 'manual', version: 1, can_edit: true, ...fields });


describe('the API', () => {
  test('queries: groups, an input\'s annotations, history, labels', async () => {
    const calls = stubApi({
      'GET /api/v1/annotation-groups': { groups: [group(1)], can_create: true, my_teams: [{ id: 3, name: 'isp' }] },
      'GET /api/v1/inputs/5/annotations': { annotations: [annotation(9)], groups: [group(1)] },
      'GET /api/v1/annotations/9/history': { changes: [{ version: 1, action: 'create' }] },
      'GET /api/v1/annotation-groups/1/labels': { labels: [{ label: 'night', count: 3 }] },
    });
    const groups = annotationGroupsQuery('a/b');
    expect(groups.queryKey).toEqual(['annotation-groups', 'a/b', { archived: false }]);
    expect((await groups.queryFn({})).my_teams).toEqual([{ id: 3, name: 'isp' }]);
    expect(calls[0].url).toBe('/api/v1/annotation-groups?project=a%2Fb&archived=0');
    const of_input = inputAnnotationsQuery(5, 'a/b');
    expect(await of_input.queryFn({})).toEqual({ annotations: [annotation(9)], groups: [group(1)] });
    expect(calls[1].url).toBe('/api/v1/inputs/5/annotations?project=a%2Fb');
    expect(inputAnnotationsQuery(null, 'p').enabled).toBe(false);
    expect(await annotationHistoryQuery(9, 1).queryFn({})).toEqual([{ version: 1, action: 'create' }]);
    // annotations being saved have no history yet
    expect(annotationHistoryQuery('new-1').enabled).toBe(false);
    expect(await groupLabelsQuery(1, ' ni ').queryFn({})).toEqual([{ label: 'night', count: 3 }]);
    expect(calls[3].url).toBe('/api/v1/annotation-groups/1/labels?q=ni');
  });

  test('changes send the version they saw; deletes say it in the query string', async () => {
    const calls = stubApi({ 'POST /api/v1/inputs/5/annotations': () => [201, annotation(9)] });
    expect(await createAnnotation(5, { group: 1, x: 1, y: 2, w: 3, h: 4, image_width: 400, image_height: 300 })).toEqual(annotation(9));
    expect(calls[0]).toMatchObject({ method: 'POST', body: { group: 1, x: 1, y: 2, w: 3, h: 4, labels: [], fields: {}, method: 'manual', image_width: 400, image_height: 300 } });
    await updateAnnotation(9, 3, { x: 5, labels: ['a'] });
    expect(calls[1]).toMatchObject({ method: 'PATCH', url: '/api/v1/annotations/9', body: { x: 5, labels: ['a'], version: 3 } });
    await deleteAnnotation(9, 4);
    expect(calls[2]).toMatchObject({ method: 'DELETE', url: '/api/v1/annotations/9?version=4' });
    await restoreAnnotation(9);
    expect(calls[3]).toMatchObject({ method: 'POST', url: '/api/v1/annotations/9/restore' });
    await createAnnotationGroup({ project: 'p', name: ' af-team / train ', team: 3 });
    expect(calls[4].body).toEqual({ project: 'p', name: 'af-team/train', team: 3 });
  });

  test('409 stale_version: the current annotation', () => {
    const error = (status, data) => ({ response: { status, data } });
    expect(stale_annotation(error(409, { reason: 'stale_version', version: 4, annotation: annotation(9, { version: 4 }) }))).toMatchObject({ id: 9, version: 4 });
    expect(stale_annotation(error(409, { reason: 'stale_version' }))).toBe(null);
    // other conflicts aren't about versions
    expect(stale_annotation(error(409, { reason: 'archived_group' }))).toBe(undefined);
    expect(stale_annotation(error(500, {}))).toBe(undefined);
  });
});


describe('helpers', () => {
  test('group names: like paths, personal ones in the user\'s namespace', () => {
    expect(normalize_group_name(' af-team /  train  patches/ ')).toBe('af-team/train patches');
    expect(group_name_problem('af-team/train', 'alice')).toBe(null);
    expect(group_name_problem('~alice/sandbox', 'alice')).toBe(null);
    expect(group_name_problem('~bob/sandbox', 'alice')).toMatch('~alice/');
    expect(group_name_problem('~alice', 'alice')).toMatch('~alice/sandbox');
    expect(group_name_problem('a:b', 'alice')).toMatch('“:”');
    expect(group_name_problem(' / ', 'alice')).toBe('Give the group a name.');
  });

  test('the group to draw into: the one picked last, else personal, else the first users edit', () => {
    const groups = [group(1, { can_edit: false }), group(2), group(3, { personal: true }), group(4, { archived: true })];
    expect(drawable_groups(groups).map(g => g.id)).toEqual([2, 3]);
    expect(default_draw_group(groups, 2).id).toBe(2);
    expect(default_draw_group(groups, 1).id).toBe(3);
    expect(default_draw_group(groups, null).id).toBe(3);
    expect(default_draw_group([group(1, { can_edit: false })], null)).toBe(null);
  });

  test('the groups listed: with annotations of the input first, then those users draw into', () => {
    const groups = [group(1, { name: 'b' }), group(2, { name: 'a' }), group(3, { name: 'c', can_edit: false })];
    expect(listed_groups(groups, [annotation(1, { group: 1 })]).map(g => [g.id, g.shown_count])).toEqual([[1, 1], [2, 0]]);
    expect(is_group_visible(group(1), undefined)).toBe(true);
    expect(is_group_visible(group(1), { 1: false })).toBe(false);
  });

  test('annotations as editable regions, in the input\'s pixels', () => {
    expect(annotation_region(annotation(9, { labels: ['a', 'b'] }), { color: 'red' })).toEqual({
      id: 9, x: 10, y: 20, w: 30, h: 40, imageWidth: 400, imageHeight: 300, label: 'a, b', color: 'red', readOnly: false,
    });
    // without its image's size: the input's
    expect(annotation_region(annotation(9, { image_width: null, image_height: null, can_edit: false }), { fallback_size: { width: 800, height: 600 } }))
      .toMatchObject({ imageWidth: 800, imageHeight: 600, readOnly: true });
    expect(input_size({ metadata: { width: 800, height: '600' } })).toEqual({ width: 800, height: 600 });
    expect(input_size({})).toBe(null);
  });

  test('rectangles drawn on a preview are saved in the input\'s pixels, when its size is known', () => {
    const drawn = { x: 10, y: 20, w: 30, h: 40, imageWidth: 1024, imageHeight: 768 };
    expect(to_input_pixels(drawn, { width: 4096, height: 3072 })).toEqual({ x: 40, y: 80, w: 120, h: 160, image_width: 4096, image_height: 3072 });
    expect(to_input_pixels(drawn, null)).toEqual({ x: 10, y: 20, w: 30, h: 40, image_width: 1024, image_height: 768 });
    expect(to_input_pixels({ ...drawn, imageWidth: 4096, imageHeight: 3072 }, { width: 4096, height: 3072 })).toMatchObject({ x: 10, image_width: 4096 });
  });

  test('what users type in fields and labels', () => {
    expect(parse_field_value(' 12 ')).toBe(12);
    expect(parse_field_value('-1.5')).toBe(-1.5);
    expect(parse_field_value('true')).toBe(true);
    expect(parse_field_value('high')).toBe('high');
    expect(parse_field_value('00012345678901234567')).toBe('00012345678901234567');
    expect(clean_labels([' night', 'night', '', 'street '])).toEqual(['night', 'street']);
  });

  test('ids of annotations being saved', () => {
    const id = temporary_id();
    expect(is_saved_id(id)).toBe(false);
    expect(is_saved_id(12)).toBe(true);
    expect(is_saved_id('12')).toBe(true);
  });

  test('history and methods in words', () => {
    expect(method_label('manual')).toBe('Drawn by hand');
    expect(method_label('model:yolo-v8')).toBe('Model yolo-v8');
    const before = { x: 1, y: 2, w: 3, h: 4, labels: ['day'], fields: { a: 1 } };
    expect(describe_change({ action: 'create', after: before })).toEqual(['Created 3×4 at (1, 2), labels day']);
    expect(describe_change({ action: 'update', before, after: { ...before, x: 5 } })).toEqual(['Moved to (5, 2)']);
    expect(describe_change({ action: 'update', before, after: { ...before, w: 9, labels: ['night'], fields: { a: 2 } } }))
      .toEqual(['Resized to 9×4 at (1, 2)', 'Labels: +night −day', 'Fields: a']);
    expect(describe_change({ action: 'delete' })).toEqual(['Deleted']);
  });

  test("where a collection's version saved from code came from", () => {
    const source = { script: 'tools/triage.py', argv: ['tools/triage.py', '--night', 'a b'], host: 'gpu-3', cwd: '/home/a/repo', git: { repository: 'git@host:isp.git', commit: 'a1b2c3d4e5f6', dirty: false } };
    expect(describe_source(source)).toBe('Saved by a script: tools/triage.py at a1b2c3d on gpu-3');
    expect(describe_source({ ...source, git: { commit: 'a1b2c3d4e5f6', dirty: true }, host: null })).toBe('Saved by a script: tools/triage.py at a1b2c3d (with local changes)');
    expect(describe_source(null)).toBe(null);
    expect(source_details(source)).toEqual([
      ['Command', 'tools/triage.py --night "a b"'],
      ['Folder', '/home/a/repo'],
      ['Repository', 'git@host:isp.git'],
      ['Commit', 'a1b2c3d4e5f6'],
      ['Host', 'gpu-3'],
    ]);
  });
});


describe("the Inputs page's filters", () => {
  test('by group, label and "with annotations", in the URL', () => {
    const filters = inputs_filters_from_query({ annotation_group: '3', label: 'night', annotated: '1' });
    expect(filters).toMatchObject({ annotation_group: '3', label: 'night', annotated: true });
    expect(inputs_params('p', filters)).toMatchObject({ annotation_group: '3', label: 'night', annotated: 1 });
    // not a group id: ignored
    expect(inputs_filters_from_query({ annotation_group: 'x' }).annotation_group).toBe('');
    const none = inputs_params('p', inputs_filters_from_query({}));
    expect([none.annotation_group, none.label, none.annotated]).toEqual([undefined, undefined, undefined]);
    expect(filters_key(filters)).not.toBe(filters_key(inputs_filters_from_query({})));
    expect(describe_filters({ ...filters, annotation_group_name: 'isp/defects', live: false })).toEqual(['annotation group isp/defects', 'label "night"']);
    expect(describe_filters({ annotated: true, live: false })).toEqual(['with annotations']);
  });
});


describe('run viewers: QA-Board annotations', () => {
  const provider = () => regionProviders().find(p => p.id === 'annotations-qaboard');

  test('a set of the run\'s input, in the Annotations section, when the run says its input', async () => {
    const p = provider();
    expect(p).toMatchObject({ section: 'Annotations', auto: true, overview: true, source: 'qaboard', label: 'QA-Board annotations' });
    expect(p.available({ output_new: { id: 1, test_input_id: 5 } })).toBe(true);
    expect(p.available({ output_new: { id: 1 } })).toBe(false);
    expect(p.available({ output_new: { id: 1, test_input_id: 5, deleted: true } })).toBe(false);
    expect(p.key({ output_new: { id: 1, test_input_id: 5 }, path: 'out.png', project: 'p/sub' })).toEqual([5, 'p/sub']);

    const calls = stubApi({ 'GET /api/v1/inputs/5/annotations': { annotations: [annotation(9, { labels: ['night'] })], groups: [group(1, { name: 'isp/defects' }), group(2)] } });
    const found = await p.find({ output_new: { id: 1, test_input_id: 5, test_input_metadata: {} }, project: 'p/sub' }, {});
    expect(calls[0].url).toBe('/api/v1/inputs/5/annotations?project=p%2Fsub');
    // only the groups with annotations of the input, shown until users hide them
    expect(found.groups).toHaveLength(1);
    expect(found.groups[0]).toMatchObject({ id: 1, label: 'isp/defects', count: 1, shown: true, url: '/p/sub/inputs?input=5' });
    expect(isGroupShown(found.groups[0], undefined)).toBe(true);
    expect(isGroupShown(found.groups[0], { 1: false })).toBe(false);
    expect(found.regions[0]).toMatchObject({ id: 9, group: 1, x: 10, imageWidth: 400, label: 'night', detail: 'isp/defects', href: '/p/sub/inputs?input=5', hrefLabel: 'Edit in Inputs' });

    // a server without annotations: none
    stubApi({ 'GET /api/v1/inputs/5/annotations': () => [404, { error: 'Not found' }] });
    expect(await p.find({ output_new: { id: 1, test_input_id: 5 }, project: 'p' }, {})).toEqual({ groups: [], regions: [] });
  });

  test('regions are scaled from the input\'s size when the annotation doesn\'t say it', () => {
    const found = qaboardAnnotationsToRegions({ annotations: [annotation(9, { image_width: null, image_height: null })], groups: [group(1)] }, { metadata: { width: 800, height: 600 } });
    expect(found.regions[0]).toMatchObject({ imageWidth: 800, imageHeight: 600, label: 'g1' });
    expect(found.regions[0].href).toBe(undefined);
  });
});
