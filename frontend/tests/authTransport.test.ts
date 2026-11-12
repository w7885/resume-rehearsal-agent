import test from 'node:test';
import assert from 'node:assert/strict';
import { activateAccessToken, configureTransport, apiFetch, clearAccessToken, verifyAccessToken } from '../src/authTransport.ts';

test('HTTP is rejected by default and only explicit loopback development is allowed', () => {
  for (const base of ['http://127.0.0.1:3001', 'http://localhost:3001']) {
    assert.throws(() => configureTransport(base, () => {}));
    assert.doesNotThrow(() => configureTransport(base, () => {}, true));
  }
  for (const base of ['http://example.com:3001', 'http://0.0.0.0:3001',
    'http://127.0.0.1.evil.test:3001', 'http://127.0.0.1',
    'http://user:secret@localhost:3001', 'http://localhost:3001/path',
    'http://localhost:3001/?token=private', 'http://localhost:3001/#private']) {
    assert.throws(() => configureTransport(base, () => {}, true), base);
  }
  assert.doesNotThrow(() => configureTransport('https://api.example.com', () => {}));
  configureTransport('', () => {});
});

test('verification cannot unlock workspace requests and a stale 401 cannot invalidate a new login', async () => {
  const original = globalThis.fetch;
  let invalidated = 0;
  try {
    configureTransport('https://api.example.com', () => { invalidated++; }, false, true);
    globalThis.fetch = async (input, init) => {
      assert.equal(input, 'https://api.example.com/api/me');
      assert.equal(new Headers(init?.headers).get('Authorization'), 'Bearer candidate');
      return Response.json({ id: 'candidate' });
    };
    assert.equal((await verifyAccessToken('candidate')).status, 200);
    await assert.rejects(apiFetch('/api/sessions'), /先登录/);
    activateAccessToken('old', Date.now() + 60000);
    let complete!: (response: Response) => void;
    globalThis.fetch = (_input, init) => {
      assert.equal(new Headers(init?.headers).get('Authorization'), 'Bearer old');
      return new Promise(resolve => { complete = resolve; });
    };
    const late = apiFetch('/api/sessions');
    activateAccessToken('new', Date.now() + 60000);
    complete(new Response(null, { status: 401 }));
    await assert.rejects(late, { name: 'AbortError' });
    assert.equal(invalidated, 0);
    globalThis.fetch = async () => new Response(null, { status: 503 });
    assert.equal((await apiFetch('/api/sessions')).status, 503);
    assert.equal(invalidated, 0);
    globalThis.fetch = async (_input, init) => {
      assert.equal(new Headers(init?.headers).get('Authorization'), 'Bearer new');
      return new Response(null, { status: 401 });
    };
    await assert.rejects(apiFetch('/api/sessions'), /已失效/);
    assert.equal(invalidated, 1);
    await assert.rejects(apiFetch('/api/sessions'), /先登录/);
  } finally {
    globalThis.fetch = original;
    clearAccessToken();
    configureTransport('', () => {});
  }
});

test('development requests retain bearer authentication and expire closed', async () => {
  const original = globalThis.fetch;
  let count = 0;
  try {
    configureTransport('http://127.0.0.1:3001', () => {}, true);
    activateAccessToken('synthetic-token', Date.now() + 60000);
    globalThis.fetch = async (input, init) => {
      count++;
      assert.equal(input, 'http://127.0.0.1:3001/api/sessions');
      assert.equal(new Headers(init?.headers).get('Authorization'), 'Bearer synthetic-token');
      assert.equal(init?.credentials, 'omit');
      return new Response('{}');
    };
    await apiFetch('/api/sessions');
    await assert.rejects(apiFetch('https://other.example/api/sessions'));
    activateAccessToken('expired', Date.now() - 1000);
    await assert.rejects(apiFetch('/api/sessions'), /登录已过期/);
    assert.equal(count, 1);
  } finally {
    globalThis.fetch = original;
    clearAccessToken();
    configureTransport('', () => {});
  }
});
