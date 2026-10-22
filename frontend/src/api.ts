export type Health = { status: 'ok'; model_configured: boolean };

export async function readHealth(signal: AbortSignal): Promise<Health> {
  const response = await fetch('/api/health', { signal, cache: 'no-store' });
  if (!response.ok) throw new Error('服务暂时无法响应，请稍后重新检查。');
  const body: unknown = await response.json();
  if (
    typeof body !== 'object' || body === null ||
    !('status' in body) || body.status !== 'ok' ||
    !('model_configured' in body) || typeof body.model_configured !== 'boolean'
  ) throw new Error('服务返回了无法识别的状态，请重新检查。');
  return { status: body.status, model_configured: body.model_configured };
}

import type { ChatMaterial, ChatMessageResult, ChatSession, Conversation, Detail, DraftResult, Session, Source } from './types';

export class ApiError extends Error {
  code: string;
  constructor(message: string, code = 'network_error') { super(message); this.name = 'ApiError'; this.code = code; }
}
export const requestId = () => crypto.randomUUID();
const object = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value);
const integer = (value: unknown) => Number.isSafeInteger(value) && Number(value) > 0;
function isSession(value: unknown): value is Session {
  return object(value) && typeof value.sessionId === 'string' && typeof value.direction === 'string' &&
    integer(value.stateRevision) && integer(value.draftRevision) && integer(value.currentResumeGeneration) &&
    (value.mode === 'local' || value.mode === 'mock') && typeof value.updatedAt === 'string';
}
const isReference = (value: unknown) => object(value) && typeof value.sourceId === 'string' && integer(value.sourceRevision) && typeof value.quote === 'string';
const isSource = (value: unknown) => object(value) && typeof value.sourceId === 'string' && integer(value.sourceRevision) && typeof value.text === 'string' && typeof value.role === 'string' && object(value.origin) && typeof value.origin.kind === 'string';
function hasDraft(value: unknown): value is DraftResult {
  return object(value) && isSession(value.session) && object(value.draft) && typeof value.draft.markdown === 'string' &&
    Array.isArray(value.draft.blocks) && value.draft.blocks.every(b => object(b) && typeof b.blockId === 'string' && integer(b.blockRevision) && typeof b.markdown === 'string' && Array.isArray(b.sourceRefs) && b.sourceRefs.every(isReference));
}
function isDetail(value: unknown): value is Detail {
  if (!object(value)) return false;
  const record: Record<string, unknown> = value;
  const references = (v: unknown) => Array.isArray(v) && v.every(r => object(r) && typeof r.sourceId === 'string' && integer(r.sourceRevision) && typeof r.quote === 'string');
  return hasDraft(value) && ['messages', 'materials', 'suggestions', 'issues', 'events'].every(key => Array.isArray(record[key])) &&
    (record.messages as unknown[]).every(m => object(m) && typeof m.messageId === 'string' && typeof m.content === 'string' && typeof m.kind === 'string' && typeof m.role === 'string') &&
    (record.materials as unknown[]).every(m => object(m) && ['materialId', 'name', 'role', 'parseStatus', 'useStatus', 'mediaType'].every(k => typeof m[k] === 'string')) &&
    (record.suggestions as unknown[]).every(s => object(s) && typeof s.suggestionId === 'string' && typeof s.before === 'string' && typeof s.after === 'string' && typeof s.reason === 'string' &&
      ['replace', 'insert_after', 'delete'].includes(String(s.operation)) && ['pending', 'accepted', 'rejected', 'invalidated'].includes(String(s.status)) &&
      object(s.target) && typeof s.target.blockId === 'string' && integer(s.target.expectedBlockRevision) && references(s.sourceRefs) && Array.isArray(s.gaps) && s.gaps.every(g => typeof g === 'string') &&
      (s.decision === undefined || (object(s.decision) && typeof s.decision.feedback === 'string' && typeof s.decision.decision === 'string'))) &&
    (record.issues as unknown[]).every(i => object(i) && typeof i.issueId === 'string' && typeof i.summary === 'string' && typeof i.status === 'string' && typeof i.isCurrent === 'boolean' && references(i.sourceRefs)) &&
    (record.events as unknown[]).every(e => object(e) && typeof e.runId === 'string' && integer(e.sequence) && typeof e.type === 'string' && typeof e.mode === 'string' && object(e.payload) && (e.payload.message === undefined || typeof e.payload.message === 'string'));
}
async function http<T>(url: string, method: string, body?: unknown, check?: (v: unknown) => boolean, raw?: Blob): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10_000);
  try {
    const response = await fetch(url, { method, signal: controller.signal, cache: 'no-store',
      headers: body === undefined && raw === undefined ? undefined : { 'Content-Type': raw ? raw.type || 'application/octet-stream' : 'application/json' },
      body: raw ?? (body === undefined ? undefined : JSON.stringify(body)) });
    const envelope: unknown = await response.json().catch(() => {
      if (response.status >= 500) throw new ApiError('后端暂时无法响应，保存结果未确认。请保留输入，恢复连接后重试。', 'service_unavailable');
      throw new ApiError('服务返回格式异常；请保留输入，按原操作重试。', 'invalid_response');
    });
    if (!object(envelope) || typeof envelope.ok !== 'boolean') throw new ApiError('服务结果无法识别，请保留输入后重试。', 'invalid_response');
    if (!response.ok || !envelope.ok) {
      const error = object(envelope.error) ? envelope.error : {};
      throw new ApiError(typeof error.message === 'string' ? error.message : '操作未确认成功，请保留输入后重试。', typeof error.code === 'string' ? error.code : 'http_error');
    }
    if (!('data' in envelope) || (check && !check(envelope.data))) throw new ApiError('服务返回的数据不完整，请保留输入后重试。', 'invalid_response');
    return envelope.data as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError('无法确认保存结果。请检查本机后端，保留输入后重试；同一次操作不会重复保存。');
  } finally { clearTimeout(timeout); }
}
const path = (sid: string) => `/api/sessions/${encodeURIComponent(sid)}`;
export const listSessions = () => http<Session[]>('/api/sessions', 'GET', undefined, v => Array.isArray(v) && v.every(isSession));
export const readSession = (sid: string) => http<Detail>(path(sid), 'GET', undefined, isDetail);
export const createSession = (payload: { requestId: string; direction: string }) => http<Session>('/api/sessions', 'POST', payload, isSession);
export const createDemo = (id: string) => http<Detail>('/api/demo/sessions', 'POST', { requestId: id }, isDetail);
export const runDemo = (sid: string, payload: { requestId: string; expectedStateRevision: number }) => http<Detail>(`${path(sid)}/mock-analysis`, 'POST', payload, isDetail);
export type DraftWrite = { requestId: string; expectedStateRevision: number; markdown: string };
export const writeDraft = (sid: string, payload: DraftWrite) => http<DraftResult>(`${path(sid)}/draft`, 'PUT', payload, hasDraft);
export const decideSuggestion = (sid: string, suggestionId: string, payload: { requestId: string; expectedStateRevision: number; expectedBlockRevision: number; decision: 'accept' | 'reject' | 'revise'; feedback: string }) =>
  http<DraftResult>(`${path(sid)}/suggestions/${encodeURIComponent(suggestionId)}/decision`, 'POST', payload, hasDraft);
