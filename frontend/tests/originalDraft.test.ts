import test from 'node:test';
import assert from 'node:assert/strict';
import { ApiError, readConversation } from '../src/api.ts';

const baseline = { sessionId: 's', resumeGeneration: 3, markdown: '初版正文', sourceRefs: [{ sourceId: 'source', sourceRevision: 1, quote: '初版正文' }], materialIds: ['file'] };
const conversation = {
  session: { sessionId: 's', direction: '测试', stateRevision: 9, draftRevision: 5, currentResumeGeneration: 3, status: 'idle', mode: 'local', updatedAt: '', title: '会话', directionStatus: 'provided', targetDirection: '测试' },
  draft: { markdown: '后来修改过的当前稿', blocks: [] }, messages: [], materials: [], capabilities: { agentAvailable: false, decisionExecutionAvailable: false },
};

test('initial import remains distinct from current draft and absence is not replaced with edited text', async () => {
  const original = globalThis.fetch;
  try {
    for (const value of [baseline, null, undefined]) {
      globalThis.fetch = async () => Response.json({ ok: true, data: { ...conversation, originalDraft: value } });
      const result = await readConversation('s');
      assert.deepEqual(result.originalDraft, value);
      assert.equal(result.draft.markdown, conversation.draft.markdown);
    }
  } finally { globalThis.fetch = original; }
});

test('baseline from a foreign session, old generation or malformed sources is rejected', async () => {
  const original = globalThis.fetch;
  try {
    for (const value of [
      { ...baseline, sessionId: 'other' }, { ...baseline, resumeGeneration: 2 }, { ...baseline, markdown: null },
      { ...baseline, sourceRefs: [{ sourceId: 'source', sourceRevision: 0, quote: '无效版本' }] },
      { ...baseline, materialIds: null }, { ...baseline, materialIds: ['file', 'file'] },
    ]) {
      globalThis.fetch = async () => Response.json({ ok: true, data: { ...conversation, originalDraft: value } });
      await assert.rejects(readConversation('s'), (error: ApiError) => error.code === 'invalid_response');
    }
  } finally { globalThis.fetch = original; }
});
