import { useEffect, useState } from 'react';
import { feedbackRequest, statuses, type Status } from './feedbackApi';

type Day = { date: string; count: number };
type Overview = {
  totalRegistrations: number;
  recentRegistrations: number;
  emailVerification: { verified: number; unverified: number };
  registrations: { recent: Day[]; previous: Day[] };
  feedback: Record<Status, number>;
};

const number = new Intl.NumberFormat('zh-CN');
const percent = new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: 1 });
const shortDate = (day: Day) => day.date.slice(5).replace('-', '/');

export default function AdminOverview({ onDenied, onPending }: { onDenied: () => void; onPending: () => void }) {
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    feedbackRequest<Overview>('/api/admin/overview', '暂时无法读取概览。', { signal: controller.signal }, onDenied).then(
      result => { if (!controller.signal.aborted) setData(result); },
      () => { if (!controller.signal.aborted) setError(true); },
    );
    return () => controller.abort();
  }, [attempt, onDenied]);

  if (error) return <div className="admin-overview-error"><p role="alert" className="auth-error">暂时无法读取概览，请稍后重试。</p>
    <button type="button" onClick={() => { setError(false); setAttempt(value => value + 1); }}>重试</button></div>;
  if (!data) return <p role="status">正在读取概览…</p>;

  const { recent, previous } = data.registrations;
  const { verified, unverified } = data.emailVerification;
  const sampleSize = verified + unverified;
  const registrationScale = Math.max(1, ...recent.map(day => day.count), ...previous.map(day => day.count));
  const feedbackScale = Math.max(1, ...Object.values(data.feedback));

  return <div className="admin-overview">
    <div className="admin-metrics">
      <div className="admin-metric"><span>累计注册</span><strong>{number.format(data.totalRegistrations)} <small>人</small></strong></div>
      <div className="admin-metric"><span>近 7 天新增</span><strong>{number.format(data.recentRegistrations)} <small>人</small></strong></div>
      <div className="admin-metric"><span>邮箱验证率</span><strong>{sampleSize ? percent.format(verified / sampleSize) : '暂无数据'}</strong></div>
      <button type="button" className="admin-metric admin-metric-link" onClick={onPending}>
        <span>待处理反馈</span><strong>{number.format(data.feedback.pending)} <small>条</small></strong><span>查看列表 →</span>
      </button>
    </div>
    <figure className="admin-chart admin-trend">
      <figcaption><h2>每日新增注册</h2><p>北京时间 · 完整自然日 · 单位：人</p></figcaption>
      <ul className="admin-legend">
        <li><i className="admin-swatch recent" />近 7 天（{shortDate(recent[0])}–{shortDate(recent[6])}）</li>
        <li><i className="admin-swatch previous" />前 7 天（{shortDate(previous[0])}–{shortDate(previous[6])}）</li>
      </ul>
      <div className="admin-trend-scroll" role="region" aria-label="两周新增注册对比" tabIndex={0}>
        <ol className="admin-trend-groups">
          {recent.map((day, index) => <li className="admin-trend-group" key={day.date}>
            {([['recent', day], ['previous', previous[index]]] as const).map(([series, entry]) =>
              <div className="admin-column" key={entry.date} role="img" aria-label={`${entry.date}，新增 ${entry.count} 人`}>
                <div className="admin-column-plot" aria-hidden="true">
                  <div className={`admin-column-fill ${series}`} style={{ height: `${entry.count / registrationScale * 100}%` }}>
                    <span>{number.format(entry.count)}</span>
                  </div>
                </div>
                <time aria-hidden="true" dateTime={entry.date}>{shortDate(entry)}</time>
              </div>)}
          </li>)}
        </ol>
      </div>
    </figure>
    <figure className="admin-chart">
      <figcaption><h2>近期邮箱验证</h2><p>近 7 个完整自然日注册 · 当前验证状态</p></figcaption>
      {sampleSize ? <>
        <div className="admin-stacked" aria-hidden="true">
          <div className="recent" style={{ width: `${verified / sampleSize * 100}%` }} />
          <div className="previous" style={{ width: `${unverified / sampleSize * 100}%` }} />
        </div>
        <ul className="admin-legend"><li><i className="admin-swatch recent" />已验证 {number.format(verified)} 人</li>
          <li><i className="admin-swatch previous" />未验证 {number.format(unverified)} 人</li></ul>
      </> : <p>暂无数据</p>}
    </figure>
    <figure className="admin-chart">
      <figcaption><h2>反馈状态分布</h2><p>当前状态 · 单位：条</p></figcaption>
      <ul className="admin-feedback-bars">
        {(Object.keys(statuses) as Status[]).map(status => <li key={status}>
          <span>{statuses[status]}</span><div className="admin-bar-track" aria-hidden="true">
            <div className={status} style={{ width: `${data.feedback[status] / feedbackScale * 100}%` }} />
          </div><strong>{number.format(data.feedback[status])}</strong>
        </li>)}
      </ul>
    </figure>
  </div>;
}
