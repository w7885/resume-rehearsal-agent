import test from 'node:test';
import assert from 'node:assert/strict';
import { ChatAgentOperation } from '../src/chatAgent.ts';

function packet(id: string, type: string, sequence: number, payload: object = {}, sessionId = 's', generation = 1) {
  return `event: ${type}\ndata: ${JSON.stringify({ sessionId, requestId: id, type, sequence, generation, runId: `r${generation}`, payload })}\n\n`;
}
function stream(text: string) {
  const bytes = new TextEncoder().encode(text);
  return new Response(new ReadableStream({ start(c) { for (let i = 0; i < bytes.length; i += 7) c.enqueue(bytes.slice(i, i + 7)); c.close(); } }), { headers: { 'Content-Type': 'text/event-stream' } });
}

test('analysis uses a saved message ID, handles fragmented UTF8 and Host continuation generations', async () => {
  let calls = 0, notices = 0;
  const operation = new ChatAgentOperation('s', 'saved-user', 3, () => { notices++; }, async (url, init) => {
    calls++;
    assert.equal(url, '/api/sessions/s/runs');
    const body = JSON.parse(String(init?.body));
    assert.deepEqual(Object.keys(body).sort(), ['expectedStateRevision', 'requestId', 'userMessageId']);
    return stream(packet(body.requestId, 'progress', 1, { message: '正在核对材料。' }) + packet(body.requestId, 'question', 2, { messageId: 'saved-question' }, 's', 2) + packet(body.requestId, 'done', 3, { outcome: 'waiting_user' }, 's', 2));
  });
  const a = operation.run(), b = operation.run(); assert.equal(a, b); await a; await operation.run();
  assert.equal(calls, 1); assert.equal(notices, 3); assert.equal(operation.runId, 'r2');
});

test('unknown stream result retries exactly the same request and never resends raw chat', async () => {
  const bodies: string[] = [];
  const operation = new ChatAgentOperation('s', 'm', 7, undefined, async (_, init) => {
    bodies.push(String(init?.body)); const id = JSON.parse(bodies.at(-1)!).requestId;
    return stream(packet(id, 'progress', 1) + (bodies.length > 1 ? packet(id, 'done', 2, { outcome: 'completed' }) : ''));
  });
  await assert.rejects(operation.run(), /连接中断/); await operation.run();
  assert.equal(bodies.length, 2); assert.equal(bodies[0], bodies[1]);
});

test('foreign, out-of-order and unpersisted question events fail closed', async () => {
  for (const kind of ['foreign', 'order', 'question']) {
    const operation = new ChatAgentOperation('s', 'm', 7, undefined, async (_, init) => {
      const id = JSON.parse(String(init?.body)).requestId;
      return stream(kind === 'foreign' ? packet(id, 'done', 1, { outcome: 'completed' }, 'other') : kind === 'order' ? packet(id, 'progress', 2) + packet(id, 'done', 1, { outcome: 'completed' }) : packet(id, 'question', 1));
    });
    await assert.rejects(operation.run());
  }
});

test('persisted interrupted replay is not represented as successful analysis', async () => {
  const operation = new ChatAgentOperation('s', 'm', 7, undefined, async (_, init) => {
    const id = JSON.parse(String(init?.body)).requestId;
    return stream(packet(id, 'done', 1, { outcome: 'cancelled', replayed: true }));
  });
  await assert.rejects(operation.run(), /停止或未完成/);
});

test('stop calls the current run cancellation endpoint and does not resend messages', async () => {
  const calls: string[] = [];
  const operation = new ChatAgentOperation('s', 'm', 7, undefined, async (url, init) => {
    calls.push(String(url));
    if (String(url).endsWith('/cancel')) return Response.json({ ok: true });
    const id = JSON.parse(String(init?.body)).requestId;
    return stream(packet(id, 'progress', 1) + packet(id, 'done', 2, { outcome: 'completed' }));
  });
  await operation.run(); await operation.stop();
  assert.deepEqual(calls, ['/api/sessions/s/runs', '/api/runs/r1/cancel']);
});

test('default browser transport preserves the global fetch receiver', async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async function(input, init) {
    assert.equal(this, globalThis);
    const id = JSON.parse(String(init?.body)).requestId;
    return stream(packet(id, 'done', 1, { outcome: 'completed' }));
  };
  try { await new ChatAgentOperation('s', 'm', 7).run(); }
  finally { globalThis.fetch = original; }
});
