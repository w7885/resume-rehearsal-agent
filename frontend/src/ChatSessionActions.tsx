import { useEffect, useId, useRef, useState } from 'react';
import { Icon } from './ChatIcons';
import type { ChatSession } from './types';
import { sessionTitleError } from './chatRename';

export function SessionHistoryRow({ session, selected, disabled, renameAvailable, onOpen, onDelete, onRename }: {
  session: ChatSession; selected: boolean; disabled: boolean; renameAvailable: boolean; onOpen: () => void; onDelete: () => void; onRename: () => void;
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
    {open && <div id={id} className="cp-session-menu"><button className="cp-session-rename" title={renameAvailable ? undefined : '当前后端尚未加载重命名功能'} disabled={!renameAvailable || session.status === 'running'} onClick={() => { setOpen(false); onRename(); }}><Icon name="edit" size={17} />重命名</button><button onClick={() => { setOpen(false); onDelete(); }}><Icon name="trash" size={17} />删除会话</button></div>}
  </div>;
}

export function RenameSessionDialog({ session, value, busy, locked, error, onChange, onCancel, onConfirm }: {
  session: ChatSession; value: string; busy: boolean; locked: boolean; error: string;
  onChange: (value: string) => void; onCancel: () => void; onConfirm: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const id = useId();
  const validation = sessionTitleError(value);
  useEffect(() => { dialog.current?.showModal(); input.current?.focus(); input.current?.select(); }, []);
  return <dialog className="cp-delete-dialog cp-rename-dialog" ref={dialog} aria-labelledby={`${id}-title`} aria-busy={busy} onCancel={event => { event.preventDefault(); if (!busy) onCancel(); }}>
    <form onSubmit={event => { event.preventDefault(); if (!busy && !validation && session.status !== 'running') onConfirm(); }}>
      <h2 id={`${id}-title`}>重命名会话</h2>
      <label htmlFor={`${id}-name`}>会话名称</label>
      <input id={`${id}-name`} ref={input} value={value} maxLength={100} disabled={busy || locked} autoComplete="off" aria-invalid={!!validation} aria-describedby={`${id}-help ${id}-error`} onChange={event => onChange(event.target.value)} />
      <p id={`${id}-help`} className="cp-rename-help">{Array.from(value.trim()).length}/50字，仅修改会话名称。</p>
      <p className="cp-rename-current">当前名称：{session.title}</p>
      <p id={`${id}-error`} className="cp-delete-error" role={error || validation ? 'alert' : undefined}>{error || validation}</p>
      {locked && !busy && <p className="cp-rename-help">保存结果尚未确认，可重试；取消不会撤销可能已保存的名称。</p>}
      <div className="cp-delete-actions"><button type="button" disabled={busy} onClick={onCancel}>取消</button><button type="submit" className="cp-rename-confirm" disabled={busy || !!validation || session.status === 'running' || (!locked && value.trim() === session.title)}>{busy ? '正在保存…' : locked ? '重试保存' : '保存'}</button></div>
    </form>
  </dialog>;
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
