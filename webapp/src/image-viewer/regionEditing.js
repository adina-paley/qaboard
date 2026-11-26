// Editable regions: the geometry of drawing, moving and resizing rectangles, without React or the DOM.
//
// A region is in the pixels of its own image: imageWidth x imageHeight when it says so (e.g. the input an annotation
// was drawn on), else the image shown. Edits stay in that frame, in whole pixels, inside the image, at least 1x1.
// The screen is in CSS pixels: the viewer's transform ({ origin, scale }: where image pixel (0, 0) is drawn, and how
// many CSS pixels an image pixel takes) maps the image shown to the pane.

// The 8 handles of a selected region: corners and sides, by compass direction
export const HANDLES = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'];
// A drag shorter than this (CSS pixels) is a click
export const MIN_DRAG = 3;
// Arrows move the selected region by 1 pixel, with Shift by 10
export const NUDGE = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };

const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
const finite = value => Number.isFinite(value);

// The frame of a region: the size of its image (bounds), and how much larger the image shown is (sx, sy)
export const regionFrame = (region, size) => {
  const width = region?.imageWidth || size?.width || 0;
  const height = region?.imageHeight || size?.height || 0;
  return {
    width,
    height,
    sx: size?.width && width ? size.width / width : 1,
    sy: size?.height && height ? size.height / height : 1,
  };
};

// Whole pixels: the edges are rounded, so that a rectangle keeps its other edges when one moves
export const snapRect = ({ x, y, w, h }) => {
  const x0 = Math.round(Math.min(x, x + w));
  const y0 = Math.round(Math.min(y, y + h));
  const x1 = Math.round(Math.max(x, x + w));
  const y1 = Math.round(Math.max(y, y + h));
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
};

// In whole pixels, at least 1x1, inside the bounds ({ width, height }) when they're known: moved in when it's out,
// made smaller when it's larger than the image
export const clampRect = (rect, bounds) => {
  let { x, y, w, h } = snapRect(rect);
  w = Math.max(1, w);
  h = Math.max(1, h);
  if (bounds?.width > 0) {
    w = Math.min(w, bounds.width);
    x = clamp(x, 0, bounds.width - w);
  }
  if (bounds?.height > 0) {
    h = Math.min(h, bounds.height);
    y = clamp(y, 0, bounds.height - h);
  }
  return { x, y, w, h };
};

const clampPoint = ({ x, y }, bounds) => ({
  x: bounds?.width > 0 ? clamp(x, 0, bounds.width) : x,
  y: bounds?.height > 0 ? clamp(y, 0, bounds.height) : y,
});

// The rectangle dragged from a to b (points in image pixels, any direction), inside the bounds, in whole pixels.
// null when it's less than a pixel wide or high.
export const rectFromDrag = (a, b, bounds) => {
  const p = clampPoint(a, bounds);
  const q = clampPoint(b, bounds);
  const rect = snapRect({ x: p.x, y: p.y, w: q.x - p.x, h: q.y - p.y });
  return rect.w >= 1 && rect.h >= 1 ? rect : null;
};

// Moved by (dx, dy) image pixels, keeping its size, inside the bounds
export const moveRect = (rect, dx, dy, bounds) => clampRect({ ...rect, x: rect.x + dx, y: rect.y + dy }, bounds);

