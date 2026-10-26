import { test } from 'node:test';
import assert from 'node:assert/strict';
import { appendVoiceText, VoiceInputOperation } from '../src/voiceInput.ts';
import type { CaptureOptions, VoiceServices } from '../src/voiceInput.ts';
import { pcmWave } from '../src/voiceAudio.ts';
import { readVoiceStatus, transcribeWave } from '../src/voiceApi.ts';

function deferred<T>() { let resolve!: (value:T)=>void, reject!: (reason:unknown)=>void; const promise=new Promise<T>((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject}; }
function fixture() {
  const audio=new Blob(['audio'],{type:'audio/wav'});
  let captures=0, stops=0, cancels=0, calls=0, options!:CaptureOptions;
  const delivered: Array<{key:string;text:string}>=[];
  const services:VoiceServices={capture:async value=>{captures++;options=value;return {stop:async()=>{stops++;return audio;},cancel:()=>{cancels++;}};},
    transcribe:async()=>{calls++;return '识别文字';}};
  const op=new VoiceInputOperation(services,()=>{},(key,text)=>delivered.push({key,text}));
  return {op,services,audio,delivered,stats:()=>({captures,stops,cancels,calls}),options:()=>options};
}

test('voice appends to the latest typed text and never submits a chat message',async()=>{
  const f=fixture();let draft='已有文字';f.op.onText=(_key,text)=>{draft=appendVoiceText(draft,text);};
  await f.op.start('one');draft='用户录音期间修改了文字';
  await f.op.stop();assert.equal(draft,'用户录音期间修改了文字\n识别文字');assert.equal(f.op.blocking(),false);
  assert.deepEqual(f.stats(),{captures:1,stops:1,cancels:0,calls:1});
});
test('cancel during permission wait releases a late microphone without delivering text',async()=>{
  const f=fixture(), permission=deferred<{stop():Promise<Blob>;cancel():void}>();
  f.services.capture=()=>permission.promise;
  const start=f.op.start('old');f.op.cancel();let closed=0;
  permission.resolve({stop:async()=>f.audio,cancel:()=>{closed++;}});await start;
  assert.equal(closed,1);assert.equal(f.op.state.phase,'idle');assert.equal(f.delivered.length,0);
});
test('session switch cancellation ignores an old transcription even if transport resolves late',async()=>{
  const f=fixture(), transcript=deferred<string>();f.services.transcribe=()=>transcript.promise;
  await f.op.start('old');const stop=f.op.stop();await Promise.resolve();f.op.cancel();
  transcript.resolve('旧会话文字');await stop;assert.equal(f.delivered.length,0);assert.equal(f.op.state.phase,'idle');
});
test('explicit transcription retry reuses the exact recording and delivers only once',async()=>{
  const f=fixture();let count=0;const received:Blob[]=[];
  f.services.transcribe=async audio=>{received.push(audio);if(count++===0)throw new Error('temporary');return '重试结果';};
  await f.op.start('one');await f.op.stop();assert.equal(f.op.state.canRetry,true);assert.equal(f.op.blocking(),true);
  await f.op.retry();assert.equal(received[0],received[1]);assert.deepEqual(f.delivered,[{key:'one',text:'重试结果'}]);
  assert.equal(f.stats().captures,1);
});
test('double stop and double start do not duplicate recording or transcription',async()=>{
  const f=fixture();await Promise.all([f.op.start('one'),f.op.start('one')]);await Promise.all([f.op.stop(),f.op.stop()]);
  assert.equal(f.stats().captures,1);assert.equal(f.stats().calls,1);assert.equal(f.delivered.length,1);
});
test('permission error permits typing and retry cancellation discards only unsent audio',async()=>{
  const f=fixture();f.services.capture=async()=>{throw new Error('麦克风权限未允许');};
  await f.op.start('one');assert.equal(f.op.state.phase,'error');assert.equal(f.op.blocking(),false);
  f.op.cancel();assert.equal(f.op.state.error,'');assert.equal(f.delivered.length,0);
});
test('text overflow never truncates an existing draft and keeps recording available for retry',async()=>{
  const f=fixture();const draft='字'.repeat(50000);f.op.onText=(_key,text)=>{appendVoiceText(draft,text);};
  await f.op.start('one');await f.op.stop();assert.equal(f.op.state.phase,'error');assert.equal(f.op.state.canRetry,true);
  assert.equal(draft.length,50000);assert.throws(()=>appendVoiceText('old','  '));
});
test('60 second limit requests one controlled stop',async()=>{
  const f=fixture();await f.op.start('one');f.options().onLimit();await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(f.stats().stops,1);assert.equal(f.delivered.length,1);
});
test('PCM encoding is mono 16kHz WAV with finite, clipped samples',async()=>{
  const audio=pcmWave([new Float32Array(24000).fill(2)],48000);const view=new DataView(await audio.arrayBuffer());
  assert.equal(audio.type,'audio/wav');assert.equal(view.getUint16(22,true),1);assert.equal(view.getUint32(24,true),16000);
  assert.equal(view.getUint32(40,true),16000);assert.equal(view.getInt16(44,true),32767);
  assert.throws(()=>pcmWave([new Float32Array(10)],16000));
});
test('voice transport uses only the local transcription route and validates its envelope',async()=>{
  const old=globalThis.fetch;const calls:Array<{url:string;body:unknown}>=[];
  try {
    globalThis.fetch=async(url,init)=>{calls.push({url:String(url),body:init?.body});return new Response(JSON.stringify({ok:true,data:{text:'本地结果',language:'zh-CN',local:true}}));};
    const audio=new Blob(['wav']);assert.equal(await transcribeWave(audio,new AbortController().signal),'本地结果');
    assert.equal(calls[0].url,'/api/voice/transcribe');assert.equal(calls[0].body,audio);
    globalThis.fetch=async()=>new Response(JSON.stringify({ok:true,data:{available:'yes',language:'zh-CN',maxSeconds:60,reason:null}}));
    await assert.rejects(readVoiceStatus(new AbortController().signal));
  } finally {globalThis.fetch=old;}
});
