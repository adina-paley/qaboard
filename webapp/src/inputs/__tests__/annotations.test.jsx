/**
 * The Inputs viewer's annotations (docs/design/annotations.md): the panel, and what the image viewer calls
 * (useAnnotationEditor's `viewer` props: onRegionCreate, onRegionChange, onRegionDelete). Optimistic changes, 409s,
 * Undo, typed edits saved on Enter, blur or when the panel goes away. Also the Inputs page's filters, and where a
 * collection's version saved from code came from. The API layer's tests: annotationsApi.test.js.
 * Run with: cd webapp && npm test -- annotations
 */
import { screen, fireEvent, waitFor, within, act } from '@testing-library/react';
import { unstable_HistoryRouter as HistoryRouter } from 'react-router';

import { RouteMatch, history } from '../../router';
import { route_paths } from '../../route_paths';
import { renderWithProviders } from '../../test-utils';
import { toaster } from '../../toaster';
import { useAnnotationEditor } from '../useAnnotationEditor';
import { AnnotationsPanel } from '../AnnotationsPanel';
import { SavedFrom } from '../CollectionPage';
import InputsPage from '../InputsPage';


const alice = { is_logged: true, user_id: 1, user_name: 'alice', full_name: 'Alice A' };
const the_input = { id: 5, database: '/data', path: 'night/a.png', name: 'a.png', width: 1000, height: 500, runs: 1 };
const group = (id, fields = {}) => ({ id, project: 'p', name: `g${id}`, description: '', team: null, personal: false, can_edit: true, can_manage: false, archived: false, inherited: false, count: 0, ...fields });
const annotation = (id, fields = {}) => ({
  id, group: 1, input: 5, x: 10, y: 20, w: 30, h: 40, image_width: 400, image_height: 300, labels: ['day'], fields: {}, method: 'manual',
  version: 1, can_edit: true, created_by: { id: 2, user_name: 'bob', full_name: 'Bob B' }, created_at: '2026-11-01T10:00:00Z', ...fields,
});
const groups = [group(1, { name: 'isp/defects' }), group(2, { name: '~alice/sandbox', personal: true }), group(4, { name: 'theirs', can_edit: false })];

// Answers the API calls (the longest matching prefix; a function answers [status, body]), and records them
const stubApi = (answers = {}) => {
  const calls = [];
  vi.stubGlobal('fetch', vi.fn(async (url, init = {}) => {
    const method = init.method ?? 'GET';
    const body = init.body ? JSON.parse(init.body) : undefined;
    calls.push({ url, method, body });
    const key = Object.keys(answers).sort((a, b) => b.length - a.length).find(k => `${method} ${url}`.startsWith(k));
    const answer = key ? answers[key] : {};
    const [status, data] = typeof answer === 'function' ? answer(url, body) : [200, answer];
    return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
  }));
  return calls;
};

// The server: a PATCH saves the next version, a POST makes annotation 11
const server = (overrides = {}) => ({
  'GET /api/v1/inputs/5/annotations': { annotations: [annotation(9), annotation(10, { group: 4, labels: ['theirs'], can_edit: false })], groups },
  'GET /api/v1/annotation-groups': { groups, can_create: true, can_share: true, my_teams: [{ id: 3, name: 'isp' }] },
  'POST /api/v1/inputs/5/annotations': (url, body) => [201, annotation(11, { ...body, labels: body.labels ?? [] })],
  'PATCH /api/v1/annotations/9': (url, body) => [200, annotation(9, { ...body, version: body.version + 1 })],
  'DELETE /api/v1/annotations/9': () => [200, annotation(9, { deleted_at: '2026-11-26T10:00:00Z', version: 2 })],
  'POST /api/v1/annotations/9/restore': () => [200, annotation(9, { version: 3 })],
  'POST /api/v1/annotation-groups': (url, body) => [201, group(7, { ...body, personal: body.name.startsWith('~') })],
  'GET /api/v1/annotation-groups/1/labels': { labels: [{ label: 'night', count: 4 }] },
  ...overrides,
});

// The panel, and what the image viewer would call
let editor;
const Harness = ({ input }) => {
  editor = useAnnotationEditor({ project: 'p', input });
  return <AnnotationsPanel editor={editor}/>;
};
const renderPanel = (answers = server(), input = the_input) => {
  const calls = stubApi(answers);
  const view = renderWithProviders(<Harness input={input}/>, { user: alice, siteConfig: {} });
  return { calls, ...view };
};
const sent = (calls, method) => calls.filter(c => c.method === method);
const last_toast = () => vi.mocked(toaster.show).mock.calls.at(-1)?.[0];

