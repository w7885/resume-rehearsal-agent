import { ApiError, requestId } from './api.ts';

/** One saved user turn, one stable request. Never resends the raw chat message. */
export class ChatAgentOperation {
  readonly id = requestId();
  runId?: string;
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
  constructor(sessionId: string, userMessageId: string, revision: number,
    notify: () => void = () => {}, transport: typeof fetch = (input, init) => globalThis.fetch(input, init)) {
    this.sessionId = sessionId; this.userMessageId = userMessageId; this.revision = revision;
    this.notify = notify; this.transport = transport;
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
    const response = await this.transport(`/api/sessions/${encodeURIComponent(this.sessionId)}/runs`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: this.controller.signal,
      body: JSON.stringify({ requestId: this.id, userMessageId: this.userMessageId, expectedStateRevision: this.revision }),
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
    let buffer = '', lastSequence = 0, generation = 0, done = false;
    try {
      while (true) {
        const chunk = await reader.read();
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
          }
          this.notify();
          if (event.type === 'done') {
            done = true;
            if (!['completed', 'waiting_user'].includes(event.payload.outcome)) {
              throw new ApiError('本次分析已停止或未完成，消息和当前稿仍已保存。', 'agent_interrupted');
            }
          }
        }
        if (chunk.done) break;
      }
      if (!done) throw new ApiError('分析连接中断，结果尚未确认；可以重试分析，不会重复发送原话。', 'service_unavailable');
      this.completed = true;
    } catch (error) { await reader.cancel().catch(() => {}); throw error; }
    finally { reader.releaseLock(); }
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
