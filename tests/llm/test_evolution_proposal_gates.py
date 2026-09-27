"""规则提案闸门回归（规划文档 §4.5 / §12.1 的提案部分）。

关键判据：模型只能提交提案，**不能**把提案变成硬规则；
样本数够但独立性/反例不足 ⇒ 降级为待验证观察；
触碰 L0/L1 或声称基线失效 ⇒ 只进人工审批队列。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from scripts.evolution.memory_review import (  # noqa: E402
    MIN_INDEPENDENT_GROUPS, MIN_TIME_WINDOWS, evaluate_rule_proposals)
from scripts.evolution.review_context import independent_sample_groups  # noqa: E402
from scripts.evolution_shield import audit_structured_lesson  # noqa: E402


def _trade(idx, open_time, *, signal_id="", snap=None, inst="BTC", side="long"):
    return {"inst": inst, "side": side, "open_time": open_time, "signal_id": signal_id,
            "entry_snapshot": snap, "strategy_version": "trend_following@1"}


TRADES = [
    _trade(0, "2026-09-20 09:00:00"),
    _trade(1, "2026-09-21 09:00:00"),
    _trade(2, "2026-09-22 09:00:00"),
]


def _proposal(text="趋势追随多单在 RSI 极值时不再追价，等待回踩确认", **over):
    base = {"text": text, "scope": {"strategy_modes": ["trend_following"]},
            "evidence_ids": ["trade:0", "trade:1", "trade:2"],
            "independent_sample_groups": 3, "counterexample_count": 1,
            "counterexamples_checked": True}
    base.update(over)
    return base


def _gate(proposals, trades=None, groups=2):
    return evaluate_rule_proposals(
        rule_proposals=proposals, closed_trades=TRADES if trades is None else trades,
        audit_structured_lesson=audit_structured_lesson,
        independent_sample_groups=groups)


class PromotionGateTests(unittest.TestCase):
    def test_a_qualified_proposal_is_accepted_as_reviewed_heuristic(self):
        result = _gate([_proposal()])
        self.assertEqual(len(result["accepted"]), 1)
        self.assertEqual(result["accepted"][0]["evidence_level"], "REVIEWED_HEURISTIC")
        self.assertEqual(result["metadata"][_proposal()["text"]]["evidence_level"],
                         "REVIEWED_HEURISTIC")

    def test_scope_is_required(self):
        result = _gate([_proposal(scope={})])
        self.assertEqual(result["accepted"], [])
        self.assertEqual(result["rejected"][0]["rejection_reasons"], ["SCOPE_REQUIRED"])

    def test_absolute_claims_are_rejected(self):
        result = _gate([_proposal(text="浮盈保本可以锁死胜率不再回撤")])
        self.assertEqual(result["accepted"], [])
        self.assertEqual(len(result["rejected"]), 1)

    def test_hard_rule_tampering_goes_to_human_approval(self):
        result = _gate([_proposal(text="趋势追随多单把杠杆提高到 5x 以放大收益")])
        self.assertEqual(result["accepted"], [])
        self.assertEqual(len(result["requires_approval"]), 1)
        self.assertIn("RULE_PROPOSAL_REQUIRES_APPROVAL", result["reason_codes"])

    def test_baseline_invalidation_goes_to_human_approval(self):
        result = _gate([_proposal(invalidates_baseline=True,
                                  target_lesson_id="lesson_wide_atr_stop")])
        self.assertEqual(result["accepted"], [])
        self.assertEqual(len(result["requires_approval"]), 1)

    def test_missing_counterexample_check_downgrades_to_observation(self):
        result = _gate([_proposal(counterexamples_checked=False)])
        self.assertEqual(result["accepted"], [])
        self.assertEqual(result["observation_only"][0]["rejection_reasons"],
                         ["COUNTEREXAMPLE_CHECK_MISSING"])
        self.assertIn("RULE_PROPOSAL_OBSERVATION_ONLY", result["reason_codes"])

    def test_too_few_independent_groups_downgrades_to_observation(self):
        result = _gate([_proposal(independent_sample_groups=1,
                                  evidence_ids=["trade:0"])])
        self.assertEqual(result["accepted"], [])
        self.assertEqual(result["observation_only"][0]["rejection_reasons"],
                         ["INSUFFICIENT_INDEPENDENT_SAMPLES"])

    def test_too_few_time_windows_downgrades_to_observation(self):
        result = _gate([_proposal(evidence_ids=["trade:0", "trade:0", "trade:0"])],
                       trades=[_trade(0, "2026-09-20 09:00:00")])
        self.assertEqual(result["accepted"], [])
        self.assertIn(result["observation_only"][0]["rejection_reasons"][0],
                      {"INSUFFICIENT_INDEPENDENT_SAMPLES", "INSUFFICIENT_TIME_WINDOWS"})

    def test_observation_metadata_is_recorded(self):
        result = _gate([_proposal(independent_sample_groups=1,
                                  evidence_ids=["trade:0"])])
        text = _proposal()["text"]
        self.assertEqual(result["metadata"][text]["evidence_level"], "OBSERVATION_ONLY")


class IndependentSampleGroupTests(unittest.TestCase):
    def test_split_orders_of_one_signal_count_once(self):
        trades = [_trade(0, "2026-09-20 09:00:00", signal_id="sig-1"),
                  _trade(1, "2026-09-20 09:00:05", signal_id="sig-1"),
                  _trade(2, "2026-09-20 09:00:10", signal_id="sig-1")]
        count, groups = independent_sample_groups(trades)
        self.assertEqual(count, 1)
        self.assertEqual(list(groups.values())[0], [0, 1, 2])

    def test_distinct_signals_count_separately(self):
        trades = [_trade(0, "2026-09-20 09:00:00", signal_id="sig-1"),
                  _trade(1, "2026-09-21 09:00:00", signal_id="sig-2")]
        self.assertEqual(independent_sample_groups(trades)[0], 2)

    def test_same_hour_repeats_collapse_without_signal_ids(self):
        trades = [_trade(0, "2026-09-20 09:01:00"),
                  _trade(1, "2026-09-20 09:31:00"),
                  _trade(2, "2026-09-20 10:01:00")]
        self.assertEqual(independent_sample_groups(trades)[0], 2)

    def test_identical_snapshots_at_the_same_hour_still_collapse(self):
        snap = {"velocity": 1.0, "jerk": 0.2}
        trades = [_trade(0, "2026-09-20 09:01:00", snap=snap),
                  _trade(1, "2026-09-20 09:31:00", snap=dict(snap)),
                  _trade(2, "2026-09-22 10:01:00", snap={"velocity": 2.0})]
        self.assertEqual(independent_sample_groups(trades)[0], 2)


class ThresholdTests(unittest.TestCase):
    def test_documented_thresholds(self):
        self.assertGreaterEqual(MIN_INDEPENDENT_GROUPS, 2)
        self.assertGreaterEqual(MIN_TIME_WINDOWS, 2)


if __name__ == "__main__":
    unittest.main()
