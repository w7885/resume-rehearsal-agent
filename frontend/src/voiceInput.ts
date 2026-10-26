/** Unsent dictation: one capture, explicit retry, no automatic chat submission. */
export type VoicePhase = 'idle' | 'requesting' | 'recording' | 'transcribing' | 'error';
export type VoiceSnapshot = { phase: VoicePhase; seconds: number; error: string; canRetry: boolean };
export type Capture = { stop(): Promise<Blob>; cancel(): void };
export type CaptureOptions = { signal: AbortSignal; onSeconds(value: number): void; onLimit(): void; onError(error: Error): void };
export type VoiceServices = { capture(options: CaptureOptions): Promise<Capture>; transcribe(audio: Blob, signal: AbortSignal): Promise<string> };

export function appendVoiceText(previous: string, text: string): string {
  if (!text.trim()) throw new Error('没有识别到文字，请重新录音。');
  const next = previous + (previous && !/\s$/.test(previous) ? '\n' : '') + text.trim();
  if (next.length > 50000) throw new Error('合并后文字超过输入上限，请先整理已有文字，再重试转写。');
  return next;
}

export class VoiceInputOperation {
  services: VoiceServices;
  onState: (value: VoiceSnapshot) => void;
  onText: (key: string, text: string) => void;
  state: VoiceSnapshot = { phase: 'idle', seconds: 0, error: '', canRetry: false };
  private generation = 0;
  private key = '';
  private capture: Capture | null = null;
  private audio: Blob | null = null;
  private abort: AbortController | null = null;

  constructor(services: VoiceServices, onState: (value: VoiceSnapshot) => void, onText: (key: string, text: string) => void) {
    this.services = services; this.onState = onState; this.onText = onText;
  }
  private update(changes: Partial<VoiceSnapshot>) { this.state = { ...this.state, ...changes }; this.onState(this.state); }
  private requesting() { return this.state.phase === 'requesting'; }
  blocking() { return this.state.phase !== 'idle' && (this.state.phase !== 'error' || this.state.canRetry); }
  cancel() {
    this.generation++; this.abort?.abort(); this.abort = null;
    this.capture?.cancel(); this.capture = null; this.audio = null;
    this.update({ phase: 'idle', seconds: 0, error: '', canRetry: false });
  }
  private fail(error: unknown) {
    this.capture?.cancel(); this.capture = null;
    this.update({ phase: 'error', error: error instanceof Error ? error.message : '录音未完成，请重试。', canRetry: !!this.audio });
  }
  async start(key: string) {
    if (this.state.phase === 'requesting' || this.state.phase === 'recording' || this.state.phase === 'transcribing') return;
    this.cancel(); this.key = key;
    const version = this.generation;
    this.abort = new AbortController();
    this.update({ phase: 'requesting' });
    try {
      const capture = await this.services.capture({ signal: this.abort.signal,
        onSeconds: seconds => { if (version === this.generation) this.update({ seconds }); },
        onLimit: () => { if (version === this.generation) void this.stop(); },
        onError: error => { if (version === this.generation) { this.abort?.abort(); this.fail(error); } },
      });
      if (version !== this.generation || !this.requesting()) { capture.cancel(); return; }
      this.capture = capture; this.update({ phase: 'recording' });
    } catch (error) { if (version === this.generation) this.fail(error); }
  }
  async stop() {
    if (this.state.phase !== 'recording' || !this.capture) return;
    const version = this.generation, capture = this.capture;
    this.capture = null; this.update({ phase: 'transcribing' });
    try {
      const audio = await capture.stop();
      if (version !== this.generation) return;
      this.audio = audio;
      await this.recognize(version);
    } catch (error) { capture.cancel(); if (version === this.generation) this.fail(error); }
  }
  async retry() {
    if (this.state.phase !== 'error' || !this.audio) return;
    this.update({ phase: 'transcribing', error: '' });
    await this.recognize(this.generation);
  }
  private async recognize(version: number) {
    if (!this.audio) return;
    this.abort?.abort(); this.abort = new AbortController();
    try {
      const text = await this.services.transcribe(this.audio, this.abort.signal);
      if (version !== this.generation) return;
      this.onText(this.key, text);
      this.audio = null; this.update({ phase: 'idle', error: '', canRetry: false });
    } catch (error) { if (version === this.generation) this.fail(error); }
  }
}
