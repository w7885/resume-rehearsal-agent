import { ApiError, readWebSettings, requestId, writeWebSettings } from './api.ts';
import type { WebSettingsWrite } from './api';
import type { WebSettings } from './types';

export type WebEntry = { settings?: WebSettings; phase: 'idle' | 'loading' | 'saving' | 'error' | 'unsupported'; error: string; choiceRejected?: boolean; pending?: WebSettingsWrite };
type Services = { read: typeof readWebSettings; write: typeof writeWebSettings };
/** State is scoped by session; an unknown write retains its exact request for retry. */
export class WebSettingsController {
  private entries = new Map<string, WebEntry>();
  private jobs = new Map<string, Promise<void>>();
  private notify: () => void;
  private services: Services;
  constructor(notify: () => void = () => {}, services: Services = { read: readWebSettings, write: writeWebSettings }) { this.notify = notify; this.services = services; }
  get(sid: string): WebEntry { return this.entries.get(sid) ?? { phase: 'idle', error: '' }; }
  private update(sid: string, value: WebEntry) { this.entries.set(sid, value); this.notify(); }
  private perform(sid: string, work: () => Promise<void>) {
    const running = this.jobs.get(sid); if (running) return running;
    const job = work(); this.jobs.set(sid, job);
    void job.finally(() => { this.jobs.delete(sid); }).catch(() => {});
    return job;
  }
  load(sid: string): Promise<void> {
    if (this.get(sid).pending) return Promise.resolve();
    return this.perform(sid, async () => {
      const old = this.get(sid); this.update(sid, { ...old, phase: 'loading', error: '' });
      try { this.update(sid, { settings: await this.services.read(sid), phase: 'idle', error: old.choiceRejected ? old.error : '', choiceRejected: old.choiceRejected }); }
      catch (error) { this.fail(sid, error); }
    });
  }
  private fail(sid: string, error: unknown) {
    const unsupported = error instanceof ApiError && ['http_error','not_found'].includes(error.code);
    this.update(sid, { ...this.get(sid), phase: unsupported ? 'unsupported' : 'error',
      error: unsupported ? '当前后端不支持联网设置，请保持关闭并继续使用已有材料。' : '联网设置未确认，请重试；确认前暂停新分析。' });
  }
  change(sid: string, enabled: boolean): Promise<void> {
    const entry = this.get(sid);
    if (entry.pending || !entry.settings || ['loading','saving','unsupported'].includes(entry.phase)) return Promise.resolve();
    const pending = { requestId: requestId(), enabled, expectedWebSettingsRevision: entry.settings.webSettingsRevision };
    this.update(sid, { ...entry, pending, choiceRejected: false });
    return this.retry(sid);
  }
  retry(sid: string): Promise<void> {
    const entry = this.get(sid); if (!entry.pending) return this.load(sid);
    return this.perform(sid, async () => {
      this.update(sid, { ...entry, phase: 'saving', error: '' });
      try {
        await this.services.write(sid, entry.pending!);
        // A replayed acknowledgement can be older than another tab's setting.
        const latest = await this.services.read(sid);
        this.update(sid, { settings: latest, phase: 'idle', error: '' });
      } catch (error) {
        if (error instanceof ApiError && ['stale_state','request_conflict','scope_violation','session_not_found'].includes(error.code)) {
          try {
            const latest = await this.services.read(sid);
            this.update(sid, { settings: latest, phase: 'idle', choiceRejected: true, error: '联网设置已变化，本次切换未生效；请核对当前状态后重新选择。' });
          } catch { this.fail(sid, error); }
        } else this.fail(sid, error);
      }
    });
  }
  async prepare(sid: string, initialEnabled?: boolean) {
    const running = this.jobs.get(sid); if (running) await running;
    if (this.get(sid).pending) await this.retry(sid);
    if (!this.get(sid).settings) await this.load(sid);
    let entry = this.get(sid);
    if (initialEnabled !== undefined && entry.settings?.enabled !== initialEnabled && entry.phase === 'idle') {
      if (entry.choiceRejected || entry.error) throw new ApiError('联网设置曾发生冲突，请先核对开关并重新选择。');
      await this.change(sid, initialEnabled); entry = this.get(sid);
      if (entry.settings?.enabled !== initialEnabled) throw new ApiError('新会话联网选择尚未保存；请确认设置后重试分析。');
    }
    if (entry.pending || ['error','loading','saving'].includes(entry.phase) || initialEnabled && entry.phase === 'unsupported')
      throw new ApiError(entry.error || '请先确认联网设置。');
    // An older backend has no web tool: normal non-web analysis remains usable.
  }
}