export const saveMessage = (sid: string, payload: { requestId: string; expectedStateRevision: number; content: string; kind: string }) => http<{ message: { messageId: string } }>(`${path(sid)}/messages`, 'POST', payload,
  v => object(v) && object(v.message) && typeof v.message.messageId === 'string' && integer(v.stateRevision));
export const pasteMaterial = (sid: string, payload: { requestId: string; expectedStateRevision: number; name: string; role: string; text: string }) => http(`${path(sid)}/materials`, 'POST', payload);
export function uploadMaterial(sid: string, payload: { requestId: string; expectedStateRevision: number; name: string; role: string }, file: File) {
  const query = new URLSearchParams(Object.entries(payload).map(([key, value]) => [key, String(value)]));
  return http(`${path(sid)}/materials?${query}`, 'POST', undefined, undefined, new Blob([file], { type: file.name.toLowerCase().endsWith('.md') ? 'text/markdown' : file.type }));
}
export const deleteSession = (sid: string, payload: { requestId: string; expectedStateRevision: number; confirmed: true }) => http(path(sid), 'DELETE', payload);
export const replaceDraft = (sid: string, payload: { requestId: string; expectedStateRevision: number; expectedDraftRevision: number; materialId: string; authorizationMessageId: string; confirmed: true; completeConfirmed: true }) => http<DraftResult>(`${path(sid)}/draft/replacement`, 'POST', payload, hasDraft);
export const readSource = (sid: string, ref: { sourceId: string; sourceRevision: number }) => http<{ source: Source; historical: boolean }>(`${path(sid)}/sources/${encodeURIComponent(ref.sourceId)}/revisions/${ref.sourceRevision}`, 'GET', undefined,
  v => object(v) && isSource(v.source) && object(v.source) && v.source.sourceId === ref.sourceId && v.source.sourceRevision === ref.sourceRevision && typeof v.historical === 'boolean');
