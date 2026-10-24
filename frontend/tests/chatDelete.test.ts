import test from 'node:test';
import assert from 'node:assert/strict';
import { ApiError } from '../src/api.ts';
import { ChatDeleteOperation } from '../src/chatDelete.ts';
import type { SessionDeleteWrite } from '../src/api';
import type { Conversation } from '../src/types';

test('delete coalesces duplicate clicks and replays a completed operation without another request', async () => {
  let calls = 0;
  const operation = new ChatDeleteOperation('target', 7, async (sid, payload) => {
    calls++; assert.equal(sid, 'target'); assert.equal(payload.expectedStateRevision, 7); assert.equal(payload.confirmed, true);
    return { sessionId: sid, deleted: true };
  });
  const first = operation.run(), second = operation.run();
  assert.equal(first, second); await first; await operation.run(); assert.equal(calls, 1);
});

test('unknown deletion retries identical ID and confirmed revision', async () => {
  const writes: SessionDeleteWrite[] = [];
  const operation = new ChatDeleteOperation('target', 7, async (sid, payload) => {
    writes.push({ ...payload });
    if (writes.length === 1) throw new ApiError('lost response');
    return { sessionId: sid, deleted: true };
  });
  await assert.rejects(operation.run()); await operation.run();
  assert.deepEqual(writes[0], writes[1]);
});

test('conflict and busy never fetch a new version or silently retry deletion', async () => {
  for (const code of ['stale_state', 'run_busy']) {
    let calls = 0, reads = 0;
    const operation = new ChatDeleteOperation('target', 7, async () => { calls++; throw new ApiError('not deleted', code); }, async () => { reads++; return {} as Conversation; });
    await assert.rejects(operation.run(), (error: ApiError) => error.code === code);
    assert.equal(calls, 1); assert.equal(reads, 0);
  }
});

test('already absent target needs independent read confirmation and never deletes a different session', async () => {
  const missing = new ApiError('gone', 'session_not_found');
  let reads = 0;
  const operation = new ChatDeleteOperation('target', 7, async () => { throw missing; }, async sid => { assert.equal(sid, 'target'); reads++; throw missing; });
  await operation.run(); await operation.run(); assert.equal(reads, 1);
  const exists = new ChatDeleteOperation('target', 7, async () => { throw missing; }, async () => ({} as Conversation));
  await assert.rejects(exists.run());
  const unknown = new ChatDeleteOperation('target', 7, async () => { throw missing; }, async () => { throw new ApiError('offline'); });
  await assert.rejects(unknown.run(), /offline/);
});

test('delete API rejects a successful envelope naming a different session or missing deletion proof', async () => {
  const original = globalThis.fetch;
  try {
    const { deleteSession } = await import('../src/api.ts');
    for (const data of [{ sessionId: 'other', deleted: true }, { sessionId: 'target', deleted: false }, {}]) {
      globalThis.fetch = async () => new Response(JSON.stringify({ ok: true, data }), { status: 200 });
      await assert.rejects(deleteSession('target', { requestId: 'delete', expectedStateRevision: 7, confirmed: true }), (error: ApiError) => error.code === 'invalid_response');
    }
  } finally { globalThis.fetch = original; }
});
