import { useEffect, useRef, useState } from 'react';
import * as api from './api';
import { useWorkbench } from './useWorkbench';
import MarkdownPreview from './MarkdownPreview';
import type { Material, Source, SourceRef, Suggestion } from './types';

const STATUS = { pending: '待确认', accepted: '已接受', rejected: '已拒绝', invalidated: '已失效' };
const SAVE = { saved: '已保存', dirty: '未保存', saving: '保存中', error: '未保存 · 请重试', conflict: '未保存 · 版本冲突' };

function Evidence({ sid, reference }: { sid: string; reference: SourceRef }) {
  const [value, setValue] = useState<{ source: Source; historical: boolean }>();
  const [error, setError] = useState(''), [open, setOpen] = useState(false), [loading, setLoading] = useState(false);
  const load = async () => {
    setOpen(!open); if (open || value) return;
    setLoading(true); setError('');
    try { setValue(await api.readSource(sid, reference)); }
    catch (e) { setError(e instanceof Error ? e.message : '无法读取依据，请重试。'); }
    finally { setLoading(false); }
  };
  return <div className="evidence"><button className="link-button" onClick={() => { void load(); }} aria-expanded={open}>查看依据原文 · 版本 {reference.sourceRevision}</button>
    {open && <div className="evidence-body">{loading && <p role="status">正在读取确切来源…</p>}{error && <p role="alert">{error} <button onClick={() => setOpen(false)}>收起后重试</button></p>}
      {value && <><p className="small">{value.historical ? '历史来源 · 仅供回顾' : '当前有效来源'} · {value.source.origin.kind === 'message' ? '用户消息' : '上传材料'}{value.source.origin.page ? ` · 第 ${value.source.origin.page} 页` : ''}{value.source.origin.lineStart ? ` · 第 ${value.source.origin.lineStart}—${value.source.origin.lineEnd} 行` : ''}</p><blockquote>{reference.quote}</blockquote><details><summary>完整原文</summary><pre>{value.source.text}</pre></details></>}
    </div>}</div>;
}

function SuggestionCard({ suggestion: s, sid, stateRevision, feedback, setFeedback, disabled, onDecision }: { suggestion: Suggestion; sid: string; stateRevision: number; feedback: string; setFeedback: (value: string) => void; disabled: boolean; onDecision: (s: Suggestion, decision: 'accept' | 'reject' | 'revise', feedback: string) => void }) {
  const [adjust, setAdjust] = useState(!!feedback), [validation, setValidation] = useState('');
  return <article className="suggestion-card" aria-label={`修改建议：${STATUS[s.status]}`}>
    <div className="row between"><h3>{s.operation === 'delete' ? '删减建议' : s.operation === 'insert_after' ? '补充建议' : '表达优化建议'}</h3><span className={`badge ${s.status}`}>{STATUS[s.status]}</span></div>
    <div className="change-before"><span className="small">原文</span><p>{s.before}</p></div><div className="change-after"><span className="small">建议内容</span><p>{s.operation === 'delete' ? '删除这段正文' : s.after}</p></div><p className="reason">{s.reason}</p>
    {s.gaps.length > 0 && <details><summary>仍有哪些限制</summary><ul>{s.gaps.map((g, i) => <li key={i}>{g}</li>)}</ul></details>}
    {s.sourceRefs.map(ref => <Evidence key={`${ref.sourceId}-${ref.sourceRevision}-${stateRevision}`} sid={sid} reference={ref} />)}
    {s.status === 'pending' ? <><div className="button-row"><button className="primary-button" disabled={disabled} onClick={() => onDecision(s, 'accept', '')}>接受并写入当前稿</button><button disabled={disabled} onClick={() => onDecision(s, 'reject', '')}>拒绝</button><button disabled={disabled} onClick={() => setAdjust(!adjust)} aria-expanded={adjust}>要求调整</button></div>
      {adjust && <div className="adjust-form"><label htmlFor={`feedback-${s.suggestionId}`}>希望怎样调整</label><textarea id={`feedback-${s.suggestionId}`} value={feedback} onChange={e => setFeedback(e.target.value)} maxLength={50000} disabled={disabled} placeholder="例如：保留协助的职责边界，不写未核实的数字" />{validation && <p role="alert">{validation}</p>}<button disabled={disabled} onClick={() => { if (!feedback.trim()) { setValidation('请先填写调整要求。'); return; } setValidation(''); onDecision(s, 'revise', feedback); }}>保存调整反馈</button><p className="small muted">只保存反馈，当前稿保持原样；真实 Agent 尚未接入，不会自动生成新说法。</p></div>}</>
      : <p className="small muted">{s.status === 'accepted' ? '这项修改已保存；后续编辑可能再次改变当前稿。' : s.status === 'invalidated' ? '这条建议不能直接采用，请以当前稿为准。' : '正文未因这条建议而改变。'}{s.decision?.feedback ? ` 反馈：${s.decision.feedback}` : ''}</p>}
  </article>;
}

