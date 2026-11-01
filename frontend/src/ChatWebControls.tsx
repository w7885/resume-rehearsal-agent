import type { ExternalReference } from './types';
import type { WebEntry } from './webSettings';
import { referenceNotice, referenceTime, safeExternalUrl } from './chatWeb';
import { Icon } from './ChatIcons';

export function WebControl({ entry, enabled, staged, disabled, onToggle, onRetry }: {
  entry: WebEntry; enabled: boolean; staged: boolean; disabled: boolean; onToggle: () => void; onRetry: () => void;
}) {
  const busy = ['loading','saving'].includes(entry.phase);
  const unknown = !!entry.pending || entry.phase === 'error';
  return <div className="cp-web-control">
    <button className={`cp-web-toggle ${enabled ? 'cp-web-on' : ''}`} role="switch" aria-checked={enabled} aria-describedby="web-setting-status"
      disabled={disabled || busy || unknown || entry.phase === 'unsupported'} onClick={onToggle}>
      <Icon name="globe" size={17} /><span>联网搜索</span><strong>{unknown ? '待确认' : enabled ? '开' : '关'}</strong>
    </button>
    <span id="web-setting-status" role={entry.error ? 'alert' : 'status'}>
      {entry.error || (entry.phase === 'loading' ? '正在读取联网设置…' : entry.phase === 'saving' ? '正在保存，当前仍以上次确认状态为准…' : staged ? '发送时保存本次选择；仅检索公开岗位信息。' : enabled ? '已开启，资料不足时按需搜索；可在处理中关闭。' : '已关闭；保留已保存的外部参考。')}
    </span>
    {(entry.phase === 'error' || entry.phase === 'unsupported') && <button className="cp-web-retry" disabled={disabled || busy} onClick={onRetry}>{entry.pending ? '重试原切换' : '重读设置'}</button>}
  </div>;
}

export function ExternalReferences({ references }: { references?: ExternalReference[] }) {
  if (!references?.length) return null;
  return <details className="cp-external-references"><summary>外部参考（{references.length}）</summary>
    <p>公开网页用于理解岗位，不能证明你的个人经历。以下保留获取时内容。</p>
    {references.map(ref => {
      const url = safeExternalUrl(ref.url);
      return <section key={ref.referenceId}>
        {url ? <a href={url} target="_blank" rel="noopener noreferrer">{ref.title || '公开网页'}</a> : <strong>{ref.title || '来源链接不可用'}</strong>}
        <p>{referenceNotice(ref)}</p><p>获取：{referenceTime(ref.retrievedAt)}{ref.publishedAt ? ` · 发布：${referenceTime(ref.publishedAt)}` : ' · 发布日期未提供'}</p>
        <p>{ref.contentStatus === 'sufficient' ? '来源内容' : '来源片段 · 可能不完整'}</p><blockquote>{ref.content.slice(0, 1200)}{ref.content.length > 1200 ? '…' : ''}</blockquote>
      </section>;
    })}
  </details>;
}
