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

function snapshot(id: string, outcome = 'completed', status = 'done') {
  return { ok: true, data: { requestId: id, runId: 'r1', status, outcome,
    events: [JSON.parse(packet(id, 'progress', 1).split('data: ')[1]),
      JSON.parse(packet(id, 'done', 2, { outcome }).split('data: ')[1])] } };
}

test('successful done is terminal even when the connection fails on its next read', async () => {
  let reads = 0, posts = 0, cancelled = 0;
  const operation = new ChatAgentOperation('s', 'm', 7, undefined, async (_, init) => {
    posts++;
    const id = JSON.parse(String(init?.body)).requestId;
    return { ok: true, headers: new Headers({ 'content-type': 'text/event-stream' }),
      body: { getReader: () => ({ read: async () => {
        if (++reads === 1) return { done: false, value: new TextEncoder().encode(packet(id, 'done', 1, { outcome: 'completed' })) };
        throw new TypeError('network error');
      }, cancel: async () => { cancelled++; }, releaseLock() {} }) } } as Response;
  });
  await operation.run(); await operation.run();
  assert.equal(reads, 1); assert.equal(posts, 1); assert.equal(cancelled, 1);
});

test('lost stream completion is reconciled with one GET and never another POST', async () => {
  const methods: string[] = [];
  let id = '';
  const operation = new ChatAgentOperation('s', 'm', 7, undefined, async (url, init) => {
    methods.push(init?.method ?? 'GET');
    if (init?.method === 'POST') {
      id = JSON.parse(String(init.body)).requestId;
      return stream(packet(id, 'progress', 1));
    }
    assert.equal(url, `/api/sessions/s/agent-requests/${id}`);
    return Response.json(snapshot(id));
  });
  await operation.run(); await operation.run();
  assert.deepEqual(methods, ['POST', 'GET']); assert.equal(operation.runId, 'r1');
});

test('a lost POST response can recover an already persisted waiting-user result', async () => {
  let id = '', posts = 0, gets = 0;
  const operation = new ChatAgentOperation('s', 'm', 7, undefined, async (_, init) => {
    if (init?.method === 'POST') { posts++; id = JSON.parse(String(init.body)).requestId; throw new TypeError('network error'); }
    gets++; return Response.json(snapshot(id, 'waiting_user'));
  });
  await operation.run();
  assert.equal(posts, 1); assert.equal(gets, 1);
});

test('network error during reading reconciles the persisted result and releases the reader', async () => {
  let id = '', reads = 0, cancelled = 0, released = 0, gets = 0;
  const operation = new ChatAgentOperation('s', 'm', 7, undefined, async (_, init) => {
    if (init?.method === 'GET') { gets++; return Response.json(snapshot(id)); }
    id = JSON.parse(String(init?.body)).requestId;
    return { ok:true, headers:new Headers({ 'content-type':'text/event-stream' }),
      body:{ getReader:() => ({ read:async () => {
        if (++reads === 1) return { done:false, value:new TextEncoder().encode(packet(id, 'progress', 1)) };
        throw new TypeError('network error');
      }, cancel:async () => { cancelled++; }, releaseLock() { released++; } }) } } as Response;
  });
  await operation.run();
  assert.equal(reads, 2); assert.equal(gets, 1); assert.equal(cancelled, 1); assert.equal(released, 1);
});

test('failed and interrupted snapshots remain failures even if a done event exists', async () => {
  for (const [status, outcome] of [['failed', 'agent_failed'], ['interrupted', 'cancelled'], ['failed', 'completed']]) {
    let id = '', gets = 0;
    const operation = new ChatAgentOperation('s', 'm', 7, undefined, async (_, init) => {
      if (init?.method === 'POST') { id = JSON.parse(String(init.body)).requestId; return stream(''); }
      gets++; return Response.json(snapshot(id, outcome, status));
    });
    await assert.rejects(operation.run(), /停止或未完成/);
    assert.equal(gets, 1);
  }
});

