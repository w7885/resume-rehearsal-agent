/** Tokens stay in memory and are attached only to the configured API origin. */
let token = '';
let expiresAt = 0;
let onExpired = () => {};
let base = '';

export function configureTransport(apiBase: string, expired: () => void) {
  if (apiBase && (new URL(apiBase).protocol !== 'https:' || new URL(apiBase).pathname !== '/')) throw new Error('后端地址配置无效。');
  base = apiBase.replace(/\/$/, ''); onExpired = expired;
}
export function setAccessToken(value: string, seconds: number) {
  token = value; expiresAt = Date.now() + seconds * 1000;
}
export function clearAccessToken() { token = ''; expiresAt = 0; }
export async function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const path = String(input);
  if (!path.startsWith('/api/') && path !== '/api') throw new Error('请求地址无效。');
  const headers = new Headers(init?.headers);
  if (token) {
    if (Date.now() >= expiresAt) { clearAccessToken(); onExpired(); throw new Error('登录已过期，请重新登录。'); }
    headers.set('Authorization', 'Bearer ' + token);
  }
  const response = await globalThis.fetch(base + path, { ...init, headers, credentials: 'omit', redirect: 'error' });
  // Let the user see the error while keeping unsent text mounted. Explicit
  // sign-out clears the workspace; a network failure must not silently do so.
  return response;
}
