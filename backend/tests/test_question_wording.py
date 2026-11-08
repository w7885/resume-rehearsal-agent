"""Natural questions without automatic name/title quotation prefixes."""
import unittest
from app.agent.guard import GuardState, check
from app.agent.loop import _fallback_question
from app.agent.memory import ExperienceDoc, Fact, Question, record_question
from app.agent.questions import detail_slot
from app.agent.ports import SessionContext, SessionInfo
from app.agent.questions import guided_question, with_answer_guidance
from app.agent.schemas import AskStep


class QuestionWordingTests(unittest.TestCase):
    def state(self, title='张明'):
        doc = ExperienceDoc(experience_id='p1', seq=1, kind='project', title=title, raw_text='参与接口测试。')
        return GuardState(session=SessionContext(session=SessionInfo(session_id='test')), focus_doc=doc, anchor_text=doc.raw_text)

    def test_fallback_does_not_prefix_name_missing_title_or_raw_text(self):
        for title in ('张明', '无', '', '校园平台'):
            state = self.state(title)
            question, slot = _fallback_question(state.focus_doc)
            self.assertIn('接口测试', question)
            self.assertIn('没参与的不用补充', question)
            self.assertEqual(slot, 'responsibility')
            self.assertIsNone(check(AskStep(action='ask_user', message=question, slot=slot), state).violation)

    def test_fallback_prioritizes_a_concrete_action_after_responsibility(self):
        doc = ExperienceDoc(experience_id='p1', seq=1, kind='project', title='校园平台', raw_text='参与接口测试。')
        doc.facts.append(Fact(id='f1', slot='responsibility', statement='负责接口测试', created_at='2026-01-01T00:00:00Z'))
        question, slot = _fallback_question(doc)
        self.assertEqual(slot, 'action')
        self.assertIn('小任务', question)
        self.assertIn('挑一件具体的事说', question)

    def terse_responsibility_doc(self):
        doc = ExperienceDoc(
            experience_id='p1', seq=1, kind='project', title='学生信息管理系统 开发成员',
            raw_text='使用 Python 编写学生信息管理程序，实现学生信息的增加、删除、查询。',
        )
        doc.questions.append(Question(
            id='q1', slot='responsibility', text='你负责哪部分？', fingerprint='f',
            outcome='answered', answer_kind='answered', answer_quote='数据库表设计',
        ))
        doc.facts.append(Fact(
            id='f1', slot='responsibility', statement='负责数据库表设计',
            created_at='2026-01-01T00:00:00Z',
        ))
        return doc

    def test_short_responsibility_answer_asks_detail_before_effect(self):
        doc = self.terse_responsibility_doc()
        state = GuardState(
            session=SessionContext(session=SessionInfo(session_id='test')),
            focus_doc=doc, anchor_text=doc.raw_text,
        )
        result = check(AskStep(action='ask_user', message='这段经历交付后带来了什么实际变化？', slot='effect'), state)
        self.assertEqual(result.violation.code, 'ask_detail_first')
        tech = guided_question(doc, 'tech')
        self.assertIsNone(
            check(AskStep(action='ask_user', message=tech, slot='tech'), state).violation,
            tech,
        )

    def test_fallback_asks_detail_before_effect(self):
        doc = self.terse_responsibility_doc()
        question, slot = _fallback_question(doc)
        self.assertEqual(slot, 'tech')
        self.assertIn('实际用到了什么工具或方法', question)

    def tech_doc(self, raw_text='学生信息管理系统，负责课程作业。'):
        doc = ExperienceDoc(experience_id='p1', seq=1, kind='project',
                            title='学生信息管理系统', raw_text=raw_text)
        doc.questions.append(Question(
            id='q1', slot='responsibility', text='你负责哪部分？', fingerprint='f1',
            outcome='answered', answer_kind='answered', answer_quote='设计'))
        doc.questions.append(Question(
            id='q2', slot='tech', text='用了什么工具？', fingerprint='f2',
            outcome='answered', answer_kind='answered', answer_quote='使用 Python'))
        return doc

    def test_short_tech_answer_asks_one_concrete_action(self):
        doc = self.tech_doc()
        question, slot = _fallback_question(doc)
        self.assertEqual(slot, 'action')
        self.assertIn('Python', question)
        self.assertIn('哪一步', question)
        state = GuardState(
            session=SessionContext(session=SessionInfo(session_id='test')),
            focus_doc=doc, anchor_text=doc.raw_text,
        )
        self.assertIsNone(
            check(AskStep(action='ask_user', message=question, slot=slot), state).violation,
            question,
        )

    def test_action_already_in_resume_is_not_asked_again(self):
        doc = self.tech_doc('使用 Python 实现学生信息查询功能。')
        self.assertIsNone(detail_slot(doc))
        self.assertEqual(_fallback_question(doc)[1], 'effect')

    def test_detail_question_once_answered_unblocks_effect(self):
        doc = self.terse_responsibility_doc()
        doc.questions.append(Question(
            id='q2', slot='tech', text='用了什么技术？', fingerprint='f2',
            outcome='answered', answer_kind='answered', answer_quote='Python + SQLite',
        ))
        state = GuardState(
            session=SessionContext(session=SessionInfo(session_id='test')),
            focus_doc=doc, anchor_text=doc.raw_text,
        )
        self.assertIsNone(
            check(AskStep(action='ask_user', message='这段经历交付后带来了什么实际变化？', slot='effect'), state).violation
        )

    def test_natural_question_needs_no_verbatim_excerpt(self):
        result = check(AskStep(action='ask_user', message='你具体负责哪部分工作？', slot='responsibility'), self.state())
        self.assertIsNone(result.violation)

    def test_model_generated_prefix_is_rejected_for_natural_retry(self):
        for prefix in ('「张明」', '【无】', '[校园平台]', '“参与接口测试”'):
            result = check(AskStep(action='ask_user', message=prefix + '这部分是你负责的吗？', slot='responsibility'), self.state())
            self.assertEqual(result.violation.code, 'question_prefix')

    def test_other_segment_and_repeated_declined_question_checks_remain(self):
        state = self.state()
        state.other_titles = ['商城项目']
        result = check(AskStep(action='ask_user', message='商城项目中你负责哪部分？', slot='responsibility'), state)
        self.assertEqual(result.violation.code, 'question_cross_segment')
        state.focus_doc.declined = ['responsibility']
        result = check(AskStep(action='ask_user', message='你具体负责哪部分工作？', slot='responsibility'), state)
        self.assertEqual(result.violation.code, 'question_declined')

    def test_fallback_question_uses_rotated_variant(self):
        doc = self.state().focus_doc
        doc.questions.append(Question(id='q1', slot='responsibility', text='你负责哪部分？', fingerprint='f'))
        question, slot = _fallback_question(doc)
        self.assertEqual(slot, 'responsibility')
        self.assertIn('说一件你亲手做过的', question)

    def test_answer_hint_after_question_mark_is_not_a_second_question(self):
        result = check(AskStep(action='ask_user', message='学生信息管理系统做完后带来了什么变化？比如省掉哪一步或收到哪条反馈。', slot='effect'), self.state())
        self.assertIsNone(result.violation)

    def test_record_question_marks_requirement_followup(self):
        doc = self.state().focus_doc
        question = record_question(doc, '岗位要求里提到「负责跨部门需求对接」，你有类似经历吗？',
                                   'responsibility', evidence_ref='req-1',
                                   evidence_quote='负责跨部门需求对接')
        self.assertEqual(doc.requirement_followups, ['responsibility'])
        self.assertEqual(question.evidence_ref, 'req-1')
        self.assertEqual(question.evidence_quote, '负责跨部门需求对接')

    def test_two_question_words_in_the_question_are_still_multi(self):
        result = check(AskStep(action='ask_user', message='你是怎么发现什么问题这件事的？', slot='action'), self.state())
        self.assertEqual(result.violation.code, 'question_multi')