test('running or unreachable status never causes automatic submission or false success', async () => {
  for (const kind of ['running', 'offline', 'denied']) {
    const methods: string[] = []; let id = '';
    const operation = new ChatAgentOperation('s', 'm', 7, undefined, async (_, init) => {
      methods.push(init?.method ?? 'GET');
      if (init?.method === 'POST') { id = JSON.parse(String(init.body)).requestId; return stream(''); }
      if (kind === 'offline') throw new TypeError('offline');
      return kind === 'denied' ? Response.json({ ok:false }, { status:403 }) : Response.json(snapshot(id, 'completed', 'running'));
    });
    await assert.rejects(operation.run(), /连接中断/);
    assert.deepEqual(methods, ['POST', 'GET']);
  }
});

test('snapshot recovery rejects foreign scope, stale cursors, malformed order and contradictory outcomes', async () => {
  const changes = [
    (s: ReturnType<typeof snapshot>) => { s.data.requestId = 'other'; },
    (s: ReturnType<typeof snapshot>) => { s.data.events[1].sessionId = 'other'; },
    (s: ReturnType<typeof snapshot>) => { s.data.events[1].requestId = 'other'; },
    (s: ReturnType<typeof snapshot>) => { s.data.events[1].sequence = 1; },
    (s: ReturnType<typeof snapshot>) => { s.data.events[1].generation = 0; },
    (s: ReturnType<typeof snapshot>) => { s.data.events[1].payload.outcome = 'cancelled'; },
    (s: ReturnType<typeof snapshot>) => { s.data.events.pop(); },
    (s: ReturnType<typeof snapshot>) => { s.data.events[0].payload.replayed = true; },
    (s: ReturnType<typeof snapshot>) => { s.data.runId = 'foreign-root'; },
    (s: ReturnType<typeof snapshot>) => { s.data.events[0].runId = 'changed-observed-run'; s.data.runId = 'changed-observed-run'; },
    (s: ReturnType<typeof snapshot>) => { s.data.events.shift(); s.data.events[0].sequence = 2; },
    (s: ReturnType<typeof snapshot>) => { s.data.events[0].type = 'question'; },
    (s: ReturnType<typeof snapshot>) => { s.data.events.push({ ...s.data.events[1], sequence:3 }); },
  ];
  for (const change of changes) {
    let id = '', posts = 0;
    const operation = new ChatAgentOperation('s', 'm', 7, undefined, async (_, init) => {
      if (init?.method === 'POST') { posts++; id = JSON.parse(String(init.body)).requestId; return stream(packet(id, 'progress', 1)); }
      const value = snapshot(id); change(value); return Response.json(value);
    });
    await assert.rejects(operation.run()); assert.equal(posts, 1);
  }
});

test('protocol violations never fall back to a saved success snapshot', async () => {
  for (const kind of ['foreign', 'malformed', 'failed']) {
    let gets = 0;
    const operation = new ChatAgentOperation('s', 'm', 7, undefined, async (_, init) => {
      if (init?.method === 'GET') { gets++; throw Error('must not reconcile'); }
      const id = JSON.parse(String(init?.body)).requestId;
      return stream(kind === 'foreign' ? packet(id, 'done', 1, { outcome:'completed' }, 'other') :
        kind === 'malformed' ? 'data: broken-json\n\n' : packet(id, 'done', 1, { outcome:'cancelled' }));
    });
    await assert.rejects(operation.run()); assert.equal(gets, 0);
  }
});

test('recovery preserves root run binding across valid continuation generations', async () => {
  let id = '';
  const operation = new ChatAgentOperation('s', 'm', 7, undefined, async (_, init) => {
    if (init?.method === 'POST') { id = JSON.parse(String(init.body)).requestId; return stream(packet(id, 'progress', 1)); }
    const value = snapshot(id); value.data.events[1].generation = 2; value.data.events[1].runId = 'r2';
    return Response.json(value);
  });
  await operation.run(); assert.equal(operation.runId, 'r2');
});

