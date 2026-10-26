/* Same-origin AudioWorklet: capture mono PCM, never play microphone audio. */
class VoiceInputCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(2048); this.offset = 0; this.done = false;
    this.port.onmessage = event => {
      if (event.data?.type !== 'flush') return;
      this.done = true;
      if (this.offset) { const samples = this.buffer.slice(0, this.offset); this.port.postMessage({ type: 'pcm', samples }, [samples.buffer]); }
      this.port.postMessage({ type: 'flushed' });
    };
  }
  process(inputs) {
    if (this.done) return false;
    const input = inputs[0]?.[0];
    if (!input) return true;
    for (const sample of input) {
      this.buffer[this.offset++] = sample;
      if (this.offset === this.buffer.length) {
        const samples = this.buffer; this.port.postMessage({ type: 'pcm', samples }, [samples.buffer]);
        this.buffer = new Float32Array(2048); this.offset = 0;
      }
    }
    return true;
  }
}
registerProcessor('voice-input-capture', VoiceInputCapture);
