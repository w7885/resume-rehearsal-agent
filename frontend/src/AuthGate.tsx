import { useCallback, useEffect, useLayoutEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { activateAccessToken, apiFetch, clearAccessToken, configureTransport, onAccessExpired, verifyAccessToken } from './authTransport';
import './auth.css';
import { AccountAuth, consumeAuthCallback, signupCodePattern, type Recovery } from './accountAuth';
import { readProfile, storeAccess, storedAccess, type AccessSession, type AccountProfile } from './accountSession';
import AccountSettings from './AccountSettings';
import AdminFeedback from './AdminFeedback';
import AdminAccounts from './AdminAccounts';
import AdminOverview from './AdminOverview';
import './admin.css';

const env = (import.meta as ImportMeta & { env?: Record<string, string> }).env ?? {};
const remote = env.VITE_APP_MODE === 'remote';
const supabase = env.VITE_SUPABASE_URL?.replace(/\/$/, '') ?? '';
const publicKey = env.VITE_SUPABASE_PUBLISHABLE_KEY ?? '';
let configurationError = false;
const openRegistration = env.VITE_AUTH_ACCESS_MODE === 'open';
const signupCodeEnabled = env.VITE_SIGNUP_VERIFICATION === 'code';
if (env.VITE_AUTH_ACCESS_MODE && !['open', 'invited'].includes(env.VITE_AUTH_ACCESS_MODE)) configurationError = true;
if (env.VITE_SIGNUP_VERIFICATION && !['link', 'code'].includes(env.VITE_SIGNUP_VERIFICATION)) configurationError = true;
// Vite replaces DEV with false in production builds; HTTP is only for loopback development.
try { configureTransport(env.VITE_API_BASE_URL ?? '', () => {}, import.meta.env.DEV, remote); }
catch { configurationError = true; }
if (remote && (!env.VITE_API_BASE_URL || !publicKey || !/^https:\/\/[^/]+$/.test(supabase))) configurationError = true;
let account: AccountAuth | undefined;
const callback = remote ? consumeAuthCallback(window.location.href, url => window.history.replaceState(null, '', url)) : { notice:'' };
if (remote && !configurationError) {
  try { account = new AccountAuth(supabase, publicKey, window.location.origin); }
  catch { configurationError = true; }
}

class AccessDenied extends Error {}
const adminHash = '#/admin';
const adminDeniedMessage = '此账号无管理员权限。';
const isAdminRoute = () => window.location.hash === adminHash;

async function verify(session: AccessSession, signal?: AbortSignal) {
  let response: Response;
  try {
    const deadline = AbortSignal.timeout(15000);
    response = await verifyAccessToken(session.token, signal ? AbortSignal.any([signal, deadline]) : deadline);
  } catch {
    throw new Error('暂时无法连接工作台，请检查网络后重试。');
  }
  if (response.status === 401 || response.status === 403 || session.expiresAt <= Date.now()) {
    throw new AccessDenied('登录已失效，或账号暂未获准使用。请重新登录。');
  }
  if (!response.ok) throw new Error('暂时无法核对账号，请稍后重试。');
  return readProfile(response);
}

function clearSessionLink() {
  const url = new URL(window.location.href);
  if (url.searchParams.has('session')) {
    url.searchParams.delete('session');
    window.history.replaceState(null, '', url);
  }
}

type Workspace = (onOpenSettings?: () => void) => ReactNode;

export default function AuthGate({ children }: { children: Workspace }) {
  if (configurationError) return <main className="auth-shell"><section className="auth-card"><h1>服务配置待完成</h1><p>请联系项目维护者完成部署配置后再登录。</p></section></main>;
  if (!remote || !account) return children();
  return <RemoteAuthGate account={account}>{children}</RemoteAuthGate>;
}

function RemoteAuthGate({ account, children }: { account: AccountAuth; children: Workspace }) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState(''), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [confirmation, setConfirmation] = useState(''), [notice, setNotice] = useState(callback.notice);
  const [recovery, setRecovery] = useState<Recovery | undefined>('recovery' in callback ? callback.recovery : undefined);
  const [mode, setMode] = useState<'login' | 'signup' | 'signupCode' | 'recover' | 'reset'>('recovery' in callback && callback.recovery ? 'reset' : 'login');
  const [signupCode, setSignupCode] = useState('');
  const [restore, setRestore] = useState<'checking' | 'retry' | 'done'>('checking');
  const [access, setAccess] = useState<{ profile: AccountProfile; session: AccessSession } | null>(null);
  const [workspaceOwner, setWorkspaceOwner] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [adminRoute, setAdminRoute] = useState(isAdminRoute);
  const owner = useRef<string | null>(null);
  const locked = useRef(false);
  const activeSession = access?.session;
  const updateProfile = useCallback((profile: AccountProfile) => {
    setAccess(current => current?.profile.id === profile.id ? { ...current, profile } : current);
  }, []);
  const revokeAdminAccess = useCallback(() => {
    setAccess(current => current ? { ...current, profile: { ...current.profile, isAdmin: false } } : current);
  }, []);
  useEffect(() => {
    const syncRoute = () => { setAdminRoute(isAdminRoute()); setSettingsOpen(false); };
    window.addEventListener('hashchange', syncRoute);
    return () => window.removeEventListener('hashchange', syncRoute);
  }, []);
  function navigateToAdmin(open: boolean) {
    const url = new URL(window.location.href);
    url.hash = open ? adminHash : '';
    window.history.pushState(null, '', url);
    setAdminRoute(open);
    setSettingsOpen(false);
    changeMode('login');
  }
  function accept(session: AccessSession, profile: AccountProfile) {
    if (owner.current && owner.current !== profile.id) {
      clearSessionLink();
      setWorkspaceOwner(null);
    }
    owner.current = profile.id;
    storeAccess(undefined, session);
    setEmail(profile.email);
    setAccess({ profile, session });
  }
  useLayoutEffect(() => {
    // Install new credentials only once the prior account's workspace has
    // been removed and before the next workspace runs its initial requests.
    if (!activeSession) return;
    activateAccessToken(activeSession.token, activeSession.expiresAt);
    return clearAccessToken;
  }, [activeSession]);

  const accessOwner = access?.profile.id;
  useLayoutEffect(() => {
    if (accessOwner && !adminRoute) setWorkspaceOwner(accessOwner);
  }, [accessOwner, adminRoute]);

  useEffect(() => {
    const invalidate = () => {
      clearAccessToken();
      storeAccess(undefined, null);
      setAccess(null);
      setSettingsOpen(false);
      setMode('login');
      setError('登录已失效，请重新登录。');
    };
    const removeListener = onAccessExpired(invalidate);
    let timer: number | undefined;
    function checkDeadline() {
      if (!activeSession) return;
      const remaining = activeSession.expiresAt - Date.now();
      if (remaining <= 0) invalidate();
      else timer = window.setTimeout(checkDeadline, Math.min(remaining, 2_147_483_647));
    }
    checkDeadline();
    return () => { window.clearTimeout(timer); removeListener(); };
  }, [activeSession]);

  useEffect(() => {
    if (restore === 'done') return;
    if (callback.notice || callback.recovery) storeAccess(undefined, null);
    const session = storedAccess();
    if (!session) { setRestore('done'); return; }
    if (restore === 'retry') {
      const timer = window.setTimeout(() => setRestore('checking'), Math.min(session.expiresAt - Date.now(), 2_147_483_647));
      return () => window.clearTimeout(timer);
    }
    const controller = new AbortController();
    void (async () => {
      try {
        const profile = await verify(session, controller.signal);
        if (controller.signal.aborted) return;
        if (session.expiresAt <= Date.now()) throw new AccessDenied('登录已过期，请重新登录。');
        accept(session, profile);
        setRestore('done');
      } catch (cause) {
        if (controller.signal.aborted) return;
        setError(cause instanceof Error ? cause.message : '暂时无法恢复登录，请稍后重试。');
        if (cause instanceof AccessDenied || session.expiresAt <= Date.now()) {
          storeAccess(undefined, null);
          setRestore('done');
        } else setRestore('retry');
      }
    })();
    return () => controller.abort();
  }, [restore]);

  function changeMode(next: typeof mode) {
    if (locked.current) return;
    setMode(next); setError(''); setNotice(''); setPassword(''); setConfirmation(''); setRecovery(undefined); setSignupCode('');
  }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (locked.current) return;
    locked.current = true; setBusy(true); setError(''); setNotice('');
    try {
      if (mode === 'signup') {
        await account.signup(email, password, confirmation);
        setEmail(email.trim()); setPassword(''); setConfirmation('');
        setMode(signupCodeEnabled ? 'signupCode' : 'login');
        setNotice(signupCodeEnabled ? '请查收注册验证码，也可以检查垃圾邮件。' : '注册请求已提交。请查收验证邮件，验证后即可登录；已有账号可直接登录。'); return;
      }
      if (mode === 'signupCode') {
        await account.verifySignup(email, signupCode);
        setSignupCode(''); setMode('login');
        setNotice('邮箱已验证，请使用注册时设置的密码登录。'); return;
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
      const login = await account.login(email, password);
      const session = { token: login.token, expiresAt: Date.now() + login.seconds * 1000 };
      const profile = await verify(session);
      if (session.expiresAt <= Date.now()) throw new AccessDenied('登录已过期，请重新登录。');
      if (isAdminRoute() && !profile.isAdmin) {
        setPassword('');
        throw new AccessDenied(adminDeniedMessage);
      }
      accept(session, profile);
      setPassword('');
    } catch (cause) {
      clearAccessToken();
      setError(cause instanceof Error ? cause.message : '账号操作未完成，请稍后重试。');
    } finally { locked.current = false; setBusy(false); }
  }
  async function resendSignupCode() {
    if (locked.current) return;
    locked.current = true; setBusy(true); setError(''); setNotice('');
    try {
      await account.resendSignup(email);
      setSignupCode('');
      setNotice('已请求重新发送，请查收最新邮件。');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '暂时无法重新发送，请稍后重试。');
    } finally { locked.current = false; setBusy(false); }
  }
  function signOut() {
    if (!window.confirm('退出将清除本页尚未发送的文字；已保存的会话会保留。确认退出？')) return;
    clearAccessToken();
    storeAccess(undefined, null);
    clearSessionLink();
    owner.current = null;
    setWorkspaceOwner(null);
    setSettingsOpen(false);
    setAccess(null);
    setEmail('');
    changeMode('login');
  }

  return <>
    {workspaceOwner && <div key={workspaceOwner} className="auth-workspace" hidden={!access || adminRoute} inert={!access || adminRoute}>
      {children(() => setSettingsOpen(true))}
    </div>}
    {access && !adminRoute && settingsOpen && <AccountSettings profile={access.profile} account={account}
      onClose={() => setSettingsOpen(false)} onSignOut={signOut} onUpdate={updateProfile}
      onOpenAdmin={() => navigateToAdmin(true)} />}
    {access && adminRoute && (access.profile.isAdmin
      ? <AdminLanding key={`admin:${access.profile.id}`} onDenied={revokeAdminAccess} onReturn={() => navigateToAdmin(false)} onSignOut={signOut} />
      : <main className="auth-shell"><section className="auth-card">
        <h1>管理后台</h1><p className="auth-error" role="alert">{adminDeniedMessage}</p>
        <div className="auth-actions"><button type="button" onClick={() => navigateToAdmin(false)}>返回工作台</button></div>
      </section></main>)}
    {!access && restore !== 'done' && <main className="auth-shell"><section className="auth-card">
      <h1>恢复登录</h1>
      {restore === 'checking' ? <p role="status">正在核对账号…</p> : <>
        <p className="auth-error" role="alert">{error}</p>
        <div className="auth-actions">
          <button type="button" onClick={() => setRestore('checking')}>重试</button>
          <button type="button" onClick={() => { storeAccess(undefined, null); setRestore('done'); setError(''); }}>重新登录</button>
        </div>
      </>}
    </section></main>}
    {!access && restore === 'done' && <main className="auth-shell"><section className="auth-card" aria-labelledby="auth-title">
    <span className="auth-brand">历历在练</span><h1 id="auth-title">{mode === 'signupCode' ? '验证邮箱' : adminRoute && mode === 'login' ? '管理员登录' : '把经历，讲清楚。'}</h1>
    <p>{mode === 'signupCode' ? '在这里填写注册邮件中的验证码。' : mode === 'reset' ? '设置新密码后，请重新登录。' : mode === 'recover' ? '输入注册邮箱，我们将发送密码重置邮件。' :
      adminRoute ? '使用已获授权的项目账号进入管理后台。' : openRegistration ? '注册并验证邮箱后即可使用，无需人工审核。' : '本轮仅开放受邀账号。'}</p>
    {!adminRoute && openRegistration && mode !== 'reset' && mode !== 'signupCode' && <nav className="auth-actions" aria-label="账号操作">
      <button type="button" aria-pressed={mode === 'login'} disabled={busy} onClick={() => changeMode('login')}>登录</button>
      <button type="button" aria-pressed={mode === 'signup'} disabled={busy} onClick={() => changeMode('signup')}>注册账号</button>
    </nav>}
    <form onSubmit={submit} aria-label={mode === 'signupCode' ? '验证邮箱' : mode === 'signup' ? '注册账号' : mode === 'recover' ? '找回密码' : mode === 'reset' ? '设置新密码' : '登录账号'}>
      {mode !== 'reset' && <><label htmlFor="auth-email">邮箱</label><input id="auth-email" type="email" autoComplete="username" value={email} onChange={event => setEmail(event.target.value)} required disabled={busy} readOnly={mode === 'signupCode'} /></>}
      {mode === 'signupCode' && <><label htmlFor="auth-signup-code">邮箱验证码</label>
        <input id="auth-signup-code" type="text" inputMode="numeric" autoComplete="one-time-code" autoFocus
          pattern={signupCodePattern} maxLength={10} value={signupCode} onChange={event => setSignupCode(event.target.value.trim())} required disabled={busy} /></>}
      {mode !== 'recover' && mode !== 'signupCode' && <><label htmlFor="auth-password">{mode === 'reset' ? '新密码' : '密码'}</label><input id="auth-password" type="password" autoComplete={mode === 'login' ? 'current-password' : 'new-password'} value={password} onChange={event => setPassword(event.target.value)} required minLength={mode === 'login' ? undefined : 8} maxLength={mode === 'login' ? undefined : 128} disabled={busy} /></>}
      {(mode === 'signup' || mode === 'reset') && <><small>密码需为 8～128 个字符。</small><label htmlFor="auth-confirmation">再次输入密码</label><input id="auth-confirmation" type="password" autoComplete="new-password" value={confirmation} onChange={event => setConfirmation(event.target.value)} required disabled={busy} /></>}
      {error && <p className="auth-error" role="alert">{error}</p>}
      {notice && <p className="auth-notice" role="status">{notice}</p>}
      <button className="auth-submit" disabled={busy} type="submit">{busy ? '正在处理…' : mode === 'signupCode' ? '验证并完成注册' : mode === 'signup' ? (signupCodeEnabled ? '注册并发送验证码' : '注册并发送验证邮件') : mode === 'recover' ? '发送重置邮件' : mode === 'reset' ? '更新密码' : adminRoute ? '登录管理后台' : '登录工作台'}</button>
    </form>
    {mode === 'signupCode' ? <div className="auth-actions">
      <button type="button" disabled={busy} onClick={resendSignupCode}>重新发送验证码</button>
      <button type="button" disabled={busy} onClick={() => changeMode('signup')}>更换邮箱</button>
      <button type="button" disabled={busy} onClick={() => changeMode('login')}>返回登录</button>
    </div> : <div className="auth-actions"><button type="button" disabled={busy} onClick={() => changeMode(mode === 'recover' || mode === 'reset' ? 'login' : 'recover')}>{mode === 'recover' || mode === 'reset' ? '返回登录' : '忘记密码？'}</button></div>}
    {mode === 'login' && <div className="auth-actions"><button type="button" disabled={busy} onClick={() => navigateToAdmin(!adminRoute)}>{adminRoute ? '返回普通登录' : '管理员登录'}</button></div>}
    <p className="auth-privacy">材料与对话按账号保存。AI 分析额度由全站共享，用尽后仍可查看已保存内容。启用分析时，相关内容会发送至配置的模型服务；测试阶段请使用脱敏材料。</p>
    </section></main>}
  </>;
}

