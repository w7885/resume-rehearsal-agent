import test from 'node:test';
import assert from 'node:assert/strict';
import { configureTransport, apiFetch, clearAccessToken, setAccessToken } from '../src/authTransport.ts';

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

test('development requests retain bearer authentication and expire closed', async () => {
  const original = globalThis.fetch;
  let count = 0;
  try {
    configureTransport('http://127.0.0.1:3001', () => {}, true);
    setAccessToken('synthetic-token', 60);
    globalThis.fetch = async (input, init) => {
      count++;
      assert.equal(input, 'http://127.0.0.1:3001/api/sessions');
      assert.equal(new Headers(init?.headers).get('Authorization'), 'Bearer synthetic-token');
      assert.equal(init?.credentials, 'omit');
      return new Response('{}');
    };
    await apiFetch('/api/sessions');
    await assert.rejects(apiFetch('https://other.example/api/sessions'));
    setAccessToken('expired', -1);
    await assert.rejects(apiFetch('/api/sessions'), /登录已过期/);
    assert.equal(count, 1);
  } finally {
    globalThis.fetch = original;
    clearAccessToken();
    configureTransport('', () => {});
  }
});
