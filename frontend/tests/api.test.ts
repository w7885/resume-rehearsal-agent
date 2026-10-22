import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ApiError, listSessions, readSession, writeDraft, uploadMaterial } from '../src/api.ts';

const session = { sessionId: 'synthetic', direction: '构造', stateRevision: 1, draftRevision: 1, currentResumeGeneration: 1, updatedAt: '', mode: 'local' };
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
