import type { ExternalReference } from './types';

export function safeExternalUrl(value: string): string | null {
  try {
    const url = new URL(value);
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) return null;
    return url.href;
  } catch { return null; }
}
export function isExternalReferences(value: unknown): boolean {
  return value === undefined || Array.isArray(value) && value.length <= 20 && value.every(item => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return false;
    const r = item as Record<string, unknown>;
    return ['referenceId','title','url','content','retrievedAt','matchStatus','hiringStatus','contentStatus'].every(k => typeof r[k] === 'string') &&
      (r.publishedAt === null || typeof r.publishedAt === 'string') && (r.hiringNotice === undefined || typeof r.hiringNotice === 'string');
  });
}
export function referenceNotice(ref: ExternalReference) {
  if (['closed', 'conflict'].includes(ref.hiringStatus)) return '来源已下线或状态冲突，仅供历史回顾，请补充 JD。';
  return '岗位内容参考 · 招聘状态未核实，不代表当前在招。';
}
export function referenceTime(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '时间未提供' : date.toLocaleString('zh-CN', { hour12: false });
}
