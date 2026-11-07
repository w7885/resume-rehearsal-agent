import { useCallback, useEffect, useRef, useState } from 'react';
import type { KeyboardEvent, PointerEvent } from 'react';
import type { PanelKind } from './panelResize';

type PanelResizerProps = {
  kind: PanelKind;
  label: string;
  current: number;
  min: number;
  max: number;
  onResize: (width: number) => void;
  onActiveChange: (active: boolean) => void;
};

/** A focusable vertical separator. Window listeners keep the drag stable outside the handle. */
export default function PanelResizer({ kind, label, current, min, max, onResize, onActiveChange }: PanelResizerProps) {
  const [active, setActive] = useState(false);
  const drag = useRef<{ clientX: number; width: number } | null>(null);
  const cleanup = useRef<(() => void) | null>(null);

  const stop = useCallback(() => {
    drag.current = null;
    cleanup.current?.();
    cleanup.current = null;
    setActive(false);
    onActiveChange(false);
  }, [onActiveChange]);

  useEffect(() => () => {
    cleanup.current?.();
    cleanup.current = null;
  }, []);

  const start = (event: PointerEvent<HTMLDivElement>) => {
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    event.preventDefault();
    try { event.currentTarget.setPointerCapture(event.pointerId); }
    catch { /* Window listeners still track this drag. */ }
    drag.current = { clientX: event.clientX, width: current };
    setActive(true);
    onActiveChange(true);
    const move = (moveEvent: globalThis.PointerEvent) => {
      const state = drag.current;
      if (!state || moveEvent.buttons === 0) return;
      onResize(state.width + (kind === 'history' ? 1 : -1) * (moveEvent.clientX - state.clientX));
    };
    const end = () => stop();
    window.addEventListener('pointermove', move, true);
    window.addEventListener('pointerup', end, true);
    window.addEventListener('pointercancel', end, true);
    window.addEventListener('blur', end);
    cleanup.current = () => {
      window.removeEventListener('pointermove', move, true);
      window.removeEventListener('pointerup', end, true);
      window.removeEventListener('pointercancel', end, true);
      window.removeEventListener('blur', end);
    };
  };
  const keydown = (event: KeyboardEvent<HTMLDivElement>) => {
    const step = event.shiftKey ? 64 : 16;
    let next: number | null = null;
    if (event.key === 'Home') next = min;
    else if (event.key === 'End') next = max;
    else if (event.key === 'ArrowLeft') next = current + (kind === 'history' ? -step : step);
    else if (event.key === 'ArrowRight') next = current + (kind === 'history' ? step : -step);
    if (next === null) return;
    event.preventDefault();
    onResize(next);
  };

  return <div
    className={`cp-resizer cp-resizer-${kind}${active ? ' cp-resizer-active' : ''}`}
    role="separator"
    aria-orientation="vertical"
    aria-label={label}
    aria-valuemin={min}
    aria-valuemax={max}
    aria-valuenow={Math.round(current)}
    tabIndex={0}
    onPointerDown={start}
    onKeyDown={keydown}
  />;
}
