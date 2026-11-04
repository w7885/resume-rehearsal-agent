import { ApiError, renameSession, requestId } from './api.ts';
import type { SessionRenameWrite } from './api';
import type { ChatSession } from './types';

export function sessionTitleError(value: string): string {
  if (/[\u0000-\u001f\u007f-\u009f]/u.test(value)) return '会话名称不能包含换行或控制字符。';
  const length = Array.from(value.trim()).length;
  return length < 1 || length > 50 ? '请输入1～50字的会话名称。' : '';
}

/** One confirmed title/version/request. Unknown results retry this same write. */
export class ChatRenameOperation {
  private readonly payload: SessionRenameWrite;
  private running?: Promise<ChatSession>;
  private result?: ChatSession;
  private write: typeof renameSession;
  constructor(sessionId: string, revision: number, title: string, write = renameSession) {
    const error = sessionTitleError(title);
    if (error) throw new ApiError(error, 'invalid_input');
    this.sessionId = sessionId; this.write = write;
    this.payload = { requestId: requestId(), expectedStateRevision: revision, title: title.trim() };
  }
  readonly sessionId: string;
  run(): Promise<ChatSession> {
    if (this.result) return Promise.resolve(this.result);
    if (this.running) return this.running;
    const work = this.write(this.sessionId, this.payload).then(value => { this.result = value; return value; });
    this.running = work;
    void work.finally(() => { this.running = undefined; }).catch(() => {});
    return work;
  }
}