from app.agent.loop import _fallback_ask, _requirement_question


class RequirementQuestionTests(unittest.TestCase):
    def requirements(self):
        return [{'requirementId': 'req-1', 'referenceId': 'ref-1',
                 'quote': '负责跨部门需求对接与排期推进'}]

    def declined_doc(self, kind='project', answer_kind='legacy'):
        doc = ExperienceDoc(experience_id='p1', seq=1, kind=kind, raw_text='参与接口测试。')
        doc.declined = ['responsibility']
        doc.questions.append(Question(
            id='q1', slot='responsibility', text='你负责哪部分？', fingerprint='f',
            outcome='declined', answer_kind=answer_kind))
        return doc

    def test_requirement_question_targets_a_declined_slot_once(self):
        doc = self.declined_doc()
        text, slot, ref, quote = _requirement_question(doc, self.requirements())
        self.assertEqual(slot, 'responsibility')
        self.assertEqual(ref, 'req-1')
        self.assertEqual(quote, '负责跨部门需求对接与排期推进')
        self.assertEqual(text.count('？'), 1)
        self.assertLessEqual(len(text), 120)
        self.assertIn('没有也可以', text)

        state = GuardState(
            session=SessionContext(
                session=SessionInfo(session_id='test'),
                web_context={'available': True, 'requirements': self.requirements()}),
            focus_doc=doc, anchor_text=doc.raw_text)
        self.assertIsNone(check(AskStep(
            action='ask_user', message=text, slot=slot,
            evidence_ref=ref, evidence_quote=quote), state).violation)

    def test_no_second_requirement_question_for_the_same_slot(self):
        doc = ExperienceDoc(experience_id='p1', seq=1, kind='project', raw_text='参与接口测试。')
        doc.declined = ['responsibility']
        doc.requirement_followups = ['responsibility']
        self.assertIsNone(_requirement_question(doc, self.requirements()))

    def test_no_requirements_means_no_generated_followup(self):
        self.assertIsNone(_requirement_question(self.declined_doc(), []))

    def test_absent_or_refused_decline_is_not_reasked(self):
        # 明确没有或拒答的槽不因岗位要求再打开。
        for kind in ('absent', 'refused'):
            with self.subTest(kind=kind):
                self.assertIsNone(
                    _requirement_question(self.declined_doc(answer_kind=kind), self.requirements()))

    def test_uncertain_decline_can_be_reasked(self):
        self.assertIsNotNone(
            _requirement_question(self.declined_doc(answer_kind='uncertain'), self.requirements()))

    def test_section_document_gets_no_requirement_question(self):
        self.assertIsNone(
            _requirement_question(self.declined_doc(kind='skills', answer_kind='uncertain'),
                                  self.requirements()))

    def test_invalid_requirement_question_falls_back_to_generic(self):
        # 要求原文含“以及”会让生成问题被判 multi（一次只能问一个点）；
        # 这时仍应退回通用问题，而不是直接结束本轮。
        requirements = [{'requirementId': 'req-1', 'referenceId': 'ref-1',
                         'quote': '负责跨部门需求对接以及排期推进'}]
        doc = self.declined_doc()
        state = GuardState(
            session=SessionContext(
                session=SessionInfo(session_id='test'),
                web_context={'available': True, 'requirements': requirements}),
            focus_doc=doc, anchor_text=doc.raw_text)
        text, slot, ref, quote, violations = _fallback_ask(doc, requirements, state)
        self.assertEqual([violation.code for violation in violations], ['question_multi'])
        self.assertIsNotNone(text)
        self.assertIsNone(ref)
        self.assertEqual(slot, 'action')
        self.assertIsNone(
            check(AskStep(action='ask_user', message=text, slot=slot), state).violation, text)


