import type { Capture, CaptureOptions } from './voiceInput';

export function pcmWave(chunks: Float32Array[], rate: number): Blob {
  const count = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  if (!Number.isFinite(rate) || rate < 8000 || rate > 96000 || count < rate / 4 || count > rate * 60) throw new Error('请录制 0.25～60 秒的语音。');
  const input = new Float32Array(count);
  let at = 0; for (const chunk of chunks) { input.set(chunk, at); at += chunk.length; }
  const frames = Math.floor(count * 16000 / rate), buffer = new ArrayBuffer(44 + frames * 2), view = new DataView(buffer);
  const write = (offset: number, text: string) => { for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i)); };
  write(0, 'RIFF'); view.setUint32(4, buffer.byteLength - 8, true); write(8, 'WAVE'); write(12, 'fmt ');
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, 16000, true); view.setUint32(28, 32000, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  write(36, 'data'); view.setUint32(40, frames * 2, true);
  for (let i = 0; i < frames; i++) {
    const begin = Math.floor(i * rate / 16000), end = Math.min(count, Math.max(begin + 1, Math.floor((i + 1) * rate / 16000)));
    let total = 0; for (let j = begin; j < end; j++) total += Number.isFinite(input[j]) ? input[j] : 0;
    const sample = Math.max(-1, Math.min(1, total / (end - begin)));
    view.setInt16(44 + i * 2, Math.round(sample * (sample < 0 ? 32768 : 32767)), true);
  }
  return new Blob([buffer], { type: 'audio/wav' });
}

export async function captureMicrophone(options: CaptureOptions): Promise<Capture> {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || !window.AudioContext || !window.AudioWorkletNode) throw new Error('当前浏览器暂不支持录音，可在 Chrome/Edge 打开页面，或继续文字输入。');
  let stream: MediaStream | null = null, context: AudioContext | null = null;
  let node: AudioWorkletNode | null = null, source: MediaStreamAudioSourceNode | null = null;
  let timer: ReturnType<typeof setInterval> | null = null, closed = false;
  let flush: (() => void) | null = null;
  const chunks: Float32Array[] = []; let samples = 0;
  const cleanup = () => {
    if (closed) return; closed = true;
    if (timer) clearInterval(timer);
    if (node) { node.port.onmessage = null; node.disconnect(); }
    source?.disconnect(); stream?.getTracks().forEach(track => track.stop());
    void context?.close().catch(() => {}); flush?.();
    options.signal.removeEventListener('abort', cleanup);
  };
  options.signal.addEventListener('abort', cleanup, { once: true });
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    if (options.signal.aborted) { stream.getTracks().forEach(track => track.stop()); throw new Error('已取消录音。'); }
    context = new AudioContext({ sampleRate: 16000 });
    await context.resume();
    await context.audioWorklet.addModule(new URL('voice-capture.js', document.baseURI).href);
    if (options.signal.aborted) throw new Error('已取消录音。');
    node = new AudioWorkletNode(context, 'voice-input-capture');
    source = context.createMediaStreamSource(stream);
    const rate = context.sampleRate;
    node.port.onmessage = event => {
      if (closed) return;
      if (event.data?.type === 'flushed') { flush?.(); return; }
      if (event.data?.type !== 'pcm' || !(event.data.samples instanceof Float32Array)) return;
      const remaining = rate * 60 - samples;
      if (remaining > 0) { const chunk = event.data.samples.slice(0, remaining); chunks.push(chunk); samples += chunk.length; }
    };
    stream.getTracks().forEach(track => { track.onended = () => { if (!closed) options.onError(new Error('麦克风已中断，已有文字保留，可以重新录音。')); }; });
    source.connect(node); node.connect(context.destination);
    const started = Date.now();
    timer = setInterval(() => { if (closed) return; const seconds = Math.min(60, Math.floor((Date.now() - started) / 1000)); options.onSeconds(seconds); if (seconds >= 60) { if (timer) clearInterval(timer); options.onLimit(); } }, 250);
    return {
      cancel: cleanup,
      stop: async () => {
        if (closed) throw new Error('录音已取消。');
        await new Promise<void>((resolve, reject) => {
          const timeout = setTimeout(() => reject(new Error('录音收尾未完成，请重新录制。')), 1200);
          flush = () => { clearTimeout(timeout); resolve(); };
          node!.port.postMessage({ type: 'flush' });
        });
        if (options.signal.aborted) throw new Error('录音已取消。');
        cleanup();
        return pcmWave(chunks, rate);
      },
    };
  } catch (error) {
    cleanup();
    if (error instanceof DOMException && ['NotAllowedError', 'SecurityError'].includes(error.name)) throw new Error('未获得麦克风权限。请在浏览器站点设置中允许麦克风，再试一次。');
    if (error instanceof DOMException && error.name === 'NotFoundError') throw new Error('没有找到麦克风，请检查设备连接后再试。');
    throw error;
  }
}