const isParseReport = (v: unknown) => v === null || v === undefined || object(v) && typeof v.message === 'string' && typeof v.status === 'string' && Array.isArray(v.warnings) && v.warnings.every(w => object(w) && typeof w.message === 'string' && typeof w.code === 'string');
const isRecognitionReport = (v: unknown) => v === null || v === undefined || object(v) && typeof v.message === 'string' && typeof v.status === 'string' && ['host','synthetic'].includes(String(v.mode)) && (v.pageCount === null || integer(v.pageCount)) && Number.isSafeInteger(v.completed) && Number(v.completed)>=0 && Number.isSafeInteger(v.failed) && Number(v.failed)>=0 && Number(v.failed)<=Number(v.completed);
export const readMaterial = (sid: string, id: string) => http<{ material: import('./types').Material; sources: Source[] }>(`${path(sid)}/materials/${encodeURIComponent(id)}`, 'GET', undefined,
  v => object(v) && object(v.material) && v.material.materialId === id && isParseReport(v.material.parseReport) && isRecognitionReport(v.material.recognitionReport) && Array.isArray(v.sources) && v.sources.every(isSource));
export type SourceCorrectionWrite = { requestId: string; expectedStateRevision: number; expectedSourceRevision: number; text: string };
export const correctMaterialSource = (sid: string, mid: string, sourceId: string, payload: SourceCorrectionWrite) => http<{ source: Source; stateRevision: number; draftRevision: number }>(`${path(sid)}/materials/${encodeURIComponent(mid)}/sources/${encodeURIComponent(sourceId)}/revisions`, 'POST', payload,
  v => object(v) && isSource(v.source) && object(v.source) && v.source.sourceId === sourceId && v.source.sourceRevision === payload.expectedSourceRevision + 1 && integer(v.stateRevision) && integer(v.draftRevision));
export const fileUrl = (sid: string, id: string) => `${path(sid)}/materials/${encodeURIComponent(id)}/file`;

function isPages(v: unknown) {
  if (!object(v) || typeof v.materialId !== 'string' || typeof v.renderVersion !== 'string' || !(v.pageCount === null || integer(v.pageCount)) || !integer(v.limit) || typeof v.historical !== 'boolean' || typeof v.recognitionAvailable !== 'boolean' || !Array.isArray(v.pages)) return false;
  return v.pages.length === Math.min(Number(v.pageCount ?? 1), Number(v.limit)) && v.pages.every((p, index) => object(p) && p.page === index + 1 && ['pending', 'ready', 'failed'].includes(String(p.status)) && (p.width === null || integer(p.width)) && (p.height === null || integer(p.height)) && (p.status !== 'ready' || integer(p.width) && integer(p.height)) && (p.errorMessage === null || typeof p.errorMessage === 'string') && (p.errorCode === null || typeof p.errorCode === 'string'));
}
export const readMaterialPages = (sid: string, mid: string) => http<import('./types').MaterialPages>(`${path(sid)}/materials/${encodeURIComponent(mid)}/pages`, 'GET', undefined, v => isPages(v) && object(v) && v.materialId === mid);
export type PagePreparationWrite = { requestId: string; expectedStateRevision: number; pageNumber: number };
export const prepareMaterialPage = (sid: string, mid: string, payload: PagePreparationWrite) => http<import('./types').MaterialPages>(`${path(sid)}/materials/${encodeURIComponent(mid)}/pages/prepare`, 'POST', payload, v => isPages(v) && object(v) && v.materialId === mid);
export const materialPageUrl = (sid: string, mid: string, page: number) => `${path(sid)}/materials/${encodeURIComponent(mid)}/pages/${page}/image`;
export type ImageOrderWrite = { requestId: string; expectedStateRevision: number; materialIds: string[] };
export const reorderImages = (sid: string, payload: ImageOrderWrite) => http<Conversation>(`${path(sid)}/materials/image-order`,'POST',payload,isConversation);
export const requestRecognitionRetry = (sid: string, mid: string, payload: {requestId: string; expectedStateRevision: number}) => http<{retryId: string; status: 'pending'; stateRevision: number}>(`${path(sid)}/materials/${encodeURIComponent(mid)}/recognition-retry`,'POST',payload,
  v=>object(v)&&typeof v.retryId==='string'&&v.status==='pending'&&integer(v.stateRevision));

