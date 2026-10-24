import { useState } from 'react';
import { Icon } from './ChatIcons';
import MarkdownPreview from './MarkdownPreview';
import type { BufferSnapshot } from './draftBuffer';
import type { ChatMaterial, Conversation } from './types';

const STATUS = { saved: '已保存', dirty: '未保存', saving: '保存中…', error: '未保存 · 保存失败', conflict: '未保存 · 版本冲突' };
export default function ChatDraftPanel({ conversation, snapshot, disabled, onChange, onSave, onRecover, onClose, onEvidence, onOriginalMaterial }: {
  conversation: Conversation | null; snapshot: BufferSnapshot; disabled: boolean;
  onChange: (text: string) => void; onSave: () => void; onRecover: (keep: boolean) => void; onClose: () => void; onEvidence: () => void;
  onOriginalMaterial: (material: ChatMaterial) => void;
}) {
  const [view, setView] = useState<'original' | 'edit' | 'preview'>('edit'), [notice, setNotice] = useState('');
  const mock = conversation?.session.mode === 'mock';
  const original = view === 'original';
  const baseline = conversation?.originalDraft;
  const hasOriginal = !!baseline && baseline.sessionId === conversation?.session.sessionId && baseline.resumeGeneration === conversation?.session.currentResumeGeneration;
  const text = original ? hasOriginal ? baseline.markdown : '' : mock ? conversation.draft.markdown : snapshot.text;
  const unsaved = !original && !mock && snapshot.status !== 'saved';
  const originals = conversation?.materials.filter(material => hasOriginal && baseline.materialIds.includes(material.materialId)) ?? [];
  const switchView = (next: typeof view) => { setView(next); setNotice(''); };
  const copy = async () => {
    try { await navigator.clipboard.writeText(text); setNotice(unsaved ? '已复制可见正文，修改仍未保存。' : original ? '已复制初版简历。' : '已复制当前稿。'); }
    catch { setNotice('剪贴板不可用，请选中文字复制，或下载 Markdown。'); }
  };
  const download = () => {
    const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' }));
    const link = document.createElement('a'); link.href = url;
    link.download = `${mock ? '模拟示例-' : ''}${original ? '初版简历' : '当前简历'}${unsaved ? '-未保存' : ''}.md`;
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    setNotice(unsaved ? '已导出可见正文，文件名标记“未保存”。' : original ? '已导出初版简历正文。' : '已导出当前稿正文。');
  };
  return <><button className="cp-draft-shade" aria-label="关闭当前稿" onClick={onClose} /><aside className="cp-draft cp-editable-draft" aria-label="当前稿编辑与预览">
    <header className="cp-draft-header"><div><h2>当前稿</h2><span role="status">{!conversation ? '尚未创建会话' : original ? '初版 · 只读' : mock ? '模拟历史 · 只读' : STATUS[snapshot.status]}</span></div><button className="cp-icon" aria-label="收起当前稿" onClick={onClose}><Icon name="close" size={18} /></button></header>
    <div className="cp-draft-tools"><div className="cp-draft-tabs"><button aria-pressed={original} onClick={() => switchView('original')}>初版简历</button>{mock ? <button aria-pressed={!original} onClick={() => switchView('preview')}>只读预览</button> : <><button aria-pressed={view === 'edit'} onClick={() => switchView('edit')}>编辑</button><button aria-pressed={view === 'preview'} onClick={() => switchView('preview')}>预览</button></>}</div><div><button disabled={!conversation || original && !hasOriginal} onClick={() => { void copy(); }}>复制</button><button disabled={!conversation || original && !hasOriginal} onClick={download}>下载</button></div></div>
    <div className="cp-document-scroll">
      {!conversation ? <div className="cp-empty-paper"><Icon name="panel" size={27} /><p>从一条消息开始</p><span>创建对话后，可在这里编写当前稿。</span></div>
        : original && !hasOriginal ? <div className="cp-empty-paper"><Icon name="panel" size={27} /><p>暂无初版简历</p><span>本会话没有可靠的首次导入记录。</span></div>
          : original || view === 'preview' || mock ? <div className="cp-paper" aria-label={original ? '初版简历正文' : '当前稿预览'}><MarkdownPreview markdown={text} /></div>
          : <><label className="cp-sr-only" htmlFor="chat-draft-editor">当前稿 Markdown 正文</label><textarea id="chat-draft-editor" className="cp-draft-editor" value={text} onChange={event => { onChange(event.target.value); setNotice(''); }} maxLength={50000} disabled={disabled} spellCheck={false} placeholder="在这里编写或粘贴你的简历正文…" /></>}
    </div>
    <div className="cp-draft-save-area">
      {snapshot.status === 'error' && !mock && !original && <div className="cp-live-error" role="alert"><p>{snapshot.error} 文字仍保留，可重试保存或导出。</p><button disabled={disabled} onClick={onSave}>重试保存</button></div>}
      {snapshot.status === 'conflict' && !mock && !original && <div className="cp-live-error" role="alert"><p>服务器版本已变化，本地文字仍保留，请选择保留哪一份。</p>{snapshot.remote && <details><summary>查看服务器稿</summary><pre>{snapshot.remote.draft.markdown}</pre></details>}<div><button disabled={disabled} onClick={() => onRecover(true)}>保留本地文字并保存</button><button disabled={disabled} onClick={() => onRecover(false)}>采用服务器稿</button></div></div>}
      {notice && <p className="cp-draft-notice" role="status">{notice}</p>}
      {!original && conversation?.draftReviewRequired && <p className="cp-draft-review" role="status">部分表述存在未决冲突或依据已更新，交付前请核对事实。下载不会消除这一提示。</p>}
      {conversation && <div className="cp-draft-bottom-actions">{original ? originals.map(material => <button key={material.materialId} onClick={() => onOriginalMaterial(material)}>{originals.length === 1 ? '查看上传原件' : `查看原件：${material.name}`}</button>) : <><button onClick={onEvidence}>查看修改依据</button>{!mock && <button disabled={disabled || snapshot.status === 'conflict'} onClick={onSave}>立即保存</button>}</>}</div>}
      <p className="cp-draft-help">{original ? '初版只读。下载仅含当前可见正文。' : mock ? '模拟示例仅供回顾。' : '停止输入后自动保存。下载仅含当前可见正文。'}</p>
    </div>
    <footer className="cp-draft-footer"><span className="cp-small-dot" />{conversation?.session.directionStatus === 'pending' ? '求职方向待明确' : conversation?.session.targetDirection ?? '尚未创建会话'}</footer>
  </aside></>;
}
