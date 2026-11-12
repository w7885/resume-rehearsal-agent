export type AccessSession = { token: string; expiresAt: number };
export type AccountProfile = { id: string; email: string; emailVerified: boolean; displayName: string };

const storageKey = 'lilizaillian.account.access.v1';

export function storedAccess(storage?: Storage, now = Date.now()): AccessSession | null {
  try {
    const value = JSON.parse((storage ?? window.sessionStorage).getItem(storageKey) ?? 'null');
    if (typeof value?.token === 'string' && value.token && value.token.length <= 16377 &&
      typeof value.expiresAt === 'number' && Number.isFinite(value.expiresAt) && value.expiresAt > now) {
      return { token: value.token, expiresAt: value.expiresAt };
    }
  } catch { /* Unavailable storage or corrupt entry. */ }
  storeAccess(storage, null);
  return null;
}

export function storeAccess(storage: Storage | undefined, session: AccessSession | null) {
  try {
    const target = storage ?? window.sessionStorage;
    if (session) target.setItem(storageKey, JSON.stringify(session));
    else target.removeItem(storageKey);
  } catch { /* Login still works when session storage is blocked. */ }
}

export async function readProfile(response: Response): Promise<AccountProfile> {
  const profile = await response.json();
  if (!profile || typeof profile.id !== 'string' || !profile.id || typeof profile.email !== 'string' ||
    typeof profile.emailVerified !== 'boolean' || typeof profile.displayName !== 'string') {
    throw new Error('工作台返回的账号资料无效，请稍后重试。');
  }
  return profile;
}