function isChatSession(value: unknown): value is ChatSession {
  if (!object(value)) return false;
  const record: Record<string, unknown> = value;
  return isSession(value) && typeof record.title === 'string' &&
    (record.directionStatus === 'pending' ? record.targetDirection === null : record.directionStatus === 'provided' && typeof record.targetDirection === 'string');
}
function isChatMaterial(value: unknown): value is ChatMaterial {
  return object(value) && ['materialId', 'sessionId', 'name', 'mediaType', 'role', 'parseStatus', 'useStatus'].every(k => typeof value[k] === 'string') &&
    Number.isSafeInteger(value.byteSize) && Number(value.byteSize) >= 0 && typeof value.current === 'boolean';
}
function isChatMessage(value: unknown) {
  return object(value) && typeof value.messageId === 'string' && typeof value.sessionId === 'string' && ['user', 'assistant'].includes(String(value.role)) &&
    typeof value.content === 'string' && typeof value.kind === 'string' && typeof value.createdAt === 'string' && integer(value.sequence) &&
    ['legacy', 'chat'].includes(String(value.inputChannel)) && typeof value.historical === 'boolean' && Array.isArray(value.attachments) && value.attachments.every(isChatMaterial) &&
    (value.replyToMessageId===undefined||value.replyToMessageId===null||typeof value.replyToMessageId==='string') &&
    (value.assistant===undefined||value.assistant===null||object(value.assistant)&&['reply','question','change_question'].includes(String(value.assistant.kind))&&['host','synthetic'].includes(String(value.assistant.mode))&&typeof value.assistant.replyToUserMessageId==='string'&&(value.assistant.questionState===null||['awaiting_reply','reply_saved','outdated','history'].includes(String(value.assistant.questionState)))&&Array.isArray(value.assistant.sourceRefs)&&value.assistant.sourceRefs.every(isReference));
}
function isConversation(value: unknown): value is Conversation {
  if (!object(value)) return false;
  const record: Record<string, unknown> = value;
  return hasDraft(value) && isChatSession(record.session) && Array.isArray(record.messages) && record.messages.every(isChatMessage) &&
    (record.draftReviewRequired === undefined || typeof record.draftReviewRequired === 'boolean') &&
    Array.isArray(record.materials) && record.materials.every(isChatMaterial) && object(record.capabilities) &&
    typeof record.capabilities.agentAvailable === 'boolean' && typeof record.capabilities.decisionExecutionAvailable === 'boolean';
}
export const listChatSessions = () => http<ChatSession[]>('/api/sessions', 'GET', undefined, v => Array.isArray(v) && v.every(isChatSession));
export const createChatSession = (payload: { requestId: string }) => http<ChatSession>('/api/chat/sessions', 'POST', payload, isChatSession);
export const readConversation = (sid: string) => http<Conversation>(`${path(sid)}/conversation`, 'GET', undefined, isConversation);
export type ChatMessageWrite = { requestId: string; expectedStateRevision: number; content: string; attachmentIds: string[];replyToMessageId?:string };
export const saveChatMessage = (sid: string, payload: ChatMessageWrite) => http<ChatMessageResult>(`${path(sid)}/chat/messages`, 'POST', payload,
  v => object(v) && isChatSession(v.session) && isChatMessage(v.message) && integer(v.stateRevision) && integer(v.draftRevision));
export async function uploadChatMaterial(sid: string, payload: { requestId: string; expectedStateRevision: number; name: string }, file: File): Promise<ChatMaterial> {
  const result = await uploadMaterial(sid, { ...payload, role: 'unknown' }, file);
  if (!object(result) || !object(result.material) || result.material.sessionId !== sid || result.material.role !== 'unknown' || !integer(result.stateRevision)) {
    throw new ApiError('附件保存结果无法确认，请按原发送重试。', 'invalid_response');
  }
  const material = { ...result.material, byteSize: file.size, current: true };
  if (!isChatMaterial(material)) throw new ApiError('附件元信息不完整，请按原发送重试。', 'invalid_response');
  return material;
}
