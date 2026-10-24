import asyncio
import unittest

from app.agent.guard import GuardState, check
from app.agent.loop import run_loop
from app.agent.schemas import StepDecisionAdapter
from app.agent.memory import (
    ExperienceDoc,
    Question,
    SessionDoc,
    derive_phase,
    load_experience,
    load_session,
    next_unconfirmed,
    question_fingerprint,
    save_experience,
    save_session,
    sync_experiences,
)
from app.agent.ports import DraftBlock, SessionContext, SessionInfo, SourceRef, Suggestion
from app.agent.router import handle_message
from app.agent.store import InMemoryStateStore
from fakes import FakeLLM, FakeRepo

MARKDOWN = "校园二手交易平台：负责前端页面开发。"
SESSION = "s_01"
FINISH = {"action": "finish", "outcome": "completed", "summary": ""}


def make_repo(
    *,
    direction: str = "",
    direction_confirmed: bool = False,
    markdown: str = MARKDOWN,
) -> FakeRepo:
    return FakeRepo(
        session_id=SESSION,
        direction=direction,
        direction_confirmed=direction_confirmed,
        experience_order=["exp_01"],
        current_focus_id="exp_01",
        draft_blocks=[
            DraftBlock(
                block_id="exp_01",
                experience_id="exp_01",
                block_revision=1,
                markdown=markdown,
                source_refs=[
                    SourceRef(source_id="src_1", quote="", source_revision=1)
                ],
            )
        ],
    )


def seed_experience(store, *, streak=0, asked=None, slot="result", answered=False) -> None:
    doc = ExperienceDoc(
        experience_id="exp_01",
        seq=1,
        kind="project",
        title="校园二手交易平台",
        raw_text=MARKDOWN,
        source_ids=["src_1"],
        block_id="exp_01",
        block_revision=1,
        no_fact_streak=streak,
    )
    if asked:
        doc.questions.append(
            Question(
                id="q1",
                slot=slot,
                text=asked,
                fingerprint=question_fingerprint(asked),
                round=1,
                outcome="answered" if answered else "asked",
            )
        )
    save_experience(store, SESSION, doc, None)


def run_turn(repo, store, llm, text, message_id="m1"):
    return asyncio.run(
        handle_message(
            session_id=SESSION,
            text=text,
            repo=repo,
            store=store,
            llm=llm,
            message_id=message_id,
        )
    )


def run_loop_once(repo, store, llm, *, answered=False):
    return asyncio.run(
        run_loop(
            run_id="r_test",
            session_id=SESSION,
            repo=repo,
            store=store,
            llm=llm,
            focus_experience_id="exp_01",
            answered_question=answered,
        )
    )


class DeclineAndStreakTests(unittest.TestCase):
    def test_uncertain_reply_is_declined(self):
        repo, store = make_repo(), InMemoryStateStore()
        seed_experience(store, asked="这段经历的结果怎么样？", slot="result")
        llm = FakeLLM(decide_script=[FINISH])
        run_turn(repo, store, llm, "不确定")
        doc, _ = load_experience(store, SESSION, "exp_01")
        self.assertEqual(doc.questions[0].outcome, "declined")
        self.assertIn("result", doc.declined)

    def test_bare_maybe_is_not_declined(self):
        repo, store = make_repo(), InMemoryStateStore()
        seed_experience(store, asked="这段经历的结果怎么样？", slot="result")
        llm = FakeLLM(decide_script=[FINISH])
        run_turn(repo, store, llm, "可能负责了接口")
        doc, _ = load_experience(store, SESSION, "exp_01")
        self.assertEqual(doc.questions[0].outcome, "answered")
        self.assertNotIn("result", doc.declined)

    def test_two_answers_without_facts_set_streak(self):
        repo, store = make_repo(), InMemoryStateStore()
        seed_experience(store, asked="这段经历的结果怎么样？", slot="result")
        llm = FakeLLM(
            decide_script=[
                {
                    "action": "ask_user",
                    "message": "「校园二手交易平台」这部分的动作具体有哪些？",
                    "slot": "action",
                    "rationale": "动作未确认",
                },
                FINISH,
            ]
        )
        run_turn(repo, store, llm, "我不清楚")
        doc, _ = load_experience(store, SESSION, "exp_01")
        self.assertEqual(doc.no_fact_streak, 1)
        run_turn(repo, store, llm, "就这些吧", message_id="m2")
        doc, _ = load_experience(store, SESSION, "exp_01")
        self.assertEqual(doc.no_fact_streak, 2)

    def test_skip_routed_decline_increments_streak(self):
        repo, store = make_repo(), InMemoryStateStore()
        seed_experience(store, streak=1, asked="这段经历的结果怎么样？", slot="result")
        llm = FakeLLM(decide_script=[FINISH])
        run_turn(repo, store, llm, "跳过")
        doc, _ = load_experience(store, SESSION, "exp_01")
        self.assertEqual(doc.questions[0].outcome, "declined")
        self.assertEqual(doc.no_fact_streak, 2)

    def test_new_fact_resets_streak(self):
        repo, store = make_repo(), InMemoryStateStore()
        seed_experience(store, streak=2)
        llm = FakeLLM(
            decide_script=[
                {
                    "action": "tool",
                    "tool": "save_facts",
                    "args": {
                        "experience_id": "exp_01",
                        "facts": [
                            {
                                "slot": "action",
                                "statement": "负责前端页面开发",
                            }
                        ],
                    },
                    "rationale": "记录动作",
                },
                FINISH,
            ]
        )
        run_loop_once(repo, store, llm, answered=True)
        doc, _ = load_experience(store, SESSION, "exp_01")
        self.assertEqual(doc.no_fact_streak, 0)


