import test from 'node:test';
import assert from 'node:assert/strict';
import { ConversationReadback } from '../src/conversationReadback.ts';
import { ApiError, readConversation } from '../src/api.ts';
import type { Conversation } from '../src/types';

const conversation = (sid = 'synthetic', revision = 2): Conversation => ({
  session: { sessionId:sid, direction:'test', title:'test', directionStatus:'pending', targetDirection:null,
    stateRevision:revision, draftRevision:1, currentResumeGeneration:1, status:'idle', mode:'local', updatedAt:'' },
  messages:[], materials:[], draft:{ markdown:'saved result', blocks:[] },
  capabilities:{ agentAvailable:true, decisionExecutionAvailable:true },
});

test('after a real client timeout, recovers by GET without resending analysis or uploads', async t => {
  t.mock.timers.enable({ apis:['setTimeout'] });
  const previous = globalThis.fetch, methods: string[] = [];
  let reads = 0, notices = 0;
  try {
    globalThis.fetch = async (url, init) => {
      assert.equal(String(url), '/api/sessions/synthetic/conversation');
      methods.push(init?.method ?? 'GET'); assert.equal(init?.body, undefined);
      if (++reads === 1) return new Promise<Response>((_, reject) => {
        init!.signal!.addEventListener('abort', () => reject(new DOMException('timeout', 'AbortError')), { once:true });
      });
      return Response.json({ ok:true, data:conversation() });
    };
    const result = new ConversationReadback().read('synthetic', conversation, readConversation, () => notices++);
    t.mock.timers.tick(10_000);
    assert.equal((await result)?.draft.markdown, 'saved result');
    assert.deepEqual(methods, ['GET','GET']); assert.equal(notices, 1);
  } finally { globalThis.fetch = previous; }
});

test('ordinary successful reads need no retry or syncing notice', async () => {
  let calls = 0;
  assert.ok(await new ConversationReadback().read('synthetic', conversation, async () => { calls++; return conversation(); }, () => assert.fail()));
  assert.equal(calls, 1);
});

test('persistent service failure stops at three reads and preserves the failure', async () => {
  let calls = 0;
  const error = new ApiError('unavailable','service_unavailable');
  await assert.rejects(new ConversationReadback().read('synthetic', conversation, async () => { calls++; throw error; }, () => {}), e => e === error);
  assert.equal(calls, 3);
});

test('authorization, missing resource, malformed response and version failures never retry', async () => {
  for (const code of ['unauthorized','session_not_found','invalid_response','stale_state']) {
    let calls = 0;
    await assert.rejects(new ConversationReadback().read('synthetic', conversation, async () => { calls++; throw new ApiError('rejected',code); }, () => assert.fail()));
    assert.equal(calls, 1);
  }
});

test('switching sessions or unmounting discards late results without another read', async () => {
  for (const target of [conversation('other'), null]) {
    let current: Conversation | null = conversation(), finish!: (v: Conversation) => void;
    const pending = new ConversationReadback().read('synthetic', () => current, () => new Promise(resolve => { finish = resolve; }), () => assert.fail());
    current = target; finish(conversation()); assert.equal(await pending, null);
  }
});

test('cancel aborts an in-flight API GET and prevents further recovery', async () => {
  const previous = globalThis.fetch, operation = new ConversationReadback();
  let calls = 0, aborted = false;
  try {
    globalThis.fetch = async (_, init) => new Promise<Response>((_, reject) => {
      calls++; init!.signal!.addEventListener('abort', () => { aborted = true; reject(new Error('cancelled')); }, { once:true });
    });
    const pending = operation.read('synthetic', conversation, readConversation, () => assert.fail());
    operation.cancel(); assert.equal(await pending, null);
    assert.equal(aborted, true); assert.equal(calls, 1);
  } finally { globalThis.fetch = previous; }
});

test('foreign sessions and older revisions or resume generations cannot overwrite active results', async () => {
  for (const saved of [conversation('foreign'), conversation('synthetic',1),
    { ...conversation(), session:{ ...conversation().session, draftRevision:0 } },
    { ...conversation(), session:{ ...conversation().session, currentResumeGeneration:0 } }]) {
    await assert.rejects(new ConversationReadback().read('synthetic', conversation, async () => saved, () => assert.fail()), e => e instanceof ApiError && e.code === 'stale_state');
  }
});

test('newer user edits during a read are protected from an older response', async () => {
  let current = conversation(), finish!: (v: Conversation) => void;
  const pending = new ConversationReadback().read('synthetic', () => current, () => new Promise(resolve => { finish = resolve; }), () => assert.fail());
  current = conversation('synthetic',3); finish(conversation());
  await assert.rejects(pending, e => e instanceof ApiError && e.code === 'stale_state');
});
