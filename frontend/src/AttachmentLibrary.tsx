import { useEffect, useRef, useState } from 'react';
import * as api from './api';
import type { ChatMaterial, Conversation } from './types';

type Task = { kind:'order'; payload:api.ImageOrderWrite; confirmed:boolean } | { kind:'retry'; materialId:string; payload:{requestId:string;expectedStateRevision:number}; confirmed:boolean };
const image = (material:ChatMaterial) => material.current && ['image/png','image/jpeg'].includes(material.mediaType);

export default function AttachmentLibrary({conversation,disabled,attached,onAdd,onSelect,onGuard,beforeChange,onChanged}:{
  conversation:Conversation;disabled:boolean;attached:string[];onAdd:(material:ChatMaterial)=>void;onSelect:(material:ChatMaterial)=>void;
  onGuard:(value:boolean)=>void;beforeChange:()=>Promise<void>;onChanged:()=>Promise<void>;
}) {
  const [ordering,setOrdering]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('');
  const [pending,setPending]=useState(false);
  const task=useRef<Task|null>(null),gate=useRef(false);
  const sid=conversation.session.sessionId;
  const images=conversation.materials.filter(image);
  useEffect(()=>{onGuard(busy||pending);return()=>onGuard(false);},[busy,pending,onGuard]);
  const perform=async(build?: (fresh:Conversation)=>Task)=>{
    if(gate.current)return;
    gate.current=true;setBusy(true);setError('');setNotice('');
    try{
      if(!task.current){
        if(!build)return;
        await beforeChange();
        task.current=build(await api.readConversation(sid));setPending(true);
      }
      const action=task.current;
      if(!action.confirmed){
        if(action.kind==='order')await api.reorderImages(sid,action.payload);
        else await api.requestRecognitionRetry(sid,action.materialId,action.payload);
        action.confirmed=true;
      }
      await onChanged();
      setNotice(action.kind==='order'?'图片顺序已更新。':'已记录，等待识别服务处理。');
      task.current=null;setPending(false);
    }catch(failure){
      setError(task.current?.confirmed?'操作已保存，暂未读回，请重试读取。':failure instanceof Error?failure.message:'操作未确认，请重试。');
      if(failure instanceof api.ApiError&&['stale_state','invalid_input','scope_violation','source_not_found','session_not_found','request_conflict','unsupported_format'].includes(failure.code)&&!task.current?.confirmed){task.current=null;setPending(false);}
    }finally{gate.current=false;setBusy(false);}
  };
  const move=(mid:string,delta:number)=>{void perform(fresh=>{
    const ids=fresh.materials.filter(image).map(material=>material.materialId),index=ids.indexOf(mid);
    if(index<0||index+delta<0||index+delta>=ids.length)throw new api.ApiError('图片顺序已变化，请重新选择。','stale_state');
    [ids[index],ids[index+delta]]=[ids[index+delta],ids[index]];
    return {kind:'order',confirmed:false,payload:{requestId:api.requestId(),expectedStateRevision:fresh.session.stateRevision,materialIds:ids}};
  });};
  const retry=(mid:string)=>{void perform(fresh=>({kind:'retry',materialId:mid,confirmed:false,payload:{requestId:api.requestId(),expectedStateRevision:fresh.session.stateRevision}}));};
  const locked=disabled||busy||pending;
  return <>{images.length>1&&conversation.session.mode==='local'&&<button disabled={locked} onClick={()=>setOrdering(!ordering)}>{ordering?'完成顺序调整':'调整图片顺序'}</button>}
    {error&&<div className="cp-live-error" role="alert"><p>{error}</p>{pending&&<button disabled={busy} onClick={()=>{void perform();}}>{task.current?.confirmed?'重试读取':'重试原操作'}</button>}</div>}{notice&&<p role="status">{notice}</p>}
    <ul className="cp-live-materials">{conversation.materials.map(material=>{const index=images.findIndex(item=>item.materialId===material.materialId);return <li key={material.materialId}>
      <button className="cp-live-material-name" disabled={busy||pending} onClick={()=>onSelect(material)}>{material.name}</button>
      {ordering&&index>=0&&<div className="cp-image-order"><button disabled={locked||index===0} aria-label={`上移 ${material.name}`} onClick={()=>move(material.materialId,-1)}>上移</button><button disabled={locked||index===images.length-1} aria-label={`下移 ${material.name}`} onClick={()=>move(material.materialId,1)}>下移</button></div>}
      <div className="cp-library-actions"><button disabled={locked||!material.current||attached.includes(material.materialId)} onClick={()=>onAdd(material)}>附到下一条消息</button>
      {material.current&&material.recognitionReport&&conversation.session.mode==='local'&&<button disabled={locked} onClick={()=>retry(material.materialId)}>申请重新识别</button>}</div>
    </li>;})}</ul>
  </>;
}
