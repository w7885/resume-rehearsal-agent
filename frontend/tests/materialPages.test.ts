import { test } from 'node:test';
import assert from 'node:assert/strict';
import { prepareMaterialPage, readMaterialPages, materialPageUrl } from '../src/api.ts';

const original = globalThis.fetch;
const pending = { page: 1, status: 'pending', width: null, height: null, errorCode: null, errorMessage: null };
const manifest = { materialId: 'material-a', renderVersion: 'local-v1', pageCount: 2, limit: 40, historical: false, recognitionAvailable: false, pages: [pending, { ...pending, page: 2 }] };
const response = (data: unknown) => new Response(JSON.stringify({ ok: true, data }));

test('page metadata rejects wrong attachment, reordered pages or invalid ready dimensions', async () => {
  try {
    globalThis.fetch = async () => response(manifest);
    assert.equal((await readMaterialPages('session-a','material-a')).recognitionAvailable, false);
    for (const bad of [{ ...manifest, materialId:'foreign' }, { ...manifest, pages:[manifest.pages[1], manifest.pages[0]] }, { ...manifest, pages:[{ ...pending,status:'ready' },manifest.pages[1]] }]) {
      globalThis.fetch = async () => response(bad);
      await assert.rejects(readMaterialPages('session-a','material-a'));
    }
    assert.equal(materialPageUrl('session-a','material-a',2), '/api/sessions/session-a/materials/material-a/pages/2/image');
  } finally { globalThis.fetch = original; }
});

test('page preparation posts caller request unchanged without file paths or model parameters', async () => {
  try {
    const payload = { requestId:'same-preparation', expectedStateRevision:3, pageNumber:2 };
    let sent: unknown, url='';
    globalThis.fetch = async (input, init) => { url=String(input); sent=JSON.parse(String(init?.body)); return response(manifest); };
    await prepareMaterialPage('session-a','material-a',payload);
    assert.deepEqual(sent,payload); assert.match(url, /materials\/material-a\/pages\/prepare$/);
  } finally { globalThis.fetch = original; }
});
