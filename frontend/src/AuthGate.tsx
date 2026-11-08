import { useRef, useState, type FormEvent, type ReactNode } from 'react';
import { apiFetch, configureTransport, setAccessToken, clearAccessToken } from './authTransport';
import './auth.css';
import { AccountAuth, consumeAuthCallback, type Recovery } from './accountAuth';

const env = (import.meta as ImportMeta & { env?: Record<string, string> }).env ?? {};
const remote = env.VITE_APP_MODE === 'remote';
const supabase = env.VITE_SUPABASE_URL?.replace(/\/$/, '') ?? '';
const publicKey = env.VITE_SUPABASE_PUBLISHABLE_KEY ?? '';
let configurationError = false;
const openRegistration = env.VITE_AUTH_ACCESS_MODE === 'open';
if (env.VITE_AUTH_ACCESS_MODE && !['open', 'invited'].includes(env.VITE_AUTH_ACCESS_MODE)) configurationError = true;
try { configureTransport(env.VITE_API_BASE_URL ?? '', () => {}); }
catch { configurationError = true; }
if (remote && (!env.VITE_API_BASE_URL || !publicKey || !/^https:\/\/[^/]+$/.test(supabase))) configurationError = true;
let account: AccountAuth | undefined;
const callback = remote ? consumeAuthCallback(window.location.href, url => window.history.replaceState(null, '', url)) : { notice:'' };
if (remote && !configurationError) {
  try { account = new AccountAuth(supabase, publicKey, window.location.origin); }
  catch { configurationError = true; }
}

export default function AuthGate({ children }: { children: ReactNode }) {
  const [signedIn, setSignedIn] = useState(false), [email, setEmail] = useState('');
  const [password, setPassword] = useState(''), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [confirmation, setConfirmation] = useState(''), [notice, setNotice] = useState(callback.notice);
  const [recovery, setRecovery] = useState<Recovery | undefined>('recovery' in callback ? callback.recovery : undefined);
  const [mode, setMode] = useState<'login' | 'signup' | 'recover' | 'reset'>('recovery' in callback && callback.recovery ? 'reset' : 'login');
  const locked = useRef(false);
  function changeMode(next: typeof mode) {
    if (locked.current) return;
    setMode(next); setError(''); setNotice(''); setPassword(''); setConfirmation(''); setRecovery(undefined);
  }
  if (!remote && !configurationError) return children;
  if (configurationError) return <main className="auth-shell"><section className="auth-card"><h1>服务配置待完成</h1><p>请联系项目维护者完成部署配置后再登录。</p></section></main>;
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (locked.current || !account) return;
    locked.current = true; setBusy(true); setError(''); setNotice('');
    try {
      if (mode === 'signup') {
        await account.signup(email, password, confirmation);
        setPassword(''); setConfirmation(''); setMode('login');
        setNotice('注册请求已提交。请查收验证邮件，验证后即可登录；已有账号可直接登录。'); return;
      }
      if (mode === 'recover') {
        await account.recover(email);
        setNotice('如果该邮箱可以找回密码，将收到重置邮件；请检查收件箱和垃圾邮件。'); return;
      }
      if (mode === 'reset') {
        if (!recovery) throw new Error('密码重置链接已失效，请重新申请。');
        await account.reset(recovery, password, confirmation);
        setRecovery(undefined); setPassword(''); setConfirmation(''); setMode('login');
        setNotice('密码已更新，请使用新密码登录。'); return;
      }
      const session = await account.login(email, password);
      setAccessToken(session.token, session.seconds);
      let access: Response;
      try { access = await apiFetch('/api/sessions'); }
      catch { throw new Error('邮箱密码验证已通过，但暂时无法连接工作台，请稍后重试。'); }
      if (!access.ok) throw new Error(access.status >= 500 ? '工作台暂时无法核对账号，请稍后重试。' : '账号暂未获准使用，或邮箱尚未验证。');
      setPassword(''); setSignedIn(true);
    } catch (cause) {
      clearAccessToken();
      setError(cause instanceof Error ? cause.message : '账号操作未完成，请稍后重试。');
    } finally { locked.current = false; setBusy(false); }
  }
  if (signedIn) return <><div className="auth-account"><span>{email}</span><button onClick={() => {
    if (!window.confirm('退出将清除本页尚未发送的文字；已保存的会话会保留。确认退出？')) return;
    clearAccessToken(); setSignedIn(false);
  }}>退出登录</button></div>{children}</>;
  return <main className="auth-shell"><section className="auth-card" aria-labelledby="auth-title">
    <span className="auth-brand">历历在练</span><h1 id="auth-title">把经历，讲清楚。</h1>
    <p>{mode === 'reset' ? '设置新密码后，请重新登录。' : mode === 'recover' ? '输入注册邮箱，我们将发送密码重置邮件。' :
      openRegistration ? '注册并验证邮箱后即可使用，无需人工审核。刷新页面后需重新登录。' : '本轮仅开放受邀账号，刷新页面后需重新登录。'}</p>
    {openRegistration && mode !== 'reset' && <nav className="auth-actions" aria-label="账号操作">
      <button type="button" aria-pressed={mode === 'login'} disabled={busy} onClick={() => changeMode('login')}>登录</button>
      <button type="button" aria-pressed={mode === 'signup'} disabled={busy} onClick={() => changeMode('signup')}>注册账号</button>
    </nav>}
    <form onSubmit={submit} aria-label={mode === 'signup' ? '注册账号' : mode === 'recover' ? '找回密码' : mode === 'reset' ? '设置新密码' : '登录账号'}>
      {mode !== 'reset' && <><label htmlFor="auth-email">邮箱</label><input id="auth-email" type="email" autoComplete="username" value={email} onChange={event => setEmail(event.target.value)} required disabled={busy} /></>}
      {mode !== 'recover' && <><label htmlFor="auth-password">{mode === 'reset' ? '新密码' : '密码'}</label><input id="auth-password" type="password" autoComplete={mode === 'login' ? 'current-password' : 'new-password'} value={password} onChange={event => setPassword(event.target.value)} required minLength={mode === 'login' ? undefined : 8} maxLength={mode === 'login' ? undefined : 128} disabled={busy} /></>}
      {(mode === 'signup' || mode === 'reset') && <><small>密码需为 8～128 个字符。</small><label htmlFor="auth-confirmation">再次输入密码</label><input id="auth-confirmation" type="password" autoComplete="new-password" value={confirmation} onChange={event => setConfirmation(event.target.value)} required disabled={busy} /></>}
      {error && <p className="auth-error" role="alert">{error}</p>}
      {notice && <p className="auth-notice" role="status">{notice}</p>}
      <button className="auth-submit" disabled={busy} type="submit">{busy ? '正在处理…' : mode === 'signup' ? '注册并发送验证邮件' : mode === 'recover' ? '发送重置邮件' : mode === 'reset' ? '更新密码' : '登录工作台'}</button>
    </form>
    <div className="auth-actions"><button type="button" disabled={busy} onClick={() => changeMode(mode === 'recover' || mode === 'reset' ? 'login' : 'recover')}>{mode === 'recover' || mode === 'reset' ? '返回登录' : '忘记密码？'}</button></div>
    <p className="auth-privacy">材料与对话按账号保存。AI 分析额度由全站共享，用尽后仍可查看已保存内容。启用分析时，相关内容会发送至配置的模型服务；测试阶段请使用脱敏材料。</p>
  </section></main>;
}
