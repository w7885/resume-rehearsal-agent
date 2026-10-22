import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import * as api from './api';
import { DraftBuffer } from './draftBuffer';
import type { BufferSnapshot } from './draftBuffer';
import type { Conversation, DraftResult } from './types';

const EMPTY: BufferSnapshot = { text: '', saved: '', status: 'saved', error: '' };
const subscribeEmpty = () => () => {};
const snapshotEmpty = () => EMPTY;

/** The editor stays mounted even when its panel is hidden. All chat mutations
 * and navigation must flush this same buffer before proceeding. */
export function useChatDraft(paused: boolean, onSaved: (sid: string, result: DraftResult) => void) {
  const ref = useRef<DraftBuffer | null>(null);
  const [buffer, setBuffer] = useState<DraftBuffer | null>(null);
  const savedCallback = useRef(onSaved);
  savedCallback.current = onSaved;
  const snapshot = useSyncExternalStore(buffer?.subscribe ?? subscribeEmpty, buffer?.getSnapshot ?? snapshotEmpty);

  const activate = (value: Conversation | null) => {
    let next: DraftBuffer | null = null;
    if (value && value.session.mode === 'local') {
      const sid = value.session.sessionId;
      next = new DraftBuffer(value, async payload => {
        const result = await api.writeDraft(sid, payload);
        if (ref.current === next) savedCallback.current(sid, result);
        return result;
      }, () => api.readConversation(sid));
    }
    ref.current = next; setBuffer(next);
  };
  useEffect(() => {
    if (paused || snapshot.status !== 'dirty') return;
    const timer = window.setTimeout(() => { void buffer?.flush().catch(() => {}); }, 500);
    return () => window.clearTimeout(timer);
  }, [buffer, snapshot.text, snapshot.status, paused]);

  return { ref, buffer, snapshot, activate, flush: async () => { await ref.current?.flush(); } };
}
