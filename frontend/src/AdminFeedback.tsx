import { useEffect, useRef, useState, type FormEvent } from 'react';
import { categories, feedbackRequest, jsonBody, statuses, type Category, type Status, type FeedbackDetail, type FeedbackItem } from './feedbackApi';
import './feedback.css';

const formatDate = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', dateStyle: 'medium', timeStyle: 'short',
});
const endpoint = '/api/admin/feedback';

export default function AdminFeedback({ onDenied, initialStatus = '' }: { onDenied: () => void; initialStatus?: Status | '' }) {
  const [category, setCategory] = useState<Category | ''>('');
  const [status, setStatus] = useState<Status | ''>(initialStatus);
  const [page, setPage] = useState(1);
  const [refresh, setRefresh] = useState(0);
  const [rows, setRows] = useState<{ items: FeedbackItem[]; hasMore: boolean } | null>(null);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams({ page: String(page) });
    if (category) params.set('category', category);
    if (status) params.set('status', status);
    setRows(null);
    setError('');
    feedbackRequest<{ items: FeedbackItem[]; hasMore: boolean }>(endpoint + '?' + params,
      '暂时无法读取反馈列表。', { signal: controller.signal }, onDenied).then(
      data => { if (!controller.signal.aborted) setRows(data); },
      () => { if (!controller.signal.aborted) setError('暂时无法读取反馈列表，请重试。'); },
    );
    return () => controller.abort();
  }, [category, status, page, refresh, onDenied]);

  if (selected) return <FeedbackEditor key={selected} id={selected} onDenied={onDenied}
    onBack={() => { setSelected(null); setRefresh(value => value + 1); }} />;

  return <section aria-label="反馈列表">
    <div className="feedback-filters">
      <label>反馈类型 <select value={category} onChange={event => { setCategory(event.target.value as Category | ''); setPage(1); }}>
        <option value="">全部类型</option>
        {Object.entries(categories).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select></label>
      <label>处理状态 <select value={status} onChange={event => { setStatus(event.target.value as Status | ''); setPage(1); }}>
        <option value="">全部状态</option>
        {Object.entries(statuses).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select></label>
    </div>
    {error ? <><p className="auth-error" role="alert">{error}</p>
      <button type="button" onClick={() => setRefresh(value => value + 1)}>重试</button></>
      : !rows ? <p role="status">正在读取反馈…</p>
      : !rows.items.length ? <p>暂无符合条件的反馈。</p>
      : <ul className="feedback-list">{rows.items.map(item => <li key={item.id}>
        <div><strong>{categories[item.category]} · {statuses[item.status]}</strong>
          <time dateTime={item.createdAt}>{formatDate.format(new Date(item.createdAt))}</time>
          <p>{item.summary}</p></div>
        <button type="button" onClick={() => setSelected(item.id)}>查看详情</button>
      </li>)}</ul>}
    <div className="auth-actions">
      <button type="button" disabled={page <= 1} onClick={() => setPage(value => value - 1)}>上一页</button>
      <span>第 {page} 页</span>
      <button type="button" disabled={!rows?.hasMore} onClick={() => setPage(value => value + 1)}>下一页</button>
    </div>
  </section>;
}

function FeedbackEditor({ id, onDenied, onBack }: { id: string; onDenied: () => void; onBack: () => void }) {
  const [detail, setDetail] = useState<FeedbackDetail | null>(null);
  const [status, setStatus] = useState<Status>('pending');
  const [note, setNote] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const locked = useRef(false);
  const path = endpoint + '/' + id;
  useEffect(() => {
    const controller = new AbortController();
    setError('');
    setSaved(false);
    setDetail(null);
    feedbackRequest<FeedbackDetail>(path, '暂时无法读取反馈详情。', { signal: controller.signal }, onDenied).then(
      data => { if (!controller.signal.aborted) { setDetail(data); setStatus(data.status); setNote(data.note); } },
      cause => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : '读取失败。'); },
    );
    return () => controller.abort();
  }, [path, refresh, onDenied]);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (locked.current || !detail) return;
    locked.current = true;
    setBusy(true);
    setError('');
    setSaved(false);
    try {
      const data = await feedbackRequest<FeedbackDetail>(path, '保存失败，请重试。',
        jsonBody('PATCH', { status, note, revision: detail.revision }), onDenied);
      setDetail(data);
      setNote(data.note);
      setSaved(true);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '保存失败，请重试。');
    } finally {
      setBusy(false);
      locked.current = false;
    }
  }

  return <section aria-labelledby="feedback-detail-title">
    <div className="auth-actions"><button type="button" disabled={busy} onClick={onBack}>返回反馈列表</button></div>
    <h2 id="feedback-detail-title">反馈详情</h2>
    {error && <><p className="auth-error" role="alert">{error}</p>
      <button type="button" disabled={busy} onClick={() => {
        if (detail && (status !== detail.status || note !== detail.note)
          && !window.confirm('重新读取会清除尚未保存的修改，是否继续？')) return;
        setRefresh(value => value + 1);
      }}>重新读取详情</button></>}
    {!detail && !error && <p role="status">正在读取详情…</p>}
    {detail && <>
      <p>{categories[detail.category]} · {formatDate.format(new Date(detail.createdAt))}</p>
      <p>提交者邮箱：{detail.submitterEmail}</p>
      <p className="feedback-text">{detail.description}</p>
      <form onSubmit={save}>
        <label htmlFor="feedback-status">处理状态</label>
        <select id="feedback-status" disabled={busy} value={status} onChange={event => { setStatus(event.target.value as Status); setSaved(false); }}>
          {Object.entries(statuses).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
        </select>
        <label htmlFor="feedback-note">内部备注</label>
        <textarea id="feedback-note" rows={4} maxLength={2000} value={note}
          disabled={busy} onChange={event => { setNote(event.target.value); setSaved(false); }} />
        <button type="submit" className="auth-submit" disabled={busy || (status === detail.status && note.trim() === detail.note)}>保存处理结果</button>
        {saved && <p role="status" className="auth-notice">处理结果已保存。</p>}
      </form>
      <h3>处理记录</h3>
      {detail.history.length ? <ul className="feedback-list">{detail.history.map((entry, index) => <li key={detail.revision - index}>
        <div><strong>{entry.oldStatus === entry.newStatus ? '更新内部备注' : statuses[entry.oldStatus] + ' → ' + statuses[entry.newStatus]}</strong>
          <time dateTime={entry.changedAt}>{formatDate.format(new Date(entry.changedAt))}</time>
          <p>操作者：{entry.actorId}</p>{entry.note && <p className="feedback-text">{entry.note}</p>}</div>
      </li>)}</ul> : <p>暂无处理记录。</p>}
    </>}
  </section>;
}