ANSWER_ROUTE = {
    "action": "answer",
    "target_experience_id": "exp_01",
    "scope": "experience",
    "confidence": 1,
}


def seed_session(store, **fields) -> None:
    save_session(store, SESSION, SessionDoc(**fields), None)


class GoalTests(unittest.TestCase):
    def test_goal_question_asked_once(self):
        repo, store = make_repo(direction=""), InMemoryStateStore()
        llm = FakeLLM(route_script=[ANSWER_ROUTE], decide_script=[])
        events = run_turn(repo, store, llm, "改一下这段")
        questions = [event for event in events if event.type == "question"]
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0].payload["question_id"], "goal")
        self.assertFalse(any(kind == "decide" for kind, _ in llm.calls))
        session, _ = load_session(store, SESSION)
        self.assertTrue(session.goal_asked)
        self.assertTrue(session.awaiting_goal)

    def test_placeholder_direction_still_asks_goal(self):
        repo, store = make_repo(direction="新对话"), InMemoryStateStore()
        llm = FakeLLM(route_script=[ANSWER_ROUTE], decide_script=[])
        events = run_turn(repo, store, llm, "帮我改一下")
        questions = [event for event in events if event.type == "question"]
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0].payload["question_id"], "goal")

    def test_goal_answer_is_captured_and_run_continues(self):
        repo, store = make_repo(direction=""), InMemoryStateStore()
        seed_session(store, goal_asked=True, awaiting_goal=True)
        seed_experience(store, asked="这段经历你负责哪些部分？", slot="responsibility", answered=True)
        propose = {
            "action": "tool",
            "tool": "propose_change",
            "args": {
                "operation": "replace",
                "target": "exp_01",
                "before": MARKDOWN,
                "after": "校园二手交易平台：负责前端页面开发与联调。",
                "reason": "补充职责",
                "source_refs": [
                    {
                        "source_id": "src_1",
                        "source_revision": 1,
                        "quote": "React",
                    }
                ],
                "gaps": [],
            },
            "rationale": "补充职责",
        }
        llm = FakeLLM(decide_script=[propose, FINISH], route_script=[])
        events = run_turn(repo, store, llm, "销售岗")
        self.assertFalse(any(kind == "route" for kind, _ in llm.calls))
        self.assertTrue(any(event.type == "suggestion" for event in events))
        session, _ = load_session(store, SESSION)
        self.assertEqual(session.goal, "销售岗")
        self.assertFalse(session.awaiting_goal)

    def test_direction_suppresses_goal_question(self):
        repo, store = make_repo(
            direction="前端开发实习", direction_confirmed=True
        ), InMemoryStateStore()
        llm = FakeLLM(route_script=[ANSWER_ROUTE], decide_script=[FINISH])
        events = run_turn(repo, store, llm, "改一下这段")
        self.assertFalse(
            any(
                event.type == "question"
                and event.payload.get("question_id") == "goal"
                for event in events
            )
        )