// Resized by dragging a handle ('nw', 'n', ... see HANDLES) by (dx, dy) image pixels. The edges dragged stay inside
// the bounds, the others don't move. Dragged past the opposite edge, the rectangle flips; it's never less than 1x1.
export const resizeRect = (rect, handle, dx, dy, bounds) => {
  const { x, y, w, h } = snapRect(rect);
  const edges = { left: x, top: y, right: x + w, bottom: y + h };
  const limit = (value, max) => clamp(Math.round(value), 0, max > 0 ? max : Infinity);
  const unbounded = value => Math.round(value);
  const fit = bounds?.width > 0 ? v => limit(v, bounds.width) : unbounded;
  const fit_y = bounds?.height > 0 ? v => limit(v, bounds.height) : unbounded;
  if (handle.includes('w')) edges.left = fit(edges.left + dx);
  if (handle.includes('e')) edges.right = fit(edges.right + dx);
  if (handle.includes('n')) edges.top = fit_y(edges.top + dy);
  if (handle.includes('s')) edges.bottom = fit_y(edges.bottom + dy);
  // at least a pixel: the edge dragged gives way
  const grow = (lo, hi, dragged_lo, max) => {
    if (hi - lo >= 1 || lo - hi >= 1) return [Math.min(lo, hi), Math.max(lo, hi)];
    if (dragged_lo) return lo - 1 >= 0 ? [lo - 1, hi] : [lo, hi + 1];
    return !(max > 0) || hi + 1 <= max ? [lo, hi + 1] : [lo - 1, hi];
  };
  const [left, right] = grow(edges.left, edges.right, handle.includes('w'), bounds?.width);
  const [top, bottom] = grow(edges.top, edges.bottom, handle.includes('n'), bounds?.height);
  return { x: left, y: top, w: right - left, h: bottom - top };
};

// Moved with an arrow key (see NUDGE), 10 pixels with Shift. null for other keys.
export const nudgeRect = (rect, key, { shift = false } = {}, bounds) => {
  const step = NUDGE[key];
  if (!step) return null;
  const k = shift ? 10 : 1;
  return moveRect(rect, step[0] * k, step[1] * k, bounds);
};

// A rectangle in the middle of the image, a quarter of its size: what "Add rectangle" makes, to adjust from there
export const centeredRect = (bounds, fraction = 0.25) => {
  const width = bounds?.width || 0;
  const height = bounds?.height || 0;
  const w = Math.max(1, Math.round(width * fraction));
  const h = Math.max(1, Math.round(height * fraction));
  return { x: Math.max(0, Math.round((width - w) / 2)), y: Math.max(0, Math.round((height - h) / 2)), w, h };
};

export const sameRect = (a, b) => !!a && !!b && a.x === b.x && a.y === b.y && a.w === b.w && a.h === b.h;

// Where a region is drawn in the pane, in CSS pixels. frame: regionFrame(region, size of the image shown)
export const rectToScreen = (rect, frame, transform) => {
  const kx = transform.scale * frame.sx;
  const ky = transform.scale * frame.sy;
  return { left: transform.origin.x + rect.x * kx, top: transform.origin.y + rect.y * ky, width: rect.w * kx, height: rect.h * ky };
};

// The point of a region's frame (fractional pixels) at a point of the pane (CSS pixels)
export const screenToFrame = ({ x, y }, frame, transform) => ({
  x: (x - transform.origin.x) / (transform.scale * frame.sx),
  y: (y - transform.origin.y) / (transform.scale * frame.sy),
});

// A drag of (dx, dy) CSS pixels, in pixels of a region's frame
export const screenDelta = (dx, dy, frame, transform) => ({ dx: dx / (transform.scale * frame.sx), dy: dy / (transform.scale * frame.sy) });

// The smallest region at a point of the image shown (in its pixels), or null. regions: [{ x, y, w, h, imageWidth?, ... }]
export const regionAt = (regions, point, size) => {
  let best = null;
  let best_area = Infinity;
  (regions ?? []).forEach((region, index) => {
    if (![region?.x, region?.y, region?.w, region?.h].every(finite)) return;
    const { sx, sy } = regionFrame(region, size);
    const x0 = region.x * sx, y0 = region.y * sy, x1 = (region.x + region.w) * sx, y1 = (region.y + region.h) * sy;
    if (point.x < Math.min(x0, x1) || point.x > Math.max(x0, x1) || point.y < Math.min(y0, y1) || point.y > Math.max(y0, y1)) return;
    const area = Math.abs((x1 - x0) * (y1 - y0));
    if (area < best_area) {
      best = index;
      best_area = area;
    }
  });
  return best;
};

// The handle's cursor
export const handleCursor = handle => ({ nw: 'nwse-resize', se: 'nwse-resize', ne: 'nesw-resize', sw: 'nesw-resize', n: 'ns-resize', s: 'ns-resize', e: 'ew-resize', w: 'ew-resize' })[handle];

// "120×80 at (10, 20)"
export const describeRect = rect => rect ? `${rect.w}×${rect.h} at (${rect.x}, ${rect.y})` : '';
