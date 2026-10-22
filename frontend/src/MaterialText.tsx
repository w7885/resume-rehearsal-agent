import { useEffect, useRef, useState } from 'react';
import * as api from './api';
import type { Material, Source } from './types';
import type { SourceCorrectionWrite } from './api';

export const sourcePosition = (source: Source) => `${source.origin.kind === 'message' ? '用户原话' : '材料原文'}${source.origin.page ? ` · 第 ${source.origin.page} 页` : ''}${source.origin.paragraph ? ` · 第 ${source.origin.paragraph} 段` : ''}${source.origin.table ? ` · 表 ${source.origin.table} / 行 ${source.origin.row} / 列 ${source.origin.column}` : ''}${source.origin.part?.includes('header') ? ' · 页眉' : source.origin.part?.includes('footer') ? ' · 页脚' : ''}${source.origin.lineStart ? ` · 第 ${source.origin.lineStart}—${source.origin.lineEnd ?? source.origin.lineStart} 行` : ''}`;
const errorText = (error: unknown) => error instanceof Error ? error.message : '操作未确认，请保留输入后重试。';
const STATUS: Record<string, string> = { pending: '待提取', parsed: '文本已提取', partial: '部分结果 · 待核对', failed: '提取未完成' };

export default function MaterialText({ sid, id, editable, onGuard, beforeChange, onChanged }: {
  sid: string; id: string; editable: boolean; onGuard: (value: boolean) => void;
  beforeChange: () => Promise<void>; onChanged: () => Promise<void>;
}) {
  const [value, setValue] = useState<{ material: Material; sources: Source[] } | null>(null);
  const [loading, setLoading] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [editing, setEditing] = useState<Source | null>(null), [text, setText] = useState('');
  const [saving, setSaving] = useState(false);
  const pending = useRef<SourceCorrectionWrite | null>(null), confirmed = useRef(false), gate = useRef(false);
  useEffect(() => {
    onGuard(!!editing);
    const protect = (event: BeforeUnloadEvent) => { if (editing) { event.preventDefault(); event.returnValue = ''; } };
    window.addEventListener('beforeunload', protect);
    return () => { window.removeEventListener('beforeunload', protect); onGuard(false); };
  }, [editing, onGuard]);
  const load = async () => {
    if (editing || loading) return;
    setLoading(true); setError('');
    try { setValue(await api.readMaterial(sid, id)); } catch (failure) { setError(errorText(failure)); }
    finally { setLoading(false); }
  };
  const save = async () => {
    if (!editing || gate.current || !text.trim()) return;
    gate.current = true; setSaving(true); setError('');
    try {
      if (!pending.current && !confirmed.current) {
        await beforeChange();
        const fresh = await api.readConversation(sid);
        pending.current = { requestId: api.requestId(), expectedStateRevision: fresh.session.stateRevision, expectedSourceRevision: editing.sourceRevision, text };
      }
      if (!confirmed.current) {
        await api.correctMaterialSource(sid, id, editing.sourceId, pending.current!);
        confirmed.current = true;
      }
      // Acknowledged writes are never repeated when the following reads fail.
      await onChanged();
      setValue(await api.readMaterial(sid, id));
      setNotice('已保存为新的来源版本，旧引用和原文件保留；当前稿不会自动改写。');
      pending.current = null; confirmed.current = false; setEditing(null); setText('');
    } catch (failure) {
      setError(confirmed.current ? '修正已保存，读回未完成，请重试读取；不会重复写入。' : errorText(failure));
      if (failure instanceof api.ApiError && ['stale_state', 'invalid_input', 'scope_violation', 'source_not_found', 'session_not_found', 'request_conflict'].includes(failure.code) && !confirmed.current) pending.current = null;
    } finally { gate.current = false; setSaving(false); }
  };
  const latest = new Map<string, number>();
  value?.sources.forEach(source => latest.set(source.sourceId, Math.max(latest.get(source.sourceId) ?? 0, source.sourceRevision)));
  return <section className="cp-extracted"><button disabled={loading || !!editing} onClick={() => { void load(); }}>{loading ? '正在读取…' : value ? '重新读取已提取原文' : '查看已提取原文'}</button>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {value && <><p><strong>{STATUS[value.material.parseStatus] ?? value.material.parseStatus}</strong></p><p>{value.material.recognitionReport?.message ?? value.material.parseReport?.message ?? '这是此前保存的材料，未记录本次解析报告。'}</p>{!value.material.recognitionReport && value.material.parseReport?.warnings.map((warning, index) => <p className="cp-parse-warning" key={index}>{warning.message}</p>)}
      {!value.sources.length ? <p>暂无可用提取文本，可下载原件、转换文件后重新上传，或将文字粘贴到聊天。</p> : <><p>原文快照按保存顺序列出；历史版本不自动作为当前事实。</p>{value.sources.map(source => <details key={`${source.sourceId}-${source.sourceRevision}`}><summary>{sourcePosition(source)} · 版本 {source.sourceRevision}{source.sourceRevision !== latest.get(source.sourceId) ? ' · 历史快照' : ''}</summary><pre>{source.text}</pre>{editable && source.sourceRevision === latest.get(source.sourceId) && <button disabled={!!editing} onClick={() => { setEditing(source); setText(source.text); setError(''); setNotice(''); }}>修正提取文字</button>}</details>)}</>}
    </>}
    {editing && <div className="cp-source-edit"><label htmlFor="source-correction">修正提取文字 · {sourcePosition(editing)}</label><textarea id="source-correction" value={text} maxLength={50000} readOnly={!!pending.current || confirmed.current} disabled={saving} onChange={event => setText(event.target.value)} /><p>请按原件修正提取错误；保存后新增版本，不替换原文件或当前稿。先保存或取消本次修改，再关闭详情。</p><button disabled={saving || !text.trim()} onClick={() => { void save(); }}>{confirmed.current ? '重试读取已保存修正' : pending.current ? '重试原修正' : '保存修正为新版本'}</button><button disabled={saving || !!pending.current || confirmed.current} onClick={() => { setEditing(null); setText(''); setError(''); }}>取消修改</button></div>}
  </section>;
}
