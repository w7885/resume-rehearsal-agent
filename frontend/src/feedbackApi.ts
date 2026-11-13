import { apiFetch } from './authTransport';

export const categories = { bug: '功能故障', suggestion: '使用建议', other: '其他' } as const;
export const statuses = { pending: '待处理', in_progress: '处理中', resolved: '已处理' } as const;
export type Category = keyof typeof categories;
export type Status = keyof typeof statuses;
export type FeedbackItem = { id: string; category: Category; summary: string; status: Status; createdAt: string };
export type FeedbackDetail = Omit<FeedbackItem, 'summary'> & {
  description: string;
  submitterEmail: string;
  note: string;
  revision: number;
  history: { actorId: string; oldStatus: Status; newStatus: Status; note: string; changedAt: string }[];
};

export class AccountRequestError extends Error {
  constructor(readonly status: number, message: string) { super(message); }
}

export async function feedbackRequest<T>(path: string, fallback: string, options?: RequestInit, onDenied?: () => void): Promise<T> {
  const response = await apiFetch(path, {
    ...options,
    cache: 'no-store',
    signal: options?.signal ? AbortSignal.any([options.signal, AbortSignal.timeout(15000)]) : AbortSignal.timeout(15000),
  });
  if (response.status === 403) onDenied?.();
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new AccountRequestError(response.status, data?.error?.message ?? fallback);
  }
  return response.json();
}

export function jsonBody(method: 'POST' | 'PATCH', body: object): RequestInit {
  return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
}
