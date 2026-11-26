// The geometry of editable regions (regionEditing.js): pure functions, in whole pixels, inside the image.
import { describe, it, expect } from 'vitest';

import {
  centeredRect, clampRect, describeRect, moveRect, nudgeRect, rectFromDrag, rectToScreen, regionAt, regionFrame, resizeRect,
  sameRect, screenDelta, screenToFrame, snapRect,
} from '../regionEditing';

const bounds = { width: 100, height: 50 };

describe('snapping and clamping', () => {
  it('rounds the edges, not the size: the other edges stay', () => {
    expect(snapRect({ x: 1.4, y: 2.6, w: 3.4, h: 1.2 })).toEqual({ x: 1, y: 3, w: 4, h: 1 });
    // negative sizes go left or up
    expect(snapRect({ x: 10, y: 10, w: -4, h: -2 })).toEqual({ x: 6, y: 8, w: 4, h: 2 });
  });

  it('keeps regions inside the image, at least 1x1', () => {
    expect(clampRect({ x: -5, y: 45, w: 10, h: 10 }, bounds)).toEqual({ x: 0, y: 40, w: 10, h: 10 });
    expect(clampRect({ x: 3, y: 3, w: 0, h: 0.2 }, bounds)).toEqual({ x: 3, y: 3, w: 1, h: 1 });
    // larger than the image: as large as it
    expect(clampRect({ x: 10, y: 0, w: 500, h: 20 }, bounds)).toEqual({ x: 0, y: 0, w: 100, h: 20 });
    // the image's size unknown: only whole pixels
    expect(clampRect({ x: -3.2, y: 4.6, w: 2, h: 2 })).toEqual({ x: -3, y: 5, w: 2, h: 2 });
  });
});

describe('drawing', () => {
  it('makes a rectangle from a drag, in any direction', () => {
    expect(rectFromDrag({ x: 10.4, y: 5.6 }, { x: 2.2, y: 20.5 }, bounds)).toEqual({ x: 2, y: 6, w: 8, h: 15 });
    expect(rectFromDrag({ x: 2, y: 3 }, { x: 12, y: 13 }, bounds)).toEqual({ x: 2, y: 3, w: 10, h: 10 });
  });

  it('stops at the edges of the image', () => {
    expect(rectFromDrag({ x: 90, y: 40 }, { x: 140, y: -20 }, bounds)).toEqual({ x: 90, y: 0, w: 10, h: 40 });
  });

  it('is nothing when less than a pixel wide or high', () => {
    expect(rectFromDrag({ x: 10, y: 10 }, { x: 10.3, y: 30 }, bounds)).toBe(null);
    expect(rectFromDrag({ x: 120, y: 10 }, { x: 150, y: 30 }, bounds)).toBe(null);
  });
});

describe('moving', () => {
  it('keeps the size, inside the image', () => {
    const rect = { x: 10, y: 10, w: 20, h: 10 };
    expect(moveRect(rect, 5.4, -2.6, bounds)).toEqual({ x: 15, y: 7, w: 20, h: 10 });
    expect(moveRect(rect, 200, 200, bounds)).toEqual({ x: 80, y: 40, w: 20, h: 10 });
    expect(moveRect(rect, -200, -200, bounds)).toEqual({ x: 0, y: 0, w: 20, h: 10 });
  });

  it('nudges with the arrows, by 10 with Shift', () => {
    const rect = { x: 10, y: 10, w: 20, h: 10 };
    expect(nudgeRect(rect, 'ArrowRight', {}, bounds)).toEqual({ x: 11, y: 10, w: 20, h: 10 });
    expect(nudgeRect(rect, 'ArrowUp', { shift: true }, bounds)).toEqual({ x: 10, y: 0, w: 20, h: 10 });
    expect(nudgeRect(rect, 'ArrowLeft', { shift: true }, bounds)).toEqual({ x: 0, y: 10, w: 20, h: 10 });
    expect(nudgeRect(rect, 'ArrowDown', {}, bounds)).toEqual({ x: 10, y: 11, w: 20, h: 10 });
    expect(nudgeRect(rect, 'a', {}, bounds)).toBe(null);
  });
});

