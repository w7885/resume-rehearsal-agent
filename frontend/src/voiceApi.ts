export type VoiceStatus = { available: boolean; language: string; maxSeconds: number; reason: string | null };
const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);

async function request(url: string, signal: AbortSignal, audio?: Blob): Promise<Record<string, unknown>> {
  let response: Response;
  try { response = await fetch(url, { method: audio ? 'POST' : 'GET', signal, cache: 'no-store',
    headers: audio ? { 'Content-Type': 'audio/wav' } : undefined, body: audio }); }
  catch (error) { if (signal.aborted) throw error; throw new Error('暂时无法连接本地语音服务，已有文字仍保留。'); }
  const result: unknown = await response.json().catch(() => null);
  if (!object(result) || typeof result.ok !== 'boolean') throw new Error('语音服务返回异常，可重试转写，已有文字不会丢失。');
  if (!response.ok || !result.ok) throw new Error(object(result.error) && typeof result.error.message === 'string' ? result.error.message : '语音暂未转写成功，请重试。');
  if (!object(result.data)) throw new Error('语音结果不完整，请重试。');
  return result.data;
}

export async function readVoiceStatus(signal: AbortSignal): Promise<VoiceStatus> {
  const value = await request('/api/voice/status', signal);
  if (typeof value.available !== 'boolean' || value.language !== 'zh-CN' || value.maxSeconds !== 60 || !(value.reason === null || typeof value.reason === 'string')) throw new Error('语音配置结果无法识别。');
  return value as VoiceStatus;
}

export async function transcribeWave(audio: Blob, signal: AbortSignal): Promise<string> {
  const timeout = new AbortController();
  const abort = () => timeout.abort(); signal.addEventListener('abort', abort, { once: true });
  if (signal.aborted) timeout.abort();
  const timer = setTimeout(() => timeout.abort(), 55_000);
  try {
    const value = await request('/api/voice/transcribe', timeout.signal, audio);
    if (typeof value.text !== 'string' || !value.text.trim() || value.text.length > 50000 || value.local !== true || value.language !== 'zh-CN') throw new Error('没有获得有效转写文字，请重试或重新录音。');
    return value.text;
  } catch (error) {
    if (timeout.signal.aborted && !signal.aborted) throw new Error('语音转写等待超时，可以重试，已有文字仍保留。');
    throw error;
  } finally { clearTimeout(timer); signal.removeEventListener('abort', abort); }
}
