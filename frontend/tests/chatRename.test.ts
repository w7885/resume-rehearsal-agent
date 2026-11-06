import test from 'node:test';
import assert from 'node:assert/strict';
import { ApiError, renameSession, readRenameSupport } from '../src/api.ts';
import { ChatRenameOperation, sessionTitleError } from '../src/chatRename.ts';
import type { SessionRenameWrite } from '../src/api';
import type { ChatSession } from '../src/types';

const session = (title = '新名称'): ChatSession => ({ sessionId: 'target', title, direction: '开发实习',
  stateRevision: 8, draftRevision: 2, currentResumeGeneration: 1, createdAt: '', updatedAt: '', status: 'idle',
  mode: 'local', directionStatus: 'provided', targetDirection: '开发实习' });

test('rename coalesces clicks and never repeats a confirmed write', async () => {
  let calls = 0;
  const operation = new ChatRenameOperation('target', 7, '  新名称  ', async (sid, payload) => {
    calls++; assert.equal(sid, 'target'); assert.equal(payload.title, '新名称'); assert.equal(payload.expectedStateRevision, 7);
    return session();
  });
  const first = operation.run(), second = operation.run();
  assert.equal(first, second); await first; await operation.run(); assert.equal(calls, 1);
});

test('unknown rename retries the same request, title, and revision', async () => {
  const writes: SessionRenameWrite[] = [];
  const operation = new ChatRenameOperation('target', 7, '新名称', async (_sid, payload) => {
    writes.push({ ...payload });
    if (writes.length === 1) throw new ApiError('response lost');
    return session();
  });
  await assert.rejects(operation.run()); await operation.run();
  assert.deepEqual(writes[0], writes[1]);
});

test('conflict and busy never silently rebase a rename', async () => {
  for (const code of ['stale_state', 'run_busy']) {
    const revisions: number[] = [];
    const operation = new ChatRenameOperation('target', 7, '新名称', async (_sid, payload) => {
      revisions.push(payload.expectedStateRevision); throw new ApiError('rejected', code);
    });
    await assert.rejects(operation.run(), (error: ApiError) => error.code === code);
    assert.deepEqual(revisions, [7]);
  }
});

test('single-line Unicode titles validate without truncation', () => {
  for (const title of ['', '  ', '名'.repeat(51), '两\n行', '有\t字符']) assert.notEqual(sessionTitleError(title), '');
  assert.equal(sessionTitleError('名'.repeat(50)), '');
  assert.equal(sessionTitleError('😀'.repeat(50)), '');
  assert.notEqual(sessionTitleError('😀'.repeat(51)), '');
  assert.throws(() => new ChatRenameOperation('target', 7, '  '), (error: ApiError) => error.code === 'invalid_input');
});

test('rename API preserves payload and rejects foreign, stale, or wrong-title success', async () => {
  const original = globalThis.fetch;
  const payload = { requestId: 'fixed-rename', expectedStateRevision: 7, title: '新名称' };
  try {
    let sent: unknown;
    globalThis.fetch = async (url, init) => {
      assert.equal(url, '/api/sessions/target/title'); assert.equal(init?.method, 'PATCH');
      sent = JSON.parse(String(init?.body)); return Response.json({ ok: true, data: session() });
    };
    assert.equal((await renameSession('target', payload)).title, '新名称'); assert.deepEqual(sent, payload);
    for (const invalid of [{ ...session(), sessionId: 'foreign' }, { ...session(), title: '别的名称' }, { ...session(), stateRevision: 6 }]) {
      globalThis.fetch = async () => Response.json({ ok: true, data: invalid });
      await assert.rejects(renameSession('target', payload), (error: ApiError) => error.code === 'invalid_response');
    }
  } finally { globalThis.fetch = original; }
});

test('old backends do not advertise rename; method discovery is read-only', async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async (_url, init) => {
      assert.equal(init?.method, 'OPTIONS'); assert.equal(init?.body, undefined);
      return new Response(null, { status: 404 });
    };
    assert.equal(await readRenameSupport(), false);
    globalThis.fetch = async () => new Response(null, { status: 204 });
    assert.equal(await readRenameSupport(), true);
    globalThis.fetch = async () => new Response(null, { status: 204, headers: { Allow: 'PATCH, OPTIONS' } });
    assert.equal(await readRenameSupport(), true);
  } finally { globalThis.fetch = original; }
});