describe('resizing by the handles', () => {
  const rect = { x: 10, y: 10, w: 20, h: 10 };

  it('moves the edges of the handle, not the others', () => {
    expect(resizeRect(rect, 'se', 5, 5, bounds)).toEqual({ x: 10, y: 10, w: 25, h: 15 });
    expect(resizeRect(rect, 'nw', 5, -5, bounds)).toEqual({ x: 15, y: 5, w: 15, h: 15 });
    expect(resizeRect(rect, 'n', 50, -3, bounds)).toEqual({ x: 10, y: 7, w: 20, h: 13 });
    expect(resizeRect(rect, 'e', 2.6, 50, bounds)).toEqual({ x: 10, y: 10, w: 23, h: 10 });
    expect(resizeRect(rect, 'sw', -4, 1, bounds)).toEqual({ x: 6, y: 10, w: 24, h: 11 });
    expect(resizeRect(rect, 'w', 3, 0, bounds)).toEqual({ x: 13, y: 10, w: 17, h: 10 });
    expect(resizeRect(rect, 's', 0, -4, bounds)).toEqual({ x: 10, y: 10, w: 20, h: 6 });
    expect(resizeRect(rect, 'ne', -10, -10, bounds)).toEqual({ x: 10, y: 0, w: 10, h: 20 });
  });

  it('stops at the edges of the image', () => {
    expect(resizeRect(rect, 'se', 500, 500, bounds)).toEqual({ x: 10, y: 10, w: 90, h: 40 });
    expect(resizeRect(rect, 'nw', -500, -500, bounds)).toEqual({ x: 0, y: 0, w: 30, h: 20 });
  });

  it('flips when dragged past the opposite edge, never less than a pixel', () => {
    expect(resizeRect(rect, 'e', -30, 0, bounds)).toEqual({ x: 0, y: 10, w: 10, h: 10 });
    expect(resizeRect(rect, 'e', -20, 0, bounds)).toEqual({ x: 10, y: 10, w: 1, h: 10 });
    expect(resizeRect(rect, 'w', 20, 0, bounds)).toEqual({ x: 29, y: 10, w: 1, h: 10 });
    expect(resizeRect(rect, 'n', 0, 10, bounds)).toEqual({ x: 10, y: 19, w: 20, h: 1 });
    // at the image's edge, it stays inside
    expect(resizeRect({ x: 99, y: 0, w: 1, h: 1 }, 'w', 5, 0, bounds)).toEqual({ x: 99, y: 0, w: 1, h: 1 });
  });
});

describe('frames: regions of an image of another size', () => {
  it("scales from the region's image to the image shown", () => {
    // drawn on the full-size input (2000x1000), shown on a preview of 1000x500
    const region = { x: 200, y: 100, w: 400, h: 200, imageWidth: 2000, imageHeight: 1000 };
    const frame = regionFrame(region, { width: 1000, height: 500 });
    expect(frame).toEqual({ width: 2000, height: 1000, sx: 0.5, sy: 0.5 });
    const transform = { origin: { x: 10, y: 20 }, scale: 0.5 };
    expect(rectToScreen(region, frame, transform)).toEqual({ left: 60, top: 45, width: 100, height: 50 });
    // and back: a point on screen, a drag on screen, in the region's own pixels
    expect(screenToFrame({ x: 60, y: 45 }, frame, transform)).toEqual({ x: 200, y: 100 });
    expect(screenDelta(10, -5, frame, transform)).toEqual({ dx: 40, dy: -20 });
    // without imageWidth, the region is in the pixels of the image shown
    expect(regionFrame({ x: 0, y: 0, w: 1, h: 1 }, { width: 1000, height: 500 })).toEqual({ width: 1000, height: 500, sx: 1, sy: 1 });
  });

  it('edits stay in the frame of the region, inside its image', () => {
    const frame = regionFrame({ imageWidth: 2000, imageHeight: 1000 }, { width: 1000, height: 500 });
    expect(moveRect({ x: 1900, y: 0, w: 100, h: 100 }, 400, 0, frame)).toEqual({ x: 1900, y: 0, w: 100, h: 100 });
  });

  it('finds the smallest region under a point of the image shown', () => {
    const size = { width: 1000, height: 500 };
    const regions = [
      { id: 'big', x: 0, y: 0, w: 500, h: 500 },
      { id: 'small', x: 200, y: 100, w: 100, h: 100, imageWidth: 2000, imageHeight: 1000 },
      { id: 'broken', x: null, y: 0, w: 10, h: 10 },
    ];
    expect(regionAt(regions, { x: 120, y: 70 }, size)).toBe(1);
    expect(regionAt(regions, { x: 300, y: 300 }, size)).toBe(0);
    expect(regionAt(regions, { x: 800, y: 300 }, size)).toBe(null);
  });
});

describe('helpers', () => {
  it('adds rectangles in the middle of the image, to adjust', () => {
    expect(centeredRect({ width: 1000, height: 500 })).toEqual({ x: 375, y: 188, w: 250, h: 125 });
    expect(centeredRect({ width: 2, height: 2 })).toEqual({ x: 1, y: 1, w: 1, h: 1 });
    expect(centeredRect({ width: 10, height: 10 }, 0.5)).toEqual({ x: 3, y: 3, w: 5, h: 5 });
  });

  it('compares and describes rectangles', () => {
    expect(sameRect({ x: 1, y: 2, w: 3, h: 4 }, { x: 1, y: 2, w: 3, h: 4, label: 'a' })).toBe(true);
    expect(sameRect({ x: 1, y: 2, w: 3, h: 4 }, null)).toBe(false);
    expect(describeRect({ x: 1, y: 2, w: 3, h: 4 })).toBe('3×4 at (1, 2)');
  });
});