class GuideVariationTests(unittest.TestCase):
    """引导文案按槽位和重试次数轮换，不再逐字复用。"""

    def doc(self):
        return ExperienceDoc(experience_id='p1', seq=1, kind='project', raw_text='参与接口测试。')

    def test_first_asks_alternate_by_slot(self):
        doc = self.doc()
        responsibility = with_answer_guidance('你负责哪部分？', doc, 'responsibility')
        action = with_answer_guidance('你做了什么？', doc, 'action')
        self.assertIn('没参与的不用补充', responsibility)
        self.assertIn('挑一件具体的事说', action)

    def test_same_slot_retry_switches_guide(self):
        doc = self.doc()
        first = with_answer_guidance('你做了什么？', doc, 'action')
        doc.questions.append(Question(id='q1', slot='action', text='你做了什么？', fingerprint='f'))
        second = with_answer_guidance('你做了什么？', doc, 'action')
        self.assertIn('挑一件具体的事说', first)
        self.assertIn('遇到的情况→采取的做法', second)

    def test_model_question_with_its_own_hint_is_kept(self):
        doc = self.doc()
        text = '接口测试里你做了哪部分？可以从测试用例或联调中选一项。'
        self.assertEqual(with_answer_guidance(text, doc, 'responsibility'), text)

    def test_guided_question_variants_stay_bounded(self):
        doc = self.doc()
        for slot in ('responsibility', 'action', 'effect', 'context', 'evidence', 'tech'):
            for variant in (0, 1):
                with self.subTest(slot=slot, variant=variant):
                    text = guided_question(doc, slot, variant=variant)
                    self.assertEqual(text.count('？'), 1)
                    self.assertLessEqual(len(text), 120)

    def test_effect_followup_has_two_variants(self):
        doc = self.doc()
        texts = {
            guided_question(doc, 'effect', effect_followup=True, variant=variant)
            for variant in (0, 1)
        }
        self.assertEqual(len(texts), 2)

    def test_every_generated_question_passes_the_guard(self):
        doc = self.doc()
        state = GuardState(
            session=SessionContext(session=SessionInfo(session_id='test')),
            focus_doc=doc, anchor_text=doc.raw_text,
        )
        cases = [(slot, variant, False) for slot in
                 ('responsibility', 'action', 'effect', 'context', 'evidence', 'tech')
                 for variant in (0, 1)]
        cases += [('effect', variant, True) for variant in (0, 1)]
        for slot, variant, followup in cases:
            text = guided_question(doc, slot, effect_followup=followup, variant=variant)
            with self.subTest(slot=slot, variant=variant, followup=followup):
                self.assertIsNone(
                    check(AskStep(action='ask_user', message=text, slot=slot), state).violation,
                    text,
                )
