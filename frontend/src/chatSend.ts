import { ApiError, createChatSession, readConversation, requestId, saveChatMessage, uploadChatMaterial } from './api.ts';
import type { ChatMessageWrite } from './api';
import type { ChatMaterial, ChatMessageResult, ChatSession, Conversation } from './types';

export type ChatAttachment = { localId: string; file?: File; material?: ChatMaterial };
export type ChatSendServices = {
  create: (payload: { requestId: string }) => Promise<ChatSession>;
  read: (sid: string) => Promise<Conversation>;
  upload: (sid: string, payload: { requestId: string; expectedStateRevision: number; name: string }, file: File) => Promise<ChatMaterial>;
  send: (sid: string, payload: ChatMessageWrite) => Promise<ChatMessageResult>;
};
const defaults: ChatSendServices = { create: createChatSession, read: readConversation, upload: uploadChatMaterial, send: saveChatMessage };

/** A single user send, including separately committed original-file uploads.
 * Unknown outcomes always retry the exact original request before continuing.
 */
export class ChatSendOperation {
  session?: ChatSession;
  private createId = requestId();
  private content: string;
  private steps: { item: ChatAttachment; id: string; payload?: { requestId: string; expectedStateRevision: number; name: string } }[];
  private messageId = requestId();
  private messagePayload?: ChatMessageWrite;
  private result?: ChatMessageResult;
  private services: ChatSendServices;
  private running?: Promise<ChatMessageResult>;
  private failure?: unknown;
  private notify: () => void;
  private replyTo?: string;

  constructor(content: string, attachments: ChatAttachment[], session?: ChatSession, services = defaults, notify = () => {}, replyTo?: string) {
    this.content = content; this.session = session; this.services = services; this.notify = notify;
    this.steps = attachments.map(item => ({ item: { ...item }, id: requestId() }));
    this.replyTo=replyTo;
  }
  attachments() { return this.steps.map(step => ({ ...step.item })); }
  isMessageSaved() { return !!this.result; }
  canReturnToEditing() {
    return !this.result && this.failure instanceof ApiError && ['invalid_input', 'unsupported_format', 'parse_failed', 'scope_violation', 'source_not_found', 'session_not_found', 'stale_state', 'request_conflict'].includes(this.failure.code);
  }
  private async current() {
    const current = await this.services.read(this.session!.sessionId);
    if (current.session.mode !== 'local') throw new ApiError('模拟会话只供回顾，请新建个人材料对话。', 'scope_violation');
    if (current.session.currentResumeGeneration !== this.session!.currentResumeGeneration) throw new ApiError('会话的简历范围已改变，请保留输入后重新编辑。', 'stale_state');
    return current;
  }
  run(): Promise<ChatMessageResult> {
    if (this.result) return Promise.resolve(this.result);
    if (this.running) return this.running;
    const work = this.execute(); this.running = work;
    void work.finally(() => { this.running = undefined; this.notify(); }).catch(() => {});
    return work;
  }
  private async execute() {
    this.failure = undefined;
    try {
      if (!this.content.trim() && !this.steps.length) throw new ApiError('请输入消息或添加附件。', 'invalid_input');
      if (this.steps.length > 10) throw new ApiError('一次最多发送 10 个附件。', 'invalid_input');
      this.session ??= await this.services.create({ requestId: this.createId });
      this.notify();
      for (const step of this.steps) {
        if (step.item.material) {
          if (step.item.material.sessionId !== this.session.sessionId) throw new ApiError('不能引用另一会话的附件。', 'scope_violation');
          if (!step.item.material.current) throw new ApiError('历史附件不能随新消息发送，请重新上传。', 'stale_state');
          continue;
        }
        if (!step.item.file) throw new ApiError('附件已不可用，请重新选择。', 'invalid_input');
        if (!step.payload) step.payload = { requestId: step.id, expectedStateRevision: (await this.current()).session.stateRevision, name: step.item.file.name };
        step.item.material = await this.services.upload(this.session.sessionId, step.payload, step.item.file);
        this.notify();
      }
      if (!this.messagePayload) this.messagePayload = { requestId: this.messageId, expectedStateRevision: (await this.current()).session.stateRevision,
        content: this.content, attachmentIds: this.steps.map(step => step.item.material!.materialId),...(this.replyTo?{replyToMessageId:this.replyTo}:{}) };
      this.result = await this.services.send(this.session.sessionId, this.messagePayload);
      this.notify();
      return this.result;
    } catch (error) { this.failure = error; throw error; }
  }
}
