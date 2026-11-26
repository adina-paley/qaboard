// Editable regions, over the first image: draw a rectangle by dragging (in drawing mode), select one (a click on it,
// see ImageViewer's clicks), move it by dragging it, resize it by its 8 handles. Arrows, Delete and Esc are handled
// with the viewer's other keys (ImageViewer.jsx). The geometry is in regionEditing.js.
//
// Over the image, only what's being edited takes the pointer: the selected region and its handles, the labels, and
// everything while drawing. Elsewhere, drags pan the image and the wheel zooms, as usual.
import { useEffect, useRef, useState } from 'react';

import {
  HANDLES, MIN_DRAG, describeRect, handleCursor, moveRect, rectFromDrag, rectToScreen, regionAt, regionFrame, resizeRect,
  sameRect, screenDelta, screenToFrame,
} from './regionEditing';

// Labels of every region while they're few and large enough on screen, else of the selected one
const MAX_LABELED = 25;
const MIN_LABELED_WIDTH = 28;
const LABEL_ROOM = 48;

// Where the image is drawn in a pane, as users zoom and pan: { origin, scale }
const useTransform = (controller, pane) => {
  const [transform, setTransform] = useState(null);
  useEffect(() => {
    const update = () => {
      const next = controller.imageTransform(controller.geometry(pane));
      setTransform(previous => previous && next && previous.scale === next.scale && previous.origin.x === next.origin.x && previous.origin.y === next.origin.y ? previous : next);
    };
    update();
    return controller.onDraw(update);
  }, [controller, pane]);
  return transform;
};

