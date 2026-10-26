"""Constructed proposals test personal claim provenance; no model calls."""
import unittest

from app.agent.guard import check
from app.agent.memory import Question
from app.agent.schemas import StepDecisionAdapter
from test_segment_guard import state


class PersonalClaimGuardTests(unittest.TestCase):
    def probe(self, after, *, before='参与接口测试。', supplement='', jd='', kind='project'):
        guard = state(answered=True, effect=True)
        guard.focus_doc.kind = kind
        guard.focus_block_markdown = before
        guard.focus_doc.raw_text = before
        guard.source_texts['resume'] = before
        refs = [{'source_id': 'resume', 'source_revision': 1, 'quote': before}]
        for source_id, text, kind in [('supplement', supplement, 'supplement'), ('jd', jd, 'jd')]:
            if text:
                guard.source_texts[source_id] = text
                guard.source_kinds[source_id] = kind
                refs.append({'source_id': source_id, 'source_revision': 1, 'quote': text})
        proposal = StepDecisionAdapter.validate_python({'action': 'tool', 'tool': 'propose_change', 'args': {
            'operation': 'replace', 'target': 'p1', 'before': before, 'after': after,
            'reason': '构造来源约束测试', 'source_refs': refs, 'gaps': []}})
        return check(proposal, guard)

    def test_all_section_types_reject_unsubstantiated_personal_claims(self):
        # Isolate the section branches with identical valid source/target setup;
        # project/work interview prerequisites are already satisfied by probe.
        for kind in ('project', 'work', 'profile', 'education', 'skills', 'summary', 'awards'):
            for claim, expected_code in (
                ('已取得教师资格证', 'unsupported_section_claim'),
                ('已取得博士学位', 'unsupported_section_claim'),
                ('熟练掌握Python', 'unsupported_section_claim'),
                ('邮箱：sample@example.test', 'profile_identity_changed'),
                ('电话：13800000000', 'profile_identity_changed'),
            ):
                with self.subTest(section=kind, claim=claim):
                    result = self.probe('协助接口功能测试，' + claim + '。', kind=kind)
                    self.assertIsNotNone(result.violation)
                    self.assertEqual(result.violation.code, expected_code)

    def test_all_section_types_allow_explicitly_supported_personal_claims(self):
        # These synthetic quotes are affirmative personal sources, not JD text
        # or model memory. A passing claim still needs user confirmation later.
        for kind in ('project', 'work', 'profile', 'education', 'skills', 'summary', 'awards'):
            for claim, supplement in (
                ('持有教师资格证', '我已取得教师资格证。'),
                ('已取得博士学位', '我已取得博士学位。'),
                ('熟练掌握Python', '我熟练掌握Python。'),
                ('邮箱：sample@example.test', '我的邮箱：sample@example.test。'),
                ('电话：13800000000', '我的电话：13800000000。'),
            ):
                with self.subTest(section=kind, claim=claim):
                    result = self.probe('协助接口功能测试，' + claim + '。',
                                        supplement=supplement, kind=kind)
                    self.assertIsNone(result.violation)

    def test_experience_cannot_gain_unsupported_credentials_or_levels(self):
        for claim in ('已取得执业医师资格证', '持有教师资格证', '拥有网络工程师资格证书',
                      '持有驾驶证', '熟练掌握Python', '熟练使用Python', '精通Python', '熟悉Python', '博士'):
            with self.subTest(claim=claim):
                self.assertEqual(self.probe('参与接口测试，' + claim + '。').violation.code,
                                 'unsupported_section_claim')

    def test_quoted_affirmative_personal_claim_is_allowed(self):
        for claim in ('已取得教师资格证', '持有驾驶证', '熟练掌握Python', '精通Python'):
            with self.subTest(claim=claim):
                self.assertIsNone(self.probe('协助接口测试，' + claim + '。', supplement='我' + claim + '。').violation)

    def test_original_qualification_survives_rewording(self):
        self.assertIsNone(self.probe('协助接口功能测试，持有教师资格证。',
            before='参与接口测试，已取得教师资格证。').violation)

    def test_negation_hypothesis_question_and_plan_are_not_proof(self):
        for source in ('我没有教师资格证。', '我不持有教师资格证。', '我未持有教师资格证。', '我尚未取得教师资格证。',
                       '无教师资格证。', '我并非拥有教师资格证。',
                       '如果已取得教师资格证，可以这样写。', '我有教师资格证吗？',
                       '我有教师资格证吗', '我计划取得教师资格证。'):
            with self.subTest(source=source):
                self.assertEqual(self.probe('协助接口测试，已取得教师资格证。', supplement=source).violation.code,
                                 'unsupported_section_claim')

    def test_original_negation_cannot_be_dropped_to_invent_qualification(self):
        self.assertEqual(self.probe('参与接口功能测试，已取得教师资格证。',
            before='参与接口测试，未取得教师资格证。').violation.code, 'unsupported_section_claim')

    def test_skill_level_is_bound_to_the_named_skill(self):
        self.assertEqual(self.probe('协助接口测试，熟练掌握Python。',
            supplement='我熟练掌握Java，了解Python。').violation.code, 'unsupported_section_claim')
        self.assertEqual(self.probe('协助接口测试，熟练掌握Python。',
            supplement='我不熟练掌握Python。').violation.code, 'unsupported_section_claim')

    def test_same_level_skill_lists_can_be_reformatted(self):
        for before, after in (
            ('熟练掌握Python和SQL。', '熟练掌握 Python、SQL。'),
            ('熟练掌握Python、SQL。', '熟练掌握 Python和SQL。'),
            ('熟练掌握Python和SQL。', '熟练掌握 Python与SQL。'),
            ('熟练掌握Python与SQL。', '熟练掌握 SQL、Python。'),
            ('熟练使用Python及SQL。', '熟练掌握 Python、SQL。'),
        ):
            with self.subTest(before=before, after=after):
                self.assertIsNone(self.probe(after, before=before, kind='skills').violation)

    def test_skill_list_cannot_borrow_level_from_another_skill(self):
        for before in ('熟练掌握Python，了解SQL。', '熟练掌握Python和了解SQL。',
                       '熟练掌握Python与熟悉SQL。'):
            with self.subTest(before=before):
                self.assertEqual(self.probe('熟练掌握 Python、SQL。', before=before, kind='skills').violation.code,
                                 'unsupported_section_claim')

    def test_every_credential_in_a_list_needs_evidence(self):
        self.assertEqual(self.probe('协助接口测试，已取得教师资格证和驾驶证。',
            supplement='我已取得驾驶证。').violation.code, 'unsupported_section_claim')
        self.assertIsNone(self.probe('协助接口测试，持有教师资格证和驾驶证。',
            supplement='我已取得教师资格证和驾驶证。').violation)

    def test_jd_cannot_prove_a_personal_qualification(self):
        self.assertEqual(self.probe('协助接口测试，已取得教师资格证。',
            jd='任职要求：已取得教师资格证。').violation.code, 'unsupported_section_claim')

    def test_contact_identity_is_protected_in_experience_too(self):
        self.assertEqual(self.probe('协助接口测试，邮箱：other@example.com。').violation.code,
                         'profile_identity_changed')

    def test_sqlite_library_can_be_named_as_its_database_without_warning(self):
        result = self.probe('使用 SQLite 完成接口测试。', before='使用sqlite3参与接口测试。')
        self.assertIsNone(result.violation)
        self.assertEqual(result.gaps, [])

    def test_explicit_absence_does_not_revive_original_effect(self):
        guard = state(answered=True, effect=True)
        before = '我负责接口测试，完成测试用例，提高了测试效率。'
        guard.focus_doc.raw_text = before
        guard.focus_block_markdown = before
        guard.source_texts['resume'] = before
        guard.focus_doc.facts = []
        guard.focus_doc.questions.append(Question(id='stop', slot='effect', text='实际效果？',
            fingerprint='stop', outcome='declined', answer_kind='absent', answered_at='2026-10-26T10:00:00Z'))
        proposal = StepDecisionAdapter.validate_python({'action':'tool', 'tool':'propose_change', 'args': {
            'operation':'replace', 'target':'p1', 'before':before,
            'after':'我负责接口功能测试，提高了测试效率。', 'reason':'测试已撤回效果不复活',
            'source_refs':[{'source_id':'resume','source_revision':1,'quote':before}], 'gaps':[]}})
        self.assertEqual(check(proposal, guard).violation.code, 'effect_claim_without_evidence')

    def test_refusing_more_questions_does_not_retract_original_effect(self):
        guard = state(answered=True, effect=True)
        before = '我负责接口测试，完成测试用例，提高了测试效率。'
        guard.focus_doc.raw_text = before
        guard.focus_block_markdown = before
        guard.source_texts['resume'] = before
        guard.focus_doc.facts = []
        guard.focus_doc.questions.append(Question(id='stop', slot='effect', text='实际效果？',
            fingerprint='stop', outcome='declined', answer_kind='refused', answered_at='2026-10-26T10:00:00Z'))
        proposal = StepDecisionAdapter.validate_python({'action':'tool', 'tool':'propose_change', 'args': {
            'operation':'replace', 'target':'p1', 'before':before,
            'after':'我负责接口功能测试，提高了测试效率。', 'reason':'拒答不是撤回原文效果',
            'source_refs':[{'source_id':'resume','source_revision':1,'quote':before}], 'gaps':[]}})
        self.assertIsNone(check(proposal, guard).violation)

    def test_different_new_technology_still_warns(self):
        result = self.probe('使用PostgreSQL完成接口测试。', before='使用sqlite3参与接口测试。')
        self.assertIn('缺少依据：PostgreSQL', result.gaps)


if __name__ == '__main__':
    unittest.main()
