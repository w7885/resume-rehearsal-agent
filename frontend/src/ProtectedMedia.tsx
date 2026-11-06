import { useEffect, useState } from 'react';
import { apiFetch } from './authTransport';

export function ProtectedImage({ url, onError }: { url: string; onError: () => void }) {
  const [source, setSource] = useState('');
  useEffect(() => {
    const controller = new AbortController(); let objectUrl = '', live = true;
    setSource('');
    void apiFetch(url, { signal: controller.signal, cache: 'no-store' }).then(async response => {
      if (!response.ok) throw new Error('附件读取失败');
      const blob = await response.blob();
      if (!live) return;
      objectUrl = URL.createObjectURL(blob); setSource(objectUrl);
    }).catch(() => { if (live) onError(); });
    return () => { live = false; controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
    // A new inline callback must not restart a successful image request.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url]);
  return source ? <img src={source} alt="附件内容" onError={onError} /> : null;
}

export function ProtectedDownload({ url, name }: { url: string; name: string }) {
  const [busy, setBusy] = useState(false), [error, setError] = useState(false);
  async function download() {
    setBusy(true); setError(false);
    try {
      const response = await apiFetch(url, { signal: AbortSignal.timeout(20000), cache: 'no-store' });
      if (!response.ok) throw new Error('附件读取失败');
      const objectUrl = URL.createObjectURL(await response.blob());
      const link = document.createElement('a'); link.href = objectUrl; link.download = name;
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    } catch { setError(true); }
    finally { setBusy(false); }
  }
  return <span><button className="cp-attachment-download" onClick={() => void download()} disabled={busy}>{busy ? '读取中…' : '下载'}</button>{error && <span role="alert">下载未成功，请重试。</span>}</span>;
}