afterEach(() => {
  vi.unstubAllGlobals();
  vi.mocked(toaster.show).mockClear();
  window.localStorage.clear();
  editor = undefined;
});


describe('the Annotations panel', () => {
  test('lists the annotations by group; selecting one shows its rectangle, labels and how it was made', async () => {
    renderPanel();
    const list = await screen.findByRole('list', { name: 'QA-Board annotation groups' });
    expect(within(list).getByText('isp/defects')).toBeInTheDocument();
    // read-only groups say so
    expect(within(list).getByText('theirs')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /^day/ }));
    const details = screen.getByRole('region', { name: 'Selected annotation' });
    expect(within(details).getByRole('textbox', { name: 'x (pixels)' })).toHaveValue('10');
    expect(within(details).getByRole('textbox', { name: 'h (pixels)' })).toHaveValue('40');
    expect(within(details).getByText('Drawn by hand')).toBeInTheDocument();
    expect(within(details).getByText(/Bob B/)).toBeInTheDocument();
    // the viewer gets them as editable regions, the read-only group's without handles
    expect(editor.viewer.editableRegions.map(r => [r.id, r.readOnly])).toEqual([[9, false], [10, true]]);
    expect(editor.viewer.selectedRegion).toBe(9);
  });

  test('hiding a group hides its annotations in the viewer', async () => {
    renderPanel();
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Show theirs' }));
    expect(editor.viewer.editableRegions.map(r => r.id)).toEqual([9]);
  });

  test('typed coordinates are saved on Enter, with the version they change', async () => {
    const { calls } = renderPanel();
    fireEvent.click(await screen.findByRole('button', { name: /^day/ }));
    const x = screen.getByRole('textbox', { name: 'x (pixels)' });
    fireEvent.change(x, { target: { value: '15' } });
    fireEvent.keyDown(x, { key: 'Enter' });
    await waitFor(() => expect(sent(calls, 'PATCH')).toHaveLength(1));
    expect(sent(calls, 'PATCH')[0]).toMatchObject({ url: '/api/v1/annotations/9', body: { x: 15, version: 1 } });
    // the version it saved: the next change starts from it
    fireEvent.change(x, { target: { value: '16' } });
    fireEvent.blur(x);
    await waitFor(() => expect(sent(calls, 'PATCH')).toHaveLength(2));
    expect(sent(calls, 'PATCH')[1].body).toEqual({ x: 16, version: 2 });
  });

  test('kept inside the image, and nothing silly is saved', async () => {
    const { calls } = renderPanel();
    fireEvent.click(await screen.findByRole('button', { name: /^day/ }));
    const w = screen.getByRole('textbox', { name: 'w (pixels)' });
    fireEvent.change(w, { target: { value: 'wide' } });
    fireEvent.keyDown(w, { key: 'Enter' });
    expect(w).toHaveAttribute('aria-invalid', 'true');
    // Esc puts back what was there
    fireEvent.keyDown(w, { key: 'Escape' });
    expect(w).toHaveValue('30');
    fireEvent.change(w, { target: { value: '5000' } });
    fireEvent.keyDown(w, { key: 'Enter' });
    await waitFor(() => expect(sent(calls, 'PATCH')).toHaveLength(1));
    // the image is 400 wide
    expect(sent(calls, 'PATCH')[0].body).toEqual({ x: 0, w: 400, version: 1 });
    expect(screen.getByRole('status')).toHaveTextContent('Kept inside the image');
  });

  test('a typed edit is saved when the annotation is unselected, not lost', async () => {
    const { calls } = renderPanel();
    fireEvent.click(await screen.findByRole('button', { name: /^day/ }));
    fireEvent.change(screen.getByRole('textbox', { name: 'y (pixels)' }), { target: { value: '21' } });
    act(() => editor.setSelected(null));
    await waitFor(() => expect(sent(calls, 'PATCH')).toHaveLength(1));
    expect(sent(calls, 'PATCH')[0].body).toEqual({ y: 21, version: 1 });
  });

  test('labels: added with Enter, autocompleted from the group', async () => {
    const { calls } = renderPanel();
    fireEvent.click(await screen.findByRole('button', { name: /^day/ }));
    const labels = screen.getByRole('combobox', { name: 'Labels' });
    fireEvent.change(labels, { target: { value: 'night' } });
    fireEvent.keyDown(labels, { key: 'Enter' });
    await waitFor(() => expect(sent(calls, 'PATCH')).toHaveLength(1));
    expect(sent(calls, 'PATCH')[0].body).toEqual({ labels: ['day', 'night'], version: 1 });
    await waitFor(() => expect(calls.some(c => c.url.startsWith('/api/v1/annotation-groups/1/labels'))).toBe(true));
  });

  test('moved in the viewer: shown at once, saved one after the other', async () => {
    const { calls } = renderPanel();
    await screen.findByRole('button', { name: /^day/ });
    act(() => {
      editor.viewer.onRegionChange(9, { x: 1, y: 2, w: 30, h: 40 }, { reason: 'move' });
      editor.viewer.onRegionChange(9, { x: 3, y: 2, w: 30, h: 40 }, { reason: 'nudge' });
    });
    expect(editor.viewer.editableRegions[0]).toMatchObject({ x: 3, y: 2 });
    await waitFor(() => expect(sent(calls, 'PATCH')).toHaveLength(2));
    expect(sent(calls, 'PATCH').map(c => c.body)).toEqual([{ x: 1, y: 2, w: 30, h: 40, version: 1 }, { x: 3, y: 2, w: 30, h: 40, version: 2 }]);
    // read-only: nothing is sent
    act(() => editor.viewer.onRegionChange(10, { x: 1, y: 1, w: 1, h: 1 }));
    expect(sent(calls, 'PATCH')).toHaveLength(2);
  });

  test("409: someone changed it meanwhile. Theirs shows, and users are told what wasn't saved", async () => {
    const { calls } = renderPanel(server({
      'PATCH /api/v1/annotations/9': () => [409, { reason: 'stale_version', version: 3, error: 'Someone changed this annotation meanwhile', annotation: annotation(9, { x: 99, version: 3, updated_by: { full_name: 'Bob B' } }) }],
    }));
    fireEvent.click(await screen.findByRole('button', { name: /^day/ }));
    act(() => editor.viewer.onRegionChange(9, { x: 1, y: 20, w: 30, h: 40 }));
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'x (pixels)' })).toHaveValue('99'));
    expect(sent(calls, 'PATCH')).toHaveLength(1);
    expect(last_toast()).toMatchObject({ intent: 'warning', message: expect.stringMatching(/Bob B changed this annotation meanwhile.*the new position wasn't saved/) });
  });

  test('deleting hides it at once; Undo restores it', async () => {
    const { calls } = renderPanel();
    fireEvent.click(await screen.findByRole('button', { name: /^day/ }));
    fireEvent.click(screen.getByRole('button', { name: /Delete/ }));
    expect(screen.queryByRole('button', { name: /^day/ })).toBeNull();
    await waitFor(() => expect(sent(calls, 'DELETE')).toHaveLength(1));
    expect(sent(calls, 'DELETE')[0].url).toBe('/api/v1/annotations/9?version=1');
    await waitFor(() => expect(last_toast()?.action?.text).toBe('Undo'));
    await act(async () => { await last_toast().action.onClick(); });
    expect(sent(calls, 'POST').at(-1).url).toBe('/api/v1/annotations/9/restore');
    expect(await screen.findByRole('button', { name: /^day/ })).toBeInTheDocument();
  });

  test('the Delete key in the viewer does the same; read-only annotations stay', async () => {
    const { calls } = renderPanel();
    await screen.findByRole('button', { name: /^day/ });
    act(() => { editor.viewer.onRegionDelete(10); });
    expect(sent(calls, 'DELETE')).toHaveLength(0);
    act(() => { editor.viewer.onRegionDelete(9); });
    await waitFor(() => expect(sent(calls, 'DELETE')).toHaveLength(1));
  });

  test('read-only annotations: no handles, no Delete, fields disabled', async () => {
    renderPanel();
    fireEvent.click(await screen.findByRole('button', { name: /^theirs/ }));
    const details = screen.getByRole('region', { name: 'Selected annotation' });
    expect(within(details).getByText('read-only')).toBeInTheDocument();
    expect(within(details).getByRole('textbox', { name: 'x (pixels)' })).toBeDisabled();
    expect(within(details).queryByRole('button', { name: /Delete/ })).toBeNull();
  });

  test('drawn rectangles go to the group picked, in the input\'s pixels, and stay selected once saved', async () => {
    const { calls } = renderPanel();
    await screen.findByRole('button', { name: /^day/ });
    // personal groups first, until users pick another
    expect(screen.getByRole('combobox', { name: 'Draw into' })).toHaveValue('2');
    fireEvent.change(screen.getByRole('combobox', { name: 'Draw into' }), { target: { value: '1' } });
    // drawn on a preview half the size of the input
    act(() => { editor.viewer.onRegionCreate({ x: 10, y: 10, w: 20, h: 20, imageWidth: 500, imageHeight: 250 }); });
    // shown at once, selected
    expect(screen.getByRole('region', { name: 'Selected annotation' })).toHaveTextContent('saving…');
    await waitFor(() => expect(sent(calls, 'POST')).toHaveLength(1));
    expect(sent(calls, 'POST')[0]).toMatchObject({ url: '/api/v1/inputs/5/annotations', body: { group: 1, x: 20, y: 20, w: 40, h: 40, image_width: 1000, image_height: 500, method: 'manual' } });
    await waitFor(() => expect(editor.selected).toBe(11));
    // remembered for the project
    expect(window.localStorage.getItem('qaboard.annotations.draw-group.p')).toBe('1');
  });

  test('"Add rectangle": without a pointer, a box in the middle of the input to adjust', async () => {
    const { calls } = renderPanel();
    fireEvent.click(await screen.findByRole('button', { name: 'Add rectangle' }));
    await waitFor(() => expect(sent(calls, 'POST')).toHaveLength(1));
    expect(sent(calls, 'POST')[0].body).toMatchObject({ group: 2, x: 375, y: 188, w: 250, h: 125, image_width: 1000, image_height: 500 });
    // and to edit by its fields
    expect(await screen.findByRole('textbox', { name: 'x (pixels)' })).toHaveValue('375');
  });

  test('a change made while the rectangle is being saved follows it', async () => {
    const { calls } = renderPanel(server({ 'PATCH /api/v1/annotations/11': (url, body) => [200, annotation(11, { ...body, version: body.version + 1 })] }));
    await screen.findByRole('button', { name: /^day/ });
    let id;
    act(() => { id = editor.viewer.onRegionCreate({ x: 0, y: 0, w: 10, h: 10, imageWidth: 1000, imageHeight: 500 }); });
    act(() => { editor.viewer.onRegionChange(id, { x: 5, y: 0, w: 10, h: 10 }); });
    await waitFor(() => expect(sent(calls, 'PATCH')).toHaveLength(1));
    expect(sent(calls, 'PATCH')[0]).toMatchObject({ url: '/api/v1/annotations/11', body: { x: 5, version: 1 } });
    await waitFor(() => expect(editor.viewer.editableRegions.find(r => r.id === 11)).toMatchObject({ x: 5 }));
  });

  test('without a group to draw into, Draw opens the new group dialog: a personal one', async () => {
    const read_only = [group(4, { name: 'theirs', can_edit: false })];
    const { calls } = renderPanel(server({
      'GET /api/v1/inputs/5/annotations': { annotations: [], groups: read_only },
      'GET /api/v1/annotation-groups': { groups: read_only, can_create: true, can_share: false, my_teams: [] },
    }));
    fireEvent.click(await screen.findByRole('button', { name: /Draw/ }));
    const dialog = await screen.findByRole('dialog', { name: /New annotation group/ });
    // developers make the project's groups: personal ones only
    expect(within(dialog).getByRole('radio', { name: /Developers of p/ })).toBeDisabled();
    expect(within(dialog).getByRole('radio', { name: /Only me/ })).toBeChecked();
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Name' }), { target: { value: 'sandbox' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create group' }));
    await waitFor(() => expect(sent(calls, 'POST')).toHaveLength(1));
    expect(sent(calls, 'POST')[0]).toMatchObject({ url: '/api/v1/annotation-groups', body: { project: 'p', name: '~alice/sandbox', team: null } });
  });

  test('a team\'s group; a taken name is said in the dialog', async () => {
    const { calls } = renderPanel(server({ 'POST /api/v1/annotation-groups': () => [409, { error: 'taken', reason: 'name_taken' }] }));
    await screen.findByRole('button', { name: /^day/ });
    fireEvent.change(screen.getByRole('combobox', { name: 'Draw into' }), { target: { value: '__new__' } });
    const dialog = await screen.findByRole('dialog', { name: /New annotation group/ });
    fireEvent.click(within(dialog).getByRole('radio', { name: /A team/ }));
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Name' }), { target: { value: 'isp/defects' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create group' }));
    expect(await within(dialog).findByText(/already has a group named isp\/defects/)).toBeInTheDocument();
    expect(sent(calls, 'POST')[0].body).toEqual({ project: 'p', name: 'isp/defects', team: 3 });
  });

  test('external sources show after QA-Board\'s groups, read-only', async () => {
    stubApi({
      ...server(),
      'GET /api/v1/output/image/annotations/iqe': { groups: [{ id: 'w1', label: 'Workspace 1', mine: true }], annotations: [{ id: 'a', group: 'w1', x: 1, y: 2, w: 3, h: 4, label: 'Noise' }] },
    });
    renderWithProviders(<Harness input={the_input}/>, { user: alice, siteConfig: { annotation_sources: [{ id: 'iqe', label: 'IQE' }] } });
    const source = await screen.findByRole('region', { name: 'Annotations in IQE' });
    expect(await within(source).findByText('Workspace 1')).toBeInTheDocument();
    await waitFor(() => expect(editor.viewer.editableRegions.find(r => r.id === 'ext:iqe:a')).toMatchObject({ readOnly: true, label: 'Noise' }));
    fireEvent.click(within(source).getByRole('button', { name: /^Noise/ }));
    expect(screen.getByRole('region', { name: 'Selected annotation' })).toHaveTextContent('IQE');
  });

  test('Esc is the viewer\'s while drawing or with an annotation selected', async () => {
    renderPanel();
    await screen.findByRole('button', { name: /^day/ });
    expect(editor.captures_escape).toBe(false);
    act(() => editor.setDrawing(true));
    expect(editor.captures_escape).toBe(true);
  });
});