function MaterialRow({ material: m, sid, disabled, replace }: { material: Material; sid: string; disabled: boolean; replace: () => void }) {
  const [text, setText] = useState<string | null>(null), [error, setError] = useState('');
  const view = async () => { try { const result = await api.readMaterial(sid, m.materialId); setText(result.sources.map(s => s.text).join('\n\n') || '尚未提取文本；已保存原文件。'); setError(''); } catch (e) { setError(e instanceof Error ? e.message : '材料读取失败。'); } };
  return <li className="material-row"><div className="row between"><strong>{m.name}</strong><span className="small">{m.useStatus === 'history' ? '历史材料' : m.parseStatus === 'pending' ? '原件已存 · 待解析' : m.useStatus === 'pending' ? '待澄清用途' : '当前材料'}</span></div>
    <div className="button-row"><button onClick={() => { void view(); }}>查看原文</button><a href={api.fileUrl(sid, m.materialId)} download>下载原件</a>{m.role === 'resume' && m.parseStatus === 'parsed' && m.useStatus === 'pending' && <button disabled={disabled} onClick={replace}>以此完整新版替换当前稿</button>}</div>
    {text !== null && <details open><summary>已保存文本</summary><pre>{text}</pre><button onClick={() => setText(null)}>收起原文</button></details>}{error && <p role="alert">{error}</p>}</li>;
}

