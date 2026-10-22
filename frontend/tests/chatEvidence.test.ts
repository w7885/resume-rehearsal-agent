import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ApiError, correctMaterialSource, readMaterial, readSource } from '../src/api.ts';

const original = globalThis.fetch;
const source = { sourceId: 'source-a', sourceRevision: 1, text: '当时的原文', role: 'supplement', origin: { kind: 'message', messageId: 'message-a' } };
function respond(data: unknown) { globalThis.fetch = async () => new Response(JSON.stringify({ ok: true, data }), { headers: { 'Content-Type': 'application/json' } }); }

test('evidence requests the exact quoted revision and rejects substitution of latest or foreign source', async () => {
  try {
    let url = '';
    globalThis.fetch = async input => { url = String(input); return new Response(JSON.stringify({ ok: true, data: { source, historical: true } })); };
    const value = await readSource('session-a', { sourceId: 'source-a', sourceRevision: 1 });
    assert.match(url, /sessions\/session-a\/sources\/source-a\/revisions\/1$/);
    assert.equal(value.source.text, '当时的原文'); assert.equal(value.historical, true);
    for (const other of [{ ...source, sourceRevision: 2 }, { ...source, sourceId: 'source-b' }, { ...source, origin: null }]) {
      respond({ source: other, historical: false });
      await assert.rejects(readSource('session-a', { sourceId: 'source-a', sourceRevision: 1 }), error => error instanceof ApiError && error.code === 'invalid_response');
    }
  } finally { globalThis.fetch = original; }
});

test('material inspection preserves separate text snapshots and rejects wrong material or incomplete origins', async () => {
  try {
    respond({ material: { materialId: 'material-a' }, sources: [source, { ...source, sourceRevision: 2, text: '后来纠正的原文' }] });
    const value = await readMaterial('session-a', 'material-a');
    assert.deepEqual(value.sources.map(s => [s.sourceRevision, s.text]), [[1, '当时的原文'], [2, '后来纠正的原文']]);
    respond({ material: { materialId: 'material-b' }, sources: [] });
    await assert.rejects(readMaterial('session-a', 'material-a'));
    respond({ material: { materialId: 'material-a' }, sources: [{ text: '来源不完整' }] });
    await assert.rejects(readMaterial('session-a', 'material-a'));
    respond({ material: { materialId: 'material-a' }, sources: [] });
    assert.deepEqual((await readMaterial('session-a', 'material-a')).sources, []);
  } finally { globalThis.fetch = original; }
});

test('material report validates warnings rather than presenting malformed parsing success', async () => {
  try {
    respond({ material: { materialId: 'material-a', parseReport: { status: 'partial', message: '部分结果', warnings: [{ code: 'empty_page', message: '第 2 页未提取文字' }] } }, sources: [] });
    assert.equal((await readMaterial('session-a', 'material-a')).material.parseReport?.status, 'partial');
    respond({ material: { materialId: 'material-a', parseReport: { status: 'parsed', message: '完成', warnings: [null] } }, sources: [] });
    await assert.rejects(readMaterial('session-a', 'material-a'));
  } finally { globalThis.fetch = original; }
});

test('source correction retains caller request and versions; rejects a substituted revision', async () => {
  const payload = { requestId: 'same-correction', expectedStateRevision: 8, expectedSourceRevision: 1, text: '协助接口测试' };
  try {
    let sent: unknown, url = '';
    globalThis.fetch = async (input, init) => {
      url = String(input); sent = JSON.parse(String(init?.body));
      return new Response(JSON.stringify({ ok: true, data: { source: { ...source, sourceRevision: 2, text: payload.text }, stateRevision: 9, draftRevision: 1 } }));
    };
    assert.equal((await correctMaterialSource('session-a', 'material-a', 'source-a', payload)).source.text, payload.text);
    assert.deepEqual(sent, payload); assert.match(url, /materials\/material-a\/sources\/source-a\/revisions$/);
    respond({ source: { ...source, sourceRevision: 3 }, stateRevision: 9, draftRevision: 1 });
    await assert.rejects(correctMaterialSource('session-a', 'material-a', 'source-a', payload));
  } finally { globalThis.fetch = original; }
});
