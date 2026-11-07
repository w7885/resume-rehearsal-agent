/** Supabase public Auth endpoints only; passwords and tokens are never persisted. */
const record = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);
export type Recovery = { token: string; expiresAt: number };
export function consumeAuthCallback(href: string, replace: (url: string) => void, now = Date.now()): { recovery?: Recovery; notice: string } {
  const url = new URL(href), hash = new URLSearchParams(url.hash.slice(1));
  if (!['access_token', 'refresh_token', 'error', 'error_description', 'type'].some(k => hash.has(k))) return { notice:'' };
  // Remove credentials before loading the workspace or making any request.
  url.hash = ''; replace(url.pathname + url.search);
  if (hash.has('error')) return { notice:'邮件链接已失效，请重新申请邮件。' };
  const token = hash.get('access_token'), seconds = Number(hash.get('expires_in'));
  if (hash.get('type') === 'recovery') {
    if (!token || token.length > 16384 || !Number.isFinite(seconds) || seconds <= 0) return { notice:'密码重置链接已失效，请重新申请。' };
    return { recovery:{ token, expiresAt:now + Math.min(seconds, 3600) * 1000 }, notice:'' };
  }
  return { notice:'邮箱验证已完成，请使用邮箱和密码登录。' };
}

export function validateNewPassword(password: string, confirmation: string) {
  if (password.length < 8 || password.length > 128) throw new Error('密码需为 8～128 个字符。');
  if (password !== confirmation) throw new Error('两次输入的密码不一致。');
}

export class AccountAuth {
  private readonly base: string;
  private readonly key: string;
  private readonly redirect: string;
  private readonly transport: typeof fetch;
  constructor(base: string, key: string, redirect: string, transport: typeof fetch = globalThis.fetch.bind(globalThis)) {
    const u = new URL(base), r = new URL(redirect);
    const localRedirect = r.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(r.hostname);
    if (u.protocol !== 'https:' || u.username || u.password || u.search || u.hash || !['','/'].includes(u.pathname) ||
        (r.protocol !== 'https:' && !localRedirect) || r.username || r.password || r.search || r.hash || !['','/'].includes(r.pathname) || !key) throw new Error('账号服务配置无效。');
    this.base = u.origin; this.key = key; this.redirect = r.origin + '/'; this.transport = transport;
  }
  private async request(path: string, body: object, method = 'POST', token?: string): Promise<Record<string, unknown>> {
    let response: Response;
    try {
      response = await this.transport(this.base + '/auth/v1/' + path, {
        method, credentials:'omit', redirect:'error', signal:AbortSignal.timeout(15000),
        headers:{ 'Content-Type':'application/json', apikey:this.key, ...(token ? { Authorization:'Bearer ' + token } : {}) },
        body:JSON.stringify(body),
      });
    } catch { throw new Error('账号服务暂时无法连接，请检查网络后重试。'); }
    const result: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      const code = record(result) ? result.code ?? result.error_code : '';
      if (response.status === 429) throw new Error('请求过于频繁，请稍后再试。');
      if (code === 'email_not_confirmed') throw new Error('请先打开验证邮件确认邮箱，再登录。');
      if (code === 'email_address_not_authorized' || code === 'over_email_send_rate_limit') throw new Error('验证邮件暂时无法发送，请稍后重试或联系维护者。');
      if (path.startsWith('token')) throw new Error('邮箱或密码不正确，请核对后重试。');
      if (method === 'PUT') throw new Error('密码未更新，链接可能已失效；请重新申请重置邮件。');
      throw new Error('账号操作未完成，请检查填写内容或稍后重试。');
    }
    if (!record(result)) throw new Error('账号服务返回异常，请稍后重试。');
    return result;
  }
  async login(email: string, password: string) {
    const value = await this.request('token?grant_type=password', { email:email.trim(), password });
    if (typeof value.access_token !== 'string' || !value.access_token || typeof value.expires_in !== 'number' || !Number.isFinite(value.expires_in) || value.expires_in <= 0) throw new Error('登录状态无法确认，请重试。');
    return { token:value.access_token, seconds:value.expires_in };
  }
  async signup(email: string, password: string, confirmation: string) {
    validateNewPassword(password, confirmation);
    await this.request('signup?redirect_to=' + encodeURIComponent(this.redirect), { email:email.trim(), password });
  }
  async recover(email: string) {
    await this.request('recover?redirect_to=' + encodeURIComponent(this.redirect), { email:email.trim() });
  }
  async reset(recovery: Recovery, password: string, confirmation: string, now = Date.now()) {
    validateNewPassword(password, confirmation);
    if (!recovery.token || now >= recovery.expiresAt) throw new Error('密码重置链接已失效，请重新申请。');
    await this.request('user', { password }, 'PUT', recovery.token);
  }
}
