import { useEffect, useRef, useState } from 'react';
import { Icon } from './ChatIcons';
import * as api from './api';
import { ChatSendOperation } from './chatSend';
import { ChatAgentOperation } from './chatAgent';
import { ChatDeleteOperation } from './chatDelete';
import { DeleteSessionDialog, SessionHistoryRow } from './ChatSessionActions';
import ChatDraftPanel from './ChatDraftPanel';
import ChatInspector from './ChatInspector';
import type { Inspection } from './ChatInspector';
import { useChatDraft } from './useChatDraft';
import { useVoiceInput } from './useVoiceInput';
import { appendVoiceText } from './voiceInput';
import type { ChatAttachment } from './chatSend';
import type { ChatMaterial, ChatMessage, ChatSession, Conversation } from './types';
import './chat.css';

type ComposerDraft = { text: string; attachments: ChatAttachment[]; replyToMessageId?: string };
type Pending = { key: string; operation: ChatSendOperation };
const EMPTY: ComposerDraft = { text: '', attachments: [] };
const sizeLabel = (bytes: number) => bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MiB` : `${Math.ceil(bytes / 1024)} KiB`;
const errorText = (error: unknown) => error instanceof Error ? error.message : '操作未确认成功，请保留输入后重试。';

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
  const [, setProgress] = useState(0);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [draftOpen, setDraftOpen] = useState(() => window.innerWidth >= 1000);
  const [inspecting, setInspecting] = useState<Inspection | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<{ session: ChatSession; operation: ChatDeleteOperation } | null>(null);
  const [deleting, setDeleting] = useState(false), [deleteError, setDeleteError] = useState('');
  const composer = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const thread = useRef<HTMLDivElement>(null);
  const key = conversation?.session.sessionId ?? 'new';
  const draft = drafts[key] ?? EMPTY;
  const selections = pending?.key === key ? pending.operation.attachments() : draft.attachments;
  const readOnly = conversation?.session.mode === 'mock';
  const baseUnavailable = loading || sending || !!pending || !!deleteTarget || !!readOnly;
  const voice = useVoiceInput(key, baseUnavailable, (target, text) => {
    const previous = draftsRef.current, current = previous[target] ?? EMPTY;
    const next = { ...previous, [target]: { ...current, text: appendVoiceText(current.text, text) } };
    draftsRef.current = next; setDrafts(next);
    requestAnimationFrame(() => composer.current?.focus());
  });
  const unavailable = baseUnavailable || voice.blocking;
  const editor = useChatDraft(loading || sending || !!pending || !!deleteTarget, (sid, result) => {
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
  };
  const refreshList = async () => { setSessions(await api.listChatSessions()); };
  const scrollToEnd = () => requestAnimationFrame(() => thread.current?.scrollTo({ top: thread.current.scrollHeight,
    behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' }));
  const editDraft = (changes: Partial<ComposerDraft>) => {
    const previous = draftsRef.current;
    const next = { ...previous, [key]: { ...(previous[key] ?? EMPTY), ...changes } };
    draftsRef.current = next; setDrafts(next);
  };

  useEffect(() => {
    let live = true;
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
    const element = composer.current;
    if (element) { element.style.height = 'auto'; element.style.height = `${Math.min(element.scrollHeight, 150)}px`; }
  }, [draft.text, key]);
  useEffect(() => {
    const protect = (event: BeforeUnloadEvent) => {
      if (voice.blocking || editor.ref.current?.hasUnsettled() || pending || Object.values(drafts).some(value => value.text.trim() || value.attachments.length)) { event.preventDefault(); event.returnValue = ''; }
    };
    window.addEventListener('beforeunload', protect); return () => window.removeEventListener('beforeunload', protect);
  }, [drafts, pending, voice.blocking]);

  const navigate = async (sid: string | null) => {
    if (locked.current || pendingRef.current) return;
    voice.cancel();
    setAgentTask(null);
    locked.current = true; setLoading(true); setError('');
    try {
      await editor.flush();
      install(sid ? await api.readConversation(sid) : null); setHistoryOpen(false);
      await refreshList(); requestAnimationFrame(() => { thread.current?.scrollTo({ top: 0 }); composer.current?.focus(); });
    } catch (failure) { setError(errorText(failure)); }
    finally { locked.current = false; setLoading(false); }
  };
  const send = async () => {
    if (locked.current || loading || readOnly || voice.blocking || (!pendingRef.current && !draft.text.trim() && !draft.attachments.length)) return;
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
        task = { key, operation }; pendingRef.current = task; setPending(task);
      }
      const result = await task.operation.run();
      const value = await api.readConversation(task.operation.session!.sessionId);
      install(value);
      const sentKey = task.key;
      setDrafts(previous => { const next = { ...previous }; delete next[sentKey]; delete next[value.session.sessionId]; return next; });
      pendingRef.current = null; setPending(null);
      await refreshList().catch(() => {}); scrollToEnd();
      setAgentTask(null);
      if (value.capabilities.agentAvailable) {
        const analysis = new ChatAgentOperation(value.session.sessionId, result.message.messageId, value.session.stateRevision);
        setAgentTask(analysis);
        try {
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
  const retryAgent = async () => {
    if (!agentTask || locked.current) return;
    locked.current = true; setSending(true); setError('');
    try {
      await agentTask.run();
      install(await api.readConversation(agentTask.sessionId)); setAgentTask(null); scrollToEnd();
    } catch (failure) { setError(errorText(failure)); }
    finally { locked.current = false; setSending(false); }
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

  return <div className={`cp-page cp-live ${draftOpen ? 'cp-with-draft' : ''}`}>
    <a className="cp-skip" href="#live-composer">跳到对话输入框</a>
    {historyOpen && <button className="cp-shade" aria-label="关闭会话列表" onClick={() => setHistoryOpen(false)} />}
    <aside className={`cp-history ${historyOpen ? 'cp-history-open' : ''}`} aria-label="会话历史">
      <div className="cp-brand"><span className="cp-logo">历</span><strong>历历在练</strong><button className="cp-icon cp-mobile-close" aria-label="关闭会话列表" onClick={() => setHistoryOpen(false)}><Icon name="close" /></button></div>
      <button className="cp-new" disabled={loading || sending || !!pending || !!deleteTarget} onClick={() => { void navigate(null); }}><Icon name="plus" size={18} />新对话</button>
      <div className="cp-history-label">已保存的会话</div>
      <div className="cp-live-history-list">{sessions.map(session => <SessionHistoryRow key={session.sessionId} session={session} selected={conversation?.session.sessionId === session.sessionId} disabled={loading || sending || !!pending || !!deleteTarget || editor.snapshot.status === 'saving'} onOpen={() => { void navigate(session.sessionId); }} onDelete={() => { void openDelete(session); }} />)}</div>
      {!loading && !sessions.length && <p className="cp-live-history-empty">发送第一条消息后，<br />会话会保存在这里。</p>}
      <div className="cp-sidebar-note"><span className="cp-note-line" /><p>把真实经历<br />讲得更清楚。</p></div>
      <footer className="cp-profile"><span className="cp-profile-icon">我</span><span>我的空间<small>消息保存在本机</small></span></footer>
    </aside>

    <main className="cp-chat" aria-label="真实聊天保存与恢复">
      <header className="cp-chat-header"><div className="cp-header-title"><button className="cp-icon cp-menu" aria-label="展开会话列表" onClick={() => setHistoryOpen(true)}><Icon name="menu" /></button><h1>{conversation?.session.title ?? '新对话'}</h1><span className="cp-preview-badge">{readOnly ? '模拟历史' : '本机保存'}</span></div><div className="cp-live-header-tools"><button className="cp-icon" aria-label="重新读取会话" title="重新读取会话" disabled={loading || sending || !!pending} onClick={() => { void navigate(conversation?.session.sessionId ?? null); }}><Icon name="chat" size={17} /></button><button className={`cp-draft-toggle ${draftOpen ? 'cp-selected' : ''}`} aria-label="切换当前稿" title="当前稿" aria-pressed={draftOpen} onClick={() => setDraftOpen(!draftOpen)}><Icon name="panel" size={18} /><span>当前稿</span></button></div></header>
      <div className="cp-thread" ref={thread}>
        {loading && !conversation && <p className="cp-live-system" role="status">正在读取本机历史…</p>}
        {conversation?.messages.length ? <div className="cp-message-list"><div className="cp-chat-date">已保存的原始对话</div>{conversation.messages.map(message => <article key={message.messageId} className={`cp-message cp-${message.role}`} aria-label={message.role === 'user' ? '已保存用户消息' : '已保存助手消息'}>{message.role === 'assistant' && <div className="cp-agent-avatar" aria-hidden="true">历</div>}<div className="cp-message-content">{message.historical && <span className="cp-live-historical">历史内容 · 不自动作为当前事实</span>}{message.assistant?.mode === 'synthetic' && <span className="cp-live-historical">构造示例 · 非真实 Agent 回复</span>}{message.role === 'user' && message.replyToMessageId && <span className="cp-replied-to">回复：{conversation.messages.find(item => item.messageId === message.replyToMessageId)?.content.slice(0, 70) ?? '已保存的询问'}</span>}<div className="cp-message-text">{message.content && message.content.split('\n\n').map((paragraph, index) => <p key={index}>{paragraph}</p>)}</div>{message.attachments.map(material => <button className="cp-attachment" key={material.materialId} onClick={() => setInspecting(material)}><span className="cp-file-symbol"><Icon name="attach" size={18} /></span><span>{material.name}<small>{sizeLabel(material.byteSize)}{material.current ? '' : ' · 历史附件'}</small></span><Icon name="chevron" size={15} /></button>)}{message.assistant && <div className="cp-reply-tools">{!!message.assistant.sourceRefs.length && <button onClick={() => setInspecting({replyMessage:message})}>查看依据</button>}{message.assistant.questionState === 'awaiting_reply' && <button disabled={unavailable} aria-pressed={draft.replyToMessageId === message.messageId} onClick={() => selectReply(message)}>回复这条询问</button>}</div>}</div></article>)}{(readOnly || !conversation.capabilities.agentAvailable) && <p className="cp-live-system">{readOnly ? '这是旧版模拟会话，仅恢复原始记录；请新建对话保存个人材料。' : '消息与原件已保存。分析服务当前未启用，可以继续保存材料和编辑当前稿。'}</p>}</div>
          : <div className="cp-welcome"><span className="cp-welcome-mark">历</span><h2>从你的经历开始</h2><p>说说你的求职方向，或先上传简历。<br />文字和原件会保存在本机，刷新后可以继续。</p><p className="cp-live-capability">发送消息或附件后，可在这里查看本次处理状态。</p></div>}
      </div>
      <div className="cp-composer-area">
        {error && <div className="cp-live-error" role="alert"><p>{error}</p>{pending && <div><button disabled={sending || loading} onClick={() => { void send(); }}>{pending.operation.isMessageSaved() ? '重试读取已保存会话' : '重试原发送'}</button>{pending.operation.canReturnToEditing() && <button disabled={sending || loading} onClick={() => { void returnToEditing(); }}>返回编辑</button>}</div>}</div>}
        {!!conversation?.materials.length && <button className="cp-live-library" disabled={sending} onClick={() => setInspecting('library')}><Icon name="attach" size={14} />查看本会话已保存附件（{conversation.materials.length}）</button>}
        {agentTask && <div className="cp-reply-tools">{sending ? <button onClick={() => { void agentTask.stop().catch(failure => setError(errorText(failure))); }}>停止处理</button> : <button onClick={() => { void retryAgent(); }}>重试分析</button>}<span>原消息已保存</span></div>}
        <div className="cp-composer">
          {replyTarget && <div className="cp-reply-selection"><span>回复：{replyTarget.content.slice(0, 80)}</span><button disabled={unavailable} aria-label="取消回复关联" onClick={() => editDraft({ replyToMessageId: undefined })}><Icon name="close" size={14} /></button></div>}
          {!!selections.length && <div className="cp-selected-files">{selections.map(item => <span key={item.localId}><Icon name="attach" size={14} />{item.material?.name ?? item.file?.name}{item.material ? ' · 原件已保存' : ''}<button disabled={unavailable} aria-label={`移除附件 ${item.material?.name ?? item.file?.name}`} onClick={() => editDraft({ attachments: draft.attachments.filter(value => value.localId !== item.localId) })}><Icon name="close" size={13} /></button></span>)}</div>}
          <label className="cp-sr-only" htmlFor="live-composer">聊天消息</label><textarea id="live-composer" ref={composer} value={draft.text} onChange={event => editDraft({ text: event.target.value })} maxLength={50000} placeholder={readOnly ? '模拟会话只供回顾，请新建对话…' : '输入消息，或添加附件…'} rows={1} readOnly={!!pending || !!readOnly} disabled={loading || sending} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); void send(); } }} />
          <div className="cp-composer-tools"><button className="cp-attach-button" aria-label="添加附件" disabled={unavailable} onClick={() => fileInput.current?.click()}><Icon name="attach" size={19} /><span>添加附件</span></button>
            <button className={`cp-voice-button ${voice.phase === 'recording' ? 'cp-recording' : ''}`} aria-label={voice.phase === 'recording' ? '停止录音并转成文字' : '语音输入'} aria-pressed={voice.phase === 'recording'} title="普通话录音，本机转写，校对后手动发送" disabled={baseUnavailable || !voice.available || voice.loading || voice.phase === 'requesting' || voice.phase === 'transcribing'} onClick={() => voice.phase === 'recording' ? voice.stop() : voice.start()}><Icon name={voice.phase === 'recording' ? 'stop' : 'mic'} size={18} /><span>{voice.phase === 'recording' ? '停止并转文字' : voice.phase === 'requesting' ? '等待授权…' : voice.phase === 'transcribing' ? '转写中…' : voice.canRetry ? '重新录音' : '语音输入'}</span>{voice.phase === 'recording' && <span aria-hidden="true" className="cp-voice-time">{voice.seconds}s</span>}</button>
            {voice.canRetry && <button className="cp-voice-action" disabled={baseUnavailable || voice.phase === 'transcribing'} onClick={voice.retry}>重试转写</button>}
            {voice.blocking && <button className="cp-voice-action" disabled={baseUnavailable} onClick={voice.cancel}>取消语音</button>}
            <span className="cp-composer-hint" role="status">{sending ? agentTask ? 'Agent 正在处理…' : '正在保存，请稍候…' : pending ? '本次发送未完成，输入仍保留' : voice.blocking ? '语音不会自动发送' : 'Enter 发送，Shift + Enter 换行'}</span><button className="cp-send" aria-label="发送消息" disabled={unavailable || (!draft.text.trim() && !draft.attachments.length)} onClick={() => { void send(); }}><Icon name="send" size={21} /></button></div>
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
  </div>;
}