export default function App() {
  const w = useWorkbench();
  const [health, setHealth] = useState('正在检查后端…');
  const [direction, setDirection] = useState(''), [resume, setResume] = useState(''), [jd, setJd] = useState('');
  const [files, setFiles] = useState<File[]>([]), [resumeFile, setResumeFile] = useState<File | null>(null);
  const [queuedFiles, setQueuedFiles] = useState<Record<string, File[]>>({}), [messages, setMessages] = useState<Record<string, string>>({});
  const [feedbacks, setFeedbacks] = useState<Record<string, string>>({});
  const [messageKind, setMessageKind] = useState('supplement'), [preview, setPreview] = useState(false);
  const [replacement, setReplacement] = useState<Material | null>(null), [consent, setConsent] = useState(false);
  const [replacementWords, setReplacementWords] = useState('以这份完整新版为准，替换当前稿。');
  const replaceDialog = useRef<HTMLDialogElement>(null), deleteDialog = useRef<HTMLDialogElement>(null);
  const checkHealth = async () => {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 5000);
    try { await api.readHealth(controller.signal); setHealth('后端已连接'); } catch { setHealth('后端未连接 · 输入会保留，请重查'); } finally { clearTimeout(timer); }
  };
  useEffect(() => { void checkHealth(); }, []);
  useEffect(() => {
    const protect = (e: BeforeUnloadEvent) => { if (w.buffer?.hasUnsettled() || w.retry || resume || jd || direction || files.length || Object.values(messages).some(Boolean) || Object.values(feedbacks).some(Boolean) || Object.values(queuedFiles).some(f => f.length)) { e.preventDefault(); e.returnValue = ''; } };
    window.addEventListener('beforeunload', protect); return () => window.removeEventListener('beforeunload', protect);
  }, [w.buffer, w.retry, resume, jd, direction, files, messages, feedbacks, queuedFiles]);
  const sid = w.detail?.session.sessionId ?? '', disabled = w.busy || !!w.retry;
  const message = messages[sid] ?? '', pendingFiles = queuedFiles[sid] ?? [], unsaved = w.draft.status !== 'saved';
  const chooseFiles = (list: FileList | null, initial: boolean) => {
    const selected = Array.from(list ?? []), invalid = selected.find(f => f.size > 10 * 1024 * 1024 || !/\.(md|docx|pdf|png|jpe?g)$/i.test(f.name));
    if (invalid) { w.setError(`${invalid.name}：请使用 10 MiB 以内的 MD、DOCX、PDF 或图片。`); return; }
    if (selected.length + (initial ? files.length : pendingFiles.length) > 10) { w.setError('本次最多选择 10 个待上传文件。'); return; }
    if (initial) setFiles([...files, ...selected]); else setQueuedFiles({ ...queuedFiles, [sid]: [...pendingFiles, ...selected] });
  };
  const useResumeFile = async (file: File) => { try { const text = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer()); if (!text.trim() || text.length > 50000) throw new Error('Markdown 为空或超过 50000 字符，请检查后导入。'); setResume(text); setResumeFile(file); } catch (e) { w.setError(e instanceof Error ? e.message : '无法读取 Markdown，请改为粘贴。'); } };
  const create = () => {
    if (!direction.trim() || !resume.trim()) { w.setError('请先提供求职方向和完整简历；也可从材料中选一份 Markdown 用作简历。'); return; }
    const d = direction.trim(), r = resume, j = jd, selected = [...files], original = resumeFile;
    void w.run('创建并保存会话', async () => {
      const id = api.requestId(); let session: import('./types').Session | undefined; const tasks: (() => Promise<void>)[] = []; let index = 0;
      return async () => {
        session ??= await api.createSession({ requestId: id, direction: d }); const target = session.sessionId;
        if (!tasks.length) {
          const task = (text: string, name: string, role: string, file?: File) => { const requestId = api.requestId(); let revision: number | undefined; return async () => { revision ??= (await api.readSession(target)).session.stateRevision; const payload = { requestId, expectedStateRevision: revision, name, role }; if (file) await api.uploadMaterial(target, payload, file); else await api.pasteMaterial(target, { ...payload, text }); }; };
          tasks.push(task(r, original?.name ?? '粘贴简历.md', 'resume', original ?? undefined)); if (j.trim()) tasks.push(task(j, '岗位描述.md', 'jd'));
          for (const file of selected) if (file !== original) tasks.push(task('', file.name, 'unknown', file));
        }
        while (index < tasks.length) { await tasks[index](); index++; }
        w.activate(await api.readSession(target)); setDirection(''); setResume(''); setJd(''); setFiles([]); setResumeFile(null);
      };
    });
  };
  const demo = () => { void w.run('打开独立模拟示例', async () => { const id = api.requestId(); return async () => { w.activate(await api.createDemo(id)); }; }); };
  const analyze = () => { void w.mutate('运行模拟流程', current => { const payload = { requestId: api.requestId(), expectedStateRevision: current.session.stateRevision }; return () => api.runDemo(sid, payload); }); };
  const decision = (s: Suggestion, choice: 'accept' | 'reject' | 'revise', feedback: string) => { void w.mutate(choice === 'accept' ? '接受建议' : choice === 'reject' ? '拒绝建议' : '保存调整反馈', current => { const payload = { requestId: api.requestId(), expectedStateRevision: current.session.stateRevision, expectedBlockRevision: s.target.expectedBlockRevision, decision: choice, feedback }; return async () => { await api.decideSuggestion(sid, s.suggestionId, payload); setFeedbacks(prev => { const next = { ...prev }; delete next[s.suggestionId]; return next; }); }; }); };
  const send = () => {
    if (!message.trim() && messageKind !== 'skip') return;
    const content = messageKind === 'skip' && !message.trim() ? '暂时跳过这个问题。' : message, kind = messageKind;
    void w.mutate('保存消息', current => { const payload = { requestId: api.requestId(), expectedStateRevision: current.session.stateRevision, content, kind }; return async () => { await api.saveMessage(sid, payload); setMessages(prev => ({ ...prev, [sid]: '' })); }; });
  };
  const upload = (file: File, role: string) => { void w.mutate('保存材料', current => { const payload = { requestId: api.requestId(), expectedStateRevision: current.session.stateRevision, name: file.name, role }; return async () => { await api.uploadMaterial(sid, payload, file); setQueuedFiles(prev => ({ ...prev, [sid]: (prev[sid] ?? []).filter(f => f !== file) })); }; }); };
  const doReplace = () => {
    if (!replacement || !consent || !replacementWords.trim()) return;
    const material = replacement, words = replacementWords; replaceDialog.current?.close(); setReplacement(null);
    void w.mutate('整体替换当前稿', current => {
      const msg = { requestId: api.requestId(), expectedStateRevision: current.session.stateRevision, content: words, kind: 'message' }, id = api.requestId();
      let auth: { message: { messageId: string } } | undefined, payload: Parameters<typeof api.replaceDraft>[1] | undefined;
      return async () => { auth ??= await api.saveMessage(sid, msg) as { message: { messageId: string } }; if (!payload) { const fresh = await api.readSession(sid); payload = { requestId: id, expectedStateRevision: fresh.session.stateRevision, expectedDraftRevision: fresh.session.draftRevision, materialId: material.materialId, authorizationMessageId: auth.message.messageId, confirmed: true, completeConfirmed: true }; } await api.replaceDraft(sid, payload); };
    });
  };
  const doDelete = () => {
    deleteDialog.current?.close(); void w.run('删除会话', async () => {
      const current = await api.readSession(sid), payload = { requestId: api.requestId(), expectedStateRevision: current.session.stateRevision, confirmed: true as const };
      return async () => { await api.deleteSession(sid, payload); setMessages(prev => { const next = { ...prev }; delete next[sid]; return next; }); setQueuedFiles(prev => { const next = { ...prev }; delete next[sid]; return next; }); w.activate(null); };
    });
  };
  const download = () => {
    const url = URL.createObjectURL(new Blob([w.draft.text], { type: 'text/markdown;charset=utf-8' })), link = document.createElement('a'); link.href = url; link.download = `${w.detail?.session.mode === 'mock' ? '模拟示例-' : ''}当前简历${unsaved ? '-未保存' : ''}.md`; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    w.setNotice(unsaved ? '已导出编辑区可见正文，其中有未保存修改；未接受建议未写入。' : '已导出当前稿，不含聊天、诊断或未接受建议。');
  };
  const copy = async () => { try { await navigator.clipboard.writeText(w.draft.text); w.setNotice(unsaved ? '已复制当前可见文字，仍有修改未保存。' : '已复制当前稿。'); } catch { w.setError('剪贴板不可用，可选中编辑区文字手动复制或下载 Markdown。'); } };
  const issues = w.detail?.issues.filter(i => i.isCurrent && i.status === 'open') ?? [];
  return <div className="app-shell"><a className="skip-link" href="#main-content">跳到主要内容</a>
    <header className="topbar"><div className="brand"><span className="brand-icon" aria-hidden="true">历</span>历历在练</div><div className="topbar-right"><span className="version-tag">本机工作台</span><span className="local-tag">材料留在本机 · 本页不调用模型</span></div></header>
    <div className="workspace"><aside className="sidebar" aria-label="会话历史"><div className="row between"><h2>我的会话</h2><span className="small muted">{w.sessions.length} 条</span></div><button className="new-session" disabled={disabled || w.loading} onClick={() => { void w.navigate(null); }}>＋ 新建会话</button><button className="demo-entry" disabled={disabled || w.loading} onClick={demo}>打开模拟示例<span>构造材料，免费体验完整流程</span></button>
      {w.loading ? <p role="status">正在读取历史…</p> : w.sessions.length === 0 ? <p className="empty-copy">还没有保存的会话。<br />从自己的材料开始，或先打开模拟示例。</p> : <ul className="session-list">{w.sessions.map(s => <li key={s.sessionId}><button aria-current={sid === s.sessionId ? 'page' : undefined} disabled={disabled} onClick={() => { void w.navigate(s.sessionId); }}><strong>{s.direction}</strong><span>{s.mode === 'mock' ? '模拟材料' : '本机材料'} · {new Date(s.updatedAt).toLocaleDateString('zh-CN')}</span></button></li>)}</ul>}<div className="sidebar-footer"><p>先核对依据，再决定改写。<br />未接受的建议不会进入当前稿。</p></div></aside>
    <main id="main-content" className="conversation"><div className="panel-heading"><h1>{w.detail ? w.detail.session.direction : '从真实材料，写清你的经历'}</h1><div className="row between"><span className="small muted">{health}</span><button className="link-button" disabled={w.busy} onClick={() => { void checkHealth(); void w.reload(); }}>重新检查与读取</button></div>{w.detail && <nav className="workbench-nav" aria-label="当前会话区域"><a href="#material-heading">原始材料</a><a href="#dialogue-heading">对话与补充</a><a href="#suggestion-heading">修改建议</a><a href="#draft-heading">当前稿</a></nav>}</div><div className="conversation-scroll">
      {w.error && <div className="feedback error" role="alert"><p>{w.error}</p>{w.retry && <button disabled={w.busy} onClick={() => { void w.retryLast(); }}>重试原操作：{w.retry.label}</button>}</div>}{w.notice && <p className="feedback success" role="status">{w.notice}</p>}{w.busy && <p className="small" role="status">正在处理，请稍候…</p>}
      {!w.detail ? <section aria-label="新会话材料"><div className="intro"><h2>保留原始表达，让修改有据可查</h2><p>先保存完整简历和求职方向。真实 Agent 尚未接入；你可以手改、导出，或用独立模拟示例体验建议流程。</p></div>
        <label htmlFor="direction">求职方向（必填）</label><input id="direction" value={direction} onChange={e => setDirection(e.target.value)} maxLength={120} disabled={disabled} placeholder="例如：后端开发实习" />
        <label htmlFor="resume">完整简历（必填）</label><textarea id="resume" value={resume} onChange={e => { setResume(e.target.value); setResumeFile(null); }} maxLength={50000} disabled={disabled} placeholder="粘贴真实简历原文；也可从下方材料选择 Markdown 用作简历。" />
        <label htmlFor="initial-files">添加材料 <span className="small muted">MD、Word、PDF、图片；单文件 10 MiB 以内</span></label><input id="initial-files" type="file" multiple accept=".md,.docx,.pdf,.png,.jpg,.jpeg" disabled={disabled} onChange={e => { chooseFiles(e.target.files, true); e.target.value = ''; }} />
        <ul className="file-queue">{files.map((f, i) => <li key={`${f.name}-${i}`}><span>{f.name}{f === resumeFile ? ' · 已选为简历原文' : ''}</span><div className="button-row">{/\.md$/i.test(f.name) && <button disabled={disabled} onClick={() => { void useResumeFile(f); }}>用作完整简历</button>}<button disabled={disabled} onClick={() => { setFiles(files.filter((_, index) => index !== i)); if (resumeFile === f) setResumeFile(null); }}>移除</button></div></li>)}</ul>
        <details className="jd-section"><summary>添加岗位描述 JD（选填）</summary><label htmlFor="jd">岗位描述</label><textarea id="jd" value={jd} onChange={e => setJd(e.target.value)} maxLength={50000} disabled={disabled} placeholder="用于理解岗位要求，不作为个人经历依据。" /></details><p className="small muted">其他附件按待澄清材料保存，不自动混入个人事实。Word／PDF／图片目前只保存原件，尚未解析。</p><div className="button-row"><button className="primary-button" disabled={disabled || w.loading} onClick={create}>创建并保存会话</button><span className="small muted">创建前，表单内容尚未保存</span></div>
      </section> : <>
        <div className={`mode-note ${w.detail.session.mode === 'mock' ? 'mock' : ''}`}><strong>{w.detail.session.mode === 'mock' ? '模拟模式 · 初始材料与建议为构造示例' : '本机材料 · 真实分析尚未接入'}</strong><p>{w.detail.session.mode === 'mock' ? '建议与追问为固定示例；材料保存、依据查询、接受与编辑使用真实业务接口。未调用模型，不产生费用。' : '你可以保存材料、手工编辑和导出。此页面不会对你的履历套用预设建议。'}</p></div>
        <section className="section-block" aria-labelledby="material-heading"><h2 id="material-heading">原始材料</h2><p className="small muted">原件与提取原文供核对，不因接受建议而改写。</p><ul className="materials">{w.detail.materials.map(m => <MaterialRow key={m.materialId} material={m} sid={sid} disabled={disabled || unsaved} replace={() => { setReplacement(m); setConsent(false); replaceDialog.current?.showModal(); }} />)}</ul>
          <label htmlFor="more-files">继续添加材料</label><input id="more-files" type="file" multiple accept=".md,.docx,.pdf,.png,.jpg,.jpeg" disabled={disabled} onChange={e => { chooseFiles(e.target.files, false); e.target.value = ''; }} />
          <ul className="file-queue">{pendingFiles.map((f, i) => <li key={`${f.name}-${i}`}><span>{f.name}</span><div className="button-row"><button disabled={disabled} onClick={() => upload(f, 'unknown')}>保存为待识别材料</button>{/\.md$/i.test(f.name) && <button disabled={disabled} onClick={() => upload(f, 'resume')}>保存为完整新版候选</button>}<button disabled={disabled} onClick={() => setQueuedFiles({ ...queuedFiles, [sid]: pendingFiles.filter((_, index) => index !== i) })}>移除</button></div></li>)}</ul><p className="small muted">选择新版候选不会自动覆盖；先查看原文，确认完整且同人同岗位后再整体替换。</p></section>
        <section className="section-block" aria-labelledby="dialogue-heading"><div className="row between"><h2 id="dialogue-heading">对话与补充</h2>{w.detail.session.mode === 'mock' && <button disabled={disabled || !!w.detail.events.length || unsaved} onClick={analyze}>运行模拟分析</button>}</div>
          {w.detail.events.filter(e => ['diagnosis', 'question', 'tool_progress'].includes(e.type)).map(e => <div className="agent-message" key={`${e.runId}-${e.sequence}`}><span className="small">{e.type === 'question' ? '模拟追问' : e.type === 'diagnosis' ? '模拟诊断' : '构造工具结果'}</span><p>{e.payload.message}</p></div>)}
          {w.detail.messages.length === 0 && <p className="empty-copy">可补充行动细节、纠正事实，或明确跳过问题。</p>}{w.detail.messages.map(m => <div className="user-message" key={m.messageId}><span className="small">{m.role === 'user' ? m.kind === 'correction' ? '事实纠正' : m.kind === 'skip' ? '跳过' : '用户消息' : '助手消息'}</span><p>{m.content}</p></div>)}
          <label htmlFor="message-kind">本次输入类型</label><select id="message-kind" value={messageKind} disabled={disabled} onChange={e => setMessageKind(e.target.value)}><option value="supplement">补充材料</option><option value="correction">事实纠正</option><option value="message">普通消息</option><option value="skip">跳过问题</option></select><label htmlFor="message">补充内容</label><textarea id="message" value={message} onChange={e => setMessages({ ...messages, [sid]: e.target.value })} maxLength={50000} disabled={disabled} placeholder="仅填写真实经历；尚不确定的信息可以直接说明。" /><button disabled={disabled || (!message.trim() && messageKind !== 'skip')} onClick={send}>保存消息</button><p className="small muted">消息会保存；真实 Agent 尚未接入，不会自动回复。事实纠正不会静默改动当前稿。</p></section>
        {issues.length > 0 && <section className="section-block unresolved"><h2>仍需处理的问题</h2>{issues.map(i => <div key={i.issueId}><p>{i.summary}</p>{i.sourceRefs.map(ref => <Evidence key={`${ref.sourceId}-${ref.sourceRevision}`} sid={sid} reference={ref} />)}</div>)}<p className="small">可以导出当前稿，但这些问题尚未解决，不代表简历已全部确认。</p></section>}
        <section className="section-block" aria-labelledby="suggestion-heading"><h2 id="suggestion-heading">修改建议</h2><p className="small muted">待确认建议独立保存，接受后才进入当前稿。</p>{w.detail.suggestions.length ? w.detail.suggestions.map(s => <SuggestionCard key={s.suggestionId} sid={sid} suggestion={s} stateRevision={w.detail!.session.stateRevision} feedback={feedbacks[s.suggestionId] ?? ""} setFeedback={value => setFeedbacks(prev => ({ ...prev, [s.suggestionId]: value }))} disabled={disabled || unsaved} onDecision={decision} />) : <p className="empty-copy">暂无建议。{w.detail.session.mode === 'mock' ? '点击“运行模拟分析”查看固定构造示例。' : '真实 Agent 接入后，建议会显示在这里。'}</p>}{unsaved && <p className="small warning">请先处理右侧未保存文字，再确认建议。</p>}</section>
        {w.detail.session.mode === 'mock' && <details className="section-block"><summary>查看冲突与错误的模拟示例</summary><p><strong>冲突示例：</strong>若旧稿写“负责推进”，新补充说“仅协助”，应标记待核实并提出待确认纠正，不直接覆盖。</p><p><strong>错误示例：</strong>解析失败应保留原件并提供粘贴路径；保存失败应保留文字并标记未保存。</p><p className="small muted">仅为交互说明，不是当前诊断，也不是实际发生的故障。</p></details>}
        <div className="session-actions"><button className="danger-button" disabled={disabled} onClick={() => deleteDialog.current?.showModal()}>删除当前会话</button><span className="small muted">清除本应用保存的会话及附件副本。</span></div>
      </>}
    </div></main>
    <aside className="draft-panel" aria-labelledby="draft-heading"><div className="row between"><h2 id="draft-heading">当前简历</h2><span className={`badge save-${w.draft.status}`} role="status">{w.detail ? SAVE[w.draft.status] : '尚未创建'}</span></div>
      {w.detail ? <><div className="draft-toolbar"><div className="tabs"><button aria-pressed={!preview} onClick={() => setPreview(false)}>编辑</button><button aria-pressed={preview} onClick={() => setPreview(true)}>预览</button></div><div className="button-row"><button onClick={() => { void copy(); }}>复制</button><button onClick={download}>下载 Markdown</button></div></div><p className="small muted">停止输入 500 毫秒后自动保存。预览支持基础 Markdown，不执行 HTML 或脚本。</p>
        {preview ? <MarkdownPreview markdown={w.draft.text} /> : <><label className="visually-hidden" htmlFor="draft-editor">当前稿 Markdown 编辑器</label><textarea id="draft-editor" className="draft-editor" value={w.draft.text} onChange={e => w.buffer?.setText(e.target.value)} maxLength={50000} disabled={w.busy || !!w.retry} spellCheck={false} /></>}
        {w.draft.status === 'error' && <div className="feedback error" role="alert"><p>{w.draft.error} 编辑区文字仍保留。</p><button disabled={w.busy} onClick={() => { void w.buffer?.flush().catch(() => {}); }}>重试保存</button></div>}
        {w.draft.status === 'conflict' && <div className="feedback error" role="alert"><p>其他操作已更新稿件。本地文字仍保留，请选择处理方式。</p>{w.draft.remote && <details><summary>服务器当前稿</summary><pre>{w.draft.remote.draft.markdown}</pre></details>}<div className="button-row"><button disabled={w.busy} onClick={() => { void w.recoverConflict(true); }}>保留本地文字并保存</button><button disabled={w.busy} onClick={() => { void w.recoverConflict(false); }}>放弃本地修改，采用服务器稿</button></div></div>}
        <button className="save-button" disabled={w.busy || !!w.retry || w.draft.status === 'conflict'} onClick={() => { void w.buffer?.flush().catch(() => {}); }}>立即保存</button>{issues.length > 0 && <p className="feedback warning">仍有 {issues.length} 项未解决问题。下载只含正文，不会自动消除问题。</p>}<p className="draft-note">未接受建议不进入导出。保存失败时也可导出可见文字，文件名会标记“未保存”。</p></>
        : <div className="draft-empty"><h3>你的经历，在这里成稿</h3><p>保存简历原文后，<br />可预览、编辑并导出 Markdown。</p></div>}
    </aside></div>
    <dialog ref={replaceDialog} onCancel={() => setReplacement(null)} aria-labelledby="replace-heading"><h2 id="replace-heading">整体替换当前稿</h2><p>新版将取代当前稿。已接受和手改的旧内容不会自动迁入，旧待处理建议失效，旧个人材料与问答转为历史；岗位和 JD 保留。</p><p>候选：{replacement?.name}。请先查看原文，确认内容完整。</p><label htmlFor="replacement-words">你的明确替换要求</label><textarea id="replacement-words" value={replacementWords} onChange={e => setReplacementWords(e.target.value)} maxLength={50000} /><label className="checkbox-label"><input type="checkbox" checked={consent} onChange={e => setConsent(e.target.checked)} />我已核对这是同人同岗位的完整新版，并明确要求整体替换。</label><div className="button-row"><button className="primary-button" disabled={!consent || !replacementWords.trim() || disabled} onClick={doReplace}>确认整体替换</button><button onClick={() => { replaceDialog.current?.close(); setReplacement(null); }}>取消</button></div></dialog>
    <dialog ref={deleteDialog} aria-labelledby="delete-heading"><h2 id="delete-heading">删除当前会话？</h2><p>本应用保存的消息、材料副本、建议和稿件将一并删除。电脑原文件不受影响，删除后不能在本应用恢复。</p><div className="button-row"><button className="danger-button" disabled={disabled} onClick={doDelete}>确认删除会话</button><button onClick={() => deleteDialog.current?.close()}>取消</button></div></dialog>
  </div>;
}
