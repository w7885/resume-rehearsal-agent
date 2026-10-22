import type { ReactNode } from 'react';

function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))/g).map((part, index) => {
    if (part.startsWith('**') && part.endsWith('**')) return <strong key={index}>{part.slice(2, -2)}</strong>;
    if (part.startsWith('`') && part.endsWith('`')) return <code key={index}>{part.slice(1, -1)}</code>;
    const link = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(part);
    if (link && /^(https?:\/\/|mailto:)/i.test(link[2])) return <a key={index} href={link[2]} target="_blank" rel="noopener noreferrer">{link[1]}</a>;
    return part;
  });
}

/** React text nodes only: raw HTML, scripts and images are never executed. */
export default function MarkdownPreview({ markdown }: { markdown: string }) {
  const output: ReactNode[] = [];
  let paragraph: string[] = [], code: string[] = [], fence = '', list: { ordered: boolean; lines: string[] } | null = null;
  const flush = () => {
    if (paragraph.length) { output.push(<p key={output.length}>{inline(paragraph.join('\n'))}</p>); paragraph = []; }
    if (list) {
      const children = list.lines.map((line, i) => <li key={i}>{inline(line)}</li>);
      output.push(list.ordered ? <ol key={output.length}>{children}</ol> : <ul key={output.length}>{children}</ul>); list = null;
    }
  };
  for (const line of markdown.split(/\r?\n/)) {
    const marker = /^\s{0,3}(`{3,}|~{3,})/.exec(line);
    if (fence) {
      if (marker && marker[1][0] === fence[0] && marker[1].length >= fence.length && !line.slice(marker[0].length).trim()) {
        output.push(<pre key={output.length}><code>{code.join('\n')}</code></pre>); code = []; fence = '';
      } else code.push(line);
    } else if (marker) { flush(); fence = marker[1]; }
    else if (!line.trim()) flush();
    else {
      const heading = /^(#{1,6})\s+(.+)$/.exec(line), item = /^\s*(?:([-*+])|\d+[.)])\s+(.+)$/.exec(line);
      if (heading) {
        flush(); const content = inline(heading[2]);
        // Preview headings start at h3 to keep the workbench heading hierarchy.
        const Tag = heading[1].length === 1 ? 'h3' : heading[1].length === 2 ? 'h4' : 'h5';
        output.push(<Tag key={output.length}>{content}</Tag>);
      } else if (item) {
        if (paragraph.length || (list && list.ordered !== !item[1])) flush();
        list ??= { ordered: !item[1], lines: [] }; list.lines.push(item[2]);
      } else { if (list) flush(); paragraph.push(line); }
    }
  }
  flush(); if (fence) output.push(<pre key={output.length}><code>{code.join('\n')}</code></pre>);
  return <div className="markdown-preview">{output.length ? output : <p className="muted">当前稿为空，可在编辑区输入。</p>}</div>;
}
