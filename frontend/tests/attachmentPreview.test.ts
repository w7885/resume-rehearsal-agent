import { test } from 'node:test';
import assert from 'node:assert/strict';
import { AttachmentImagePreview, attachmentText } from '../src/attachmentPreview.ts';
import { ApiError } from '../src/api.ts';
import type { MaterialPages, Source } from '../src/types.ts';

const collection = (count: number | null, ready: number[] = []): MaterialPages => ({ materialId:'m',renderVersion:'v1',pageCount:count,limit:40,historical:false,recognitionAvailable:false,pages:Array.from({length:count ?? 1},(_,index) => ({page:index+1,status:ready.includes(index+1)?'ready':'pending',width:ready.includes(index+1)?100:null,height:ready.includes(index+1)?200:null,errorCode:null,errorMessage:null})) });

test('plain attachment text shows latest saved content once, in document order', () => {
  const source = (id: string, revision: number, text: string): Source => ({sourceId:id,sourceRevision:revision,text,role:'unknown',origin:{kind:'material'}});
  assert.equal(attachmentText([source('first',1,'旧文'),source('first',2,'修正后的正文'),source('second',1,'后续段落')]), '修正后的正文\n\n后续段落');
});

test('opening prepares missing images in native order without user page actions and reuses cache', async () => {
  let value=collection(null); const calls: number[]=[];
  const preview=new AttachmentImagePreview('s','m',{read:async()=>value,revision:async()=>3,prepare:async(_s,_m,payload)=>{calls.push(payload.pageNumber);value=collection(3,Array.from({length:payload.pageNumber},(_,i)=>i+1));return value;}});
  await preview.run(); assert.deepEqual(calls,[1,2,3]); assert.ok(preview.getSnapshot()?.pages.every(p=>p.status==='ready'));
  await preview.run(); assert.deepEqual(calls,[1,2,3]);
});

test('lost preparation response is confirmed by reading cache rather than repeating work', async () => {
  let value=collection(null), calls=0;
  const preview=new AttachmentImagePreview('s','m',{read:async()=>value,revision:async()=>2,prepare:async()=>{calls++;value=collection(1,[1]);throw new ApiError('response lost');}});
  await assert.rejects(preview.run()); await preview.run(); assert.equal(calls,1); assert.equal(preview.getSnapshot()?.pages[0].status,'ready');
});

test('unknown unsuccessful preparation retries exact payload and closing stops remaining pages', async () => {
  const calls: unknown[]=[]; let fail=true;
  const preview=new AttachmentImagePreview('s','m',{read:async()=>collection(2),revision:async()=>4,prepare:async(_s,_m,payload)=>{calls.push({...payload});if(fail){fail=false;throw new ApiError('connection lost');}preview.pause();return collection(2,[1]);}});
  await assert.rejects(preview.run()); await preview.run(); assert.deepEqual(calls[0],calls[1]); assert.equal(calls.length,2);
});
