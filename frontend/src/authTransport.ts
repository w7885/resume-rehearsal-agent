/** Attach verified credentials only to the configured API origin. */
let token = '';
let expiresAt = 0;
let onExpired = () => {};
let base = '';
let requireToken = false;
let authorization = new AbortController();

export function configureTransport(apiBase: string, expired: () => void, allowLocalDevelopment = false, authenticated = false) {
  if (apiBase) {
    const url = new URL(apiBase);
    const local = allowLocalDevelopment && url.protocol === 'http:' &&
      ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname) && Boolean(url.port);
    if ((!local && url.protocol !== 'https:') || !url.hostname || url.username || url.password ||
        url.pathname !== '/' || url.search || url.hash) throw new Error('后端地址配置无效。');
  }
  base = apiBase.replace(/\/$/, ''); onExpired = expired; requireToken = authenticated;
}
export function onAccessExpired(listener: () => void) {
  onExpired = listener;
  return () => { if (onExpired === listener) onExpired = () => {}; };
}
export function activateAccessToken(value: string, deadline: number) {
  clearAccessToken();
  token = value; expiresAt = deadline;
}
export function clearAccessToken() {
  authorization.abort();
  authorization = new AbortController();
  token = ''; expiresAt = 0;
}
export function verifyAccessToken(value: string, signal?: AbortSignal) {
  return globalThis.fetch(base + '/api/me', {
    headers: { Authorization: 'Bearer ' + value }, cache: 'no-store',
    credentials: 'omit', redirect: 'error', signal,
  });
}
function expireAccess() {
  clearAccessToken();
  onExpired();
}
export async function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const path = String(input);
  if (!path.startsWith('/api/') && path !== '/api') throw new Error('请求地址无效。');
  const headers = new Headers(init?.headers);
  if (requireToken && !token) throw new Error('请先登录。');
  if (token && Date.now() >= expiresAt) { expireAccess(); throw new Error('登录已过期，请重新登录。'); }
  const active = token ? authorization.signal : undefined;
  if (token) headers.set('Authorization', 'Bearer ' + token);
  const signal = active && init?.signal ? AbortSignal.any([active, init.signal]) : active ?? init?.signal;
  const response = await globalThis.fetch(base + path, { ...init, headers, credentials: 'omit', redirect: 'error', signal });
  active?.throwIfAborted();
  if (active && response.status === 401) {
    expireAccess();
    throw new Error('登录已失效，请重新登录。');
  }
  return response;
}
