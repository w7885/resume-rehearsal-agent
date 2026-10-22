import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readConversation } from '../src/api.ts';

const original=globalThis.fetch;
const session={sessionId:'s',direction:'新对话',stateRevision:2,draftRevision:1,currentResumeGeneration:1,status:'idle',mode:'local',updatedAt:'',title:'构造',directionStatus:'pending',targetDirection:null};
const message={messageId:'a',sessionId:'s',role:'assistant',content:'构造询问',kind:'message',sequence:1,createdAt:'',inputChannel:'chat',historical:false,attachments:[],replyToMessageId:'u',assistant:{kind:'question',mode:'synthetic',replyToUserMessageId:'u',questionState:'awaiting_reply',sourceRefs:[]}};
const value={session,draft:{sessionId:'s',draftRevision:1,markdown:'原稿',blocks:[]},messages:[message],materials:[],capabilities:{agentAvailable:false,decisionExecutionAvailable:false}};

test('saved synthetic reply metadata restores without claiming that a real Agent is available',async()=>{
  try{
    globalThis.fetch=async()=>new Response(JSON.stringify({ok:true,data:value}));
    const result=await readConversation('s');assert.equal(result.messages[0].assistant?.mode,'synthetic');assert.equal(result.messages[0].assistant?.questionState,'awaiting_reply');assert.equal(result.capabilities.agentAvailable,false);
  }finally{globalThis.fetch=original;}
});

test('malformed question context and invented execution state cannot be displayed as valid replies',async()=>{
  try{
    for(const invalid of [{...message,assistant:{...message.assistant,questionState:'accepted'}},{...message,assistant:{...message.assistant,sourceRefs:[{sourceId:'missing-version'}]}},{...message,replyToMessageId:{id:'u'}}]){
      globalThis.fetch=async()=>new Response(JSON.stringify({ok:true,data:{...value,messages:[invalid]}}));
      await assert.rejects(readConversation('s'));
    }
  }finally{globalThis.fetch=original;}
});