test('cancellation while checking saved status cannot be replaced by recovered success', async () => {
  let started!: () => void, release!: () => void, id = '';
  const checking = new Promise<void>(resolve => { started = resolve; });
  const gate = new Promise<void>(resolve => { release = resolve; });
  const calls: string[] = [];
  const operation = new ChatAgentOperation('s', 'm', 7, undefined, async (url, init) => {
    calls.push(`${init?.method} ${url}`);
    if (String(url).endsWith('/cancel')) return Response.json({ ok:true });
    if (init?.method === 'POST') { id = JSON.parse(String(init.body)).requestId; return stream(packet(id, 'progress', 1)); }
    started(); await gate; assert.equal(init?.signal?.aborted, true); return Response.json(snapshot(id));
  });
  const running = operation.run();
  const rejected = assert.rejects(running);
  await checking; await operation.stop(); release(); await rejected;
  assert.equal(operation.isStopped, true); assert.equal(operation.isStopConfirmed, true);
  assert.equal(calls.filter(c => c.startsWith('POST') && !c.endsWith('/cancel')).length, 1);
});

test('web control disconnection checks its exact saved request without resending user content', async () => {
  const calls: string[] = []; const id = 'web-settings/2';
  const operation = new ChatAgentOperation('s', '', 7, undefined, async (url, init) => {
    calls.push(`${init?.method} ${url}`);
    if (init?.method === 'POST') {
      assert.deepEqual(JSON.parse(String(init.body)), { requestId:id, expectedStateRevision:7, webSettingsRevision:2 });
      return stream('');
    }
    return Response.json(snapshot(id));
  }, { requestId:id, expectedStateRevision:7, webSettingsRevision:2 });
  await operation.run();
  assert.deepEqual(calls, ['POST /api/sessions/s/web-continuation', 'GET /api/sessions/s/agent-requests/web-settings%2F2']);
});

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

test('diagnostic events such as router warnings do not abort the run', async () => {
  let calls = 0;
  const operation = new ChatAgentOperation('s', 'm', 7, undefined, async (_, init) => {
    calls++;
    const id = JSON.parse(String(init?.body)).requestId;
    return stream(packet(id, 'progress', 1) + packet(id, 'warning', 2, { code: 'router_fallback' }) + packet(id, 'done', 3, { outcome: 'waiting_user' }));
  });
  await operation.run();
  assert.equal(calls, 1);
});

test('unknown stream result retries exactly the same request and never resends raw chat', async () => {
  const bodies: string[] = []; let reads = 0;
  const operation = new ChatAgentOperation('s', 'm', 7, undefined, async (_, init) => {
    if (init?.method === 'GET') { reads++; return Response.json({ ok: false }, { status: 404 }); }
    bodies.push(String(init?.body)); const id = JSON.parse(bodies.at(-1)!).requestId;
    return stream(packet(id, 'progress', 1) + (bodies.length > 1 ? packet(id, 'done', 2, { outcome: 'completed' }) : ''));
  });
  await assert.rejects(operation.run(), /连接中断/); await operation.run();
  assert.equal(bodies.length, 2); assert.equal(bodies[0], bodies[1]);
  assert.equal(reads, 1);
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


test('validated search progress notifies UI and clears at completion', async () => {
  const seen: string[] = [];
  let operation: ChatAgentOperation;
  operation = new ChatAgentOperation('s', 'm', 1, () => seen.push(operation.progressMessage), async (_, init) => {
    const id = JSON.parse(String(init?.body)).requestId;
    return stream(packet(id, 'progress', 1, {message:'正在查找目标岗位资料。'}) + packet(id, 'progress', 2, {message:'正在读取岗位来源。'}) + packet(id, 'done', 3, {outcome:'completed'}));
  });
  await operation.run();
  assert.deepEqual(seen,['正在查找目标岗位资料。','正在读取岗位来源。','']);
});
