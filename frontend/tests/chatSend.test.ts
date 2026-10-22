import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ApiError } from '../src/api.ts';
import { ChatSendOperation } from '../src/chatSend.ts';
import type { ChatSendServices, ChatAttachment } from '../src/chatSend.ts';
import type { ChatMessageWrite } from '../src/api.ts';
import type { ChatMaterial, ChatMessageResult, ChatSession, Conversation } from '../src/types.ts';

function server() {
  const session: ChatSession = { sessionId: 'session-synthetic', direction: '新对话', title: '新对话', directionStatus: 'pending', targetDirection: null,
    stateRevision: 1, draftRevision: 1, currentResumeGeneration: 1, mode: 'local', status: 'idle', updatedAt: '' };
  const materials: ChatMaterial[] = [], messages: ChatMessageResult['message'][] = [];
  const calls: { type: string; payload: unknown; name?: string }[] = [];
  const creates = new Map<string, ChatSession>(), uploads = new Map<string, ChatMaterial>(), sends = new Map<string, ChatMessageResult>();
  const fail = { createAfterCommit: false, uploadAfterCommit: '', messageAfterCommit: false, rejectFile: '', nextRead: false };
  const copy = <T>(value: T): T => structuredClone(value);
  const conversation = (): Conversation => ({ session: copy(session), materials: copy(materials), messages: copy(messages),
    draft: { sessionId: session.sessionId, draftRevision: 1, markdown: '', blocks: [] }, capabilities: { agentAvailable: false, decisionExecutionAvailable: false } });
  const services: ChatSendServices = {
    create: async payload => {
      calls.push({ type: 'create', payload: copy(payload) });
      if (!creates.has(payload.requestId)) creates.set(payload.requestId, copy(session));
      if (fail.createAfterCommit) { fail.createAfterCommit = false; throw new ApiError('response lost'); }
      return copy(creates.get(payload.requestId)!);
    },
    read: async () => { if (fail.nextRead) { fail.nextRead = false; throw new ApiError('read failed'); } return conversation(); },
    upload: async (sid, payload, file) => {
      calls.push({ type: 'upload', payload: copy(payload), name: file.name });
      if (uploads.has(payload.requestId)) return copy(uploads.get(payload.requestId)!);
      if (file.name === fail.rejectFile) throw new ApiError('bad file', 'invalid_input');
      if (payload.expectedStateRevision !== session.stateRevision) throw new ApiError('stale', 'stale_state');
      const value: ChatMaterial = { sessionId: sid, materialId: `material-${materials.length + 1}`, name: file.name, role: 'unknown', parseStatus: 'parsed', useStatus: 'pending', mediaType: 'text/markdown', byteSize: file.size, current: true };
      materials.push(value); uploads.set(payload.requestId, copy(value)); session.stateRevision++;
      if (file.name === fail.uploadAfterCommit) { fail.uploadAfterCommit = ''; throw new ApiError('upload response lost'); }
      return copy(value);
    },
    send: async (_sid, payload: ChatMessageWrite) => {
      calls.push({ type: 'send', payload: copy(payload) });
      if (sends.has(payload.requestId)) return copy(sends.get(payload.requestId)!);
      if (payload.expectedStateRevision !== session.stateRevision) throw new ApiError('stale', 'stale_state');
      const message: ChatMessageResult['message'] = { messageId: `message-${messages.length + 1}`, sessionId: session.sessionId, role: 'user', content: payload.content, kind: 'message', sequence: messages.length + 1, createdAt: '', inputChannel: 'chat', historical: false,
        attachments: payload.attachmentIds.map(id => copy(materials.find(m => m.materialId === id)!)) };
      messages.push(message); session.stateRevision++;
      const result: ChatMessageResult = { session: copy(session), message, stateRevision: session.stateRevision, draftRevision: 1 };
      sends.set(payload.requestId, copy(result));
      if (fail.messageAfterCommit) { fail.messageAfterCommit = false; throw new ApiError('message response lost'); }
      return copy(result);
    },
  };
  return { services, session, materials, messages, calls, creates, fail };
}
const selected = (name: string): ChatAttachment => ({ localId: name, file: new File(['# 构造原文'], name, { type: 'text/markdown' }) });

test('raw chat text is sent exactly; independent sends are not keyword-parsed', async () => {
  const s = server(), text = '  如果我做过 API 集成，是不是更有优势？\n';
  const result = await new ChatSendOperation(text, [], undefined, s.services).run();
  assert.equal(result.message.content, text); assert.equal(result.session.targetDirection, null);
  assert.equal(s.calls.filter(c => c.type === 'create').length, 1);
  const payload = s.calls.find(c => c.type === 'send')!.payload as ChatMessageWrite;
  assert.equal(payload.content, text); assert.equal('decision' in payload, false);
  assert.equal(s.messages.length, 1);
});
test('double clicking a pending send shares one operation', async () => {
  const s = server(), operation = new ChatSendOperation('原话', [], undefined, s.services);
  const a = operation.run(), b = operation.run(); assert.equal(a, b);
  assert.deepEqual(await a, await b); assert.equal(s.messages.length, 1);
});

