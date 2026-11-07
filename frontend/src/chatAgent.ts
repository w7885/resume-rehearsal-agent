import { apiFetch } from './authTransport.ts';
import { ApiError, requestId } from './api.ts';

export type WebControlRequest = { requestId: string; expectedStateRevision: number; webSettingsRevision: number };
class StreamDisconnected extends Error {}
const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

/** One saved user turn, one stable request. Never resends the raw chat message. */
export class ChatAgentOperation {
  readonly id: string = requestId();
  get isStopConfirmed() { return this.stopConfirmed; }
  runId?: string;
  progressMessage = '';
  private controller?: AbortController;
  private running?: Promise<void>;
  private completed = false;
  private stopped = false;
  private stopConfirmed = false;
  get stopMessage() { return this.stopConfirmed ? '处理已结束，消息与当前稿仍已保存。' : '已停止等待；停止请求尚未确认，已保存内容保持不变。'; }
  get isStopped() { return this.stopped; }
  readonly sessionId: string;
  readonly userMessageId: string;
  readonly revision: number;
  private notify: () => void;
  private transport: typeof fetch;
  private control?: WebControlRequest;
  private observed?: { sequence: number; generation: number; runId: string };
  constructor(sessionId: string, userMessageId: string, revision: number,
    notify: () => void = () => {}, transport: typeof fetch = apiFetch, control?: WebControlRequest) {
    this.sessionId = sessionId; this.userMessageId = userMessageId; this.revision = revision;
    this.notify = notify; this.transport = transport;
    if (control) {
      this.id = control.requestId;
      this.control = { requestId: control.requestId, expectedStateRevision: control.expectedStateRevision,
        webSettingsRevision: control.webSettingsRevision };
    }
  }

