import * as api from './api.ts';
import type { PagePreparationWrite } from './api';
import type { MaterialPages, Source } from './types';

/** Display current text once, in stored document order, without history cards. */
export function attachmentText(sources: Source[]) {
  const latest = new Map<string, Source>();
  for (const source of sources) {
    if (!latest.has(source.sourceId) || latest.get(source.sourceId)!.sourceRevision < source.sourceRevision) latest.set(source.sourceId, source);
  }
  return [...latest.values()].map(source => source.text).join('\n\n');
}

type Services = {
  read: typeof api.readMaterialPages;
  prepare: typeof api.prepareMaterialPage;
  revision: (sid: string) => Promise<number>;
};
const defaults: Services = { read: api.readMaterialPages, prepare: api.prepareMaterialPage, revision: async sid => (await api.readConversation(sid)).session.stateRevision };

/** Local images appear automatically. Unknown requests keep their original IDs;
 * a subsequent successful read can confirm the cached image without resending. */
export class AttachmentImagePreview {
  private value: MaterialPages | null = null;
  private listeners = new Set<() => void>();
  private pending = new Map<number, PagePreparationWrite>();
  private running?: Promise<MaterialPages>;
  private active = true;
  private sid: string;
  private mid: string;
  private services: Services;
  constructor(sid: string, mid: string, services = defaults) { this.sid = sid; this.mid = mid; this.services = services; }
  getSnapshot = () => this.value;
  subscribe = (callback: () => void) => { this.listeners.add(callback); return () => { this.listeners.delete(callback); }; };
  pause() { this.active = false; }
  private install(value: MaterialPages) { this.value = value; this.listeners.forEach(callback => callback()); }
  run(): Promise<MaterialPages> {
    this.active = true;
    if (this.running) return this.running;
    const work = this.load(); this.running = work;
    void work.finally(() => { this.running = undefined; }).catch(() => {});
    return work;
  }
  private async load() {
    let value = await this.services.read(this.sid, this.mid);
    this.install(value);
    const total = Math.min(value.pageCount ?? 1, value.limit);
    // Preparing the first image discovers the PDF length. Re-evaluate the
    // bound after each successful result, retaining original server order.
    for (let page = 1; page <= Math.min(value.pageCount ?? total, value.limit) && this.active; page++) {
      if (value.pages.find(item => item.page === page)?.status === 'ready') { this.pending.delete(page); continue; }
      let payload = this.pending.get(page);
      if (!payload) {
        payload = { requestId: api.requestId(), expectedStateRevision: await this.services.revision(this.sid), pageNumber: page };
        this.pending.set(page, payload);
      }
      if (!this.active) break;
      try { value = await this.services.prepare(this.sid, this.mid, payload); }
      catch (failure) {
        if (failure instanceof api.ApiError && ['stale_state', 'invalid_input', 'scope_violation', 'source_not_found', 'session_not_found', 'request_conflict', 'unsupported_format'].includes(failure.code)) this.pending.delete(page);
        throw failure;
      }
      this.pending.delete(page); this.install(value);
    }
    return value;
  }
}
