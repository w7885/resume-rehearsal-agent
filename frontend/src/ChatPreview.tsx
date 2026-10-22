import { useEffect, useRef, useState } from 'react';
import { Icon } from './ChatIcons';
import './chat-preview.css';

type Message = { id: number; role: 'user' | 'assistant'; text: string; attachments?: string[]; proposal?: string; evidence?: boolean };
const ORIGINAL = '参与三人团队的校园报名工具开发，负责需求整理，协助页面开发。';
const PROPOSAL = '参与三人团队的校园报名工具开发，整理 8 条同学反馈并归纳为 5 项需求，协助页面开发。';
const SAMPLE: Message[] = [
  { id: 1, role: 'user', text: '这是我的简历，想投产品实习。帮我看看项目经历怎么写更清楚。', attachments: ['简历_产品实习.pdf'] },
  { id: 2, role: 'assistant', text: '我们先看校园报名工具这段经历。你写了“负责需求整理”，但具体做了什么还不够清楚。\n\n你当时是怎样收集和整理需求的？记得哪些细节就说哪些，不用先组织成简历语言。' },
  { id: 3, role: 'user', text: '我整理了 8 条同学反馈，把重复意见合并成 5 项需求。页面开发是协助，主要不是我做的。' },
  { id: 4, role: 'assistant', text: '这就具体多了，也能看清你的职责边界。我建议这样写：', proposal: PROPOSAL, evidence: true },
];

