export type PanelKind = 'history' | 'draft';
export type StoredPanelWidths = { history: number | null; draft: number | null };

export const PANEL_WIDTH_STORAGE_KEY = 'cp.panel-widths.v1';
export const PANEL_WIDTH_CENTER_MIN = 460;

const CONFIG: Record<PanelKind, { min: number; max: number; share: number }> = {
  history: { min: 180, max: 360, share: 0.4 },
  draft: { min: 280, max: 520, share: 0.5 },
};
const MAX_STORED_WIDTH = 2000;

/** Keep the JavaScript defaults aligned with the CSS breakpoints. */
export function defaultPanelWidth(kind: PanelKind, viewport: number) {
  if (kind === 'history') return viewport >= 1500 ? 240 : viewport <= 1150 ? 195 : 222;
  return viewport >= 1500 ? 390 : viewport <= 1150 ? 310 : 354;
}

export function panelWidthLimits(kind: PanelKind, viewport: number, otherWidth: number, otherVisible: boolean) {
  const config = CONFIG[kind];
  const safeViewport = Number.isFinite(viewport) && viewport > 0 ? viewport : 0;
  const available = safeViewport - PANEL_WIDTH_CENTER_MIN - (otherVisible ? Math.max(0, otherWidth) : 0);
  const max = Math.max(config.min, Math.min(config.max, Math.floor(safeViewport * config.share), Math.floor(available)));
  return { min: config.min, max };
}

export function clampPanelWidth(kind: PanelKind, width: number, viewport: number, otherWidth: number, otherVisible: boolean) {
  const { min, max } = panelWidthLimits(kind, viewport, otherWidth, otherVisible);
  if (!Number.isFinite(width)) return min;
  return Math.min(max, Math.max(min, Math.round(width)));
}

export function applyPanelWidths(stored: StoredPanelWidths, viewport: number, draftVisible: boolean) {
  const defaultHistory = defaultPanelWidth('history', viewport);
  const defaultDraft = defaultPanelWidth('draft', viewport);
  const draft = stored.draft == null ? defaultDraft
    : clampPanelWidth('draft', stored.draft, viewport, stored.history ?? defaultHistory, true);
  const history = stored.history == null ? defaultHistory
    : clampPanelWidth('history', stored.history, viewport, draft, draftVisible);
  return {
    history, draft,
    historyLimits: panelWidthLimits('history', viewport, draft, draftVisible),
    draftLimits: panelWidthLimits('draft', viewport, history, true),
  };
}

/** Dragging the left boundary right widens history; dragging the right boundary left widens the draft. */
export function resizePanelWidth(kind: PanelKind, startWidth: number, deltaX: number, viewport: number, otherWidth: number, otherVisible: boolean) {
  return clampPanelWidth(kind, startWidth + (kind === 'history' ? 1 : -1) * deltaX, viewport, otherWidth, otherVisible);
}

export function keyboardPanelWidth(kind: PanelKind, current: number, key: string, shiftKey: boolean, viewport: number, otherWidth: number, otherVisible: boolean) {
  const { min, max } = panelWidthLimits(kind, viewport, otherWidth, otherVisible);
  if (key === 'Home') return min;
  if (key === 'End') return max;
  const step = shiftKey ? 64 : 16;
  if (key === 'ArrowLeft') return clampPanelWidth(kind, current + (kind === 'history' ? -step : step), viewport, otherWidth, otherVisible);
  if (key === 'ArrowRight') return clampPanelWidth(kind, current + (kind === 'history' ? step : -step), viewport, otherWidth, otherVisible);
  return null;
}

export function parsePanelWidths(raw: string | null): StoredPanelWidths {
  const empty: StoredPanelWidths = { history: null, draft: null };
  if (!raw) return empty;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) return empty;
    const record = parsed as Record<string, unknown>;
    const read = (key: PanelKind) => {
      const value = record[key];
      return typeof value === 'number' && Number.isFinite(value) && value > 0 && value <= MAX_STORED_WIDTH ? value : null;
    };
    return { history: read('history'), draft: read('draft') };
  } catch {
    return empty;
  }
}

export function encodePanelWidths(widths: StoredPanelWidths) {
  return JSON.stringify({ history: widths.history, draft: widths.draft });
}
