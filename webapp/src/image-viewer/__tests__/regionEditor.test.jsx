// The editable regions' overlay (regionEditor.jsx): drawing, moving, resizing, selecting, with a fake viewer.
import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import { RegionEditor } from '../regionEditor';

// The image (1000x500) is drawn at half its size, from the pane's top-left corner: 1 CSS pixel is 2 image pixels
const controller = () => ({
  onDraw: () => () => {},
  geometry: pane => pane === 'b' ? 'b' : 'a',
  imageTransform: () => ({ origin: { x: 0, y: 0 }, scale: 0.5 }),
  zoomAt: vi.fn(),
});
const size = { width: 1000, height: 500 };
const regions = [
  { id: 1, x: 100, y: 100, w: 100, h: 50, label: 'patch' },
  // drawn on the full-size input, twice as large as the image shown
  { id: 2, x: 1200, y: 400, w: 200, h: 100, label: 'halo', imageWidth: 2000, imageHeight: 1000 },
  { id: 3, x: 600, y: 300, w: 50, h: 50, label: 'theirs', readOnly: true },
];

const setup = (props = {}) => {
  const callbacks = { onSelect: vi.fn(), onCreate: vi.fn(), onChange: vi.fn() };
  const view = render(<RegionEditor
    controller={controller()}
    regions={regions}
    selected={null}
    size={size}
    drawing={false}
    canEdit={region => !region.readOnly}
    {...callbacks}
    {...props}
  />);
  return { ...callbacks, ...view, editor: screen.getByTestId('qiv-region-editor') };
};

describe('editable regions', () => {
  it('draws a rectangle by dragging, in the pixels of the image shown', () => {
    const { editor, onCreate } = setup({ drawing: true });
    fireEvent.pointerDown(editor, { button: 0, clientX: 10, clientY: 10, pointerId: 1 });
    fireEvent.pointerMove(editor, { clientX: 40, clientY: 25, pointerId: 1 });
    // the size shows while drawing
    expect(screen.getByText('60×30 at (20, 20)')).toBeTruthy();
    fireEvent.pointerMove(editor, { clientX: 60, clientY: 35, pointerId: 1 });
    fireEvent.pointerUp(editor, { clientX: 60, clientY: 35, pointerId: 1 });
    expect(onCreate).toHaveBeenCalledWith({ x: 20, y: 20, w: 100, h: 50, imageWidth: 1000, imageHeight: 500 });
  });

  it("stops at the image's edges, and a click while drawing selects the region there", () => {
    const { editor, onCreate, onSelect } = setup({ drawing: true });
    fireEvent.pointerDown(editor, { button: 0, clientX: 490, clientY: 240, pointerId: 1 });
    fireEvent.pointerMove(editor, { clientX: 900, clientY: 900, pointerId: 1 });
    fireEvent.pointerUp(editor, { clientX: 900, clientY: 900, pointerId: 1 });
    expect(onCreate).toHaveBeenLastCalledWith({ x: 980, y: 480, w: 20, h: 20, imageWidth: 1000, imageHeight: 500 });
    // less than 3 pixels: a click, on the region at (120, 110)
    fireEvent.pointerDown(editor, { button: 0, clientX: 60, clientY: 55, pointerId: 1 });
    fireEvent.pointerMove(editor, { clientX: 61, clientY: 55, pointerId: 1 });
    fireEvent.pointerUp(editor, { clientX: 61, clientY: 55, pointerId: 1 });
    expect(onCreate).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith(1);
  });

  it('moves the selected region by dragging it, in whole pixels', () => {
    const { container, onChange } = setup({ selected: 1 });
    const body = container.querySelector('.qiv-edit-body');
    expect(body.style.left).toBe('50px');
    fireEvent.pointerDown(body, { button: 0, clientX: 60, clientY: 60, pointerId: 1 });
    fireEvent.pointerMove(body, { clientX: 70.3, clientY: 55, pointerId: 1 });
    fireEvent.pointerUp(body, { clientX: 70.3, clientY: 55, pointerId: 1 });
    expect(onChange).toHaveBeenCalledWith(1, { x: 121, y: 90, w: 100, h: 50 }, { reason: 'move' });
  });

  it('resizes it by its handles, and leaves it when nothing changed', () => {
    const { container, onChange } = setup({ selected: 1 });
    expect(container.querySelectorAll('.qiv-edit-handle')).toHaveLength(8);
    const handle = container.querySelector('[data-handle="se"]');
    fireEvent.pointerDown(handle, { button: 0, clientX: 100, clientY: 75, pointerId: 1 });
    fireEvent.pointerMove(handle, { clientX: 110, clientY: 80, pointerId: 1 });
    fireEvent.pointerUp(handle, { clientX: 110, clientY: 80, pointerId: 1 });
    expect(onChange).toHaveBeenCalledWith(1, { x: 100, y: 100, w: 120, h: 60 }, { reason: 'resize' });
    const west = container.querySelector('[data-handle="w"]');
    fireEvent.pointerDown(west, { button: 0, clientX: 50, clientY: 60, pointerId: 1 });
    fireEvent.pointerUp(west, { clientX: 50, clientY: 60, pointerId: 1 });
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it('edits regions of an image of another size in their own pixels', () => {
    const { container, onChange } = setup({ selected: 2 });
    const body = container.querySelector('.qiv-edit-body');
    // 2000 px wide, shown at 1000, drawn at half: a quarter
    expect([body.style.left, body.style.top, body.style.width, body.style.height]).toEqual(['300px', '100px', '50px', '25px']);
    fireEvent.pointerDown(body, { button: 0, clientX: 310, clientY: 110, pointerId: 1 });
    fireEvent.pointerMove(body, { clientX: 320, clientY: 110, pointerId: 1 });
    fireEvent.pointerUp(body, { clientX: 320, clientY: 110, pointerId: 1 });
    expect(onChange).toHaveBeenCalledWith(2, { x: 1240, y: 400, w: 200, h: 100 }, { reason: 'move' });
  });

  it('shows read-only regions without handles; their labels select them', () => {
    const { container, onSelect } = setup({ selected: 3 });
    expect(container.querySelector('.qiv-edit-body')).toBeNull();
    fireEvent.click(screen.getByText('patch'));
    expect(onSelect).toHaveBeenCalledWith(1);
  });

  it('shows a draft (e.g. moved with the arrows) instead of the region', () => {
    const { container } = setup({ selected: 1, draft: { id: 1, rect: { x: 110, y: 100, w: 100, h: 50 } } });
    expect(container.querySelector('.qiv-edit-body').style.left).toBe('55px');
  });

  it('puts a drag back with Esc', () => {
    const { container, onChange } = setup({ selected: 1 });
    const body = container.querySelector('.qiv-edit-body');
    fireEvent.pointerDown(body, { button: 0, clientX: 60, clientY: 60, pointerId: 1 });
    fireEvent.pointerMove(body, { clientX: 90, clientY: 60, pointerId: 1 });
    fireEvent.keyDown(window, { key: 'Escape' });
    fireEvent.pointerUp(body, { clientX: 90, clientY: 60, pointerId: 1 });
    expect(onChange).not.toHaveBeenCalled();
    expect(container.querySelector('.qiv-edit-body').style.left).toBe('50px');
  });
});
