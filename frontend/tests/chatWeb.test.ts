import { test } from 'node:test';
import assert from 'node:assert/strict';
import { safeExternalUrl, isExternalReferences, referenceNotice, referenceTime } from '../src/chatWeb.ts';
import { readConversation, readWebSettings, writeWebSettings, ApiError } from '../src/api.ts';
const ref={referenceId:'external-1',title:'<script>alert(1)</script>',url:'https://example.com/jobs/1',content:'岗位原文',retrievedAt:'2026-10-28T00:00:00Z',publishedAt:null,matchStatus:'matched',hiringStatus:'unknown',contentStatus:'partial'};
test('reference links reject executable, credential and malformed URLs',()=>{
  for(const url of ['javascript:alert(1)','data:text/html,x','file:///C:/file','https://user:pass@example.com','broken']) assert.equal(safeExternalUrl(url),null);
  assert.equal(safeExternalUrl(ref.url),ref.url);
});
test('external references remain optional for older saved conversations but malformed data is rejected',()=>{
  assert.equal(isExternalReferences(undefined),true);assert.equal(isExternalReferences([ref]),true);
  assert.equal(isExternalReferences([{...ref,content:42}]),false);assert.equal(isExternalReferences(null),false);
  assert.match(referenceNotice(ref),/未核实/);assert.match(referenceNotice({...ref,hiringStatus:'conflict'}),/冲突/);assert.equal(referenceTime('bad date'),'时间未提供');
});
test('web settings API checks shape and preserves distinct permission revision',async()=>{
  const original=globalThis.fetch;const payload={requestId:'stable',enabled:true,expectedWebSettingsRevision:7};let sent:unknown;
  try {globalThis.fetch=async(_url,init)=>{sent=JSON.parse(String(init?.body));return Response.json({ok:true,data:{enabled:true,webSettingsRevision:8}});};
    assert.deepEqual(await writeWebSettings('s',payload),{enabled:true,webSettingsRevision:8});assert.deepEqual(sent,payload);
    globalThis.fetch=async()=>Response.json({ok:true,data:{enabled:'true',webSettingsRevision:8}});await assert.rejects(readWebSettings('s'),e=>e instanceof ApiError&&e.code==='invalid_response');
    globalThis.fetch=async()=>Response.json({detail:'Not Found'},{status:404});await assert.rejects(readWebSettings('s'),e=>e instanceof ApiError&&e.code==='not_found');
  }finally{globalThis.fetch=original;}
});
test('conversation validates external metadata instead of allowing malformed citation to crash UI',async()=>{
  const original=globalThis.fetch;
  const session={sessionId:'s',direction:'',stateRevision:1,draftRevision:1,currentResumeGeneration:1,status:'active',mode:'local',updatedAt:'',title:'会话',directionStatus:'pending',targetDirection:null};
  const message={messageId:'m',sessionId:'s',role:'assistant',content:'回复',kind:'reply',sequence:1,createdAt:'',inputChannel:'chat',historical:false,attachments:[],assistant:{kind:'reply',mode:'host',replyToUserMessageId:'u',questionState:null,sourceRefs:[],externalReferences:[ref]}};
  const conversation={session,messages:[message],materials:[],draft:{sessionId:'s',draftRevision:1,markdown:'',blocks:[]},capabilities:{agentAvailable:true,decisionExecutionAvailable:true}};
  try{globalThis.fetch=async()=>Response.json({ok:true,data:conversation});assert.equal((await readConversation('s')).messages[0].assistant?.externalReferences?.[0].referenceId,'external-1');
    message.assistant.externalReferences[0]={...ref,content:null as unknown as string};await assert.rejects(readConversation('s'));
  }finally{globalThis.fetch=original;}
});
