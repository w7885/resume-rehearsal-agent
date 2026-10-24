import { useEffect, useId, useRef, useState } from 'react';
import { Icon } from './ChatIcons';
import type { ChatSession } from './types';

export function SessionHistoryRow({ session, selected, disabled, onOpen, onDelete }: {
  session: ChatSession; selected: boolean; disabled: boolean; onOpen: () => void; onDelete: () => void;
}) {
  const [open, setOpen] = useState(false);
  const row = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const id = useId();
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => { if (!row.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [open]);
  useEffect(() => { if (disabled) setOpen(false); }, [disabled]);
  return <div className={`cp-history-row ${selected ? 'cp-active' : ''}`} ref={row} onKeyDown={event => {
    if (event.key === 'Escape' && open) { event.stopPropagation(); setOpen(false); trigger.current?.focus(); }
  }}>
    <button className="cp-history-item" disabled={disabled} aria-current={selected ? 'page' : undefined} onClick={() => { setOpen(false); onOpen(); }}><Icon name="chat" size={17} /><span>{session.title}{session.mode === 'mock' ? ' · 模拟' : ''}</span></button>
    <button id={`session-actions-${session.sessionId}`} className="cp-session-more" ref={trigger} disabled={disabled} aria-label={`会话菜单：${session.title}`} aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}><Icon name="more" size={18} /></button>
    {open && <div id={id} className="cp-session-menu"><button onClick={() => { setOpen(false); onDelete(); }}><Icon name="trash" size={17} />删除会话</button></div>}
  </div>;
}

export function DeleteSessionDialog({ session, busy, error, onCancel, onConfirm }: {
  session: ChatSession; busy: boolean; error: string; onCancel: () => void; onConfirm: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => { dialog.current?.showModal(); cancel.current?.focus(); }, []);
  useEffect(() => { if (!busy) cancel.current?.focus(); }, [busy]);
  return <dialog className="cp-delete-dialog" ref={dialog} aria-labelledby="delete-session-title" aria-describedby="delete-session-description" aria-busy={busy} onCancel={event => { event.preventDefault(); if (!busy) onCancel(); }}>
    <h2 id="delete-session-title">删除会话？</h2>
    <p className="cp-delete-name">{session.title}</p>
    <p id="delete-session-description">将删除本会话的消息、附件和当前稿，无法恢复。</p>
    {error && <p className="cp-delete-error" role="alert">{error}</p>}
    <div className="cp-delete-actions"><button ref={cancel} disabled={busy} onClick={onCancel}>取消</button><button className="cp-delete-confirm" disabled={busy} onClick={onConfirm}>{busy ? '正在删除…' : '删除'}</button></div>
  </dialog>;
}
