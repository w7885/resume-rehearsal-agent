import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ApiError, listSessions, listChatSessions, readInitialConversation, readConversation, readSession, writeDraft, uploadMaterial } from '../src/api.ts';

const session = { sessionId: 'synthetic', direction: '构造', stateRevision: 1, draftRevision: 1, currentResumeGeneration: 1, updatedAt: '', mode: 'local' };
test('cancelled startup does not replay; initial conversation gets one transient recovery only', async () => {
  const original = globalThis.fetch;
  let calls = 0;
  try {
    const cancelled = new AbortController();
    globalThis.fetch = async (_url, init) => { calls++; cancelled.abort(); assert.equal(init?.signal?.aborted, true); throw new DOMException('cancelled', 'AbortError'); };
    await assert.rejects(listChatSessions(cancelled.signal));
    assert.equal(calls, 1);
    calls = 0;
    globalThis.fetch = async () => { calls++; throw new TypeError('synthetic connection failure'); };
    await assert.rejects(readInitialConversation('synthetic', new AbortController().signal));
    assert.equal(calls, 2);
    calls = 0;
    await assert.rejects(readConversation('synthetic'));
    assert.equal(calls, 1); // existing run readback owns its own retry budget.
    calls = 0;
    globalThis.fetch = async () => { calls++; return Response.json({ok:false,error:{code:'unauthorized',message:'login'}},{status:401}); };
    await assert.rejects(readInitialConversation('synthetic', new AbortController().signal));
    assert.equal(calls, 1);
  } finally { globalThis.fetch = original; }
});
test('storage read reconnects once while writes and exhausted reads are never blindly replayed', async () => {
  const original = globalThis.fetch;
  let calls = 0;
  const failure = () => Response.json({ ok: false, error: { code: 'storage_failed', message: '读取失败' } }, { status: 503 });
  try {
    globalThis.fetch = async () => ++calls === 1 ? failure() : Response.json({ ok: true, data: [session] });
    assert.equal((await listSessions()).length, 1);
    assert.equal(calls, 2);
    calls = 0;
    globalThis.fetch = async () => { calls++; return failure(); };
    await assert.rejects(listSessions(), e => e instanceof ApiError && e.code === 'storage_failed');
    assert.equal(calls, 2);
    calls = 0;
    await assert.rejects(writeDraft('synthetic', { requestId: 'same-operation', expectedStateRevision: 1, markdown: '保留文字' }),
      e => e instanceof ApiError && e.code === 'storage_failed');
    assert.equal(calls, 1);
  } finally { globalThis.fetch = original; }
});

test('read outages do not claim an unconfirmed save; writes keep the uncertain-result warning', async () => {
  const original = globalThis.fetch;
  let requests = 0;
  try {
    globalThis.fetch = async () => { requests++; throw new TypeError('synthetic network failure'); };
    await assert.rejects(listSessions(), e => e instanceof ApiError && e.code === 'network_error' &&
      e.message.includes('读取') && !e.message.includes('保存结果'));
    await assert.rejects(writeDraft('synthetic', { requestId: 'same-operation', expectedStateRevision: 1, markdown: '保留文字' }),
      e => e instanceof ApiError && e.message.includes('无法确认保存结果'));
    assert.equal(requests, 3); // GET retries once; the write is never replayed.
    globalThis.fetch = async () => new Response('unavailable', { status: 503 });
    await assert.rejects(listSessions(), e => e instanceof ApiError && e.code === 'service_unavailable' &&
      e.message.includes('读取') && !e.message.includes('保存结果'));
  } finally { globalThis.fetch = original; }
});

test('transient history failures recover once; permission rejection never retries', async () => {
  const original = globalThis.fetch;
  try {
    for (const code of ['auth_unavailable', 'service_unavailable']) {
      let calls = 0;
      globalThis.fetch = async () => ++calls === 1
        ? Response.json({ ok: false, error: { code, message: '暂时无法读取' } }, { status: 503 })
        : Response.json({ ok: true, data: [session] });
      assert.equal((await listSessions()).length, 1);
      assert.equal(calls, 2);
    }
    let calls = 0;
    globalThis.fetch = async () => { calls++; return Response.json({ ok: false,
      error: { code: 'unauthorized', message: '请重新登录' } }, { status: 401 }); };
    await assert.rejects(listSessions(), e => e instanceof ApiError && e.code === 'unauthorized');
    assert.equal(calls, 1);
  } finally { globalThis.fetch = original; }
});

test('malformed successful history is rejected instead of treated as an empty list', async () => {
  const original = globalThis.fetch;
  try { globalThis.fetch = async () => Response.json({ ok: true, data: { sessions: [] } }); await assert.rejects(listSessions(), e => e instanceof ApiError && e.code === 'invalid_response'); }
  finally { globalThis.fetch = original; }
});
test('malformed nested suggestions cannot crash the rendered workspace', async () => {
  const original = globalThis.fetch;
  try { globalThis.fetch = async () => Response.json({ ok: true, data: { session, draft: { markdown: '稿', blocks: [] }, messages: [], materials: [], suggestions: [{ status: 'pending', gaps: null }], issues: [], events: [] } }); await assert.rejects(readSession('synthetic'), e => e instanceof ApiError && e.code === 'invalid_response'); }
  finally { globalThis.fetch = original; }
});
test('version errors retain code for recovery; outgoing request preserves caller ID/version', async () => {
  const original = globalThis.fetch; const payload = { requestId: 'same-operation', expectedStateRevision: 3, markdown: '我的稿' }; let sent: unknown;
  try { globalThis.fetch = async (_url, init) => { sent = JSON.parse(String(init?.body)); return Response.json({ ok: false, error: { code: 'stale_state', message: '会话已更新' } }, { status: 409 }); }; await assert.rejects(writeDraft('synthetic', payload), e => e instanceof ApiError && e.code === 'stale_state'); assert.deepEqual(sent, payload); }
  finally { globalThis.fetch = original; }
});
test('binary upload preserves original bytes and metadata rather than converting to JSON', async () => {
  const original = globalThis.fetch; let sentUrl = '', sentBody: BodyInit | null | undefined;
  const file = new File(['# 构造 Markdown\n原文'], '构造.md', { type: 'text/markdown' });
  try { globalThis.fetch = async (url, init) => { sentUrl = String(url); sentBody = init?.body; return Response.json({ ok: true, data: {} }); }; await uploadMaterial('synthetic', { requestId: 'upload', expectedStateRevision: 1, name: file.name, role: 'unknown' }, file); assert.equal(new URL(sentUrl, 'http://local').searchParams.get('role'), 'unknown'); assert.ok(sentBody instanceof Blob); assert.equal(await sentBody.text(), await file.text()); }
  finally { globalThis.fetch = original; }
});

test('proxy outage is reported as unavailable while preserving caller retry semantics', async () => {
  const original = globalThis.fetch;
  try { globalThis.fetch = async () => new Response('proxy unavailable', { status: 502 }); await assert.rejects(writeDraft('synthetic', { requestId: 'same-operation', expectedStateRevision: 1, markdown: '保留文字' }), e => e instanceof ApiError && e.code === 'service_unavailable'); }
  finally { globalThis.fetch = original; }
});