describe('the Inputs page', () => {
  const inputs = [
    { id: 1, database: '/data', path: 'night/IMG_1.raw', name: 'IMG_1.raw', folder: 'night', runs: 1, annotations: 2 },
    { id: 2, database: '/data', path: 'night/IMG_2.raw', name: 'IMG_2.raw', folder: 'night', runs: 1, annotations: 0 },
  ];
  const renderInputs = url => {
    const calls = stubApi({
      'GET /api/v1/inputs': { inputs, total: 2, databases: [], folders: [], annotation_groups: [{ id: 3, name: 'isp/defects', count: 1 }], labels: [{ label: 'night', count: 1 }] },
      'GET /api/v1/collections': { collections: [] },
    });
    history.replace(url);
    renderWithProviders(
      <HistoryRouter history={history}><RouteMatch match={{ path: route_paths.inputs, url: url.split('?')[0], params: { project_id: 'p' } }}><InputsPage/></RouteMatch></HistoryRouter>,
      { user: alice },
    );
    return calls;
  };
  const query_of = () => Object.fromEntries(new URLSearchParams(window.location.search));

  test('filters by annotation group, label, "with annotations", with counts, in the URL', async () => {
    const calls = renderInputs('/p/inputs');
    const facets = await screen.findByRole('region', { name: 'Annotations' });
    expect(within(facets).getByRole('button', { name: /isp\/defects/ })).toHaveTextContent('1');
    fireEvent.click(within(facets).getByRole('button', { name: /isp\/defects/ }));
    expect(query_of()).toMatchObject({ annotation_group: '3' });
    fireEvent.click(within(facets).getByRole('button', { name: /night/ }));
    fireEvent.click(within(facets).getByRole('button', { name: /With annotations/ }));
    expect(query_of()).toMatchObject({ annotation_group: '3', label: 'night', annotated: '1' });
    await waitFor(() => expect(calls.some(c => c.url.includes('annotation_group=3') && c.url.includes('label=night') && c.url.includes('annotated=1'))).toBe(true));
    // the tags above the grid remove them
    fireEvent.click(within(screen.getByText('with annotations').closest('.bp6-tag')).getByRole('button'));
    expect(query_of().annotated).toBeUndefined();
  });

  test('a count of annotations on the thumbnails', async () => {
    renderInputs('/p/inputs');
    const annotated = await screen.findByRole('gridcell', { name: 'IMG_1.raw, 2 annotations' });
    expect(within(annotated).getByText('2')).toBeInTheDocument();
    expect(screen.getByRole('gridcell', { name: 'IMG_2.raw' })).toBeInTheDocument();
  });
});


describe("a collection's version saved from code", () => {
  test('says where it came from, the details on hover or focus', () => {
    renderWithProviders(<SavedFrom source={{ script: 'tools/triage.py', argv: ['tools/triage.py', '--night'], host: 'gpu-3', cwd: '/repo', git: { commit: 'a1b2c3d4e5', dirty: false } }}/>);
    const note = screen.getByText(/Saved by a script: tools\/triage.py at a1b2c3d on gpu-3/);
    expect(note.closest('[tabindex="0"]')).toHaveAttribute('aria-label', expect.stringContaining('Command: tools/triage.py --night'));
  });
});