// regions: [{ id, x, y, w, h, label?, color?, imageWidth?, imageHeight?, readOnly? }]
// selected: the id of the selected region, or null. draft: { id, rect } shown instead of the region's rectangle (e.g.
// moved with the arrows, not saved yet). size: the image shown ({ width, height }).
// canEdit(region): whether users can move and resize it. onCreate({ x, y, w, h, imageWidth, imageHeight }) in the pixels of
// the image shown; onChange(id, { x, y, w, h }, { reason: 'move' | 'resize' }) in the region's own pixels.
export const RegionEditor = ({ controller, pane = 'a', regions, selected, draft, size, drawing, canEdit, onSelect, onCreate, onChange }) => {
  const transform = useTransform(controller, pane);
  const root = useRef(null);
  // what users drag: { kind: 'draw' | 'move' | 'resize', id?, handle?, frame, original?, start?, client: {x, y}, moved, rect }
  const [drag, setDrag] = useState(null);

  // The wheel zooms here too (a native listener: React's are passive, and the page would scroll)
  const placed = transform !== null;
  useEffect(() => {
    const element = root.current;
    if (!element) return;
    const onWheel = event => {
      if (event.ctrlKey && !event.deltaY) return;
      event.preventDefault();
      controller.zoomAt(pane, event.clientX, event.clientY, event.deltaY < 0 ? 1 : -1);
    };
    element.addEventListener('wheel', onWheel, { passive: false });
    return () => element.removeEventListener('wheel', onWheel);
    // oxlint-disable-next-line react/exhaustive-deps -- again once the image is placed: the element may be new
  }, [controller, pane, placed]);

  // Esc while dragging puts it back, before the viewer's Esc (stop drawing, unselect)
  const dragging = !!drag;
  useEffect(() => {
    if (!dragging) return;
    const onKeyDown = event => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      event.stopPropagation();
      setDrag(null);
    };
    window.addEventListener('keydown', onKeyDown, true);
    return () => window.removeEventListener('keydown', onKeyDown, true);
  }, [dragging]);

  if (!transform?.scale || !size?.width) return <div ref={root} className="qiv-editor" data-testid="qiv-region-editor" />;

  const shown_frame = { width: size.width, height: size.height, sx: 1, sy: 1 };
  const local = event => {
    const rect = root.current.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  };
  const capture = event => {
    event.preventDefault();
    event.stopPropagation();
    // the moves and the release come here, wherever the pointer goes
    root.current?.setPointerCapture?.(event.pointerId);
  };

  const startDraw = event => {
    if (!drawing || event.button !== 0) return;
    capture(event);
    const start = screenToFrame(local(event), shown_frame, transform);
    setDrag({ kind: 'draw', frame: shown_frame, start, client: { x: event.clientX, y: event.clientY }, moved: false, rect: null });
  };
  const startEdit = (event, region, handle = null) => {
    if (event.button !== 0) return;
    capture(event);
    const frame = regionFrame(region, size);
    const original = { x: region.x, y: region.y, w: region.w, h: region.h };
    const current = draft?.id === region.id ? draft.rect : original;
    setDrag({ kind: handle ? 'resize' : 'move', id: region.id, handle, frame, original: current, client: { x: event.clientX, y: event.clientY }, moved: false, rect: current });
  };
  const onPointerMove = event => {
    if (!drag) return;
    const dx = event.clientX - drag.client.x;
    const dy = event.clientY - drag.client.y;
    const moved = drag.moved || Math.hypot(dx, dy) >= MIN_DRAG;
    if (!moved) return;
    let rect;
    if (drag.kind === 'draw') {
      rect = rectFromDrag(drag.start, screenToFrame(local(event), drag.frame, transform), drag.frame);
    } else {
      const delta = screenDelta(dx, dy, drag.frame, transform);
      rect = drag.kind === 'move'
        ? moveRect(drag.original, delta.dx, delta.dy, drag.frame)
        : resizeRect(drag.original, drag.handle, delta.dx, delta.dy, drag.frame);
    }
    setDrag({ ...drag, moved, rect });
  };
  const onPointerUp = event => {
    if (!drag) return;
    root.current?.releasePointerCapture?.(event.pointerId);
    const done = drag;
    setDrag(null);
    if (done.kind === 'draw') {
      if (done.moved && done.rect) return onCreate?.({ ...done.rect, imageWidth: size.width, imageHeight: size.height });
      // a click while drawing: selects the region there, if any
      const point = screenToFrame(local(event), shown_frame, transform);
      const index = regionAt(regions, point, size);
      return onSelect?.(index === null ? null : regions[index].id);
    }
    if (done.moved && done.rect && !sameRect(done.rect, done.original)) onChange?.(done.id, done.rect, { reason: done.kind });
  };
  const onPointerCancel = () => setDrag(null);

  const rectOf = region => drag?.id === region.id && drag.rect ? drag.rect : draft?.id === region.id ? draft.rect : region;
  const boxes = regions.flatMap(region => {
    if (![region.x, region.y, region.w, region.h].every(Number.isFinite)) return [];
    const frame = regionFrame(region, size);
    return [{ region, frame, screen: rectToScreen(rectOf(region), frame, transform) }];
  });
  const all_labels = boxes.length <= MAX_LABELED;
  // the selected one on top
  const ordered = [...boxes].sort((a, b) => (a.region.id === selected) - (b.region.id === selected));
  const active = ordered.find(box => box.region.id === selected) ?? null;
  const editable = active && canEdit(active.region);
  const drawn = drag?.kind === 'draw' && drag.rect ? rectToScreen(drag.rect, shown_frame, transform) : null;
  // what's being changed, in pixels
  const shown_rect = drag?.rect ?? (active ? rectOf(active.region) : null);
  const badge_at = drawn ?? (drag && active ? active.screen : null);

  return <div
    ref={root}
    className={`qiv-editor ${drawing ? 'qiv-editor-drawing' : ''} ${selected !== null && selected !== undefined ? 'qiv-editor-focus' : ''}`}
    onPointerDown={startDraw}
    onPointerMove={onPointerMove}
    onPointerUp={onPointerUp}
    onPointerCancel={onPointerCancel}
    data-testid="qiv-region-editor"
  >
    <svg width="100%" height="100%" aria-hidden="true">
      {ordered.map(({ region, screen }) => {
        const color = region.color ?? 'var(--qiv-accent)';
        const state = region.id === selected ? 'qiv-outline-active' : '';
        return <g key={region.id} className={`qiv-outline ${state} ${region.readOnly ? 'qiv-outline-readonly' : ''}`}>
          <rect className="qiv-outline-halo" x={screen.left} y={screen.top} width={screen.width} height={screen.height} />
          <rect className="qiv-outline-line" x={screen.left} y={screen.top} width={screen.width} height={screen.height} style={{ stroke: color, fill: color }} />
        </g>;
      })}
      {drawn && <rect className="qiv-edit-drawn" x={drawn.left} y={drawn.top} width={drawn.width} height={drawn.height} />}
    </svg>
    {ordered.filter(({ region, screen }) => region.id === selected || (all_labels && screen.width >= MIN_LABELED_WIDTH)).map(({ region, screen }) => {
      const top = screen.top >= LABEL_ROOM ? screen.top - 18 : Math.max(0, screen.top + screen.height - 18);
      const name = region.label || (region.readOnly ? 'Region' : 'No label');
      return <button
        key={region.id}
        type="button"
        tabIndex={-1}
        className={`qiv-outline-label ${region.id === selected ? 'qiv-active' : ''} ${region.label ? '' : 'qiv-muted-label'}`}
        style={{ left: Math.max(0, screen.left), top, borderLeftColor: region.color ?? 'var(--qiv-accent)' }}
        title={`${name}${region.readOnly ? ' (read-only)' : ''}`}
        onPointerDown={event => event.stopPropagation()}
        onClick={() => onSelect?.(region.id)}
      >{name}</button>;
    })}
    {editable && !drawn && <div
      className="qiv-edit-body"
      style={{ left: active.screen.left, top: active.screen.top, width: active.screen.width, height: active.screen.height }}
      title="Drag to move (arrows: 1 pixel, Shift+arrows: 10), drag a handle to resize, Delete to delete, Esc to unselect"
      onPointerDown={event => startEdit(event, active.region)}
    >
      {HANDLES.map(handle => <span
        key={handle}
        className={`qiv-edit-handle qiv-edit-handle-${handle}`}
        style={{ cursor: handleCursor(handle) }}
        data-handle={handle}
        onPointerDown={event => startEdit(event, active.region, handle)}
      />)}
    </div>}
    {badge_at && shown_rect && <output className="qiv-edit-size" style={{ left: Math.max(0, badge_at.left), top: badge_at.top + badge_at.height + 4 }}>
      {describeRect(shown_rect)}
    </output>}
  </div>;
};