ASK_ACTION = {
    "action": "ask_user",
    "message": "「校园二手交易平台」这部分的动作具体有哪些？",
    "slot": "action",
    "rationale": "动作未确认",
}


class BrakeTests(unittest.TestCase):
    def test_brake_rejects_ask_then_allows_finish(self):
        repo, store = make_repo(), InMemoryStateStore()
        seed_experience(store, streak=2)
        llm = FakeLLM(decide_script=[ASK_ACTION, FINISH])
        result = run_loop_once(repo, store, llm)
        self.assertEqual(result.outcome, "completed")
        codes = [
            event.payload.get("code")
            for event in result.events
            if event.type == "warning"
        ]
        self.assertIn("ask_blocked", codes)
        self.assertFalse(any(event.type == "question" for event in result.events))

    def test_brake_fallback_does_not_ask(self):
        repo, store = make_repo(), InMemoryStateStore()
        seed_experience(store, streak=2)
        llm = FakeLLM(decide_script=[ASK_ACTION, ASK_ACTION])
        result = run_loop_once(repo, store, llm)
        self.assertEqual(result.outcome, "limited")
        self.assertFalse(any(event.type == "question" for event in result.events))

    def test_context_renders_brake_section(self):
        from app.agent.context import assemble_context

        repo = make_repo()
        ctx = repo.get_session_context(SESSION)
        doc = ExperienceDoc(
            experience_id="exp_01",
            seq=1,
            kind="project",
            title="校园二手交易平台",
            raw_text=MARKDOWN,
            block_id="exp_01",
            block_revision=1,
            no_fact_streak=2,
        )
        text = assemble_context(
            ctx, [doc], {"exp_01": "interviewing"}, "exp_01"
        )
        self.assertIn("## 追问刹车", text)
        self.assertIn("禁止 ask_user", text)


class DuplicateToolTests(unittest.TestCase):
    def test_duplicate_tool_call_is_rejected(self):
        repo, store = make_repo(), InMemoryStateStore()
        seed_experience(store)
        llm = FakeLLM(
            decide_script=[
                {
                    "action": "tool",
                    "tool": "list_experiences",
                    "args": {},
                    "rationale": "查看经历",
                },
                {
                    "action": "tool",
                    "tool": "list_experiences",
                    "args": {},
                    "rationale": "再看一次",
                },
                FINISH,
            ]
        )
        result = run_loop_once(repo, store, llm)
        self.assertEqual(result.outcome, "completed")
        codes = [
            event.payload.get("code")
            for event in result.events
            if event.type == "warning"
        ]
        self.assertIn("duplicate_tool", codes)


class NormalizeTests(unittest.TestCase):
    def test_flat_propose_change_is_normalized(self):
        repo, store = make_repo(), InMemoryStateStore()
        seed_experience(store, asked="这段经历你负责哪些部分？", slot="responsibility", answered=True)
        flat = {
            "action": "propose_change",
            "operation": "replace",
            "target": "exp_01",
            "before": MARKDOWN,
            "after": "校园二手交易平台：负责前端页面开发，并完成联调。",
            "reason": "补充联调职责",
            "source_refs": [
                {
                    "source_id": "src_1",
                    "source_revision": 1,
                    "quote": "负责前端页面开发",
                }
            ],
            "gaps": [],
            "rationale": "补充职责",
        }
        llm = FakeLLM(decide_script=[flat, FINISH])
        result = run_loop_once(repo, store, llm)
        self.assertTrue(any(event.type == "suggestion" for event in result.events))
        self.assertEqual(repo.suggestions[-1].after, flat["after"])


