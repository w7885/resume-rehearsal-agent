import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DraftBuffer } from '../src/draftBuffer.ts';
import { ApiError } from '../src/api.ts';
import type { DraftWrite } from '../src/api.ts';
import type { DraftResult } from '../src/types.ts';

const state = (text: string, revision = 1): DraftResult => ({ session: { sessionId: 'synthetic', direction: '构造', stateRevision: revision, draftRevision: revision, currentResumeGeneration: 1, status: 'idle', mode: 'local', updatedAt: '' }, draft: { sessionId: 'synthetic', draftRevision: revision, markdown: text, blocks: [] }, stateRevision: revision, draftRevision: revision });

test('edits during an in-flight save are serialized; older response never erases new text', async () => {
  const calls: DraftWrite[] = []; let release!: () => void;
  const delayed = new Promise<void>(resolve => { release = resolve; });
  const b = new DraftBuffer(state('原稿'), async payload => { calls.push({ ...payload }); if (calls.length === 1) await delayed; return state(payload.markdown, payload.expectedStateRevision + 1); }, async () => state('原稿'));
  b.setText('第一处修改'); const first = b.flush();
  b.setText('更新后的文字'); assert.equal(b.getSnapshot().text, '更新后的文字');
  const second = b.flush(); assert.equal(first, second); release(); await first;
  assert.deepEqual(calls.map(c => c.markdown), ['第一处修改', '更新后的文字']);
  assert.deepEqual(calls.map(c => c.expectedStateRevision), [1, 2]);
  assert.notEqual(calls[0].requestId, calls[1].requestId);
  assert.equal(b.getSnapshot().status, 'saved'); assert.equal(b.getSnapshot().text, '更新后的文字');
});

test('unknown save result retries the original ID and body before saving later edits', async () => {
  const calls: DraftWrite[] = []; let fail = true;
  const b = new DraftBuffer(state('原稿'), async p => { calls.push({ ...p }); if (fail) { fail = false; throw new ApiError('连接中断'); } return state(p.markdown, p.expectedStateRevision + 1); }, async () => state('原稿'));
  b.setText('已提交但响应未知'); await assert.rejects(b.flush());
  assert.equal(b.getSnapshot().status, 'error'); assert.equal(b.getSnapshot().text, '已提交但响应未知');
  b.setText('失败后又编辑'); await b.flush();
  assert.deepEqual(calls[0], calls[1]); assert.notEqual(calls[1].requestId, calls[2].requestId);
  assert.equal(calls[2].markdown, '失败后又编辑'); assert.equal(calls[2].expectedStateRevision, 2);
});

test('switch must await flush; failed persistence keeps the buffer unsettled', async () => {
  const b = new DraftBuffer(state('原稿'), async () => { throw new ApiError('存储失败', 'storage_failed'); }, async () => state('原稿'));
  b.setText('不能丢失的文字'); let switched = false;
  await b.flush().then(() => { switched = true; }).catch(() => {});
  assert.equal(switched, false); assert.equal(b.hasUnsettled(), true); assert.equal(b.getSnapshot().text, '不能丢失的文字');
});

test('conflict retains local text and requires an explicit resolution with a new request', async () => {
  const calls: DraftWrite[] = []; let conflict = true;
  const b = new DraftBuffer(state('旧稿'), async p => { calls.push({ ...p }); if (conflict) { conflict = false; throw new ApiError('版本冲突', 'stale_state'); } return state(p.markdown, p.expectedStateRevision + 1); }, async () => state('其他标签的新稿', 8));
  b.setText('我的文字'); await assert.rejects(b.flush());
  assert.equal(b.getSnapshot().status, 'conflict'); assert.equal(b.getSnapshot().text, '我的文字');
  assert.equal(b.getSnapshot().remote?.draft.markdown, '其他标签的新稿');
  await assert.rejects(b.flush()); assert.equal(calls.length, 1);
  b.resolveConflict(true); await b.flush();
  assert.equal(calls[1].expectedStateRevision, 8); assert.notEqual(calls[0].requestId, calls[1].requestId);
  assert.equal(b.getSnapshot().saved, '我的文字');
});

test('adopting remote text only occurs after the user explicitly chooses it', async () => {
  let calls = 0;
  const b = new DraftBuffer(state('旧稿'), async () => { calls++; throw new ApiError('过期', 'stale_state'); }, async () => state('服务器稿', 4));
  b.setText('本地稿'); await assert.rejects(b.flush()); b.resolveConflict(false);
  assert.equal(b.getSnapshot().text, '服务器稿'); assert.equal(b.getSnapshot().status, 'saved');
  await b.flush(); assert.equal(calls, 1);
});

test('reverting visible text does not erase an uncertain previously submitted save', async () => {
  const calls: DraftWrite[] = []; let fail = true;
  const b = new DraftBuffer(state('原稿'), async p => { calls.push({ ...p }); if (fail) { fail = false; throw new ApiError('超时'); } return state(p.markdown, p.expectedStateRevision + 1); }, async () => state('原稿'));
  b.setText('可能已保存的新稿'); await assert.rejects(b.flush()); b.setText('原稿');
  assert.equal(b.hasUnsettled(), true); await b.flush();
  assert.deepEqual(calls.map(c => c.markdown), ['可能已保存的新稿', '可能已保存的新稿', '原稿']);
});
