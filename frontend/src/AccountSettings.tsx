import { useEffect, useRef, useState } from 'react';
import type { AccountAuth } from './accountAuth';
import { readProfile, type AccountProfile } from './accountSession';
import { apiFetch } from './authTransport';
import FeedbackForm from './FeedbackForm';

type Props = {
  profile: AccountProfile;
  account: AccountAuth;
  onClose: () => void;
  onSignOut: () => void;
  onUpdate: (profile: AccountProfile) => void;
  onOpenAdmin: () => void;
};

export default function AccountSettings({ profile, account, onClose, onSignOut, onUpdate, onOpenAdmin }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const lock = useRef(false);
  const [load, setLoad] = useState<'loading' | 'ready' | 'failed'>('loading');
  const [reload, setReload] = useState(0);
  const [pending, setPending] = useState(false);
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [feedbackPending, setFeedbackPending] = useState(false);
  const [feedback, setFeedback] = useState<{ text: string; error?: boolean } | null>(null);
  useEffect(() => {
    if (!dialog.current?.open) dialog.current?.showModal();
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const response = await apiFetch('/api/me', { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]) });
        if (!response.ok) throw new Error();
        const result = await readProfile(response);
        if (controller.signal.aborted) return;
        onUpdate(result);
        setLoad('ready');
      } catch {
        if (!controller.signal.aborted) setLoad('failed');
      }
    })();
    return () => controller.abort();
  }, [reload, onUpdate]);

  async function sendRecoveryEmail() {
    if (lock.current || load !== 'ready') return;
    lock.current = true;
    setPending(true);
    setFeedback(null);
    try {
      await account.recover(profile.email);
      setFeedback({ text: '重置邮件已申请，请查收邮箱。' });
    } catch (error) {
      setFeedback({ text: error instanceof Error ? error.message : '操作未完成，请稍后重试。', error: true });
    } finally {
      lock.current = false;
      setPending(false);
    }
  }

  return <dialog ref={dialog} className="auth-card account-settings" aria-labelledby="account-settings-title"
    onClose={onClose} onCancel={event => { if (lock.current || feedbackPending) event.preventDefault(); }}>
    <header>
      <h2 id="account-settings-title">{feedbackOpen ? '提交反馈' : '设置'}</h2>
      <button type="button" disabled={pending || feedbackPending}
        onClick={() => feedbackOpen ? setFeedbackOpen(false) : dialog.current?.close()}>{feedbackOpen ? '返回设置' : '关闭'}</button>
    </header>
    {feedbackOpen ? <FeedbackForm onBusy={setFeedbackPending} /> : <>
    {load === 'loading' && <p role="status">正在读取账号资料…</p>}
    {load === 'failed' && <div className="auth-actions">
      <p className="auth-error" role="alert">暂时无法读取账号资料。</p>
      <button type="button" onClick={() => { setLoad('loading'); setReload(value => value + 1); }}>重试</button>
    </div>}
    <dl className="account-settings-profile">
      <dt>账号邮箱</dt>
      <dd>{profile.email}</dd>
      <dd className="account-settings-verification">{profile.emailVerified ? '邮箱已验证' : '邮箱尚未验证'}</dd>
    </dl>
    <div className="auth-actions account-settings-actions">
      <button type="button" disabled={load !== 'ready' || pending} onClick={() => { void sendRecoveryEmail(); }}>
        {pending ? '正在发送…' : '发送改密邮件'}
      </button>
      <button type="button" disabled={pending} onClick={onSignOut}>退出登录</button>
      <button type="button" disabled={pending} onClick={() => setFeedbackOpen(true)}>提交反馈</button>
      {profile.isAdmin && <button type="button" disabled={pending} onClick={onOpenAdmin}>管理后台</button>}
    </div>
    {feedback && <p className={feedback.error ? 'auth-error' : 'auth-notice'} role={feedback.error ? 'alert' : 'status'}>{feedback.text}</p>}
    </>}
  </dialog>;
}
