export type Health = { status: 'ok'; model_configured: boolean };

export async function readHealth(signal: AbortSignal): Promise<Health> {
  const response = await fetch('/api/health', { signal, cache: 'no-store' });
  if (!response.ok) throw new Error('服务暂时无法响应，请稍后重新检查。');
  const body: unknown = await response.json();
  if (
    typeof body !== 'object' || body === null ||
    !('status' in body) || body.status !== 'ok' ||
    !('model_configured' in body) || typeof body.model_configured !== 'boolean'
  ) throw new Error('服务返回了无法识别的状态，请重新检查。');
  return { status: body.status, model_configured: body.model_configured };
}
