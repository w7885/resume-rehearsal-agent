import { test } from 'node:test';
import assert from 'node:assert/strict';
import { WebContinuationController } from '../src/webContinuation.ts';
import { ChatAgentOperation } from '../src/chatAgent.ts';
import { ApiError, readWebContinuation } from '../src/api.ts';
import type { WebContinuation } from '../src/types';
import type { WebControlRequest } from '../src/chatAgent';

const pending: WebContinuation = { sessionId: 's', status: 'pending', requestId: 'web-settings-2', webSettingsRevision: 2, expectedStateRevision: 7 };
function response(id: string) {
  return new Response(`event: done\ndata: ${JSON.stringify({ type: 'done', sessionId: 's', requestId: id,
    runId: 'r1', generation: 1, sequence: 1, payload: { outcome: 'completed' } })}\n\n`,
  { headers: { 'content-type': 'text/event-stream' } });
}
const operation = (sid: string, request: WebControlRequest, transport: typeof fetch) =>
  new ChatAgentOperation(sid, '', request.expectedStateRevision, undefined, transport, request);

test('setting event posts only its server-issued ID and revisions, never a user message', async () => {
  const calls: { url: string; body: unknown }[] = [];
  const op = operation('s', pending as WebControlRequest, async (url, init) => {
    calls.push({ url: String(url), body: JSON.parse(String(init?.body)) });
    return response('web-settings-2');
  });
  await op.run();
  assert.equal(calls[0].url, '/api/sessions/s/web-continuation');
  assert.deepEqual(calls[0].body, { requestId:'web-settings-2', webSettingsRevision:2, expectedStateRevision:7 });
  assert.equal(Object.hasOwn(calls[0].body as object, 'userMessageId'), false);
});

test('duplicate resume joins one event and reloads conversation once', async () => {
  let calls = 0, refreshed = 0; let release!: () => void;
  const gate = new Promise<void>(r => { release = r; });
  const c = new WebContinuationController(undefined, async () => { refreshed++; }, undefined, {
    read: async () => pending,
    create: (sid, request) => operation(sid, request, async () => { calls++; await gate; return response(request.requestId); }),
  });
  const first = c.resume('s'); await Promise.resolve(); const second = c.resume('s');
  release(); await Promise.all([first, second]);
  assert.equal(calls, 1); assert.equal(refreshed, 1); assert.equal(c.get('s').phase, 'idle');
});

test('unknown POST keeps exact event payload for explicit retry', async () => {
  const bodies: string[] = [];
  const c = new WebContinuationController(undefined, undefined, undefined, {
    read: async () => pending,
    create: (sid, request) => operation(sid, request, async (_, init) => {
      if (init?.method === 'GET') return Response.json({ ok:false }, { status:404 });
      bodies.push(String(init?.body)); if (bodies.length === 1) throw new ApiError('lost'); return response(request.requestId);
    }),
  });
  await c.resume('s'); assert.equal(c.get('s').phase, 'error');
  await c.resume('s'); assert.equal(bodies.length, 1);
  await c.resume('s', true); assert.equal(bodies.length, 2); assert.equal(bodies[0], bodies[1]);
});

test('another running writer is observed without another POST', async () => {
  let creates = 0;
  const c = new WebContinuationController(undefined, undefined, undefined, {
    read: async () => ({ ...pending, status: 'running', runId: 'r1' }),
    create: () => { creates++; throw Error('must not create'); },
  });
  await c.resume('s'); assert.equal(creates, 0); assert.equal(c.get('s').phase, 'pending');
});

test('confirmed busy rejection may re-read binding but never blindly retries', async () => {
  let reads = 0; const bodies: WebControlRequest[] = [];
  const c = new WebContinuationController(undefined, undefined, undefined, {
    read: async () => ({ ...pending, expectedStateRevision: 7 + reads++ }),
    create: (sid, request) => operation(sid, request, async (_, init) => {
      bodies.push(JSON.parse(String(init?.body)));
      return bodies.length === 1 ? Response.json({ error: { code: 'run_busy', message: 'busy' } }, { status:409 }) : response(request.requestId);
    }),
  });
  await c.resume('s'); assert.equal(c.get('s').phase, 'pending'); assert.equal(bodies.length, 1);
  await c.resume('s'); assert.deepEqual(bodies.map(b => b.expectedStateRevision), [7, 8]);
});

test('foreign session status and older backend never start control work', async () => {
  let creates = 0;
  for (const foreign of [true, false]) {
    const c = new WebContinuationController(undefined, undefined, undefined, {
      read: async () => { if (foreign) return { ...pending, sessionId:'other' }; throw new ApiError('old','not_found'); },
      create: () => { creates++; throw Error('must not create'); },
    });
    await c.resume('s'); assert.equal(c.get('s').phase, foreign ? 'error' : 'unsupported');
  }
  assert.equal(creates, 0);
});

test('failed conversation reload retries completed operation without another POST', async () => {
  let posts = 0, reloads = 0;
  const c = new WebContinuationController(undefined, async () => { if (++reloads === 1) throw Error('reload lost'); }, undefined, {
    read: async () => pending,
    create: (sid, request) => operation(sid, request, async () => { posts++; return response(request.requestId); }),
  });
  await c.resume('s'); assert.equal(c.get('s').phase, 'error');
  await c.resume('s', true); assert.equal(posts, 1); assert.equal(reloads, 2); assert.equal(c.get('s').phase, 'idle');
});

test('continuation API rejects foreign and malformed success envelopes', async () => {
  const original = globalThis.fetch;
  try {
    for (const value of [{ ...pending, sessionId:'other' }, { ...pending, expectedStateRevision:0 }]) {
      globalThis.fetch = async () => Response.json({ ok:true, data:value });
      await assert.rejects(readWebContinuation('s'));
    }
    globalThis.fetch = async () => Response.json({ ok:true, data:pending });
    assert.deepEqual(await readWebContinuation('s'), pending);
  } finally { globalThis.fetch = original; }
});