class SegmentMemoryTests(unittest.TestCase):
    def blocks(self):
        return [
            DraftBlock(block_id="head", experience_id="head", markdown="张三\n电话 138"),
            DraftBlock(block_id="school", experience_id="school", markdown="教育背景\n测试大学"),
            DraftBlock(block_id="p1", experience_id="p1", markdown="项目经历1\n- 动作"),
        ]

    def context(self):
        repo = make_repo()
        repo.session.experience_order = []
        repo.draft_blocks = self.blocks()
        return repo.get_session_context(SESSION)

    def test_only_experiences_get_docs_and_focus_is_first(self):
        store = InMemoryStateStore()
        doc, _ = sync_experiences(SESSION, self.context(), store, None, None)
        self.assertEqual(doc.experience_order, ["p1"])
        self.assertEqual(doc.current_focus_id, "p1")
        self.assertEqual(load_experience(store, SESSION, "p1")[0].kind, "project")
        self.assertIsNone(load_experience(store, SESSION, "head"))
        self.assertIsNone(load_experience(store, SESSION, "school"))

    def test_frozen_blocks_drop_out_but_scoped_absent_ids_survive(self):
        store = InMemoryStateStore()
        save_session(
            store,
            SESSION,
            SessionDoc(
                experience_order=["head", "p1", "gone"], current_focus_id="head"
            ),
            None,
        )
        doc, _ = sync_experiences(SESSION, self.context(), store, *load_session(store, SESSION))
        self.assertEqual(doc.experience_order, ["p1", "gone"])
        self.assertEqual(doc.current_focus_id, "p1")

    def test_accepted_segment_stays_confirmed_after_memory_rebuild(self):
        store = InMemoryStateStore()
        ctx = SessionContext(
            session=SessionInfo(
                session_id=SESSION,
                experience_order=["p1", "p2"],
                current_focus_id="p1",
            ),
            draft_blocks=[
                DraftBlock(block_id="p1", experience_id="p1", markdown="项目经历1\n- 动作"),
                DraftBlock(block_id="p2", experience_id="p2", markdown="项目经历2\n- 动作"),
            ],
            suggestions=[
                Suggestion(
                    suggestion_id="s1",
                    experience_id="p1",
                    block_id="p1",
                    status="accepted",
                )
            ],
        )
        doc, _ = sync_experiences(SESSION, ctx, store, None, None)
        docs = {
            experience_id: load_experience(store, SESSION, experience_id)[0]
            for experience_id in doc.experience_order
        }
        suggestions = {s.suggestion_id: s for s in ctx.suggestions}
        phases = {
            experience_id: derive_phase(
                item,
                suggestions.get(item.last_suggestion_id)
                if item.last_suggestion_id
                else None,
            )
            for experience_id, item in docs.items()
        }
        self.assertEqual(phases, {"p1": "confirmed", "p2": "pending"})
        self.assertEqual(
            next_unconfirmed(doc.experience_order, docs, phases, "p1"), "p2"
        )

    def test_no_experiences_clears_the_focus(self):
        repo = make_repo()
        repo.session.experience_order = []
        repo.draft_blocks = self.blocks()[:2]
        doc, _ = sync_experiences(
            SESSION, repo.get_session_context(SESSION), InMemoryStateStore(), None, None
        )
        self.assertEqual(doc.experience_order, [])
        self.assertIsNone(doc.current_focus_id)


class AskFirstGuardTests(unittest.TestCase):
    def state(self, *, answered=False, direct=False):
        doc = ExperienceDoc(
            experience_id="exp_01",
            seq=1,
            kind="project",
            title="校园二手交易平台",
            raw_text=MARKDOWN,
            source_ids=["src_1"],
            block_id="exp_01",
            block_revision=1,
        )
        if answered:
            doc.questions.append(
                Question(
                    id="q1",
                    slot="responsibility",
                    text="这部分是你本人负责的吗？",
                    fingerprint="q1",
                    outcome="answered",
                )
            )
        return GuardState(
            session=SessionContext(
                session=SessionInfo(
                    session_id=SESSION, experience_order=["exp_01", "exp_02"]
                ),
                draft_blocks=[DraftBlock(block_id="exp_01", markdown=MARKDOWN)],
            ),
            focus_doc=doc,
            anchor_text=MARKDOWN,
            focus_block_markdown=MARKDOWN,
            source_texts={"src_1": MARKDOWN},
            source_kinds={"src_1": "resume"},
            direct_rewrite=direct,
        )

    def propose(self, target="exp_01"):
        return StepDecisionAdapter.validate_python(
            {
                "action": "tool",
                "tool": "propose_change",
                "args": {
                    "operation": "replace",
                    "target": target,
                    "before": MARKDOWN,
                    "after": "校园二手交易平台：负责前端页面开发与接口联调。",
                    "reason": "补充联调职责",
                    "source_refs": [
                        {
                            "source_id": "src_1",
                            "source_revision": 1,
                            "quote": "负责前端页面开发",
                        }
                    ],
                    "gaps": [],
                },
            }
        )

    def test_propose_needs_one_answered_question_first(self):
        result = check(self.propose(), self.state())
        self.assertEqual(result.violation.code, "ask_first")
        self.assertTrue(result.violation.retryable)

    def test_answered_question_unlocks_the_rewrite(self):
        self.assertIsNone(check(self.propose(), self.state(answered=True)).violation)

    def test_explicit_direct_rewrite_skips_the_first_question(self):
        self.assertIsNone(check(self.propose(), self.state(direct=True)).violation)

    def test_other_segment_is_never_rewritten(self):
        result = check(self.propose("exp_02"), self.state(answered=True))
        self.assertEqual(result.violation.code, "wrong_target")


