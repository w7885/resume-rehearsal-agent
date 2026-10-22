import { ApiError, requestId } from './api.ts';
import type { DraftWrite } from './api';
import type { DraftResult } from './types';

type DraftState = Pick<DraftResult, 'session' | 'draft'>;
export type BufferSnapshot = { text: string; saved: string; status: 'saved' | 'dirty' | 'saving' | 'error' | 'conflict'; error: string; remote?: DraftState };

/** One serialized save stream. A failed request retains its original ID/body. */
export class DraftBuffer {
  private snapshot: BufferSnapshot;
  private listeners = new Set<() => void>();
  private revision: number;
  private pending?: DraftWrite;
  private running?: Promise<void>;
  private write: (payload: DraftWrite) => Promise<DraftResult>;
  private read: () => Promise<DraftState>;
  constructor(initial: DraftState, write: (payload: DraftWrite) => Promise<DraftResult>, read: () => Promise<DraftState>) {
    this.revision = initial.session.stateRevision;
    this.snapshot = { text: initial.draft.markdown, saved: initial.draft.markdown, status: 'saved', error: '' };
    this.write = write; this.read = read;
  }
  getSnapshot = () => this.snapshot;
  subscribe = (fn: () => void) => { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; };
  private emit(value: Partial<BufferSnapshot>) { this.snapshot = { ...this.snapshot, ...value }; this.listeners.forEach(fn => fn()); }
  hasUnsettled() { return !!this.pending || !!this.running || this.snapshot.text !== this.snapshot.saved || this.snapshot.status === 'conflict'; }
  setText(text: string) {
    this.emit({ text, status: this.snapshot.status === 'conflict' ? 'conflict' : this.running ? 'saving' : this.pending ? 'error' : text === this.snapshot.saved ? 'saved' : 'dirty' });
  }
  flush(): Promise<void> {
    if (this.running) return this.running;
    if (this.snapshot.status === 'conflict') return Promise.reject(new ApiError('有版本冲突，请先选择如何保留文字。', 'stale_state'));
    const work = this.saveAll();
    this.running = work;
    void work.finally(() => { this.running = undefined; }).catch(() => {});
    return work;
  }
  private async saveAll() {
    while (this.pending || this.snapshot.text !== this.snapshot.saved) {
      this.pending ??= { requestId: requestId(), expectedStateRevision: this.revision, markdown: this.snapshot.text };
      const payload = this.pending;
      this.emit({ status: 'saving', error: '' });
      try {
        const result = await this.write(payload);
        this.revision = result.session.stateRevision;
        this.pending = undefined;
        this.emit({ saved: payload.markdown, status: this.snapshot.text === payload.markdown ? 'saved' : 'dirty', error: '' });
      } catch (error) {
        const message = error instanceof Error ? error.message : '保存未确认成功，请重试。';
        if (error instanceof ApiError && error.code === 'stale_state') {
          this.emit({ status: 'conflict', error: message });
          try { this.emit({ remote: await this.read() }); } catch { /* Keep local text; allow an explicit refresh. */ }
        } else this.emit({ status: 'error', error: message });
        throw error;
      }
    }
  }
  async loadRemote() { this.emit({ remote: await this.read() }); }
  resolveConflict(keepLocal: boolean) {
    const remote = this.snapshot.remote;
    if (!remote) return;
    this.pending = undefined;
    this.revision = remote.session.stateRevision;
    const text = keepLocal ? this.snapshot.text : remote.draft.markdown;
    this.emit({ text, saved: remote.draft.markdown, status: text === remote.draft.markdown ? 'saved' : 'dirty', error: '', remote: undefined });
  }
}
