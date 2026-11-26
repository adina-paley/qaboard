// The Inputs page, the Collections page and a collection's page, against a mocked API: they load, select, open the viewer, without errors.
import zlib from 'node:zlib';
import { test, expect } from '@playwright/test';

const project = 'group/repo';
const commit = {
  id: 'abc123', hexsha: 'abc123', project_id: project, branch: 'master', committer_name: 'Alice', message: 'Hello world',
  authored_datetime: '2026-09-01T10:00:00Z', committed_datetime: '2026-09-01T10:00:00Z', latest_output_datetime: '2026-09-01T10:00:00Z',
  batches: {}, data: {}, parents: [],
};
const inputs = Array.from({ length: 30 }, (_, i) => ({
  id: i + 1, database: '/data', path: `night/IMG_${i + 1}.raw`, name: `IMG_${i + 1}.raw`, folder: 'night', runs: 2,
  first_run_at: '2026-09-01T10:00:00Z', last_run_at: '2026-09-02T10:00:00Z', thumbnail_url: `/api/v1/inputs/${i + 1}/thumbnail`,
}));
const collection = {
  id: 7, project, name: 'night/hard-cases', ref: 'night/hard-cases', description: '', created_by: { id: 1, user_name: 'alice' },
  created_at: '2026-09-01T10:00:00Z', updated_at: '2026-09-01T10:00:00Z', archived: false, latest_version: 1, size: 3,
  favorite: false, pinned_for: [], pinned: false, can_edit: true, inherited: false,
};
const version = { version: 1, created_by: { id: 1, user_name: 'alice' }, created_at: '2026-09-01T10:00:00Z', notes: '', size: 3 };

// Longest matching prefix wins
const api = {
  '/api/v1/config': {},
  '/api/v1/user/me/': { is_authenticated: true, user_id: 1, user_name: 'alice', full_name: 'Alice', email: 'alice@example.com', login_type: 'local' },
  '/api/v1/projects': { [project]: { id: project, data: { qatools_config: {} }, latest_commit_datetime: '2026-09-01T10:00:00Z', total_commits: 1 } },
  '/api/v1/project': { qatools_config: {} },
  '/api/v1/project/branches': ['master'],
  '/api/v1/commits': [commit],
  '/api/v1/commit-counts': { all: 1, mine: 1, without_ci: 0 },
  '/api/v1/batches': [],
  '/api/v1/inputs': { inputs, total: inputs.length, databases: [{ database: '/data', count: 30 }], folders: [{ folder: 'night', name: 'night', count: 30 }] },
  '/api/v1/inputs/2': { ...inputs[1], runs: [] },
  '/api/v1/collections': { collections: [collection] },
  '/api/v1/collections/7': { ...collection, version, versions: [version] },
  '/api/v1/collections/7/inputs': { inputs: inputs.slice(0, 3), total: 3 },
};

test.beforeEach(async ({ page }, testInfo) => {
  testInfo.errors_seen = [];
  page.on('console', msg => msg.type() === 'error' && !/Failed to load resource/.test(msg.text()) && testInfo.errors_seen.push(msg.text()));
  page.on('pageerror', error => testInfo.errors_seen.push(error.message));
  await page.addInitScript(() => localStorage.setItem('qaboard.release-notes.last-seen', '9999-12-31'));
  await page.route('**/api/**', route => {
    const { pathname } = new URL(route.request().url());
    // no thumbnails: the grid shows placeholders
    if (/\/(thumbnail|file)$/.test(pathname)) return route.fulfill({ status: 404, json: { error: 'no_thumbnail' } });
    const key = Object.keys(api).sort((a, b) => b.length - a.length).find(k => pathname === k || pathname.startsWith(`${k}/`) || pathname.startsWith(`${k}?`));
    return route.fulfill({ json: key ? api[key] : {} });
  });
});

// oxlint-disable-next-line no-empty-pattern
test.afterEach(async ({}, testInfo) => {
  expect(testInfo.errors_seen, 'errors in the browser console').toEqual([]);
});

test('inputs: select, then open one', async ({ page }) => {
  await page.goto(`/${project}/inputs`);
  await expect(page.getByRole('heading', { name: 'Inputs' })).toBeVisible();
  await page.getByRole('gridcell', { name: 'IMG_1.raw' }).click();
  await page.getByRole('gridcell', { name: 'IMG_3.raw' }).click({ modifiers: ['Shift'] });
  await expect(page.getByText('3 inputs selected')).toBeVisible();
  await page.getByRole('gridcell', { name: 'IMG_2.raw' }).dblclick();
  await expect(page.getByRole('dialog', { name: /IMG_2.raw/ })).toBeVisible();
  await expect(page).toHaveURL(/input=2/);
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
});

test('a collection', async ({ page }) => {
  await page.goto(`/${project}/collections/7`);
  await expect(page.getByRole('heading', { name: /hard-cases/ })).toBeVisible();
  await expect(page.getByRole('gridcell', { name: 'IMG_3.raw' })).toBeVisible();
  await page.getByRole('tab', { name: /Batch file/ }).click();
  await expect(page.getByLabel('qa/batches.yaml')).toContainText(/collections:\s+- night\/hard-cases@1/);
  await expect(page.getByLabel('qa/batches.yaml')).not.toContainText('- collection:');
  await page.getByRole('tab', { name: /Python/ }).click();
  await expect(page.getByLabel('Python', { exact: true })).toContainText('for item in collection:');
  // back to the Collections: their own page, apart from the Inputs
  await page.getByRole('navigation', { name: 'Breadcrumb' }).getByRole('link', { name: /Collections/ }).click();
  await expect(page).toHaveURL(new RegExp(`/${project}/collections$`));
});