  run(): Promise<void> {
    if (this.completed || this.stopped) return Promise.resolve();
    if (this.running) return this.running;
    const work = this.execute(); this.running = work;
    void work.finally(() => { this.running = undefined; }).catch(() => {});
    return work;
  }
  private async execute() {
    this.controller = new AbortController();
    this.observed = undefined;
    try { await this.readStream(); }
    catch (error) {
      // Protocol/scope failures and explicit cancellation must never become success.
      if (!(error instanceof StreamDisconnected) || this.stopped) throw error;
      if (await this.reconcile()) return;
      throw new ApiError('分析连接中断，结果尚未确认；可以重试分析，不会重复发送原话。', 'service_unavailable');
    }
  }
  private async readStream() {
    const response = await this.transport(`/api/sessions/${encodeURIComponent(this.sessionId)}/${this.control ? 'web-continuation' : 'runs'}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: this.controller!.signal,
      body: JSON.stringify(this.control ?? { requestId: this.id, userMessageId: this.userMessageId, expectedStateRevision: this.revision }),
    }).catch(error => {
      if (error instanceof ApiError) throw error;
      throw new StreamDisconnected();
    });
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      throw new ApiError(typeof body?.error?.message === 'string' ? body.error.message : '消息已保存，分析暂未完成。',
        typeof body?.error?.code === 'string' ? body.error.code : 'service_unavailable');
    }
    if (!response.headers.get('content-type')?.includes('text/event-stream') || !response.body) {
      throw new ApiError('分析结果格式无法确认，原消息仍已保存。', 'invalid_response');
    }
    const reader = response.body.getReader(), decoder = new TextDecoder();
    let buffer = '', lastSequence = 0, generation = 0;
    try {
      while (true) {
        const chunk = await reader.read().catch(() => { throw new StreamDisconnected(); });
        buffer += chunk.done ? decoder.decode() : decoder.decode(chunk.value, { stream: true });
        if (buffer.length > 1024 * 1024) throw new ApiError('分析事件过大。', 'invalid_response');
        let boundary: number;
        while ((boundary = buffer.indexOf('\n\n')) >= 0) {
          const packet = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2);
          const data = packet.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trim()).join('\n');
          if (!data) continue;
          let event;
          try { event = JSON.parse(data); } catch { throw new ApiError('分析事件格式异常。', 'invalid_response'); }
          // 未知事件类型（如 warning）只是服务端诊断，忽略即可；按类型白名单拒收会中断本轮分析。
          if (event?.sessionId !== this.sessionId || event?.requestId !== this.id || typeof event?.type !== 'string' || !event.payload || typeof event.payload !== 'object' || Array.isArray(event.payload)) {
            throw new ApiError('分析事件不属于当前请求。', 'scope_violation');
          }
          if (event.payload.replayed === true && event.type !== 'done') throw new ApiError('非法重放事件。', 'invalid_response');
          if (event.type === 'question' && typeof event.payload.messageId !== 'string') throw new ApiError('询问尚未保存。', 'invalid_response');
          if (event.payload.replayed !== true) {
            if (!Number.isSafeInteger(event.sequence) || event.sequence <= lastSequence || !Number.isSafeInteger(event.generation) || event.generation < generation || typeof event.runId !== 'string') {
              throw new ApiError('分析事件顺序无法确认。', 'invalid_response');
            }
            lastSequence = event.sequence; generation = event.generation; this.runId = event.runId;
            this.observed = { sequence: lastSequence, generation, runId: event.runId };
          }
          if (event.type === 'progress' && typeof event.payload.message === 'string') this.progressMessage = event.payload.message.slice(0, 180);
          if (event.type === 'done') this.progressMessage = '';
          this.notify();
          if (event.type === 'done') {
            if (this.stopped || !['completed', 'waiting_user'].includes(event.payload.outcome)) {
              throw new ApiError('本次分析已停止或未完成，消息和当前稿仍已保存。', 'agent_interrupted');
            }
            this.completed = true;
            // Persisted done is the result boundary; EOF is only transport cleanup.
            void reader.cancel().catch(() => {});
            return;
          }
        }
        if (chunk.done) break;
      }
      throw new StreamDisconnected();
    } catch (error) { await reader.cancel().catch(() => {}); throw error; }
    finally { reader.releaseLock(); }
  }
  private async reconcile(): Promise<boolean> {
    let response: Response;
    let envelope: unknown;
    try {
      response = await this.transport(`/api/sessions/${encodeURIComponent(this.sessionId)}/agent-requests/${encodeURIComponent(this.id)}`, {
        method: 'GET', cache: 'no-store',
        signal: AbortSignal.any([this.controller!.signal, AbortSignal.timeout(10_000)]),
      });
      if (!response.ok) return false;
      envelope = await response.json();
    } catch { return false; }
    if (this.stopped) return false;
    const invalid = () => new ApiError('已保存的分析状态无法核对，原消息仍已保留。', 'invalid_response');
    if (!record(envelope) || envelope.ok !== true || !record(envelope.data)) throw invalid();
    const value = envelope.data;
    if (value.requestId !== this.id) throw new ApiError('分析状态不属于当前请求。', 'scope_violation');
    if (typeof value.runId !== 'string' || !value.runId || !Array.isArray(value.events) ||
      !['running', 'done', 'failed', 'interrupted'].includes(String(value.status))) throw invalid();
    if (value.status === 'running') return false;
    let sequence = 0, generation = 0, last: Record<string, unknown> | undefined;
    let observedFound = !this.observed;
    for (const event of value.events) {
      if (!record(event) || event.sessionId !== this.sessionId || event.requestId !== this.id) {
        throw new ApiError('分析事件不属于当前请求。', 'scope_violation');
      }
      if (typeof event.type !== 'string' || !record(event.payload) || event.payload.replayed === true ||
        typeof event.runId !== 'string' || !event.runId || !Number.isSafeInteger(event.sequence) ||
        Number(event.sequence) <= sequence || !Number.isSafeInteger(event.generation) ||
        Number(event.generation) < Math.max(1, generation) || last?.type === 'done' ||
        (!last && event.runId !== value.runId) ||
        (event.type === 'question' && typeof event.payload.messageId !== 'string')) throw invalid();
      sequence = Number(event.sequence); generation = Number(event.generation);
      if (this.observed?.sequence === sequence) {
        if (this.observed.generation !== generation || this.observed.runId !== event.runId) throw invalid();
        observedFound = true;
      }
      last = event;
    }
    if (!observedFound || !last || last.type !== 'done' || !record(last.payload) || last.payload.outcome !== value.outcome) throw invalid();
    if (value.status !== 'done' || !['completed', 'waiting_user'].includes(String(value.outcome))) {
      throw new ApiError('本次分析已停止或未完成，消息和当前稿仍已保存。', 'agent_interrupted');
    }
    this.runId = last.runId as string;
    this.completed = true; this.progressMessage = ''; this.notify();
    return true;
  }
  async stop() {
    this.stopped = true;
    try {
      if (this.runId) {
        const response = await this.transport(`/api/runs/${encodeURIComponent(this.runId)}/cancel`, { method: 'POST' });
        if (!response.ok) throw new ApiError('停止请求尚未确认；原话和当前稿仍保留。', 'service_unavailable');
        this.stopConfirmed = true;
      }
    } finally { this.controller?.abort(); }
  }
}
