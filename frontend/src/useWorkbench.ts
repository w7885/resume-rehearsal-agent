import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import * as api from './api';
import { DraftBuffer } from './draftBuffer';
import type { BufferSnapshot } from './draftBuffer';
import type { Detail, Session } from './types';

const EMPTY: BufferSnapshot = { text: '', saved: '', status: 'saved', error: '' };
const noopSubscribe = () => () => {};
const emptySnapshot = () => EMPTY;
type Retry = { label: string; execute: () => Promise<void> };

export function useWorkbench() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [detail, setDetail] = useState<Detail | null>(null);
  const active = useRef<Detail | null>(null);
  const bufferRef = useRef<DraftBuffer | null>(null);
  const [buffer, setBuffer] = useState<DraftBuffer | null>(null);
  const draft = useSyncExternalStore(buffer?.subscribe ?? noopSubscribe, buffer?.getSnapshot ?? emptySnapshot);
  const [busy, setBusy] = useState(false);
  const gate = useRef(false);
  const [retry, setRetry] = useState<Retry | null>(null);
  const retryRef = useRef<Retry | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [loading, setLoading] = useState(true);
  const refreshList = useCallback(async () => { setSessions(await api.listSessions()); }, []);

  const activate = useCallback((value: Detail | null) => {
    active.current = value; setDetail(value);
    let next: DraftBuffer | null = null;
    if (value) {
      const sid = value.session.sessionId;
      next = new DraftBuffer(value, async payload => {
        const result = await api.writeDraft(sid, payload);
        if (bufferRef.current === next && active.current?.session.sessionId === sid) {
          const merged = { ...active.current, session: result.session, draft: result.draft };
          active.current = merged; setDetail(merged);
          void api.readSession(sid).then(fresh => {
            if (bufferRef.current === next && fresh.session.stateRevision === result.session.stateRevision) {
              active.current = fresh; setDetail(fresh);
            }
          }).catch(() => {});
          void refreshList().catch(() => {});
        }
        return result;
      }, () => api.readSession(sid));
    }
    bufferRef.current = next; setBuffer(next);
    const url = new URL(window.location.href);
    if (value) url.searchParams.set('session', value.session.sessionId); else url.searchParams.delete('session');
    window.history.replaceState(null, '', url);
  }, [refreshList]);

  useEffect(() => {
    let live = true;
    void (async () => {
      try {
        const list = await api.listSessions();
        if (!live) return;
        setSessions(list);
        const sid = new URLSearchParams(window.location.search).get('session');
        if (sid) {
          if (list.some(s => s.sessionId === sid)) {
            const restored = await api.readSession(sid);
            if (live) activate(restored);
          } else { setError('链接中的会话已不存在，可从左侧选择或新建。'); activate(null); }
        }
      } catch (e) { if (live) setError(e instanceof Error ? e.message : '读取历史失败，请重试。'); }
      finally { if (live) setLoading(false); }
    })();
    return () => { live = false; };
  }, [activate]);

  useEffect(() => {
    if (draft.status !== 'dirty' || busy || retry) return;
    const timer = window.setTimeout(() => { void buffer?.flush().catch(() => {}); }, 500);
    return () => window.clearTimeout(timer);
  }, [buffer, draft.text, draft.status, busy, retry]);

  const run = useCallback(async (label: string, prepare: () => Promise<() => Promise<void>>, isRetry = false) => {
    if (gate.current) return;
    if (retryRef.current && !isRetry) { setError('上一次操作尚未确认，请先重试原操作，避免重复保存。'); return; }
    gate.current = true; setBusy(true); setError(''); setNotice('');
    try {
      await bufferRef.current?.flush();
      const action = isRetry ? retryRef.current! : { label, execute: await prepare() };
      retryRef.current = action; setRetry(action);
      await action.execute();
      retryRef.current = null; setRetry(null);
      setNotice(`${label}完成。`);
      await refreshList().catch(() => { setNotice(`${label}已完成，历史列表暂未刷新，可稍后重查。`); });
    } catch (e) {
      setError(e instanceof Error ? e.message : '操作未确认成功，请保留输入后重试。');
      if (e instanceof api.ApiError && ['stale_state', 'request_conflict', 'session_not_found', 'source_not_found', 'invalid_input', 'unsupported_format', 'scope_violation'].includes(e.code)) {
        retryRef.current = null; setRetry(null);
        // A rejected versioned mutation did not write. Refresh only a clean
        // editor; a conflicted/dirty buffer keeps its local text and choices.
        if (active.current && !bufferRef.current?.hasUnsettled()) {
          try { activate(await api.readSession(active.current.session.sessionId)); }
          catch { if (e.code === 'session_not_found') activate(null); }
        }
      }
    } finally { gate.current = false; setBusy(false); }
  }, [activate, refreshList]);

  const mutate = (label: string, build: (current: Detail) => () => Promise<unknown>) => {
    const sid = active.current?.session.sessionId;
    if (!sid) return;
    return run(label, async () => {
      const current = await api.readSession(sid);
      const execute = build(current);
      return async () => { await execute(); activate(await api.readSession(sid)); };
    });
  };
  const navigate = (sid: string | null) => run(sid ? '恢复会话' : '打开新会话', async () => {
    const destination = sid ? await api.readSession(sid) : null;
    return async () => { activate(destination); };
  });
  const recoverConflict = async (keepLocal: boolean) => {
    if (!buffer || gate.current || retryRef.current) return;
    gate.current = true; setBusy(true);
    try {
      // A conflict can appear before the failed save has finished reading its
      // remote snapshot. Let that request settle before choosing a new version.
      await buffer.flush().catch(() => {});
      if (!buffer.getSnapshot().remote) await buffer.loadRemote();
      buffer.resolveConflict(keepLocal);
      if (keepLocal) await buffer.flush();
      activate(await api.readSession(active.current!.session.sessionId));
      setError(''); setNotice(keepLocal ? '已按你选择保留并保存本地文字。' : '已采用服务器保存稿。');
    } catch (e) { setError(e instanceof Error ? e.message : '仍未保存，请保留文字后重试。'); }
    finally { gate.current = false; setBusy(false); }
  };
  return { sessions, detail, buffer, draft, busy, retry, error, notice, loading, run, mutate, navigate, activate,
    setError, setNotice, recoverConflict,
    retryLast: () => run(retryRef.current?.label ?? '操作', async () => async () => {}, true),
    reload: () => run('重新读取', async () => {
      const sid = active.current?.session.sessionId;
      const fresh = sid ? await api.readSession(sid) : null;
      return async () => { activate(fresh); };
    }) };
}