test('the Collections page, from the sidebar', async ({ page }) => {
  await page.goto(`/${project}/inputs`);
  const entry = page.getByRole('menuitem', { name: /^Collections/ });
  await expect(entry).toHaveAttribute('href', `/${project}/collections`);
  await entry.click();
  await expect(page.getByRole('heading', { name: 'Collections', exact: true })).toBeVisible();
  await expect(entry).toHaveClass(/bp6-active/);
  await expect(page.getByRole('menuitem', { name: /^Inputs/ })).not.toHaveClass(/bp6-active/);
  await page.getByRole('searchbox', { name: 'Search collections' }).fill('hard');
  await expect(page.getByRole('link', { name: 'hard-cases' })).toBeVisible();
  await page.getByRole('button', { name: 'New collection' }).click();
  await page.getByRole('region', { name: 'New collection' }).getByRole('link', { name: /Select inputs/ }).click();
  await expect(page).toHaveURL(new RegExp(`/${project}/inputs`));
});

// A PNG of w x h pixels (a gradient), for an input browsers decode: the image viewer opens its file
const crc_table = Array.from({ length: 256 }, (_, n) => {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});
const crc32 = buffer => {
  let c = 0xffffffff;
  for (const byte of buffer) c = crc_table[(c ^ byte) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
};
const png = (w, h) => {
  const chunk = (type, data) => {
    const length = Buffer.alloc(4);
    length.writeUInt32BE(data.length);
    const body = Buffer.concat([Buffer.from(type), data]);
    const crc = Buffer.alloc(4);
    crc.writeUInt32BE(crc32(body));
    return Buffer.concat([length, body, crc]);
  };
  const header = Buffer.alloc(13);
  header.writeUInt32BE(w, 0);
  header.writeUInt32BE(h, 4);
  header.set([8, 2, 0, 0, 0], 8);
  const rows = Buffer.concat(Array.from({ length: h }, (_, y) => Buffer.from([0, ...Array.from({ length: w }, (_, x) => [x % 256, y % 256, 128]).flat()])));
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', header), chunk('IDAT', zlib.deflateSync(rows)), chunk('IEND', Buffer.alloc(0))]);
};

test('inputs: draw an annotation in the viewer', async ({ page }) => {
  const chart = { id: 31, database: '/data', path: 'day/chart.png', name: 'chart.png', folder: 'day', runs: 1, first_run_at: '2026-09-01T10:00:00Z', last_run_at: '2026-09-02T10:00:00Z' };
  const sandbox = { id: 5, project, name: '~alice/sandbox', description: '', team: null, personal: true, can_edit: true, can_manage: true, archived: false, inherited: false, count: 0 };
  const created = [];
  // after beforeEach's: these routes win
  await page.route('**/api/v1/annotation-groups**', route => route.fulfill({ json: { groups: [sandbox], can_create: true, can_share: true, my_teams: [] } }));
  await page.route('**/api/v1/inputs/31**', async route => {
    const request = route.request();
    const { pathname } = new URL(request.url());
    if (pathname.endsWith('/file')) return route.fulfill({ body: png(400, 200), contentType: 'image/png' });
    if (pathname.endsWith('/annotations') && request.method() === 'POST') {
      const body = request.postDataJSON();
      created.push(body);
      return route.fulfill({ status: 201, json: { id: 100, input: 31, ...body, version: 1, can_edit: true, created_by: { id: 1, user_name: 'alice' }, created_at: '2026-11-26T10:00:00Z' } });
    }
    if (pathname.endsWith('/annotations')) return route.fulfill({ json: { annotations: [], groups: [sandbox] } });
    return route.fulfill({ json: { ...chart, runs: [] } });
  });
  await page.goto(`/${project}/inputs?input=31`);
  const viewer = page.getByRole('dialog', { name: /chart.png/ });
  await expect(viewer).toBeVisible();
  const panel = viewer.getByRole('region', { name: 'Annotations' });
  await expect(panel.getByRole('combobox', { name: 'Draw into' })).toHaveValue('5');
  // the image, then the Draw tool (r): drag on it
  const editor = viewer.getByTestId('qiv-region-editor');
  await expect(viewer.getByRole('button', { name: 'Draw', exact: true })).toBeVisible();
  // the image is shown (its zoom is known)
  await expect(viewer.locator('.qiv-zoom')).toHaveText(/%/);
  await page.mouse.move(1, 1);
  await page.keyboard.press('r');
  await expect(viewer.getByRole('button', { name: 'Draw', exact: true })).toHaveAttribute('aria-pressed', 'true');
  const box = await editor.boundingBox();
  await page.mouse.move(box.x + box.width * 0.2, box.y + box.height * 0.2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.6, { steps: 5 });
  await page.mouse.up();
  await expect.poll(() => created.length).toBe(1);
  expect(created[0]).toMatchObject({ group: 5, method: 'manual', image_width: 400, image_height: 200 });
  expect(created[0].w).toBeGreaterThan(10);
  expect(created[0].h).toBeGreaterThan(10);
  // selected, to label it and adjust its rectangle; drawing stopped
  const details = panel.getByRole('region', { name: 'Selected annotation' });
  await expect(details.getByRole('textbox', { name: 'w (pixels)' })).toHaveValue(String(created[0].w));
  await expect(viewer.getByRole('button', { name: 'Draw', exact: true })).toHaveAttribute('aria-pressed', 'false');
  // Esc unselects first, then closes the viewer
  await page.keyboard.press('Escape');
  await expect(details).toHaveCount(0);
  await expect(viewer).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(viewer).toHaveCount(0);
});
