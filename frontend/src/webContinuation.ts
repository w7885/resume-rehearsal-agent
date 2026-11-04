import { ApiError, readWebContinuation } from './api.ts';
import { ChatAgentOperation } from './chatAgent.ts';
import type { WebControlRequest } from './chatAgent';
import type { WebContinuation } from './types';

export type ContinuationEntry = { phase: 'idle' | 'pending' | 'running' | 'error' | 'unsupported'; error: string; operation?: ChatAgentOperation; observedRevision?: number };
type Services = { read: (sid: string) => Promise<WebContinuation>; create: (sid: string, request: WebControlRequest) => ChatAgentOperation };

/** One server-issued event per permission epoch; unknown POSTs retain exact payload. */
export class WebContinuationController {
  private entries = new Map<string, ContinuationEntry>();
  private jobs = new Map<string, Promise<void>>();
  private notify: () => void;
  private completed: (sid: string) => Promise<void>;
  private started: (operation: ChatAgentOperation) => void;
  private services: Services;
  constructor(notify: () => void = () => {},
    completed: (sid: string) => Promise<void> = async () => {},
    started: (operation: ChatAgentOperation) => void = () => {},
    services: Services = { read: readWebContinuation, create: (sid, request) =>
      new ChatAgentOperation(sid, '', request.expectedStateRevision, notify, undefined, request) }) {
    this.notify = notify; this.completed = completed; this.started = started; this.services = services;
  }
  get(sid: string): ContinuationEntry { return this.entries.get(sid) ?? { phase: 'idle', error: '' }; }
  private update(sid: string, value: ContinuationEntry) { this.entries.set(sid, value); this.notify(); }
  resume(sid: string, retry = false, revision?: number): Promise<void> {
    const job = this.jobs.get(sid); if (job) return job;
    const entry = this.get(sid);
    if (entry.phase === 'unsupported' || entry.phase === 'error' && !retry) return Promise.resolve();
    if (!retry && revision !== undefined && entry.phase === 'idle' && entry.observedRevision === revision) return Promise.resolve();
    const work = this.execute(sid, revision); this.jobs.set(sid, work);
    void work.finally(() => this.jobs.delete(sid)).catch(() => {});
    return work;
  }
  private async execute(sid: string, revision?: number) {
    let entry = this.get(sid);
    let operation = entry.operation;
    try {
      if (operation?.isStopped) {
        const remote = await this.services.read(sid);
        if (remote.sessionId !== sid) throw new ApiError('接续状态不属于当前会话。', 'scope_violation');
        if (remote.status === 'idle' || remote.requestId === operation.id && ['done','interrupted','failed'].includes(remote.status)) {
          this.update(sid, { phase: 'idle', error: '', observedRevision: revision }); await this.completed(sid); return;
        }
        if (remote.status === 'running' && remote.requestId === operation.id && remote.runId) {
          operation.runId = remote.runId; await operation.stop();
          this.update(sid, { phase: 'idle', error: '', observedRevision: revision }); await this.completed(sid); return;
        }
        throw new ApiError('停止结果尚未确认；可关闭联网取消未执行的接续。', 'control_stop_unknown');
      }
      if (!operation) {
        const remote = await this.services.read(sid);
        if (remote.sessionId !== sid) throw new ApiError('接续状态不属于当前会话。', 'scope_violation');
        if (revision !== undefined && ['pending','running'].includes(remote.status) && remote.webSettingsRevision !== revision) {
          this.update(sid, { phase: 'pending', error: '' }); await this.completed(sid); return;
        }
        if (remote.status === 'running') { this.update(sid, { phase: 'pending', error: '' }); return; }
        if (remote.status !== 'pending') {
          const refresh = entry.phase === 'pending';
          this.update(sid, { phase: 'idle', error: '', observedRevision: revision });
          if (refresh) await this.completed(sid);
          return;
        }
        if (!remote.requestId || !remote.expectedStateRevision || !remote.webSettingsRevision) throw new ApiError('接续事件缺少版本。', 'invalid_response');
        operation = this.services.create(sid, { requestId: remote.requestId,
          expectedStateRevision: remote.expectedStateRevision, webSettingsRevision: remote.webSettingsRevision });
      }
      this.update(sid, { phase: 'running', error: '', operation }); this.started(operation);
      await operation.run();
      this.update(sid, { phase: 'idle', error: '', observedRevision: revision });
      await this.completed(sid);
    } catch (failure) {
      entry = this.get(sid);
      if (failure instanceof ApiError && ['run_busy','stale_state'].includes(failure.code)) {
        // Confirmed rejection: no event was started, so a later read may obtain
        // a fresh binding. Unknown failures retain the exact original operation.
        this.update(sid, { phase: 'pending', error: '' }); return;
      }
      const unsupported = failure instanceof ApiError && failure.code === 'not_found';
      this.update(sid, { ...entry, phase: unsupported ? 'unsupported' : 'error',
        operation,
        error: unsupported ? '' : '联网设置已保存，但自动接续未确认；可重试原接续，或发送下一条消息继续。' });
    }
  }
  async cancel(sid: string) {
    const operation = this.get(sid).operation;
    if (operation) await operation.stop();
    await this.jobs.get(sid);
    this.update(sid, operation && !operation.isStopConfirmed ?
      { phase: 'error', error: operation.stopMessage, operation } : { phase: 'idle', error: '' });
  }
}
