import { ApiError, deleteSession, readConversation, requestId } from './api.ts';
import type { SessionDeleteWrite } from './api';

/** An unknown result retries the same confirmed snapshot, never a newer version. */
export class ChatDeleteOperation {
  private readonly payload: SessionDeleteWrite;
  private running?: Promise<void>;
  private done = false;
  private remove: typeof deleteSession;
  private verify: typeof readConversation;
  readonly sessionId: string;
  constructor(sessionId: string, revision: number, remove = deleteSession, verify = readConversation) {
    this.sessionId = sessionId;
    this.payload = { requestId: requestId(), expectedStateRevision: revision, confirmed: true };
    this.remove = remove; this.verify = verify;
  }
  run(): Promise<void> {
    if (this.done) return Promise.resolve();
    if (this.running) return this.running;
    const work = this.execute();
    this.running = work;
    void work.finally(() => { this.running = undefined; }).catch(() => {});
    return work;
  }
  private async execute() {
    try {
      await this.remove(this.sessionId, this.payload);
    } catch (error) {
      if (!(error instanceof ApiError) || error.code !== 'session_not_found') throw error;
      try { await this.verify(this.sessionId); }
      catch (check) {
        if (check instanceof ApiError && check.code === 'session_not_found') { this.done = true; return; }
        throw check;
      }
      throw error;
    }
    this.done = true;
  }
}
