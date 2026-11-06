import test from 'node:test';
import assert from 'node:assert/strict';
import {
  applyPanelWidths, clampPanelWidth, defaultPanelWidth, encodePanelWidths, keyboardPanelWidth, panelWidthLimits, parsePanelWidths, resizePanelWidth,
} from '../src/panelResize.ts';

test('default widths stay aligned with the existing responsive breakpoints', () => {
  assert.equal(defaultPanelWidth('history', 1600), 240);
  assert.equal(defaultPanelWidth('history', 1200), 222);
  assert.equal(defaultPanelWidth('history', 1150), 195);
  assert.equal(defaultPanelWidth('history', 700), 195);
  assert.equal(defaultPanelWidth('draft', 1600), 390);
  assert.equal(defaultPanelWidth('draft', 1200), 354);
  assert.equal(defaultPanelWidth('draft', 1150), 310);
});

test('limits keep the conversation at least 460px wide', () => {
  assert.deepEqual(panelWidthLimits('history', 1200, 354, true), { min: 180, max: 360 });
  assert.deepEqual(panelWidthLimits('draft', 1200, 222, true), { min: 280, max: 518 });
  assert.deepEqual(panelWidthLimits('history', 700, 0, false), { min: 180, max: 240 });
  assert.deepEqual(panelWidthLimits('draft', 1600, 240, true), { min: 280, max: 520 });
});

test('width clamping respects panel, viewport share and center space', () => {
  assert.equal(clampPanelWidth('history', 1000, 1200, 354, true), 360);
  assert.equal(clampPanelWidth('history', 100, 1200, 354, true), 180);
  assert.equal(clampPanelWidth('draft', 1000, 1200, 222, true), 518);
  assert.equal(clampPanelWidth('draft', 100, 1200, 222, true), 280);
  assert.equal(clampPanelWidth('history', Number.NaN, 1200, 354, true), 180);
});

test('pointer movement widens the panel on the dragged side', () => {
  assert.equal(resizePanelWidth('history', 222, 40, 1200, 354, true), 262);
  assert.equal(resizePanelWidth('history', 222, -100, 1200, 354, true), 180);
  assert.equal(resizePanelWidth('draft', 354, -40, 1200, 222, true), 394);
  assert.equal(resizePanelWidth('draft', 354, 100, 1200, 222, true), 280);
  assert.equal(resizePanelWidth('draft', 354, -2000, 1200, 222, true), 518);
});

test('keyboard arrows, Shift and Home/End stay within limits', () => {
  assert.equal(keyboardPanelWidth('history', 222, 'ArrowRight', false, 1200, 354, true), 238);
  assert.equal(keyboardPanelWidth('history', 222, 'ArrowLeft', false, 1200, 354, true), 206);
  assert.equal(keyboardPanelWidth('history', 222, 'ArrowRight', true, 1200, 354, true), 286);
  assert.equal(keyboardPanelWidth('history', 222, 'Home', false, 1200, 354, true), 180);
  assert.equal(keyboardPanelWidth('history', 222, 'End', false, 1200, 354, true), 360);
  assert.equal(keyboardPanelWidth('draft', 354, 'ArrowLeft', false, 1200, 222, true), 370);
  assert.equal(keyboardPanelWidth('draft', 354, 'ArrowRight', false, 1200, 222, true), 338);
  assert.equal(keyboardPanelWidth('draft', 354, 'Home', false, 1200, 222, true), 280);
  assert.equal(keyboardPanelWidth('draft', 354, 'End', false, 1200, 222, true), 518);
  assert.equal(keyboardPanelWidth('draft', 354, 'Enter', false, 1200, 222, true), null);
});

test('applied widths clamp the draft first so the history panel is not over-restricted', () => {
  const stored = { history: 260, draft: 520 };
  assert.deepEqual(applyPanelWidths(stored, 1400, true), {
    history: 260, draft: 520, historyLimits: { min: 180, max: 360 }, draftLimits: { min: 280, max: 520 },
  });
  const narrow = applyPanelWidths(stored, 1000, true);
  assert.equal(narrow.draft, 280);
  assert.equal(narrow.history, 260);
  assert.deepEqual(narrow.historyLimits, { min: 180, max: 260 });
  assert.deepEqual(narrow.draftLimits, { min: 280, max: 280 });
  const overlay = applyPanelWidths({ history: 260, draft: 520 }, 900, false);
  assert.equal(overlay.history, 260);
});

test('stored widths reject malformed, array, wrong-type and out-of-range values', () => {
  assert.deepEqual(parsePanelWidths(null), { history: null, draft: null });
  assert.deepEqual(parsePanelWidths('{'), { history: null, draft: null });
  assert.deepEqual(parsePanelWidths('[]'), { history: null, draft: null });
  assert.deepEqual(parsePanelWidths('{"history":"222","draft":-1}'), { history: null, draft: null });
  assert.deepEqual(parsePanelWidths('{"history":9999}'), { history: null, draft: null });
  assert.deepEqual(parsePanelWidths('{"history":222,"draft":354}'), { history: 222, draft: 354 });
  assert.deepEqual(parsePanelWidths(encodePanelWidths({ history: 240, draft: 390 })), { history: 240, draft: 390 });
});
