import { test } from 'node:test';
import assert from 'node:assert/strict';
import { WebSettingsController } from '../src/webSettings.ts';
import { ApiError } from '../src/api.ts';
import type { WebSettingsWrite } from '../src/api';

const off = { enabled: false, webSettingsRevision: 1 };
test('new session choice is persisted before analysis preparation resolves', async () => {
  let current = off, writes = 0;
  const c = new WebSettingsController(undefined, { read: async () => current, write: async (_sid, p) => { writes++; current = { enabled:p.enabled, webSettingsRevision:2 }; return current; } });
  await c.prepare('new-session', true);
  assert.equal(writes, 1); assert.equal(c.get('new-session').settings?.enabled, true);
});
test('normal closed session does not write web settings during analysis', async () => {
  let writes = 0;
  const c = new WebSettingsController(undefined, { read: async () => off, write: async () => { writes++; return off; } });
  await c.prepare('s'); assert.equal(writes, 0);
});
test('unknown save keeps last confirmed status and retries exactly the same request', async () => {
  let current = off; const sent: WebSettingsWrite[] = [];
  const c = new WebSettingsController(undefined, { read: async () => current, write: async (_sid, p) => {
    sent.push(p); current = { enabled:true, webSettingsRevision:2 };
    if (sent.length === 1) throw new ApiError('network lost'); return current;
  } });
  await c.load('s'); await c.change('s', true);
  assert.equal(c.get('s').settings?.enabled, false); assert.equal(c.get('s').phase, 'error'); assert.ok(c.get('s').pending);
  await c.change('s', false); assert.equal(sent.length, 1);
  await c.retry('s'); assert.deepEqual(sent[0], sent[1]); assert.equal(c.get('s').settings?.enabled, true); assert.equal(c.get('s').pending, undefined);
});
test('duplicate click joins one in-flight save; closing remains a distinct confirmed request', async () => {
  let current = off, calls = 0; let release!: () => void;
  const wait = new Promise<void>(r => { release = r; });
  const c = new WebSettingsController(undefined, { read: async () => current, write: async (_sid, p) => { calls++; await wait; current = { enabled:p.enabled, webSettingsRevision:current.webSettingsRevision+1 }; return current; } });
  await c.load('s'); const job = c.change('s', true); await c.change('s', true); assert.equal(calls, 1);
  release(); await job; await c.change('s', false); assert.equal(calls, 2); assert.equal(c.get('s').settings?.enabled, false);
});
test('version conflict reads current setting without silently reapplying desired permission', async () => {
  let current = off, writes = 0;
  const c = new WebSettingsController(undefined, { read: async () => current, write: async () => { writes++; current = {enabled:false,webSettingsRevision:3}; throw new ApiError('changed','stale_state'); } });
  await c.load('s'); await c.change('s',true);
  assert.equal(writes, 1); assert.equal(c.get('s').settings?.webSettingsRevision, 3); assert.equal(c.get('s').pending, undefined); assert.match(c.get('s').error,/重新选择/);
});
test('replayed old success displays latest permission from another window', async () => {
  let reads = 0;
  const c = new WebSettingsController(undefined, { read: async () => ++reads === 1 ? off : {enabled:false,webSettingsRevision:3}, write: async () => ({enabled:true,webSettingsRevision:2}) });
  await c.load('s'); await c.change('s', true); assert.deepEqual(c.get('s').settings,{enabled:false,webSettingsRevision:3});
});
test('late setting responses are confined to their original session', async () => {
  let release!: () => void; const wait = new Promise<void>(r => { release=r; });
  const c = new WebSettingsController(undefined, {read:async sid => { if (sid==='a') await wait; return {enabled:sid==='a',webSettingsRevision:1}; },write:async()=>off});
  const pending=c.load('a'); await c.load('b'); release(); await pending;
  assert.equal(c.get('b').settings?.enabled,false);assert.equal(c.get('a').settings?.enabled,true);
});
test('old backend keeps offline analysis usable but never accepts enabled choice', async () => {
  const c=new WebSettingsController(undefined,{read:async()=>{throw new ApiError('not found','not_found');},write:async()=>off});
  await c.prepare('s',false);assert.equal(c.get('s').phase,'unsupported');await assert.rejects(c.prepare('s',true));
});
test('read failure after save retains original write for safe replay and blocks analysis', async () => {
  let reads=0;const sent:WebSettingsWrite[]=[];
  const c=new WebSettingsController(undefined,{read:async()=>{ if (++reads>1) throw new ApiError('lost');return off; },write:async(_sid,p)=>{sent.push(p);return {enabled:true,webSettingsRevision:2};}});
  await c.load('s');await c.change('s',true);assert.ok(c.get('s').pending);await assert.rejects(c.prepare('s'));assert.deepEqual(sent[0],sent[1]);
});

test('a rejected new-session choice is not silently rebased by retrying analysis', async () => {
  let current=off,writes=0;
  const c=new WebSettingsController(undefined,{read:async()=>current,write:async()=>{writes++;current={enabled:false,webSettingsRevision:3};throw new ApiError('conflict','stale_state');}});
  await assert.rejects(c.prepare('s',true));await c.load('s');await assert.rejects(c.prepare('s',true));assert.equal(writes,1);
});
