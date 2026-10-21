import { useEffect, useState } from 'react';
import { readHealth, type Health } from './api';

type Connection =
  | { state: 'checking' }
  | { state: 'online'; health: Health }
  | { state: 'offline'; message: string };

function DocumentIcon({ large = false }: { large?: boolean }) {
  return (
    <svg width={large ? 38 : 23} height={large ? 46 : 28} viewBox="0 0 32 38" fill="none" aria-hidden="true">
      <path d="M6 2h13l8 8v26H6V2Z" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" />
      <path d="M19 2v9h8M11 18h11M11 24h11M11 30h7" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

export default function App() {
  const [connection, setConnection] = useState<Connection>({ state: 'checking' });
  const [attempt, setAttempt] = useState(0);
  const [direction, setDirection] = useState('');
  const [resume, setResume] = useState('');
  const [jd, setJd] = useState('');

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    const timeout = window.setTimeout(() => controller.abort(), 8000);
    setConnection({ state: 'checking' });
    readHealth(controller.signal)
      .then((health) => { if (active) setConnection({ state: 'online', health }); })
      .catch((error: unknown) => {
        if (active) setConnection({
          state: 'offline',
          message: error instanceof Error && error.name !== 'AbortError'
            ? error.message : '连接超时，请确认本机后端已启动后重试。',
        });
      })
      .finally(() => window.clearTimeout(timeout));
    return () => { active = false; window.clearTimeout(timeout); controller.abort(); };
  }, [attempt]);

  const status = connection.state === 'online' ? '后端已连接'
    : connection.state === 'offline' ? '后端未连接' : '正在检查连接';
  const modelStatus = connection.state === 'online'
    ? connection.health.model_configured ? '模型密钥已配置' : '模型未配置'
    : '模型状态未知';

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">跳到主要内容</a>
      <header className="topbar">
        <div className="brand"><span className="brand-icon"><DocumentIcon /></span><span>历历在练</span></div>
        <div className="topbar-right"><span className="version-tag">基础预览版</span><span className="local-tag"><span className="local-dot" />本机工作区</span></div>
      </header>

      <div className="workspace">
        <aside className="sidebar" aria-label="历史会话">
          <div className="sidebar-heading"><h2>历史会话</h2><span className="count">0</span></div>
          <button className="new-session" disabled aria-describedby="sessions-note"><span aria-hidden="true">＋</span>新建会话</button>
          <div className="history-empty"><svg width="34" height="34" viewBox="0 0 32 32" fill="none" aria-hidden="true"><path d="M7 7h18v14H14l-7 5V7Z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round"/><path d="M12 12h8M12 16h6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/></svg><p>还没有会话</p><span id="sessions-note">会话创建与历史保存<br />将在后续版本启用</span></div>
          <div className="sidebar-footer"><strong>每一次修改，都有依据</strong><p>保留原始表达，核对修改理由，<br />再决定哪些内容写进简历。</p></div>
        </aside>

        <main id="main-content" className="conversation">
          <div className="panel-heading"><div><span className="section-kicker">新的简历任务</span><h1>让真实经历，表达得更清楚。</h1></div></div>
          <div className="conversation-scroll">
            <div className="connection-panel" aria-live="polite" aria-atomic="true">
              <div className="connection-top"><div className="connection-label"><span className={`status-dot ${connection.state}`} /><strong>{status}</strong></div><button className="text-button" onClick={() => setAttempt((value) => value + 1)} disabled={connection.state === 'checking'}>{connection.state === 'checking' ? '检查中…' : '重新检查'}</button></div>
              <p>{connection.state === 'offline' ? connection.message : connection.state === 'checking' ? '正在获取本机服务状态，不会调用模型。' : connection.health.model_configured ? '已检测到模型密钥配置。简历分析功能尚未接入。' : '基础服务可正常使用。未配置模型不会影响页面打开。'}</p>
              <span className="model-label">{modelStatus}</span>
            </div>

            <section className="intro"><h2>从你已有的简历开始</h2><p>提供经历与求职方向，之后由 Agent 查找依据、提出修改建议，再由你决定采用。</p></section>

            <div className="input-section">
              <label htmlFor="direction">求职方向<span className="required-label">必填</span></label>
              <input id="direction" value={direction} onChange={(event) => setDirection(event.target.value)} placeholder="例如：后端开发实习、产品实习" maxLength={120} aria-describedby="preview-note" />
            </div>

            <div className="input-section">
              <div className="label-row"><label htmlFor="resume">简历内容<span className="required-label">必填</span></label><span className="field-meta">支持粘贴文字</span></div>
              <textarea id="resume" value={resume} onChange={(event) => setResume(event.target.value)} placeholder={'粘贴你的简历原文…\n\n可以包含教育背景、项目经历、实习经历与技能。'} maxLength={50000} aria-describedby="preview-note" />
              <div className="input-footer"><button className="attachment-button" disabled aria-describedby="upload-note"><span aria-hidden="true">＋</span>添加文件</button><span id="upload-note">文件导入尚未启用</span><span className="char-count">{resume.length.toLocaleString()} 字符</span></div>
            </div>

            <details className="jd-section"><summary>添加岗位描述（JD）<span>选填</span></summary><label className="visually-hidden" htmlFor="jd">岗位描述（JD，选填）</label><p>提供 JD 后，后续可结合具体岗位要求调整重点。</p><textarea id="jd" value={jd} onChange={(event) => setJd(event.target.value)} placeholder="粘贴目标岗位的职责与要求…" maxLength={50000} aria-describedby="preview-note" /></details>

            <div className="action-row"><p id="preview-note">当前仅供界面预览，输入尚未保存，刷新后会清空。</p><button className="primary-button" disabled aria-describedby="analysis-note">开始完善<span aria-hidden="true">↗</span></button></div>
            <p id="analysis-note" className="analysis-note">简历分析与建议确认尚未启用，不会发送输入内容或产生模型费用。</p>
          </div>
          <footer className="conversation-footer"><span className="footer-mark" aria-hidden="true">✓</span>依据真实材料修改，不补造经历与成果。</footer>
        </main>

        <aside className="draft-panel" aria-labelledby="draft-heading">
          <div className="draft-header"><div><span className="section-kicker">完整稿件</span><h2 id="draft-heading">当前简历</h2></div><span className="draft-status">暂无稿件</span></div>
          <div className="draft-toolbar"><span className="preview-label">Markdown 预览</span><div><button disabled>复制</button><button disabled>下载</button></div></div>
          <div className="draft-empty"><div className="paper-preview" aria-hidden="true"><div className="paper-name"/><div className="paper-subtitle"/><div className="paper-rule"/><div className="paper-line long"/><div className="paper-line"/><div className="paper-line short"/><div className="paper-rule"/><div className="paper-line long"/><div className="paper-line"/><div className="paper-line short"/><span className="paper-badge"><DocumentIcon /></span></div><h3>你的简历，会在这里成稿</h3><p>后续采用的修改会汇总到当前稿，<br />并支持预览、复制与下载。</p><span className="empty-caption">尚未生成或保存任何简历</span></div>
          <div className="draft-note"><span aria-hidden="true">↳</span><p>修改建议与当前稿分别展示。<br />未接受的建议不会自动写入正文。</p></div>
        </aside>
      </div>
    </div>
  );
}