class AdvanceRouteTests(unittest.TestCase):
    def context(self, phases):
        repo = make_repo()
        repo.session.experience_order = ["exp_01", "exp_02"]
        repo.draft_blocks = [
            DraftBlock(block_id="exp_01", experience_id="exp_01", markdown="项目一\n- 动作"),
            DraftBlock(block_id="exp_02", experience_id="exp_02", markdown="项目二\n- 动作"),
        ]
        docs = [
            ExperienceDoc(experience_id="exp_01", seq=1, kind="project", title="项目一"),
            ExperienceDoc(experience_id="exp_02", seq=2, kind="project", title="项目二"),
        ]
        return repo.get_session_context(SESSION), docs

    def test_continue_targets_the_next_unconfirmed_segment(self):
        from app.agent.route import route_message

        ctx, docs = self.context(None)
        result = asyncio.run(
            route_message(
                message="继续",
                ctx=ctx,
                docs=docs,
                phases={"exp_01": "confirmed", "exp_02": "pending"},
                pending=None,
                has_pending_question=False,
                llm=FakeLLM(),
            )
        )
        self.assertEqual(result.action, "advance")
        self.assertEqual(result.target_experience_id, "exp_02")

    def test_continue_after_the_last_segment_has_no_target(self):
        from app.agent.route import route_message

        ctx, docs = self.context(None)
        result = asyncio.run(
            route_message(
                message="继续",
                ctx=ctx,
                docs=docs,
                phases={"exp_01": "confirmed", "exp_02": "confirmed"},
                pending=None,
                has_pending_question=False,
                llm=FakeLLM(),
            )
        )
        self.assertEqual(result.action, "advance")
        self.assertIsNone(result.target_experience_id)


class TokenTests(unittest.TestCase):
    def test_max_tokens_floor_for_short_block(self):
        repo, store = make_repo(), InMemoryStateStore()
        seed_experience(store)
        llm = FakeLLM(decide_script=[FINISH])
        run_loop_once(repo, store, llm)
        self.assertEqual(llm.calls[0][1]["max_tokens"], 2000)

    def test_max_tokens_scales_with_block_length(self):
        repo = make_repo(markdown="字" * 2000)
        store = InMemoryStateStore()
        seed_experience(store)
        llm = FakeLLM(decide_script=[FINISH])
        run_loop_once(repo, store, llm)
        self.assertEqual(llm.calls[0][1]["max_tokens"], 2466)


class TruncationTests(unittest.IsolatedAsyncioTestCase):
    async def test_length_finish_reason_doubles_max_tokens(self):
        import httpx

        from app.agent.llm import DeepSeekClient, LLMSettings

        seen = []
        replies = [
            {
                "choices": [
                    {
                        "message": {"content": '{"act'},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            },
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"action":"finish","outcome":"completed"}'
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        ]

        def handler(request):
            import json as jsonlib

            seen.append(jsonlib.loads(request.content)["max_tokens"])
            return httpx.Response(200, json=replies.pop(0))

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            llm = DeepSeekClient(
                LLMSettings(api_key="k", max_tokens=2000), client=client
            )
            result = await llm.complete_json(
                system="s", user="u", run_id="r"
            )
        self.assertEqual(result.data["action"], "finish")
        self.assertEqual(seen, [2000, 4000])
