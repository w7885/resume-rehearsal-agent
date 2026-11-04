import { useEffect, useRef, useState } from 'react';
import { Icon } from './ChatIcons';
import * as api from './api';
import { WebSettingsController } from './webSettings';
import { WebContinuationController } from './webContinuation';
import { WebControl } from './ChatWebControls';
import { ChatSendOperation } from './chatSend';
import { ChatAgentOperation } from './chatAgent';
import { ChatDeleteOperation } from './chatDelete';
import { DeleteSessionDialog, RenameSessionDialog, SessionHistoryRow } from './ChatSessionActions';
import { ChatRenameOperation, sessionTitleError } from './chatRename';
import ChatDraftPanel from './ChatDraftPanel';
import ChatInspector from './ChatInspector';
import type { Inspection } from './ChatInspector';
import { useChatDraft } from './useChatDraft';
import { useVoiceInput } from './useVoiceInput';
import { appendVoiceText } from './voiceInput';
import { externalTextSegments, visibleMessageParagraphs } from './chatWeb';
import type { ChatAttachment } from './chatSend';
import type { ChatMaterial, ChatMessage, ChatSession, Conversation } from './types';
import './chat.css';

type ComposerDraft = { text: string; attachments: ChatAttachment[]; replyToMessageId?: string };
type Pending = { key: string; operation: ChatSendOperation; initialWebEnabled?: boolean };
const EMPTY: ComposerDraft = { text: '', attachments: [] };
const sizeLabel = (bytes: number) => bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MiB` : `${Math.ceil(bytes / 1024)} KiB`;
const errorText = (error: unknown) => error instanceof Error ? error.message : '操作未确认成功，请保留输入后重试。';

function MessageText({ content, role }: { content: string; role: 'user' | 'assistant' }) {
  return <div className="cp-message-text">{visibleMessageParagraphs(content, role).map((paragraph, index) =>
    <p key={index}>{externalTextSegments(paragraph).map((segment, part) => segment.href
      ? <a key={part} href={segment.href} target="_blank" rel="noopener noreferrer">{segment.text}</a>
      : <span key={part}>{segment.text}</span>)}</p>)}</div>;
}

export default function ChatPage() {
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const active = useRef<Conversation | null>(null);
  const [drafts, setDrafts] = useState<Record<string, ComposerDraft>>({});
  const draftsRef = useRef(drafts); draftsRef.current = drafts;
  const [pending, setPending] = useState<Pending | null>(null);
  const pendingRef = useRef<Pending | null>(null);
  const [sending, setSending] = useState(false), [loading, setLoading] = useState(true);
  const locked = useRef(false);
  const [error, setError] = useState('');
  const [agentTask, setAgentTask] = useState<ChatAgentOperation | null>(null);
  const [queued, setQueued] = useState<string | null>(null);
  const [progressAnswer, setProgressAnswer] = useState<{ key: string; operationId: string; text: string } | null>(null);
  const checkingWeb = useRef(false);
  const [checkingContinuation, setCheckingContinuation] = useState(false);
  const [savingSwitch, setSavingSwitch] = useState(false);
  const switchSave = useRef(false);
  const [, setProgress] = useState(0);
  const webRef = useRef<WebSettingsController | null>(null);
  if (!webRef.current) webRef.current = new WebSettingsController(() => setProgress(value => value + 1));
  const web = webRef.current;
  const [newWebEnabled, setNewWebEnabled] = useState(false);
  const agentWebChoice = useRef<{ sid: string; enabled: boolean } | null>(null);
  const continuationRef = useRef<WebContinuationController | null>(null);
  if (!continuationRef.current) continuationRef.current = new WebContinuationController(
    () => setProgress(value => value + 1), async sid => {
      const fresh = await api.readConversation(sid);
      if (active.current?.session.sessionId === sid) { install(fresh); scrollToEnd(); }
      await refreshList();
    }, operation => { setSending(true); setAgentTask(operation); agentWebChoice.current = null; });
  const continuation = continuationRef.current;
  const [historyOpen, setHistoryOpen] = useState(false);
  const [draftOpen, setDraftOpen] = useState(() => window.innerWidth >= 1000);
  const [inspecting, setInspecting] = useState<Inspection | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<{ session: ChatSession; operation: ChatDeleteOperation } | null>(null);
  const [deleting, setDeleting] = useState(false), [deleteError, setDeleteError] = useState('');
  const [renameTarget, setRenameTarget] = useState<{ session: ChatSession; operation?: ChatRenameOperation } | null>(null);
  const [renameName, setRenameName] = useState(''), [renameError, setRenameError] = useState('');
  const [renaming, setRenaming] = useState(false);
  const [renameAvailable, setRenameAvailable] = useState(false);
  const composer = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const thread = useRef<HTMLDivElement>(null);
  const key = conversation?.session.sessionId ?? 'new';
  const draft = drafts[key] ?? EMPTY;
  const selections = pending?.key === key ? pending.operation.attachments() : draft.attachments;
  const webEntry = web.get(key);
  const controlEntry = continuation.get(key);
  const controlTask = !!agentTask && controlEntry.operation === agentTask;
  const webUnsettled = !!webEntry.pending || ['loading','saving','error'].includes(webEntry.phase);
  const readOnly = conversation?.session.mode === 'mock';
  const baseUnavailable = webUnsettled || loading || sending || !!pending || !!deleteTarget || !!renameTarget || !!readOnly;
  const voice = useVoiceInput(key, baseUnavailable, (target, text) => {
    const previous = draftsRef.current, current = previous[target] ?? EMPTY;
    const next = { ...previous, [target]: { ...current, text: appendVoiceText(current.text, text) } };
    draftsRef.current = next; setDrafts(next);
    requestAnimationFrame(() => composer.current?.focus());
  });
  const unavailable = baseUnavailable || voice.blocking;
  const editor = useChatDraft(loading || sending || !!pending || !!deleteTarget || !!renameTarget, (sid, result) => {
    if (active.current?.session.sessionId !== sid) return;
    const next = { ...active.current, session: { ...active.current.session, ...result.session }, draft: result.draft };
    active.current = next; setConversation(next);
    void api.readConversation(sid).then(fresh => {
      if (active.current?.session.sessionId === sid && active.current.session.stateRevision === result.session.stateRevision && fresh.session.stateRevision === result.session.stateRevision) {
        active.current=fresh;setConversation(fresh);
      }
    }).catch(()=>{});
  });

  const setUrl = (sid: string | null) => {
    const url = new URL(window.location.href);
    url.hash = '';
    if (sid) url.searchParams.set('session', sid); else url.searchParams.delete('session');
    window.history.replaceState(null, '', url);
  };
  const install = (value: Conversation | null) => {
    editor.activate(value); setInspecting(null);
    active.current = value; setConversation(value); setUrl(value?.session.sessionId ?? null);
    if (value) void web.load(value.session.sessionId);
  };
  const refreshList = async () => { setSessions(await api.listChatSessions()); };
  const scrollToEnd = () => requestAnimationFrame(() => thread.current?.scrollTo({ top: thread.current.scrollHeight,
    behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' }));
  const editDraft = (changes: Partial<ComposerDraft>) => {
    const previous = draftsRef.current;
    const next = { ...previous, [key]: { ...(previous[key] ?? EMPTY), ...changes } };
    draftsRef.current = next; setDrafts(next);
  };
  const resumeWeb = async (retry = false) => {
    const sid = active.current?.session.sessionId;
    if (!sid || queued === sid || locked.current || pendingRef.current || loading || readOnly || (agentTask && !(retry && continuation.get(sid).operation === agentTask)) ||
        editor.ref.current?.hasUnsettled() || web.get(sid).pending) return;
    const setting = web.get(sid);
    if (!retry && (!setting.settings?.enabled || setting.phase !== 'idle')) return;
    const entry = continuation.get(sid);
    if (!retry && entry.phase === 'idle' && entry.observedRevision === setting.settings?.webSettingsRevision) return;
    if (active.current?.pendingAnalysisMessageId && !retry) return;
    checkingWeb.current = true;
    setCheckingContinuation(true);
    locked.current = true;
    try { await continuation.resume(sid, retry, setting.settings?.webSettingsRevision); }
    finally {
      const entry = continuation.get(sid);
      if (entry.phase !== 'error') setAgentTask(null);
      locked.current = false; setSending(false);
      checkingWeb.current = false; setCheckingContinuation(false);
    }
  };
  const toggleWeb = async () => {
    agentWebChoice.current = null;
    if (key === 'new') { setNewWebEnabled(value => !value); return; }
    await web.change(key, !webEntry.settings?.enabled);
    const latest = web.get(key);
    if (latest.pending || latest.error || latest.phase !== 'idle') return;
    if (!latest.settings?.enabled) {
      await continuation.cancel(key).catch(failure => setError(errorText(failure)));
      if (continuation.get(key).phase === 'idle') setAgentTask(current => current === controlEntry.operation ? null : current);
    } else await resumeWeb();
  };

  useEffect(() => {
    let live = true;
    void api.readRenameSupport().then(available => { if (live) setRenameAvailable(available); });
    void (async () => {
      try {
        const list = await api.listChatSessions(); if (!live) return; setSessions(list);
        const sid = new URLSearchParams(window.location.search).get('session');
        if (sid) { const value = await api.readConversation(sid); if (live) { install(value); scrollToEnd(); } }
      } catch (failure) { if (live) { setError(errorText(failure)); if (failure instanceof api.ApiError && failure.code === 'session_not_found') setUrl(null); } }
      finally { if (live) setLoading(false); }
    })();
    return () => { live = false; };
  }, []);
  useEffect(() => {
    const refresh = () => { const sid = active.current?.session.sessionId; if (sid) void web.load(sid); };
    window.addEventListener('focus', refresh);
    return () => window.removeEventListener('focus', refresh);
  }, []);
  useEffect(() => {
    if (key === 'new') return;
    void resumeWeb();
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') void resumeWeb(); }, 2000);
    return () => window.clearInterval(timer);
  }, [key, queued, sending, loading, agentTask, webEntry.settings?.webSettingsRevision, webEntry.phase]);
  useEffect(() => {
    const element = composer.current;
    if (element) { element.style.height = 'auto'; element.style.height = `${Math.min(element.scrollHeight, 150)}px`; }
  }, [draft.text, key]);
  useEffect(() => {
    const protect = (event: BeforeUnloadEvent) => {
      if (continuation.get(key).phase === 'error' || webUnsettled || voice.blocking || editor.ref.current?.hasUnsettled() || pending || (renameTarget && (renameTarget.operation || renameName.trim() !== renameTarget.session.title)) || Object.values(drafts).some(value => value.text.trim() || value.attachments.length)) { event.preventDefault(); event.returnValue = ''; }
    };
    window.addEventListener('beforeunload', protect); return () => window.removeEventListener('beforeunload', protect);
  }, [drafts, pending, voice.blocking, renameTarget, renameName, webUnsettled]);

  const navigate = async (sid: string | null) => {
    if (locked.current || pendingRef.current || webUnsettled) return;
    voice.cancel();
    setAgentTask(null);
    locked.current = true; setLoading(true); setError('');
    try {
      await editor.flush();
      install(sid ? await api.readConversation(sid) : null); if (!sid) setNewWebEnabled(false); setHistoryOpen(false);
      await refreshList(); requestAnimationFrame(() => { thread.current?.scrollTo({ top: 0 }); composer.current?.focus(); });
    } catch (failure) { setError(errorText(failure)); }
    finally { locked.current = false; setLoading(false); }
  };
  const send = async (supplement = false) => {
    if (switchSave.current) return;
    if (sending && agentTask && !supplement && !pendingRef.current && !draft.attachments.length && !draft.replyToMessageId &&
        /^(?:你)?(?:现在)?(?:查到哪(?:一步|里)|进度(?:怎么样|如何)|到哪一步)(?:了)?[？?。]*$/.test(draft.text.trim())) {
      setProgressAnswer({ key, operationId: agentTask.id, text: agentTask.progressMessage || '正在处理当前任务。' });
      editDraft({ text: '' });
      return;
    }
    if (checkingWeb.current && !sending && !pendingRef.current && !webUnsettled && !readOnly && draft.text.trim()) {
      setQueued(key);
      return;
    }
    if (locked.current && sending && agentTask && !pendingRef.current && !webUnsettled && !readOnly && draft.text.trim()) {
      const session = active.current?.session;
      const directLink = /^(?:JD[：:]\s*)?https?:\/\/\S+$/i.test(draft.text.trim());
      if ((directLink || supplement) && webEntry.settings?.enabled && session && agentTask.runId &&
          agentTask.sessionId === session.sessionId && !draft.attachments.length && !draft.replyToMessageId) {
        switchSave.current = true;
        setSavingSwitch(true);
        const operation = new ChatSendOperation(draft.text, [], session);
        const task = { key, operation };
        pendingRef.current = task;
        setPending(task);
        try {
          await operation.run();
          await agentTask.stop();
          setAgentTask(current => current === agentTask ? null : current);
          setQueued(key);
        } catch (failure) {
          setError(operation.isMessageSaved() ? `消息已保存。${errorText(failure)}` : errorText(failure));
        } finally {
          switchSave.current = false;
          setSavingSwitch(false);
        }
        return;
      }
      setQueued(key);
      return;
    }
    if (locked.current || webUnsettled || loading || readOnly || voice.blocking || (!pendingRef.current && !draft.text.trim() && !draft.attachments.length)) return;
    setQueued(null);
    locked.current = true; setSending(true); setError('');
    let task = pendingRef.current;
    try {
      await editor.flush();
      if (!task) {
        let operation: ChatSendOperation;
        operation = new ChatSendOperation(draft.text, draft.attachments, active.current?.session, undefined, () => {
          setProgress(value => value + 1);
          if (operation.session) setUrl(operation.session.sessionId);
        }, draft.replyToMessageId);
        task = { key, operation, ...(key === 'new' ? { initialWebEnabled: newWebEnabled } : {}) }; pendingRef.current = task; setPending(task);
      }
      const initialWebEnabled = task.initialWebEnabled;
      const result = await task.operation.run();
      const value = await api.readConversation(task.operation.session!.sessionId);
      install(value);
      const sentKey = task.key;
      setDrafts(previous => { const next = { ...previous }; delete next[sentKey]; delete next[value.session.sessionId]; return next; });
      pendingRef.current = null; setPending(null);
      await refreshList().catch(() => {}); scrollToEnd();
      setAgentTask(null);
      if (!value.capabilities.agentAvailable) await web.prepare(value.session.sessionId, initialWebEnabled);
      if (value.capabilities.agentAvailable) {
        const analysis = new ChatAgentOperation(value.session.sessionId, result.message.messageId, value.session.stateRevision, () => setProgress(value => value + 1));
        setAgentTask(analysis);
        agentWebChoice.current = initialWebEnabled === undefined ? null : { sid: value.session.sessionId, enabled: initialWebEnabled };
        try {
          await web.prepare(value.session.sessionId, initialWebEnabled);
          agentWebChoice.current = null;
          await analysis.run();
          setAgentTask(null);
        } catch (failure) {
          if (analysis.isStopped) { setAgentTask(null); setError(analysis.stopMessage); }
          else setError(`消息已保存。${errorText(failure)}`);
        }
        try { install(await api.readConversation(value.session.sessionId)); scrollToEnd(); }
        catch { setError('消息已保存，分析后的会话暂未读回。可重新读取会话。'); }
      }
    } catch (failure) {
      setError(task?.operation.isMessageSaved() ? '消息已保存，但会话暂未读回。请重试读取，原发送不会重复写入。' : errorText(failure));
      await refreshList().catch(() => {});
    } finally { locked.current = false; setSending(false); }
  };
  useEffect(() => {
    if (queued !== key || sending || loading || savingSwitch || checkingContinuation || webUnsettled || locked.current) return;
    setQueued(null);
    void send();
  }, [queued, key, sending, loading, savingSwitch, checkingContinuation, webUnsettled]);
  const retryAgent = async (task = agentTask) => {
    if (!task || locked.current) return;
    if (continuation.get(task.sessionId).operation === task) { await resumeWeb(true); return; }
    locked.current = true; setSending(true); setError('');
    setAgentTask(task);
    try {
      await web.prepare(task.sessionId, agentWebChoice.current?.sid === task.sessionId ? agentWebChoice.current.enabled : undefined);
      agentWebChoice.current = null;
      await task.run();
      install(await api.readConversation(task.sessionId)); setAgentTask(null); scrollToEnd();
    } catch (failure) { setError(errorText(failure)); }
    finally { locked.current = false; setSending(false); }
  };
  const stopAgent = async () => {
    if (!agentTask) return;
    try {
      if (controlTask) {
        await continuation.cancel(agentTask.sessionId);
        if (continuation.get(agentTask.sessionId).phase === 'idle') setAgentTask(current => current === agentTask ? null : current);
      } else await agentTask.stop();
    } catch (failure) { setError(errorText(failure)); }
  };
  const returnToEditing = async () => {
    const task = pendingRef.current;
    if (!task?.operation.canReturnToEditing() || locked.current) return;
    locked.current = true; setLoading(true);
    try {
      let value: Conversation | null = null;
      if (task.operation.session) {
        try { value = await api.readConversation(task.operation.session.sessionId); }
        catch (failure) { if (!(failure instanceof api.ApiError && failure.code === 'session_not_found')) throw failure; }
      }
      const items = task.operation.attachments().map(item => {
        const material = item.material && value?.materials.find(m => m.materialId === item.material!.materialId);
        if (material?.current) return { ...item, material };
        return item.file ? { ...item, material: undefined } : { ...item, material: item.material && { ...item.material, current: false } };
      });
      const nextKey = value?.session.sessionId ?? 'new';
      setDrafts(previous => { const next = { ...previous }; next[nextKey] = { ...(previous[task.key] ?? EMPTY), attachments: items }; if (nextKey !== task.key) delete next[task.key]; return next; });
      install(value); pendingRef.current = null; setPending(null);
      setError('已确认本次消息未写入。已保存原件会继续复用；请修正输入或移除有问题的附件后重新发送。');
    } catch (failure) { setError(errorText(failure)); }
    finally { locked.current = false; setLoading(false); }
  };
  const chooseFiles = (list: FileList | null) => {
    const files = Array.from(list ?? []);
    if (files.length + draft.attachments.length > 10) { setError('一次最多发送 10 个附件。'); return; }
    const bad = files.find(file => !file.size || file.size > 10 * 1024 * 1024 || !/\.(md|docx|pdf|png|jpe?g)$/i.test(file.name));
    if (bad) { setError(`${bad.name}：请选择非空且不超过 10 MiB 的 MD、DOCX、PDF、PNG 或 JPEG 文件。`); return; }
    editDraft({ attachments: [...draft.attachments, ...files.map(file => ({ localId: api.requestId(), file }))] }); setError('');
  };
  const addSaved = (material: ChatMaterial) => {
    if (unavailable || !material.current || draft.attachments.some(item => item.material?.materialId === material.materialId)) return;
    if (draft.attachments.length >= 10) { setError('一次最多发送 10 个附件。'); return; }
    editDraft({ attachments: [...draft.attachments, { localId: api.requestId(), material }] });
    setInspecting(null);
  };
  const saveDraft = async () => { if (locked.current || pendingRef.current) return; try { await editor.flush(); setError(''); } catch (failure) { setError(errorText(failure)); } };
  const recoverDraft = async (keepLocal: boolean) => {
    const buffer = editor.ref.current;
    if (!buffer || locked.current || pendingRef.current) return;
    locked.current = true; setLoading(true);
    try {
      await buffer.flush().catch(() => {});
      if (!buffer.getSnapshot().remote) await buffer.loadRemote();
      buffer.resolveConflict(keepLocal);
      if (keepLocal) await buffer.flush();
      install(await api.readConversation(active.current!.session.sessionId)); setError('');
    } catch (failure) { setError(errorText(failure)); }
    finally { locked.current = false; setLoading(false); }
  };
  const replyTarget = conversation?.messages.find(message => message.messageId === draft.replyToMessageId);
  const selectReply = (message: ChatMessage) => {
    if (unavailable || message.assistant?.questionState !== 'awaiting_reply') return;
    editDraft({ replyToMessageId: message.messageId });
    requestAnimationFrame(() => composer.current?.focus());
  };

  const forgetDeletedSession = (sid: string) => {
    setSessions(previous => previous.filter(item => item.sessionId !== sid));
    setDrafts(previous => { const next = { ...previous }; delete next[sid]; return next; });
    if (active.current?.session.sessionId === sid) { install(null); setAgentTask(null); setHistoryOpen(false); }
  };
  const openDelete = async (session: ChatSession) => {
    if (locked.current || pendingRef.current || sending || loading || editor.snapshot.status === 'saving') return;
    voice.cancel();
    locked.current = true; setLoading(true); setError('');
    try {
      await editor.flush();
      const fresh = await api.readConversation(session.sessionId);
      if (fresh.session.status === 'running') throw new api.ApiError('会话正在处理中，请先完成或停止任务再删除。', 'run_busy');
      setDeleteError('');
      setDeleteTarget({ session: fresh.session, operation: new ChatDeleteOperation(session.sessionId, fresh.session.stateRevision) });
    } catch (failure) {
      if (failure instanceof api.ApiError && failure.code === 'session_not_found') {
        forgetDeletedSession(session.sessionId);
      }
      setError(errorText(failure));
    } finally { locked.current = false; setLoading(false); }
  };
  const cancelDelete = () => {
    if (deleting) return;
    const sid = deleteTarget?.session.sessionId;
    setDeleteTarget(null); setDeleteError('');
    requestAnimationFrame(() => document.getElementById(`session-actions-${sid}`)?.focus());
  };
  const confirmDelete = async () => {
    if (!deleteTarget || locked.current || pendingRef.current || sending) return;
    const target = deleteTarget;
    locked.current = true; setDeleting(true); setDeleteError('');
    try {
      await target.operation.run();
      const sid = target.session.sessionId;
      forgetDeletedSession(sid);
      setDeleteTarget(null); setError('');
      requestAnimationFrame(() => composer.current?.focus());
    } catch (failure) {
      if (failure instanceof api.ApiError && ['stale_state', 'run_busy'].includes(failure.code)) {
        try {
          const fresh = await api.readConversation(target.session.sessionId);
          setDeleteTarget({ session: fresh.session, operation: new ChatDeleteOperation(fresh.session.sessionId, fresh.session.stateRevision) });
          setDeleteError(failure.code === 'stale_state' ? '会话已更新，已读取最新版本，请再次确认删除。' : failure.message);
        } catch { setDeleteError('未能读取会话的最新状态，删除未确认成功。请取消后重新打开删除确认。'); }
      } else setDeleteError('删除结果未确认成功，请重试。' + errorText(failure));
    } finally { locked.current = false; setDeleting(false); }
  };

  const syncRenamedSession = (fresh: Conversation) => {
    setSessions(previous => previous.map(item => item.sessionId === fresh.session.sessionId ? fresh.session : item));
    if (active.current?.session.sessionId === fresh.session.sessionId) install(fresh);
  };
  const openRename = async (session: ChatSession) => {
    if (locked.current || pendingRef.current || sending || loading || editor.snapshot.status === 'saving') return;
    voice.cancel(); locked.current = true; setLoading(true); setError('');
    try {
      await editor.flush();
      const fresh = await api.readConversation(session.sessionId);
      if (fresh.session.status === 'running') throw new api.ApiError('会话正在处理中，请先完成或停止任务再重命名。', 'run_busy');
      setRenameName(fresh.session.title); setRenameError(''); setRenameTarget({ session: fresh.session });
    } catch (failure) { setError(errorText(failure)); }
    finally { locked.current = false; setLoading(false); }
  };
  const cancelRename = async () => {
    if (!renameTarget || renaming) return;
    const target = renameTarget;
    if (target.operation) {
      locked.current = true; setRenaming(true);
      try { syncRenamedSession(await api.readConversation(target.session.sessionId)); }
      catch { setError('上次重命名结果尚未确认，请重新读取会话查看最新名称。'); }
      finally { locked.current = false; setRenaming(false); }
    }
    setRenameTarget(null); setRenameError('');
    requestAnimationFrame(() => document.getElementById(`session-actions-${target.session.sessionId}`)?.focus());
  };
  const confirmRename = async () => {
    if (!renameTarget || locked.current || pendingRef.current || sending || sessionTitleError(renameName)) return;
    const target = renameTarget;
    const operation = target.operation ?? new ChatRenameOperation(target.session.sessionId, target.session.stateRevision, renameName);
    setRenameTarget({ ...target, operation }); locked.current = true; setRenaming(true); setRenameError('');
    try {
      await operation.run();
      const fresh = await api.readConversation(target.session.sessionId);
      syncRenamedSession(fresh); setRenameTarget(null);
      setError(fresh.session.title === renameName.trim() ? '' : '重命名请求已处理，会话随后又有更新，已显示最新名称。');
      requestAnimationFrame(() => document.getElementById(`session-actions-${target.session.sessionId}`)?.focus());
    } catch (failure) {
      if (failure instanceof api.ApiError && ['stale_state', 'run_busy'].includes(failure.code)) {
        try {
          const fresh = await api.readConversation(target.session.sessionId);
          syncRenamedSession(fresh);
          setRenameTarget({ session: fresh.session });
          setRenameError(failure.code === 'stale_state' ? '会话已更新，请核对当前名称后再次保存。' : failure.message);
        } catch { setRenameError('无法读取最新状态，请取消后重新打开重命名；输入仍保留。'); }
      } else setRenameError('保存结果尚未确认，请保留输入后重试。' + errorText(failure));
    } finally { locked.current = false; setRenaming(false); }
  };

  return <div className={`cp-page cp-live ${draftOpen ? 'cp-with-draft' : ''}`}>
    <a className="cp-skip" href="#live-composer">跳到对话输入框</a>
    {historyOpen && <button className="cp-shade" aria-label="关闭会话列表" onClick={() => setHistoryOpen(false)} />}
    <aside className={`cp-history ${historyOpen ? 'cp-history-open' : ''}`} aria-label="会话历史">
      <div className="cp-brand"><span className="cp-logo">历</span><strong>历历在练</strong><button className="cp-icon cp-mobile-close" aria-label="关闭会话列表" onClick={() => setHistoryOpen(false)}><Icon name="close" /></button></div>
      <button className="cp-new" disabled={webUnsettled || loading || sending || !!pending || !!deleteTarget || !!renameTarget} onClick={() => { void navigate(null); }}><Icon name="plus" size={18} />新对话</button>
      <div className="cp-history-label">已保存的会话</div>
      <div className="cp-live-history-list">{sessions.map(session => <SessionHistoryRow key={session.sessionId} session={session} selected={conversation?.session.sessionId === session.sessionId} disabled={webUnsettled || loading || sending || !!pending || !!deleteTarget || !!renameTarget || editor.snapshot.status === 'saving'} renameAvailable={renameAvailable} onOpen={() => { void navigate(session.sessionId); }} onDelete={() => { void openDelete(session); }} onRename={() => { void openRename(session); }} />)}</div>
      {!loading && !sessions.length && <p className="cp-live-history-empty">发送第一条消息后，<br />会话会保存在这里。</p>}
      <div className="cp-sidebar-note"><span className="cp-note-line" /><p>把真实经历<br />讲得更清楚。</p></div>
      <footer className="cp-profile"><span className="cp-profile-icon">我</span><span>我的空间<small>消息保存在本机</small></span></footer>
    </aside>

    <main className="cp-chat" aria-label="真实聊天保存与恢复">
      <header className="cp-chat-header"><div className="cp-header-title"><button className="cp-icon cp-menu" aria-label="展开会话列表" onClick={() => setHistoryOpen(true)}><Icon name="menu" /></button><h1>{conversation?.session.title ?? '新对话'}</h1><span className="cp-preview-badge">{readOnly ? '模拟历史' : '本机保存'}</span></div><div className="cp-live-header-tools"><button className="cp-icon" aria-label="重新读取会话" title="重新读取会话" disabled={loading || sending || !!pending} onClick={() => { void navigate(conversation?.session.sessionId ?? null); }}><Icon name="chat" size={17} /></button><button className={`cp-draft-toggle ${draftOpen ? 'cp-selected' : ''}`} aria-label="切换当前稿" title="当前稿" aria-pressed={draftOpen} onClick={() => setDraftOpen(!draftOpen)}><Icon name="panel" size={18} /><span>当前稿</span></button></div></header>
      <div className="cp-thread" ref={thread}>
        {loading && !conversation && <p className="cp-live-system" role="status">正在读取本机历史…</p>}
        {conversation?.messages.length ? <div className="cp-message-list"><div className="cp-chat-date">已保存的原始对话</div>{conversation.messages.map(message => <article key={message.messageId} className={`cp-message cp-${message.role}`} aria-label={message.role === 'user' ? '已保存用户消息' : '已保存助手消息'}>{message.role === 'assistant' && <div className="cp-agent-avatar" aria-hidden="true">历</div>}<div className="cp-message-content">{message.historical && <span className="cp-live-historical">历史内容 · 不自动作为当前事实</span>}{message.assistant?.mode === 'synthetic' && <span className="cp-live-historical">构造示例 · 非真实 Agent 回复</span>}{message.role === 'user' && message.replyToMessageId && <span className="cp-replied-to">回复：{conversation.messages.find(item => item.messageId === message.replyToMessageId)?.content.slice(0, 70) ?? '已保存的询问'}</span>}<MessageText content={message.content} role={message.role} />{message.attachments.map(material => <button className="cp-attachment" key={material.materialId} onClick={() => setInspecting(material)}><span className="cp-file-symbol"><Icon name="attach" size={18} /></span><span>{material.name}<small>{sizeLabel(material.byteSize)}{material.current ? '' : ' · 历史附件'}</small></span><Icon name="chevron" size={15} /></button>)}{message.assistant && <div className="cp-reply-tools">{!!message.assistant.sourceRefs.length && <button onClick={() => setInspecting({replyMessage:message})}>查看依据</button>}{message.assistant.questionState === 'awaiting_reply' && <button disabled={unavailable} aria-pressed={draft.replyToMessageId === message.messageId} onClick={() => selectReply(message)}>回复这条询问</button>}</div>}</div></article>)}{(readOnly || !conversation.capabilities.agentAvailable) && <p className="cp-live-system">{readOnly ? '这是旧版模拟会话，仅恢复原始记录；请新建对话保存个人材料。' : '消息与原件已保存。分析服务当前未启用，可以继续保存材料和编辑当前稿。'}</p>}</div>
          : <div className="cp-welcome"><span className="cp-welcome-mark">历</span><h2>从你的经历开始</h2><p>说说你的求职方向，或先上传简历。<br />文字和原件会保存在本机，刷新后可以继续。</p><p className="cp-live-capability">发送消息或附件后，可在这里查看本次处理状态。</p></div>}
      </div>
      <div className="cp-composer-area">
        <WebControl entry={webEntry} enabled={key === 'new' ? newWebEnabled : webEntry.settings?.enabled ?? false} staged={key === 'new'}
          disabled={loading || !!pending || !!deleteTarget || !!renameTarget || !!readOnly || (sending && !agentTask)}
          onToggle={() => { void toggleWeb().catch(failure => setError(errorText(failure))); }}
          onRetry={() => { if (key !== 'new') void web.retry(key); }} />
        {error && <div className="cp-live-error" role="alert"><p>{error}</p>{pending && <div><button disabled={sending || loading} onClick={() => { void send(); }}>{pending.operation.isMessageSaved() ? '重试读取已保存会话' : '重试原发送'}</button>{pending.operation.canReturnToEditing() && <button disabled={sending || loading} onClick={() => { void returnToEditing(); }}>返回编辑</button>}</div>}</div>}
        {controlEntry.error && <div className="cp-live-error" role="alert"><p>{controlEntry.error}</p><button disabled={sending || loading} onClick={() => { void resumeWeb(true); }}>{controlEntry.operation?.isStopped ? '核对停止状态' : '重试原联网接续'}</button></div>}
        {controlEntry.phase === 'pending' && <p className="cp-live-system" role="status">联网接续已保留，当前处理结束后继续。</p>}
        {!!conversation?.materials.length && <button className="cp-live-library" disabled={sending} onClick={() => setInspecting('library')}><Icon name="attach" size={14} />查看本会话已保存附件（{conversation.materials.length}）</button>}
        {agentTask && <div className="cp-reply-tools" aria-busy={sending}>{sending ? <button onClick={() => { void stopAgent(); }}>停止处理</button> : !controlTask && <button onClick={() => { void retryAgent(); }}>重试分析</button>}<span role="status">{sending ? agentTask.progressMessage || (controlTask ? '正在接续联网资料…' : 'Agent 正在处理，原消息已保存…') : controlTask ? '联网设置已保存' : '原消息已保存'}</span></div>}
        {sending && progressAnswer?.key === key && progressAnswer.operationId === agentTask?.id &&
          <p className="cp-live-system" role="status">{progressAnswer.text}</p>}
        {conversation?.pendingAnalysisMessageId && conversation.capabilities.agentAvailable && !agentTask && !pending && !sending &&
          <button className="cp-web-retry" disabled={webUnsettled || loading || checkingContinuation} onClick={() => {
            const current = active.current;
            if (current?.pendingAnalysisMessageId) void retryAgent(new ChatAgentOperation(current.session.sessionId,
              current.pendingAnalysisMessageId, current.session.stateRevision, () => setProgress(value => value + 1)));
          }}>继续分析已保存消息</button>}
        <div className="cp-composer">
          {sending && agentTask?.runId && webEntry.settings?.enabled && !webUnsettled && !readOnly && !pending &&
            !voice.blocking && queued !== key && !!draft.text.trim() && !draft.attachments.length && !draft.replyToMessageId &&
            <button className="cp-web-retry" onClick={() => { void send(true); }}>保存补充并接续</button>}
          {replyTarget && <div className="cp-reply-selection"><span>回复：{replyTarget.content.slice(0, 80)}</span><button disabled={unavailable} aria-label="取消回复关联" onClick={() => editDraft({ replyToMessageId: undefined })}><Icon name="close" size={14} /></button></div>}
          {!!selections.length && <div className="cp-selected-files">{selections.map(item => <span key={item.localId}><Icon name="attach" size={14} />{item.material?.name ?? item.file?.name}{item.material ? ' · 原件已保存' : ''}<button disabled={unavailable} aria-label={`移除附件 ${item.material?.name ?? item.file?.name}`} onClick={() => editDraft({ attachments: draft.attachments.filter(value => value.localId !== item.localId) })}><Icon name="close" size={13} /></button></span>)}</div>}
          <label className="cp-sr-only" htmlFor="live-composer">聊天消息</label><textarea id="live-composer" ref={composer} value={draft.text} onChange={event => editDraft({ text: event.target.value })} maxLength={50000} placeholder={readOnly ? '模拟会话只供回顾，请新建对话…' : '输入消息，或添加附件…'} rows={1} readOnly={!!pending || !!readOnly || queued === key} disabled={loading || (sending && !agentTask)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); void send(); } }} />
          <div className="cp-composer-tools"><button className="cp-attach-button" aria-label="添加附件" disabled={unavailable} onClick={() => fileInput.current?.click()}><Icon name="attach" size={19} /><span>添加附件</span></button>
            <button className={`cp-voice-button ${voice.phase === 'recording' ? 'cp-recording' : ''}`} aria-label={voice.phase === 'recording' ? '停止录音并转成文字' : '语音输入'} aria-pressed={voice.phase === 'recording'} title="普通话录音，本机转写，校对后手动发送" disabled={baseUnavailable || !voice.available || voice.loading || voice.phase === 'requesting' || voice.phase === 'transcribing'} onClick={() => voice.phase === 'recording' ? voice.stop() : voice.start()}><Icon name={voice.phase === 'recording' ? 'stop' : 'mic'} size={18} /><span>{voice.phase === 'recording' ? '停止并转文字' : voice.phase === 'requesting' ? '等待授权…' : voice.phase === 'transcribing' ? '转写中…' : voice.canRetry ? '重新录音' : '语音输入'}</span>{voice.phase === 'recording' && <span aria-hidden="true" className="cp-voice-time">{voice.seconds}s</span>}</button>
            {voice.canRetry && <button className="cp-voice-action" disabled={baseUnavailable || voice.phase === 'transcribing'} onClick={voice.retry}>重试转写</button>}
            {voice.blocking && <button className="cp-voice-action" disabled={baseUnavailable} onClick={voice.cancel}>取消语音</button>}
            <span className="cp-composer-hint" role="status">{queued === key ? '消息在输入框中，当前处理结束后发送。' : sending ? agentTask ? 'Agent 正在处理…' : '正在保存，请稍候…' : pending ? '本次发送未完成，输入仍保留' : voice.blocking ? '语音不会自动发送' : 'Enter 发送，Shift + Enter 换行'}</span><button className="cp-send" aria-label="发送消息" disabled={queued === key || (unavailable && !(sending && agentTask && !pending && !webUnsettled && !readOnly && !voice.blocking && draft.text.trim())) || (!draft.text.trim() && !draft.attachments.length)} onClick={() => { void send(); }}><Icon name="send" size={21} /></button>{queued === key && <button onClick={() => setQueued(null)}>取消排队</button>}</div>
          {(voice.error || voice.blocking) && <div className={`cp-voice-status ${voice.error ? 'cp-voice-error' : ''}`} role={voice.error ? 'alert' : 'status'}>{voice.error || (voice.phase === 'requesting' ? '请在浏览器中允许使用麦克风。' : voice.phase === 'recording' ? '正在录音，最长 60 秒。停止后可校对文字；切换会话会取消本段语音。' : '正在本机转写，已有文字仍保留；切换会话会取消本次结果。')}{!voice.available && !voice.loading && <button className="cp-voice-action" onClick={voice.reload}>重试语音服务</button>}</div>}
          <input ref={fileInput} className="cp-sr-only" tabIndex={-1} type="file" multiple accept=".md,.docx,.pdf,.png,.jpg,.jpeg" disabled={unavailable} onChange={event => { chooseFiles(event.target.files); event.target.value = ''; }} />
        </div>
      </div>
    </main>

    {draftOpen && <ChatDraftPanel key={key} conversation={conversation} snapshot={editor.snapshot} disabled={unavailable} onChange={text => editor.buffer?.setText(text)} onSave={() => { void saveDraft(); }} onRecover={keep => { void recoverDraft(keep); }} onClose={() => setDraftOpen(false)} onEvidence={() => setInspecting('evidence')} onOriginalMaterial={setInspecting} />}
    {inspecting && conversation && <ChatInspector inspecting={inspecting} conversation={conversation} unsaved={editor.snapshot.status !== 'saved'} disabled={unavailable} attached={selections.flatMap(item => item.material ? [item.material.materialId] : [])} onAdd={addSaved} onSelect={setInspecting} onClose={() => setInspecting(null)} beforeChange={editor.flush} onChanged={async () => {
      const fresh = await api.readConversation(conversation.session.sessionId);
      editor.activate(fresh); active.current = fresh; setConversation(fresh);
    }} />}

    {deleteTarget && <DeleteSessionDialog session={deleteTarget.session} busy={deleting} error={deleteError} onCancel={cancelDelete} onConfirm={() => { void confirmDelete(); }} />}
    {renameTarget && <RenameSessionDialog session={renameTarget.session} value={renameName} busy={renaming} locked={!!renameTarget.operation} error={renameError} onChange={setRenameName} onCancel={() => { void cancelRename(); }} onConfirm={() => { void confirmRename(); }} />}
  </div>;
}
