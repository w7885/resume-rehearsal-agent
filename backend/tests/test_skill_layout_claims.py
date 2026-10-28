"""Synthetic layout/skill regression; no provider calls or personal documents."""
import unittest

from app.agent.claim_text import claim_comparison_text
import test_agent_claims as claims


def columns(left, right):
    return left.ljust(41) + right


LAYOUT = '\n'.join([
    columns('编程能力', 'AI能力'),
    columns('▸擅⻓推荐系', '▸熟悉TensorFlow/PyTorch框架。'),
    columns('  统和前端开发。', '  有模型使用经验。'),
    columns('▸熟悉React、Redis/MySQL、Docker等开发框', '▸擅⻓Agent架构设计。'),
    columns('  架和⼯具。', '  使用过模型接口。'),
])


class SkillLayoutTests(unittest.TestCase):
    def probe(self, after, before=LAYOUT, supplement='', jd=''):
        return claims.PersonalClaimGuardTests().probe(
            after, before=before, supplement=supplement, jd=jd, kind='skills')

    def test_original_and_no_new_skills_can_be_used_together(self):
        for answer in ('没有，无需补充', '原来的技能都保留，没有新增技能'):
            with self.subTest(answer=answer):
                result = self.probe('熟悉React、Redis/MySQL、Docker等开发框架和工具。', supplement=answer)
                self.assertIsNone(result.violation)

    def test_two_columns_and_radicals_preserve_their_own_skill_levels(self):
        result = self.probe('擅长推荐系统和前端开发；熟悉Docker；熟悉TensorFlow/PyTorch框架；擅长Agent架构设计。')
        self.assertIsNone(result.violation)

    def test_affirmative_chat_supplements_do_not_replace_original_claims(self):
        self.assertIsNone(self.probe('熟悉Docker，熟悉Kubernetes。', supplement='我熟悉Kubernetes。').violation)

    def test_new_skills_and_upgrades_remain_rejected(self):
        for after in ('熟悉Kubernetes。', '了解Kubernetes。', '掌握Kubernetes。', '精通Docker。', '熟练使用Docker。',
                      '擅长Docker。', '熟悉Agent架构设计。', '精通PyTorch。'):
            with self.subTest(after=after):
                self.assertEqual(self.probe(after).violation.code, 'unsupported_section_claim')

    def test_specific_extra_scope_is_not_a_generic_label(self):
        for after in ('熟悉Docker集群部署。', '熟悉Docker安全。', '熟悉Docker安全工具。'):
            with self.subTest(after=after):
                self.assertEqual(self.probe(after).violation.code, 'unsupported_section_claim')

    def test_jd_hypotheses_negations_and_plans_cannot_supply_new_skills(self):
        for answer in ('如果熟悉Kubernetes，可以写吗？', '我没有熟悉Kubernetes。',
                       '我计划熟悉Kubernetes。', '熟悉Kubernetes吗？'):
            with self.subTest(answer=answer):
                self.assertEqual(self.probe('熟悉Kubernetes。', supplement=answer).violation.code,
                                 'unsupported_section_claim')
        self.assertEqual(self.probe('熟悉Kubernetes。', jd='任职要求：熟悉Kubernetes。').violation.code,
                         'unsupported_section_claim')
        for answer in ('我不了解Kubernetes。', '我不太了解Kubernetes。', '我未了解Kubernetes。', '我并不是了解Kubernetes。'):
            with self.subTest(answer=answer):
                self.assertEqual(self.probe('了解Kubernetes。', supplement=answer).violation.code,
                                 'unsupported_section_claim')

    def test_explicit_correction_limits_only_the_named_skill(self):
        answer = '更正：我不熟悉Docker，只了解Docker。'
        self.assertIsNone(self.probe('了解Docker，熟悉React。', supplement=answer).violation)
        self.assertEqual(self.probe('熟悉Docker，熟悉React。', supplement=answer).violation.code,
                         'unsupported_section_claim')
        self.assertEqual(self.probe('熟悉Docker。', supplement='我不熟悉Docker。').violation.code,
                         'unsupported_section_claim')

    def test_hypothetical_correction_does_not_retract_original_skill(self):
        self.assertIsNone(self.probe('熟悉Docker。', supplement='如果我不熟悉Docker，应该怎么写？').violation)

    def test_denial_of_expertise_does_not_erase_existing_familiarity(self):
        self.assertIsNone(self.probe('熟悉Docker，熟悉React。', supplement='我不精通Docker。').violation)
        self.assertEqual(self.probe('精通Docker。', supplement='我不精通Docker。').violation.code,
                         'unsupported_section_claim')
        self.assertEqual(self.probe('熟悉Docker。', supplement='我不精通Docker，只了解Docker。').violation.code,
                         'unsupported_section_claim')
        self.assertEqual(self.probe('精通Docker。', supplement='我只是了解Docker，并不是精通Docker。').violation.code,
                         'unsupported_section_claim')

    def test_layout_does_not_transfer_levels_between_columns(self):
        before = '\n'.join([
            columns('工具', '其他工具'),
            columns('▸熟练使用Java。', '▸了解SQL。'),
            columns('  用于测试。', '  用于查询。'),
            columns('▸熟悉Docker。', '▸了解Python。'),
        ])
        for after in ('熟练使用SQL。', '熟悉Python。', '熟练使用Docker。'):
            with self.subTest(after=after):
                self.assertEqual(self.probe(after, before=before).violation.code, 'unsupported_section_claim')

    def test_plain_list_labels_and_whitespace_are_not_new_skills(self):
        for before, after in (
            ('熟悉React、Docker等开发框架和工具。', '熟悉Docker、React。'),
            ('熟悉Docker。', '熟悉 Docker 等开发框架。'),
            ('熟悉TensorFlow/PyTorch框架。', '熟悉TensorFlow和PyTorch。'),
            ('熟悉Ｄｏｃｋｅｒ。', '熟悉Docker。'),
            ('熟悉Docker。', '了解Docker。'),
            ('掌握Docker。', '了解Docker。'),
        ):
            with self.subTest(before=before, after=after):
                self.assertIsNone(self.probe(after, before=before).violation)

    def test_source_parts_cannot_be_joined_to_invent_a_skill_name(self):
        self.assertEqual(self.probe('熟悉Docker。', before='熟悉Dock', supplement='  er。').violation.code,
                         'unsupported_section_claim')

    def test_ambiguous_gutters_do_not_trigger_column_reconstruction(self):
        text = '熟悉Dock    er。\n左侧    右侧。'
        self.assertEqual(self.probe('熟悉Docker。', before=text).violation.code, 'unsupported_section_claim')

    def test_comparison_view_preserves_source_and_is_deterministic(self):
        original = LAYOUT
        view = claim_comparison_text(original)
        self.assertEqual(LAYOUT, original)
        self.assertIn('擅长推荐系统', view)
        self.assertIn('Docker等开发框架和工具', view)
        self.assertEqual(view, claim_comparison_text(original))


if __name__ == '__main__':
    unittest.main()
