import { useEffect, useRef, useState } from 'react';
import { Icon } from './ChatIcons';
import * as api from './api';
import MaterialText, { sourcePosition as position } from './MaterialText';
import AttachmentContent from './AttachmentContent';
import AttachmentLibrary from './AttachmentLibrary';
import type { ChatMaterial, ChatMessage, Conversation, Detail, Source, SourceRef } from './types';

export type Inspection = ChatMaterial | 'library' | 'evidence' | {replyMessage:ChatMessage};
const errorText = (error: unknown) => error instanceof Error ? error.message : '读取失败，请重试。';

function Evidence({ sid, reference }: { sid: string; reference: SourceRef }) {
  const [open, setOpen] = useState(false), [loading, setLoading] = useState(false), [error, setError] = useState('');
  const [value, setValue] = useState<{ source: Source; historical: boolean }>();
  const load = async () => {
    if (loading) return;
    setOpen(true); setLoading(true); setError('');
    try { setValue(await api.readSource(sid, reference)); } catch (failure) { setError(errorText(failure)); }
    finally { setLoading(false); }
  };
  return <div className="cp-evidence"><blockquote>{reference.quote}</blockquote><button aria-expanded={open} onClick={() => { if (open && !error) setOpen(false); else void load(); }}>依据原文 · 版本 {reference.sourceRevision}</button>
    {open && <div>{loading && <p role="status">正在读取引用的确切版本…</p>}{error && <p role="alert">{error}<button onClick={() => { void load(); }}>重试读取</button></p>}{value && <><p>{value.historical ? '非当前有效依据 · 历史或待确认，仅供回顾' : '当前有效来源'} · {position(value.source)}</p><pre>{value.source.text}</pre></>}</div>}
  </div>;
}

function DraftEvidence({ conversation, unsaved }: { conversation: Conversation; unsaved: boolean }) {
  const [detail, setDetail] = useState<Detail | null>(null), [error, setError] = useState(''), [loading, setLoading] = useState(true), [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let live = true; setLoading(true); setError('');
    void api.readSession(conversation.session.sessionId).then(value => { if (live) setDetail(value); }).catch(failure => { if (live) setError(errorText(failure)); }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [conversation.session.sessionId, attempt]);
  const history = detail?.suggestions.filter(suggestion => suggestion.decision) ?? [];
  return <div className="cp-evidence-list">
    <p>{unsaved ? '正文有未保存修改。以下仅对应最近已保存稿，不代表本地新文字已有依据。' : '以下依据对应最近已保存稿。手动修改的文字不会自动继承旧引用。'}</p>
    {conversation.draft.blocks.some(block => block.sourceRefs.length) ? conversation.draft.blocks.filter(block => block.sourceRefs.length).map(block => <section key={`${block.blockId}-${block.blockRevision}`}><h3>已保存段落</h3><pre>{block.markdown}</pre>{block.sourceRefs.map((reference, index) => <Evidence key={index} sid={conversation.session.sessionId} reference={reference} />)}</section>) : <p>当前已保存稿没有关联的修改依据。保存正文不等于完成事实核验。</p>}
    {loading && <p role="status">正在读取历史修改记录…</p>}{error && <p role="alert">{error}<button onClick={() => setAttempt(value => value + 1)}>重试读取历史</button></p>}
    {!!history.length && <section><h3>此前的修改记录</h3><p>仅回顾已记录的决定，不会再次执行。</p>{history.map(suggestion => <details key={suggestion.suggestionId}><summary>{suggestion.decision?.decision === 'accept' ? '已接受' : suggestion.decision?.decision === 'reject' ? '已拒绝' : '曾要求调整'}的修改 · 历史记录</summary><p>{suggestion.reason}</p><p>原文</p><pre>{suggestion.before}</pre><p>当时建议</p><pre>{suggestion.after}</pre>{suggestion.decision?.feedback && <p>当时反馈：{suggestion.decision.feedback}</p>}{suggestion.sourceRefs.map((reference, index) => <Evidence key={index} sid={conversation.session.sessionId} reference={reference} />)}</details>)}</section>}
  </div>;
}

export default function ChatInspector({ inspecting, conversation, unsaved, disabled, attached, onAdd, onSelect, onClose, beforeChange, onChanged }: {
  inspecting: Inspection; conversation: Conversation; unsaved: boolean; disabled: boolean; attached: string[];
  onAdd: (material: ChatMaterial) => void; onSelect: (inspection: Inspection) => void; onClose: () => void;
  beforeChange: () => Promise<void>; onChanged: () => Promise<void>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [guarded, setGuarded] = useState(false);
  const [correctionId, setCorrectionId] = useState('');
  useEffect(() => { dialog.current?.showModal(); }, []);
  const reply = typeof inspecting === 'object' && 'replyMessage' in inspecting ? inspecting.replyMessage : null;
  const material = typeof inspecting === 'object' && !('replyMessage' in inspecting) ? inspecting : null;
  const correcting = conversation.materials.find(item => item.materialId === correctionId);
  return <dialog className={`cp-inspector ${material ? 'cp-attachment-modal' : ''}`} ref={dialog} aria-labelledby="chat-inspector-title" onCancel={event => { if (guarded) event.preventDefault(); else onClose(); }}><header><h2 id="chat-inspector-title">{inspecting === 'library' ? '本会话已保存附件' : inspecting === 'evidence' ? '修改依据与记录' : reply ? '回复依据' : material?.name}</h2>{material && <a className="cp-attachment-download" href={api.fileUrl(conversation.session.sessionId, material.materialId)} download>下载</a>}<button className="cp-icon" aria-label="关闭详情" disabled={guarded} onClick={onClose}><Icon name="close" /></button></header>
    {inspecting === 'library' ? <AttachmentLibrary conversation={conversation} disabled={disabled} attached={attached} onAdd={onAdd} onSelect={onSelect} onGuard={setGuarded} beforeChange={beforeChange} onChanged={onChanged} />
      : inspecting === 'evidence' ? <><DraftEvidence key={conversation.session.sessionId} conversation={conversation} unsaved={unsaved} />
        {!!conversation.materials.length && <details className="cp-correction-tools"><summary>修正材料文字</summary><label className="cp-sr-only" htmlFor="correction-material">选择待核对材料</label><select id="correction-material" value={correctionId} disabled={guarded} onChange={event => setCorrectionId(event.target.value)}><option value="">选择材料</option>{conversation.materials.map(item => <option key={item.materialId} value={item.materialId}>{item.name}</option>)}</select>{correcting && <MaterialText key={correcting.materialId} sid={conversation.session.sessionId} id={correcting.materialId} editable={!disabled && conversation.session.mode === 'local' && correcting.current} onGuard={setGuarded} beforeChange={beforeChange} onChanged={onChanged} />}</details>}
      </>
        : reply ? <div className="cp-evidence-list"><p>以下为这条回复当时关联的原文。</p>{reply.assistant?.sourceRefs.map((reference,index) => <Evidence key={index} sid={conversation.session.sessionId} reference={reference} />)}</div>
        : material && <AttachmentContent key={material.materialId} sid={conversation.session.sessionId} material={material} />}

  </dialog>;
}
