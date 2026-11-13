import { useRef, useState, type FormEvent } from 'react';
import { categories, feedbackRequest, jsonBody, type Category } from './feedbackApi';
import './feedback.css';

export default function FeedbackForm({ onBusy }: { onBusy: (busy: boolean) => void }) {
  const [category, setCategory] = useState<Category>('bug');
  const [description, setDescription] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const [sent, setSent] = useState(false);
  const submission = useRef<{ category: Category; description: string; submissionId: string } | null>(null);
  const locked = useRef(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (locked.current || !description.trim()) return;
    locked.current = true;
    setPending(true);
    onBusy(true);
    setError('');
    setSent(false);
    const message = description.trim();
    if (!submission.current || submission.current.category !== category || submission.current.description !== message) {
      submission.current = { category, description: message, submissionId: crypto.randomUUID() };
    }
    try {
      await feedbackRequest('/api/feedback', '暂时无法提交反馈，请重试。', jsonBody('POST', submission.current));
      submission.current = null;
      setDescription('');
      setSent(true);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '暂时无法提交反馈，请重试。');
    } finally {
      locked.current = false;
      setPending(false);
      onBusy(false);
    }
  }

  return <form onSubmit={submit}>
    <label htmlFor="feedback-category">反馈类型</label>
    <select id="feedback-category" value={category} disabled={pending} onChange={event => setCategory(event.target.value as Category)}>
      {Object.entries(categories).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
    </select>
    <label htmlFor="feedback-description">反馈描述</label>
    <textarea id="feedback-description" rows={5} maxLength={2000} required value={description}
      disabled={pending} onChange={event => { setDescription(event.target.value); setSent(false); }} />
    {error && <p className="auth-error" role="alert">{error}</p>}
    {sent && <p className="auth-notice" role="status">反馈已收到。</p>}
    <button className="auth-submit" type="submit" disabled={pending || !description.trim()}>{pending ? '正在提交…' : '提交反馈'}</button>
  </form>;
}