test('selected question association survives an unknown send response without authorizing a decision', async () => {
  const s=server();s.fail.messageAfterCommit=true;
  const operation=new ChatSendOperation('可以，但不要补写数据。',[],s.session,s.services,()=>{},'question-selected');
  await assert.rejects(operation.run());await operation.run();
  const calls=s.calls.filter(call=>call.type==='send').map(call=>call.payload as ChatMessageWrite);
  assert.equal(calls[0].replyToMessageId,'question-selected');assert.deepEqual(calls[0],calls[1]);
  assert.equal('decision' in calls[0],false);assert.equal(s.messages.length,1);
});
test('lost session creation response retries the same ID without another session', async () => {
  const s = server(); s.fail.createAfterCommit = true;
  const operation = new ChatSendOperation('不要写数字', [], undefined, s.services);
  await assert.rejects(operation.run()); assert.equal(operation.canReturnToEditing(), false);
  await operation.run(); const calls = s.calls.filter(c => c.type === 'create');
  assert.deepEqual(calls[0].payload, calls[1].payload); assert.equal(s.creates.size, 1); assert.equal(s.messages.length, 1);
});
test('partial uploads keep successful originals and retry unknown upload with identical metadata', async () => {
  const s = server(); s.fail.uploadAfterCommit = 'b.md';
  const operation = new ChatSendOperation('', [selected('a.md'), selected('b.md')], undefined, s.services);
  await assert.rejects(operation.run());
  assert.ok(operation.attachments()[0].material); assert.equal(operation.attachments()[1].material, undefined);
  assert.equal(s.materials.length, 2); assert.equal(s.messages.length, 0);
  const result = await operation.run(); const calls = s.calls.filter(c => c.type === 'upload');
  assert.equal(calls.filter(c => c.name === 'a.md').length, 1);
  assert.deepEqual(calls[1].payload, calls[2].payload);
  assert.equal(result.message.content, ''); assert.equal(result.message.attachments.length, 2);
  assert.equal(s.materials.length, 2); assert.equal(s.messages.length, 1);
});
test('lost message response repeats original ID and version, never duplicates raw message', async () => {
  const s = server(); s.fail.messageAfterCommit = true;
  const operation = new ChatSendOperation('可以', [], undefined, s.services);
  await assert.rejects(operation.run()); assert.equal(s.messages.length, 1); await operation.run();
  const calls = s.calls.filter(c => c.type === 'send'); assert.deepEqual(calls[0].payload, calls[1].payload);
  assert.equal(s.messages.length, 1); assert.equal(operation.isMessageSaved(), true);
});
test('after definite rejection, edited resend reuses uploaded originals', async () => {
  const s = server(); s.fail.rejectFile = 'broken.md';
  const failed = new ChatSendOperation('输入不能丢失', [selected('good.md'), selected('broken.md')], undefined, s.services);
  await assert.rejects(failed.run()); assert.equal(failed.canReturnToEditing(), true);
  const preserved = failed.attachments().filter(item => item.file?.name !== 'broken.md');
  assert.equal(preserved[0].material?.materialId, 'material-1');
  await new ChatSendOperation('修正后发送', preserved, failed.session, s.services).run();
  assert.equal(s.calls.filter(c => c.type === 'upload' && c.name === 'good.md').length, 1);
  assert.equal(s.messages.length, 1); assert.equal(s.materials.length, 1);
});
test('changed resume generation is rejected rather than silently rebasing attachments', async () => {
  const s = server(); const initial = structuredClone(s.session); s.session.currentResumeGeneration = 2;
  const operation = new ChatSendOperation('原话', [selected('a.md')], initial, s.services);
  await assert.rejects(operation.run(), e => e instanceof ApiError && e.code === 'stale_state');
  assert.equal(s.calls.filter(c => c.type === 'upload' || c.type === 'send').length, 0);
});
test('foreign saved attachment cannot cross a session boundary', async () => {
  const s = server(); const foreign: ChatMaterial = { sessionId: 'other', materialId: 'foreign', name: 'foreign.md', role: 'unknown', parseStatus: 'parsed', useStatus: 'pending', mediaType: 'text/markdown', byteSize: 2, current: true };
  const operation = new ChatSendOperation('原话', [{ localId: 'file', material: foreign }], structuredClone(s.session), s.services);
  await assert.rejects(operation.run(), e => e instanceof ApiError && e.code === 'scope_violation');
  assert.equal(s.messages.length, 0);
});
test('after acknowledged message, retrying a failed UI reload does not send again', async () => {
  const s = server(), operation = new ChatSendOperation('已保存原话', [], undefined, s.services);
  await operation.run(); assert.equal(operation.isMessageSaved(), true);
  s.fail.nextRead = true; await assert.rejects(s.services.read(s.session.sessionId));
  await operation.run(); await s.services.read(s.session.sessionId);
  assert.equal(s.calls.filter(c => c.type === 'send').length, 1);
});
