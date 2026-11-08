import { ApiError } from './api.ts';
import type { Conversation } from './types';

/** Bounded read-only recovery. Never resends a message or starts an Agent run. */
export class ConversationReadback {
  private controller = new AbortController();
  cancel() { this.controller.abort(); }
  get cancelled() { return this.controller.signal.aborted; }

  async read(sid: string, current: () => Conversation | null,
    read: (sid: string, signal: AbortSignal) => Promise<Conversation>,
    retrying: () => void): Promise<Conversation | null> {
    for (let attempt = 0; attempt < 3; attempt++) {
      if (this.cancelled || current()?.session.sessionId !== sid) return null;
      try {
        const value = await read(sid, this.controller.signal);
        const latest = current();
        if (this.cancelled || latest?.session.sessionId !== sid) return null;
        if (value.session.sessionId !== sid || value.session.stateRevision < latest.session.stateRevision ||
            value.session.draftRevision < latest.session.draftRevision ||
            value.session.currentResumeGeneration < latest.session.currentResumeGeneration) {
          throw new ApiError('会话已变化，请重新读取最新结果。', 'stale_state');
        }
        return value;
      } catch (error) {
        if (this.cancelled || current()?.session.sessionId !== sid) return null;
        if (!(error instanceof ApiError) || !['network_error', 'service_unavailable'].includes(error.code) || attempt === 2) throw error;
        retrying();
      }
    }
    return null;
  }
}
