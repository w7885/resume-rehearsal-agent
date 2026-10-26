import { useEffect, useRef, useState } from 'react';
import { VoiceInputOperation } from './voiceInput';
import type { VoiceSnapshot } from './voiceInput';
import { captureMicrophone } from './voiceAudio';
import { readVoiceStatus, transcribeWave } from './voiceApi';

export function useVoiceInput(key: string, disabled: boolean, onText: (key: string, text: string) => void) {
  const [state, setState] = useState<VoiceSnapshot>({ phase: 'idle', seconds: 0, error: '', canRetry: false });
  const [service, setService] = useState({ loading: true, available: false, error: '' });
  const mounted = useRef(true), scope = useRef(key), deliver = useRef(onText);
  const configVersion = useRef(0), configAbort = useRef<AbortController | null>(null);
  scope.current = key; deliver.current = onText;
  const operation = useRef<VoiceInputOperation | null>(null);
  if (!operation.current) operation.current = new VoiceInputOperation({ capture: captureMicrophone, transcribe: transcribeWave },
    next => { if (mounted.current) setState(next); },
    (target, text) => { if (mounted.current && target === scope.current) deliver.current(target, text); });
  const controller = operation.current;
  const reload = async () => {
    const version = ++configVersion.current;
    configAbort.current?.abort(); const abort = new AbortController(); configAbort.current = abort;
    setService({ loading: true, available: false, error: '' });
    const timer = setTimeout(() => abort.abort(), 10000);
    try {
      const result = await readVoiceStatus(abort.signal);
      if (mounted.current && version === configVersion.current) setService({ loading: false, available: result.available,
        error: result.available ? '' : '本地语音输入暂未就绪，可以先使用文字输入。' });
    } catch {
      if (mounted.current && version === configVersion.current) setService({ loading: false, available: false, error: '暂时无法连接本地语音服务，可以重试或继续文字输入。' });
    } finally { clearTimeout(timer); }
  };
  useEffect(() => {
    mounted.current = true; void reload();
    return () => { mounted.current = false; configVersion.current++; configAbort.current?.abort(); controller.cancel(); };
  }, []);
  useEffect(() => { controller.cancel(); }, [key]);
  useEffect(() => { if (disabled) controller.cancel(); }, [disabled]);
  return { ...state, ...service, error: state.error || service.error, blocking: controller.blocking(),
    start: () => { if (!disabled && service.available) void controller.start(scope.current); },
    stop: () => { void controller.stop(); }, cancel: () => controller.cancel(),
    retry: () => { void controller.retry(); }, reload: () => { void reload(); },
  };
}
