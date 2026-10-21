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

import type { Detail, DraftResult, Session, Source } from './types';

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
function hasDraft(value: unknown): value is DraftResult {
  return object(value) && isSession(value.session) && object(value.draft) && typeof value.draft.markdown === 'string' &&
    Array.isArray(value.draft.blocks) && value.draft.blocks.every(b => object(b) && typeof b.blockId === 'string' && integer(b.blockRevision) && typeof b.markdown === 'string' && Array.isArray(b.sourceRefs));
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
  v => object(v) && object(v.source) && typeof v.source.text === 'string' && object(v.source.origin) && typeof v.historical === 'boolean');
export const readMaterial = (sid: string, id: string) => http<{ material: import('./types').Material; sources: Source[] }>(`${path(sid)}/materials/${encodeURIComponent(id)}`, 'GET', undefined,
  v => object(v) && object(v.material) && typeof v.material.materialId === 'string' && Array.isArray(v.sources) && v.sources.every(s => object(s) && typeof s.text === 'string'));
export const fileUrl = (sid: string, id: string) => `${path(sid)}/materials/${encodeURIComponent(id)}/file`;