export default function ChatPreview() {
  const [messages, setMessages] = useState<Message[]>(SAMPLE);
  const [input, setInput] = useState('');
  const [files, setFiles] = useState<string[]>([]);
  const [draftOpen, setDraftOpen] = useState(() => window.innerWidth >= 1000);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [title, setTitle] = useState('产品实习简历优化');
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(ORIGINAL);
  const [typing, setTyping] = useState(false);
  const [notice, setNotice] = useState('');
  const [inspection, setInspection] = useState<'evidence' | string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const thread = useRef<HTMLDivElement>(null);
  const inspector = useRef<HTMLDialogElement>(null);
  const responseTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  useEffect(() => () => clearTimeout(responseTimer.current), []);
  useEffect(() => { if (inspection !== null) inspector.current?.showModal(); }, [inspection]);
  useEffect(() => {
    const element = composer.current;
    if (element) { element.style.height = 'auto'; element.style.height = `${Math.min(element.scrollHeight, 150)}px`; }
  }, [input]);

  const scrollToEnd = () => requestAnimationFrame(() => { thread.current?.scrollTo({ top: thread.current.scrollHeight, behavior: 'smooth' }); });
  const send = () => {
    if ((!input.trim() && !files.length) || typing) return;
    const text = input.trim(), attached = [...files];
    setMessages(previous => [...previous, { id: Date.now(), role: 'user', text: text || '我上传了这份材料。', attachments: attached }]);
    if (!messages.length) setTitle(text ? text.slice(0, 16) : '新的简历对话');
    setInput(''); setFiles([]); setTyping(true); setNotice(''); scrollToEnd();
    responseTimer.current = setTimeout(() => {
      setMessages(previous => [...previous, { id: Date.now() + 1, role: 'assistant', text: attached.length
        ? '附件已显示在对话里。这个预览只展示文件名；正式接入后，我会从这里继续阅读材料，和你确认求职方向与经历细节。'
        : '这条消息已加入示例对话。正式接入后，Agent 会结合前文理解你的意思，再继续追问或确认修改。' }]);
      setTyping(false); scrollToEnd();
    }, 650);
  };
  const reset = (sample: boolean) => {
    clearTimeout(responseTimer.current); setTyping(false); setMessages(sample ? SAMPLE : []);
    setInput(''); setFiles([]); setTitle(sample ? '产品实习简历优化' : '新对话');
    setDraft(sample ? ORIGINAL : ''); setEditing(false); setNotice(''); setHistoryOpen(false);
    requestAnimationFrame(() => { thread.current?.scrollTo({ top: 0 }); composer.current?.focus(); });
  };
  const download = () => {
    const text = `# 实习简历（界面预览示例）\n\n## 项目经历\n\n${draft}\n`;
    const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' }));
    const link = document.createElement('a'); link.href = url; link.download = '界面预览_示例稿.md'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const copy = async () => {
    try { await navigator.clipboard.writeText(draft); setNotice('已复制示例稿'); }
    catch { setNotice('可在编辑模式选中文字复制'); }
  };

  return <div className={`cp-page ${draftOpen ? 'cp-with-draft' : ''}`}>
    <a className="cp-skip" href="#preview-composer">跳到对话输入框</a>
    {historyOpen && <button className="cp-shade" aria-label="关闭会话列表" onClick={() => setHistoryOpen(false)} />}
    <aside className={`cp-history ${historyOpen ? 'cp-history-open' : ''}`} aria-label="示例会话历史">
      <div className="cp-brand"><span className="cp-logo">历</span><strong>历历在练</strong><button className="cp-icon cp-mobile-close" aria-label="关闭会话列表" onClick={() => setHistoryOpen(false)}><Icon name="close" /></button></div>
      <button className="cp-new" onClick={() => reset(false)}><Icon name="plus" size={18} />新对话</button>
      <div className="cp-history-label">今天</div>
      <button className={`cp-history-item ${messages.length ? 'cp-active' : ''}`} onClick={() => reset(true)}><Icon name="chat" size={17} /><span>产品实习简历优化</span></button>
      <button className="cp-history-item" onClick={() => { reset(false); setTitle('后端实习项目经历'); }}><Icon name="chat" size={17} /><span>后端实习项目经历</span></button>
      <div className="cp-sidebar-note"><span className="cp-note-line" /><p>把真实经历<br />讲得更清楚。</p></div>
      <footer className="cp-profile"><span className="cp-profile-icon">我</span><span>我的空间<small>本地界面预览</small></span></footer>
    </aside>

    <main className="cp-chat" aria-label="连续对话预览">
      <header className="cp-chat-header"><div className="cp-header-title"><button className="cp-icon cp-menu" aria-label="展开会话列表" onClick={() => setHistoryOpen(true)}><Icon name="menu" /></button><h1>{title}</h1><span className="cp-preview-badge">界面预览</span></div><button className={`cp-draft-toggle ${draftOpen ? 'cp-selected' : ''}`} aria-label="切换当前稿" title="当前稿" aria-pressed={draftOpen} onClick={() => setDraftOpen(!draftOpen)}><Icon name="panel" size={18} /><span>当前稿</span></button></header>
      <div className="cp-thread" ref={thread}>
        {messages.length ? <div className="cp-message-list"><div className="cp-chat-date">今天 · 示例对话</div>{messages.map(message => <article className={`cp-message cp-${message.role}`} key={message.id} aria-label={message.role === 'user' ? '用户消息' : '示例 Agent 回复'}>
          {message.role === 'assistant' && <div className="cp-agent-avatar" aria-hidden="true">历</div>}
          <div className="cp-message-content"><div className="cp-message-text">{message.text.split('\n\n').map((paragraph, index) => <p key={index}>{paragraph}</p>)}</div>
            {message.attachments?.map((name, index) => <button className="cp-attachment" key={`${name}-${index}`} onClick={() => setInspection(name)}><span className="cp-file-symbol"><Icon name="attach" size={18} /></span><span>{name}<small>{message.id === 1 ? '示例附件' : '仅展示文件名'}</small></span><Icon name="chevron" size={15} /></button>)}
            {message.proposal && <><blockquote className="cp-proposed-text">{message.proposal}</blockquote><p className="cp-confirm-question">这里保留了“协助页面开发”，没有扩大你的贡献。这样调整可以吗？你也可以直接告诉我想怎么改。</p></>}
            {message.evidence && <button className="cp-evidence-link" onClick={() => setInspection('evidence')}>查看这处修改的依据<Icon name="chevron" size={13} /></button>}
          </div>
        </article>)}{typing && <div className="cp-typing" role="status"><span className="cp-agent-avatar">历</span><span>示例回复正在显示<span className="cp-typing-dots">…</span></span></div>}</div>
          : <div className="cp-welcome"><span className="cp-welcome-mark">历</span><h2>从你的经历开始</h2><p>上传简历，或者直接聊聊你想完善的部分。<br />不用先想好怎么写，我们可以边聊边梳理。</p><button className="cp-example-link" onClick={() => reset(true)}>先看看一段示例对话<Icon name="chevron" size={14} /></button></div>}
      </div>
      <div className="cp-composer-area"><div className="cp-composer">
        {!!files.length && <div className="cp-selected-files">{files.map((file, index) => <span key={`${file}-${index}`}><Icon name="attach" size={14} />{file}<button aria-label={`移除附件 ${file}`} onClick={() => setFiles(previous => previous.filter((_, i) => i !== index))}><Icon name="close" size={13} /></button></span>)}</div>}
        <label className="cp-sr-only" htmlFor="preview-composer">发送给 Agent 的消息</label><textarea id="preview-composer" ref={composer} value={input} onChange={event => setInput(event.target.value)} maxLength={5000} placeholder={messages.length ? '继续聊聊，或告诉我你想怎样调整…' : '说说你的求职方向，或上传简历…'} rows={1} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); send(); } }} />
        <div className="cp-composer-tools"><button className="cp-attach-button" aria-label="添加附件" onClick={() => fileInput.current?.click()}><Icon name="attach" size={19} /><span>添加附件</span></button><span className="cp-composer-hint">Enter 发送，Shift + Enter 换行</span><button className="cp-send" aria-label="发送消息" disabled={(!input.trim() && !files.length) || typing} onClick={send}><Icon name="send" size={21} /></button></div>
        <input ref={fileInput} className="cp-sr-only" tabIndex={-1} type="file" multiple onChange={event => { setFiles(previous => [...previous, ...Array.from(event.target.files ?? [], file => file.name)].slice(0, 8)); event.target.value = ''; }} />
      </div><p className="cp-preview-note">示例对话与临时输入仅用于界面预览，不保存资料，不调用模型。</p></div>
    </main>

    {draftOpen && <><button className="cp-draft-shade" aria-label="关闭当前稿" onClick={() => setDraftOpen(false)} /><aside className="cp-draft" aria-label="当前稿预览"><header className="cp-draft-header"><div><h2>当前稿</h2><span>产品实习 · 示例内容</span></div><button className="cp-icon" aria-label="收起当前稿" onClick={() => setDraftOpen(false)}><Icon name="close" size={18} /></button></header><div className="cp-draft-tools"><button className={editing ? 'cp-selected' : ''} aria-pressed={editing} onClick={() => setEditing(!editing)}><Icon name="edit" size={15} />{editing ? '结束编辑' : '编辑'}</button><div><button aria-label="复制示例稿" onClick={() => { void copy(); }}><Icon name="copy" size={16} /></button><button aria-label="下载示例稿" onClick={download}><Icon name="download" size={17} /></button></div></div><div className="cp-document-scroll"><div className="cp-paper">{!draft && !editing ? <div className="cp-empty-paper"><Icon name="panel" size={27} /><p>还没有当前稿</p><span>先在对话中上传简历，<br />当前稿将在这里展示。</span></div> : <><div className="cp-document-title">实习简历</div><p className="cp-document-subtitle">产品实习方向</p><div className="cp-paper-rule" /><h3>项目经历</h3><h4>校园报名工具</h4><span className="cp-project-meta">三人团队 · 课程项目</span>{editing ? <><label className="cp-sr-only" htmlFor="preview-draft-editor">示例稿编辑器</label><textarea id="preview-draft-editor" value={draft} onChange={event => setDraft(event.target.value)} maxLength={5000} /></> : <p className="cp-project-text">{draft}</p>}<h3>相关能力</h3><p className="cp-skill-text">需求整理 · 用户反馈归纳<br />页面开发协作</p></>}</div><p className="cp-draft-caption">当前展示原始表述。正式接入后，明确采用的修改会同步到这里。</p></div><footer className="cp-draft-footer"><span className="cp-small-dot" />示例稿仅在此页面临时展示<span role="status">{notice}</span></footer></aside></>}

    <dialog className="cp-inspector" ref={inspector} aria-labelledby="preview-inspector-title" onCancel={() => setInspection(null)}><header><h2 id="preview-inspector-title">{inspection === 'evidence' ? '这处修改的依据' : '附件信息'}</h2><button className="cp-icon" aria-label="关闭详情" onClick={() => { inspector.current?.close(); setInspection(null); }}><Icon name="close" /></button></header>{inspection === 'evidence' ? <><p className="cp-inspector-label">示例用户补充</p><blockquote>我整理了 8 条同学反馈，把重复意见合并成 5 项需求；页面开发是协助。</blockquote><p>修改只补充了已有行动与数量，保留协助开发的职责范围，没有增加未提供的业务收益。</p><p className="cp-inspector-footnote">此处展示构造材料，供你确认依据入口的交互形式。</p></> : <><div className="cp-inspector-file"><Icon name="attach" /><strong>{inspection}</strong></div><p>本轮只预览附件在聊天中的显示方式，不读取、上传或保存文件内容。</p></>}</dialog>
  </div>;
}