function AdminLanding({ onDenied, onReturn, onSignOut }: { onDenied: () => void; onReturn: () => void; onSignOut: () => void }) {
  const [state, setState] = useState<'checking' | 'ready' | 'error'>('checking');
  const [page, setPage] = useState<'overview' | 'accounts' | 'feedback' | 'pending'>('overview');
  useEffect(() => {
    if (state !== 'checking') return;
    const controller = new AbortController();
    void (async () => {
      try {
        const response = await apiFetch('/api/admin/access', {
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]), cache: 'no-store',
        });
        if (controller.signal.aborted) return;
        if (response.status === 403) onDenied();
        else setState(response.status === 204 ? 'ready' : 'error');
      } catch {
        if (!controller.signal.aborted) setState('error');
      }
    })();
    return () => controller.abort();
  }, [state, onDenied]);

  const feedbackOpen = page === 'feedback' || page === 'pending';
  return <main className="auth-shell admin-shell"><section className="auth-card admin-console" aria-labelledby="admin-title">
    <header className="admin-header">
      <span className="auth-brand">历历在练 · 管理后台</span>
      <div className="auth-actions">
        <button type="button" onClick={onReturn}>返回工作台</button>
        <button type="button" onClick={onSignOut}>退出登录</button>
      </div>
    </header>
    {state === 'ready' && <nav className="admin-nav" aria-label="管理后台导航">
      <button type="button" aria-current={page === 'overview' ? 'page' : undefined} onClick={() => setPage('overview')}>📊 概览</button>
      <button type="button" aria-current={page === 'accounts' ? 'page' : undefined} onClick={() => setPage('accounts')}>👥 账号查询</button>
      <button type="button" aria-current={feedbackOpen ? 'page' : undefined} onClick={() => setPage('feedback')}>💬 用户反馈</button>
    </nav>}
    <h1 id="admin-title">{state !== 'ready' ? '管理后台' : page === 'overview' ? '概览' : page === 'accounts' ? '账号查询' : '用户反馈'}</h1>
    {state === 'checking' && <p role="status">正在核对管理员权限…</p>}
    {state === 'error' && <div className="admin-overview-error">
      <p className="auth-error" role="alert">暂时无法核对管理员权限，请重试。</p>
      <button type="button" onClick={() => setState('checking')}>重试</button>
    </div>}
    {state === 'ready' && page === 'overview' && <AdminOverview onDenied={onDenied} onPending={() => setPage('pending')} />}
    {state === 'ready' && page === 'accounts' && <AdminAccounts onDenied={onDenied} />}
    {state === 'ready' && feedbackOpen && <AdminFeedback key={page} initialStatus={page === 'pending' ? 'pending' : ''} onDenied={onDenied} />}
  </section></main>;
}
