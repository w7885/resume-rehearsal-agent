import { useEffect, useRef, useState, type FormEvent } from 'react';
import { AccountRequestError, feedbackRequest, jsonBody } from './feedbackApi';

type Account = { email: string; createdAt: string; emailVerified: boolean };
const dateTime = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', dateStyle: 'medium', timeStyle: 'short',
});

export default function AdminAccounts({ onDenied }: { onDenied: () => void }) {
  const [email, setEmail] = useState('');
  const [account, setAccount] = useState<Account | null>(null);
  const [outcome, setOutcome] = useState<'idle' | 'loading' | 'missing' | 'error' | 'invalid'>('idle');
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);

  async function lookup(event: FormEvent) {
    event.preventDefault();
    if (active.current) return;
    const controller = new AbortController();
    active.current = controller;
    setAccount(null);
    setOutcome('loading');
    try {
      const data = await feedbackRequest<Account>('/api/admin/accounts/lookup', '暂时无法查询账号，请稍后重试。',
        { ...jsonBody('POST', { email: email.trim() }), signal: controller.signal }, onDenied);
      if (!controller.signal.aborted) { setAccount(data); setOutcome('idle'); }
    } catch (error) {
      if (controller.signal.aborted) return;
      const status = error instanceof AccountRequestError ? error.status : undefined;
      setOutcome(status === 404 ? 'missing' : status === 422 ? 'invalid' : 'error');
    } finally {
      active.current = null;
    }
  }

  return <section className="admin-account-query">
    <form onSubmit={lookup}>
      <label htmlFor="admin-account-email">完整邮箱</label>
      <input id="admin-account-email" type="email" required maxLength={254} autoComplete="off"
        value={email} disabled={outcome === 'loading'} onChange={event => {
          setEmail(event.target.value); setOutcome('idle'); setAccount(null);
        }} />
      <button type="submit" className="auth-submit" disabled={outcome === 'loading'}>查询账号</button>
    </form>
    {outcome === 'loading' && <p role="status">正在查询账号…</p>}
    {outcome === 'missing' && <p role="status">未找到该邮箱对应的账号。</p>}
    {outcome === 'invalid' && <p role="alert" className="auth-error">请输入完整邮箱。</p>}
    {outcome === 'error' && <p role="alert" className="auth-error">暂时无法查询账号，请稍后重试。</p>}
    {account && <dl className="admin-account-result" aria-label="账号查询结果">
      <dt>邮箱</dt><dd>{account.email}</dd>
      <dt>注册时间（北京时间）</dt><dd><time dateTime={account.createdAt}>{dateTime.format(new Date(account.createdAt))}</time></dd>
      <dt>邮箱验证</dt><dd>{account.emailVerified ? '已验证' : '未验证'}</dd>
    </dl>}
  </section>;
}
