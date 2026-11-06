import { useState, type FormEvent, type ReactNode } from 'react';
import { apiFetch, configureTransport, setAccessToken, clearAccessToken } from './authTransport';
import './auth.css';

const env = (import.meta as ImportMeta & { env?: Record<string, string> }).env ?? {};
const remote = env.VITE_APP_MODE === 'remote';
const supabase = env.VITE_SUPABASE_URL?.replace(/\/$/, '') ?? '';
const publicKey = env.VITE_SUPABASE_PUBLISHABLE_KEY ?? '';
let configurationError = false;
try { configureTransport(env.VITE_API_BASE_URL ?? '', () => {}); }
catch { configurationError = true; }
if (remote && (!env.VITE_API_BASE_URL || !publicKey || !/^https:\/\/[^/]+$/.test(supabase))) configurationError = true;

export default function AuthGate({ children }: { children: ReactNode }) {
  const [signedIn, setSignedIn] = useState(false), [email, setEmail] = useState('');
  const [password, setPassword] = useState(''), [busy, setBusy] = useState(false), [error, setError] = useState('');
  if (!remote && !configurationError) return children;
  if (configurationError) return <main className="auth-shell"><section className="auth-card"><h1>服务配置待完成</h1><p>请联系项目维护者完成部署配置后再登录。</p></section></main>;
  async function login(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      const response = await fetch(supabase + '/auth/v1/token?grant_type=password', {
        method: 'POST', headers: { 'Content-Type': 'application/json', apikey: publicKey },
        body: JSON.stringify({ email: email.trim(), password }), credentials: 'omit', redirect: 'error', signal: AbortSignal.timeout(15000),
      });
      const result = await response.json();
      if (!response.ok || typeof result.access_token !== 'string' || typeof result.expires_in !== 'number' || result.expires_in <= 0) throw new Error('邮箱或密码不正确，或账号尚未启用。');
      setAccessToken(result.access_token, result.expires_in);
      const access = await apiFetch('/api/sessions');
      if (!access.ok) { clearAccessToken(); throw new Error('账号暂未获准使用，请联系项目维护者。'); }
      setPassword(''); setSignedIn(true);
    } catch (cause) {
      setError(cause instanceof Error && cause.message.includes('账号') ? cause.message : '登录未成功，请检查邮箱、密码和网络后重试。');
    } finally { setBusy(false); }
  }
  if (signedIn) return <><div className="auth-account"><span>{email}</span><button onClick={() => {
    if (!window.confirm('退出将清除本页尚未发送的文字；已保存的会话会保留。确认退出？')) return;
    clearAccessToken(); setSignedIn(false);
  }}>退出登录</button></div>{children}</>;
  return <main className="auth-shell"><section className="auth-card" aria-labelledby="auth-title">
    <span className="auth-brand">历历在练</span><h1 id="auth-title">把经历，讲清楚。</h1>
    <p>登录后继续整理你的简历。本轮仅开放受邀账号，刷新页面后需重新登录。</p>
    <form onSubmit={login}>
      <label htmlFor="auth-email">邮箱</label><input id="auth-email" type="email" autoComplete="username" value={email} onChange={event => setEmail(event.target.value)} required disabled={busy} />
      <label htmlFor="auth-password">密码</label><input id="auth-password" type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} required disabled={busy} />
      {error && <p className="auth-error" role="alert">{error}</p>}
      <button className="auth-submit" disabled={busy} type="submit">{busy ? '正在核对账号…' : '登录工作台'}</button>
    </form><p className="auth-privacy">材料与对话按账号保存。启用 AI 分析时，相关内容会发送至配置的模型服务；封闭测试阶段请使用脱敏材料。</p>
  </section></main>;
}
