import { test } from 'node:test';
import assert from 'node:assert/strict';
import { reorderImages, requestRecognitionRetry } from '../src/api.ts';

const original=globalThis.fetch;
const conversation={session:{sessionId:'s',direction:'新对话',stateRevision:3,draftRevision:1,currentResumeGeneration:1,status:'idle',mode:'local',updatedAt:'',title:'构造',directionStatus:'pending',targetDirection:null},draft:{sessionId:'s',draftRevision:1,markdown:'原稿',blocks:[]},messages:[],materials:[],capabilities:{agentAvailable:false,decisionExecutionAvailable:false}};

test('image order sends absolute image IDs and caller version unchanged',async()=>{
  try{
    let url='',sent:unknown;
    globalThis.fetch=async(input,init)=>{url=String(input);sent=JSON.parse(String(init?.body));return new Response(JSON.stringify({ok:true,data:conversation}));};
    const payload={requestId:'same-order',expectedStateRevision:2,materialIds:['image-b','image-a']};
    assert.equal((await reorderImages('s',payload)).draft.markdown,'原稿');assert.deepEqual(sent,payload);assert.match(url,/materials\/image-order$/);
  }finally{globalThis.fetch=original;}
});

test('recognition retry only records pending intent, with no model or budget parameters',async()=>{
  try{
    let sent:unknown;
    globalThis.fetch=async(_input,init)=>{sent=JSON.parse(String(init?.body));return new Response(JSON.stringify({ok:true,data:{retryId:'retry-a',status:'pending',stateRevision:4}}));};
    const payload={requestId:'same-intent',expectedStateRevision:3};
    assert.equal((await requestRecognitionRetry('s','m',payload)).status,'pending');assert.deepEqual(sent,payload);
    globalThis.fetch=async()=>new Response(JSON.stringify({ok:true,data:{retryId:'r',status:'completed',stateRevision:4}}));
    await assert.rejects(requestRecognitionRetry('s','m',payload));
  }finally{globalThis.fetch=original;}
});
