import { useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import * as api from './api';
import { attachmentText, AttachmentImagePreview } from './attachmentPreview';
import MarkdownPreview from './MarkdownPreview';
import type { ChatMaterial } from './types';

function ImageContent({ sid, material }: { sid: string; material: ChatMaterial }) {
  const operation = useMemo(() => new AttachmentImagePreview(sid, material.materialId), [sid, material.materialId]);
  const value = useSyncExternalStore(operation.subscribe, operation.getSnapshot);
  const [busy, setBusy] = useState(true), [error, setError] = useState(false), [attempt, setAttempt] = useState(0);
  const [broken, setBroken] = useState(false);
  useEffect(() => {
    let live = true; setBusy(true); setError(false); setBroken(false);
    void operation.run().catch(() => { if (live) setError(true); }).finally(() => { if (live) setBusy(false); });
    return () => { live = false; operation.pause(); };
  }, [operation, attempt]);
  const incomplete = !busy && value && (value.pages.some(page => page.status !== 'ready') || (value.pageCount ?? 0) > value.limit);
  return <div className="cp-attachment-content">
    {value?.pages.filter(page => page.status === 'ready').map(page => <img key={`${page.page}-${attempt}`} src={`${api.materialPageUrl(sid, material.materialId, page.page)}?view=${attempt}`} alt="附件内容" onError={() => setBroken(true)} />)}
    {busy && <p className="cp-attachment-loading" role="status">正在打开附件…</p>}
    {(error || incomplete || broken) && <div className="cp-attachment-unavailable" role="alert"><p>部分内容暂时无法显示，可重试或下载原文件查看。</p><button onClick={() => setAttempt(value => value + 1)}>重试</button></div>}
  </div>;
}

function TextContent({ sid, material }: { sid: string; material: ChatMaterial }) {
  const [text, setText] = useState(''), [busy, setBusy] = useState(true), [unavailable, setUnavailable] = useState(false), [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let live = true; setBusy(true); setUnavailable(false);
    void api.readMaterial(sid, material.materialId).then(value => {
      if (!live) return;
      const content = attachmentText(value.sources); setText(content);
      setUnavailable(!content.trim() || value.material.parseStatus === 'partial' || value.material.parseStatus === 'failed');
    }).catch(() => { if (live) setUnavailable(true); }).finally(() => { if (live) setBusy(false); });
    return () => { live = false; };
  }, [sid, material.materialId, attempt]);
  return <div className="cp-attachment-content">{busy && <p className="cp-attachment-loading" role="status">正在打开附件…</p>}
    {text && (material.mediaType === 'text/markdown' ? <MarkdownPreview markdown={text} /> : <div className="cp-attachment-text">{text}</div>)}
    {!busy && unavailable && <div className="cp-attachment-unavailable" role="alert"><p>部分内容暂时无法显示，可下载原文件查看。</p><button onClick={() => setAttempt(value => value + 1)}>重试</button></div>}
  </div>;
}

export default function AttachmentContent({ sid, material }: { sid: string; material: ChatMaterial }) {
  return ['application/pdf', 'image/png', 'image/jpeg'].includes(material.mediaType) ? <ImageContent sid={sid} material={material} /> : <TextContent sid={sid} material={material} />;
}
