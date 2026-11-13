import test from 'node:test';
import assert from 'node:assert/strict';
import { AccountAuth, consumeAuthCallback, validateNewPassword } from '../src/accountAuth.ts';

const base = 'https://synthetic.supabase.co', app = 'https://synthetic.example';
test('signup and recovery use public Auth endpoints and a fixed same-site redirect', async () => {
  const calls: { url:string; init?:RequestInit }[] = [];
  const client = new AccountAuth(base, 'synthetic-public', app, async (url, init) => {
    calls.push({ url:String(url), init }); return Response.json({ user:{ id:'synthetic' } });
  });
  await client.signup(' a@example.com ', 'synthetic-password', 'synthetic-password');
  await client.recover(' a@example.com ');
  assert.equal(calls[0].url, base + '/auth/v1/signup?redirect_to=' + encodeURIComponent(app + '/'));
  assert.equal(calls[1].url, base + '/auth/v1/recover?redirect_to=' + encodeURIComponent(app + '/'));
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)), { email:'a@example.com', password:'synthetic-password' });
  for (const c of calls) {
    assert.equal(c.init?.credentials, 'omit'); assert.equal(c.init?.redirect, 'error');
    assert.equal(new Headers(c.init?.headers).has('Authorization'), false);
  }
});

test('new password mismatch and expired recovery do not send requests', async () => {
  let calls = 0;
  const client = new AccountAuth(base, 'synthetic-public', app, async () => { calls++; return Response.json({}); });
  assert.throws(() => validateNewPassword('short', 'short'));
  await assert.rejects(client.signup('a@example.com', 'synthetic-password', 'different'), /不一致/);
  await assert.rejects(client.reset({ token:'synthetic-token', expiresAt:100 }, 'synthetic-password', 'synthetic-password', 101), /失效/);
  assert.equal(calls, 0);
});

test('signup verification preserves leading zeroes and resends only a signup confirmation', async () => {
  const calls: { url: string; body: unknown; auth: string | null }[] = [];
  const client = new AccountAuth(base, 'synthetic-public', app, async (url, init) => {
    calls.push({ url: String(url), body: JSON.parse(String(init?.body)), auth: new Headers(init?.headers).get('Authorization') });
    return Response.json({ access_token: 'unused', user: { email: 'a@example.com', email_confirmed_at: '2026-11-13T00:00:00Z' } });
  });
  assert.equal(await client.verifySignup(' A@example.com ', ' 01234567 '), undefined);
  await client.resendSignup(' A@example.com ');
  assert.deepEqual(calls, [
    { url: base + '/auth/v1/verify', body: { email: 'A@example.com', token: '01234567', type: 'email' }, auth: null },
    { url: base + '/auth/v1/resend?redirect_to=' + encodeURIComponent(app + '/'), body: { email: 'A@example.com', type: 'signup' }, auth: null },
  ]);
  await assert.rejects(client.verifySignup('other@example.com', '01234567'), /无法确认/);
  await assert.rejects(client.verifySignup('a@example.com', '01234X'), /数字验证码/);
  assert.equal(calls.length, 3);
});

test('signup verification rejects expired or invalid codes with a retry prompt', async () => {
  const client = new AccountAuth(base, 'synthetic-public', app, async () =>
    Response.json({ error_code: 'otp_expired' }, { status: 403 }));
  await assert.rejects(client.verifySignup('a@example.com', '012345'), /重新发送验证码/);
});

test('recovery callback strips all fragment credentials immediately and keeps reset token only in memory', () => {
  let cleaned = '';
  const result = consumeAuthCallback(app + '/#type=recovery&access_token=synthetic-token&refresh_token=synthetic-refresh&expires_in=3600', url => { cleaned = url; }, 100);
  assert.equal(cleaned, '/'); assert.deepEqual(result.recovery, { token:'synthetic-token', expiresAt:3600100 });
  assert.equal(JSON.stringify(result).includes('synthetic-refresh'), false);
  assert.equal(consumeAuthCallback(app + '/', () => {}).recovery, undefined);
});

test('verification and error callbacks never become signed-in or echo provider messages', () => {
  const verified = consumeAuthCallback(app + '/#type=signup&access_token=synthetic&expires_in=3600', () => {});
  assert.equal(verified.recovery, undefined); assert.match(verified.notice, /验证已完成/);
  const expired = consumeAuthCallback(app + '/#error=access_denied&error_description=private-diagnostic', () => {});
  assert.equal(expired.recovery, undefined); assert.equal(expired.notice.includes('private-diagnostic'), false);
  assert.equal(consumeAuthCallback(app + '/#type=recovery&access_token=x&expires_in=bad', () => {}).recovery, undefined);
});

test('reset uses recovery bearer only for the Supabase user endpoint and never auto-signs in', async () => {
  let calls = 0;
  const client = new AccountAuth(base, 'synthetic-public', app, async (url, init) => {
    calls++; assert.equal(url, base + '/auth/v1/user'); assert.equal(init?.method, 'PUT');
    assert.equal(new Headers(init?.headers).get('Authorization'), 'Bearer synthetic-token');
    assert.deepEqual(JSON.parse(String(init?.body)), { password:'synthetic-password' });
    return Response.json({ id:'synthetic-user' });
  });
  assert.equal(await client.reset({ token:'synthetic-token', expiresAt:1000 }, 'synthetic-password', 'synthetic-password', 100), undefined);
  assert.equal(calls, 1);
});

test('rate limits, unconfirmed email and transport failures give safe actionable errors', async () => {
  for (const [status, code, message] of [[429,'too_many_requests','过于频繁'], [400,'email_not_confirmed','确认邮箱'],
      [400,'email_address_not_authorized','邮件暂时无法发送']] as const) {
    const client = new AccountAuth(base, 'synthetic-public', app, async () => Response.json({ code, message:'secret-raw-provider-body' }, { status }));
    await assert.rejects(client.login('a@example.com','synthetic-password'), e => e instanceof Error && e.message.includes(message) && !e.message.includes('secret-raw'));
  }
  const offline = new AccountAuth(base, 'synthetic-public', app, async () => { throw new Error('secret-url'); });
  await assert.rejects(offline.recover('a@example.com'), /暂时无法连接/);
});

test('login validates session envelope and never accepts malformed expiry', async () => {
  for (const data of [{ access_token:'x', expires_in:0 }, { access_token:'x' }, {}]) {
    const client = new AccountAuth(base, 'synthetic-public', app, async () => Response.json(data));
    await assert.rejects(client.login('a@example.com','synthetic-password'), /无法确认/);
  }
  const client = new AccountAuth(base, 'synthetic-public', app, async () => Response.json({ access_token:'synthetic-token', expires_in:3600 }));
  assert.deepEqual(await client.login('a@example.com','synthetic-password'), { token:'synthetic-token', seconds:3600 });
});

test('invalid Auth service or callback origin cannot receive passwords or tokens', () => {
  for (const bad of ['http://synthetic.example', 'https://synthetic.example/path', 'https://user:password@synthetic.example', 'https://synthetic.example/?query=private']) {
    assert.throws(() => new AccountAuth(bad,'synthetic-public',app));
    assert.throws(() => new AccountAuth(base,'synthetic-public',bad));
  }
});

test('development callback permits only HTTP loopback origins, never a remote HTTP site', () => {
  assert.doesNotThrow(() => new AccountAuth(base,'synthetic-public','http://127.0.0.1:5181'));
  assert.doesNotThrow(() => new AccountAuth(base,'synthetic-public','http://localhost:5181'));
  assert.throws(() => new AccountAuth(base,'synthetic-public','http://127.0.0.1.attacker.example'));
});
