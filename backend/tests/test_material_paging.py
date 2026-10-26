"""Bounded source paging and model-visible observations; synthetic, offline."""
import asyncio
import unittest

from pydantic import ValidationError

from app.agent.loop import run_loop
from app.agent.memory import load_experience, save_experience
from app.agent.ports import RepoError, SourceText
from app.agent.schemas import ReadMaterialArgs, ToolStep
from app.agent.store import InMemoryStateStore
from app.agent.tools import ToolContext, execute_tool
from fakes import FakeLLM, FakeRepo
import test_agent_session_policy as policy


MARKER = '关键事实：增加删除前确认提示。'


class MaterialPagingTests(unittest.TestCase):
    def setUp(self):
        self.original = '背景。' * 900 + MARKER + '结尾。'
        self.repo = FakeRepo(sources=[SourceText(source_id='long', kind='supplement',
                                                text=self.original, source_revision=3)])
        self.context = ToolContext('s_01', self.repo, InMemoryStateStore())

    def read(self, **args):
        step = ToolStep(action='tool', tool='read_material', args={'source_ids': ['long'], **args})
        return asyncio.run(execute_tool(step, self.context))['sources'][0]

    def test_old_call_keeps_default_prefix_and_reports_continuation(self):
        page = self.read()
        self.assertEqual(page['text'], self.original[:2000])
        self.assertEqual(page['source_revision'], 3)
        self.assertEqual(page['offset'], 0)
        self.assertEqual(page['total_chars'], len(self.original))
        self.assertTrue(page['has_more'])
        self.assertEqual(page['next_offset'], 2000)

    def test_requested_range_can_reach_a_late_fact(self):
        page = self.read(offset=2600, limit=200)
        self.assertEqual(page['text'], self.original[2600:2800])
        self.assertIn(MARKER, page['text'])

    def test_pages_reconstruct_original_without_gaps_or_duplicate_characters(self):
        offset = 0
        parts = []
        while True:
            page = self.read(offset=offset, limit=1000)
            self.assertEqual(page['offset'], offset)
            self.assertLessEqual(len(page['text']), 1000)
            parts.append(page['text'])
            if not page['has_more']:
                self.assertIsNone(page['next_offset'])
                break
            self.assertEqual(page['next_offset'], offset + len(page['text']))
            offset = page['next_offset']
        self.assertEqual(''.join(parts), self.original)

    def test_empty_source_and_offset_past_end_finish_cleanly(self):
        for original, offset in (('', 0), (self.original, len(self.original) + 100)):
            with self.subTest(length=len(original), offset=offset):
                self.repo.sources[0].text = original
                page = self.read(offset=offset)
                self.assertEqual(page['text'], '')
                self.assertFalse(page['has_more'])
                self.assertIsNone(page['next_offset'])

    def test_invalid_ranges_are_rejected(self):
        for values in ({'offset': -1}, {'offset': 1.5}, {'offset': True},
                       {'limit': 0}, {'limit': 2001}, {'limit': False}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                ReadMaterialArgs.model_validate({'source_ids': ['long'], **values})

    def test_batch_calls_still_return_each_sources_default_prefix(self):
        self.repo.sources.append(SourceText(source_id='other', kind='resume', text='其他材料。'))
        step = ToolStep(action='tool', tool='read_material', args={'source_ids': ['long', 'other']})
        result = asyncio.run(execute_tool(step, self.context))
        self.assertEqual([row['text'] for row in result['sources']],
                         [self.original[:2000], '其他材料。'])

    def test_existing_session_and_source_checks_are_preserved(self):
        for context, ids, code in (
            (ToolContext('foreign', self.repo, InMemoryStateStore()), ['long'], 'session_not_found'),
            (self.context, ['missing'], 'source_not_found'),
        ):
            with self.subTest(code=code), self.assertRaises(RepoError) as caught:
                asyncio.run(execute_tool(ToolStep(action='tool', tool='read_material',
                                                  args={'source_ids': ids}), context))
            self.assertEqual(caught.exception.code, code)


class MaterialPagingLoopTests(unittest.TestCase):
    def test_late_personal_quote_can_support_a_pending_suggestion_without_writing(self):
        repo = policy.make_repo(direction='开发实习', direction_confirmed=True)
        repo.sources.append(SourceText(source_id='long', kind='supplement',
                                        text='背景。' * 900 + MARKER, source_revision=3))
        store = InMemoryStateStore()
        policy.seed_experience(store, effect=True, asked='你负责哪部分？',
                               slot='responsibility', answered=True)
        experience, revision = load_experience(store, policy.SESSION, 'exp_01')
        experience.source_ids.append('long')
        save_experience(store, policy.SESSION, experience, revision)
        original = repo.draft_blocks[0].markdown
        after = original + '\n增加删除前确认提示。'
        llm = FakeLLM(decide_script=[
            {'action': 'tool', 'tool': 'read_material',
             'args': {'source_ids': ['long'], 'offset': 2400, 'limit': 1000}},
            {'action': 'tool', 'tool': 'propose_change', 'args': {
                'operation': 'replace', 'target': 'exp_01', 'before': original,
                'after': after, 'reason': '利用补读的个人原句整理', 'gaps': [],
                'source_refs': [{'source_id': 'long', 'source_revision': 3, 'quote': MARKER}],
            }},
            policy.FINISH,
        ])
        result = asyncio.run(run_loop(run_id='late-quote', session_id=policy.SESSION,
                                      repo=repo, store=store, llm=llm,
                                      focus_experience_id='exp_01'))
        self.assertIn(MARKER, llm.calls[1][1]['user'])
        self.assertEqual(result.outcome, 'waiting_user')
        self.assertEqual(len(repo.suggestions), 1)
        self.assertEqual(repo.suggestions[0].status, 'pending')
        self.assertEqual(repo.suggestions[0].after, after)
        self.assertEqual(repo.suggestions[0].source_refs[0].source_revision, 3)
        self.assertEqual(repo.draft_blocks[0].markdown, original)

    def test_single_source_page_reaches_next_model_context_without_history_clipping(self):
        for offset, marker_position in ((0, 1200), (2400, 2700)):
            with self.subTest(offset=offset):
                repo = policy.make_repo(direction='开发实习', direction_confirmed=True)
                original = '背景。' * (marker_position // 3) + MARKER + '结尾。'
                repo.sources.append(SourceText(source_id='long', kind='supplement',
                                                text=original, source_revision=3))
                store = InMemoryStateStore()
                policy.seed_experience(store)
                experience, revision = load_experience(store, policy.SESSION, 'exp_01')
                experience.source_ids.append('long')
                save_experience(store, policy.SESSION, experience, revision)
                llm = FakeLLM(decide_script=[
                    {'action': 'tool', 'tool': 'read_material',
                     'args': {'source_ids': ['long'], 'offset': offset, 'limit': 2000}},
                    policy.FINISH,
                ])
                result = asyncio.run(run_loop(run_id='paging', session_id=policy.SESSION,
                                              repo=repo, store=store, llm=llm,
                                              focus_experience_id='exp_01'))
                prompts = [kwargs['user'] for action, kwargs in llm.calls if action == 'decide']
                self.assertEqual(len(prompts), 2)
                self.assertNotIn(MARKER, prompts[0])
                self.assertIn(MARKER, prompts[1])
                self.assertIn('"source_revision": 3', prompts[1])
                self.assertEqual(result.outcome, 'completed')
                self.assertEqual(repo.suggestions, [])


if __name__ == '__main__':
    unittest.main()
