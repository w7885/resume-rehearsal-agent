"""LLM segmentation fallback: line numbers only, rule result on any failure."""
import asyncio
import unittest

from app.agent_segmentation import refine, suspicious, validate
from app.resume_structure import split_resume

MERGED = '项目经历\n智能选课系统，2024.03 - 2024.06\n- 负责排课\n二手交易平台，2024.07 - 2024.12\n- 负责订单\n'
CLEAN = '项目经历\n智能选课系统 2024.03 - 2024.06\n- 负责排课\n'


class FakeRuntime:
    def __init__(self, boundaries=None, fail=False):
        self.boundaries, self.fail, self.calls = boundaries, fail, 0

    async def _json(self, instruction, value):
        self.calls += 1
        if self.fail:
            raise RuntimeError('model down')
        return {'boundaries': self.boundaries or []}


class SegmentationTests(unittest.TestCase):
    def test_rule_split_needs_no_model_call(self):
        runtime = FakeRuntime([99])
        self.assertFalse(suspicious(CLEAN))
        self.assertIsNone(asyncio.run(refine(runtime, CLEAN)))
        self.assertEqual(runtime.calls, 0)

    def test_merged_dated_entries_ask_for_line_numbers_only(self):
        runtime = FakeRuntime([3])
        self.assertTrue(suspicious(MERGED))
        self.assertEqual(asyncio.run(refine(runtime, MERGED)), {3})

    def test_bad_or_failed_model_result_falls_back_to_rules(self):
        self.assertIsNone(asyncio.run(refine(FakeRuntime(fail=True), MERGED)))
        # 0 是首行、2 是项目符号、99 越界、'3' 非整数，只有 3 可用。
        self.assertEqual(validate(MERGED, [0, 2, 99, '3', 3]), {3})
        self.assertIsNone(validate(MERGED, [0, '3', 99]))

    def test_boundaries_split_merged_entries_losslessly(self):
        parts = split_resume(MERGED, boundaries={3})
        self.assertEqual(parts, ['项目经历\n智能选课系统，2024.03 - 2024.06\n- 负责排课\n',
                                 '二手交易平台，2024.07 - 2024.12\n- 负责订单\n'])
        self.assertEqual(''.join(parts), MERGED)


if __name__ == '__main__':
    unittest.main()
