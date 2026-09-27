"""自进化记忆契约门（规划文档 §12.1 的证据边界部分）。

覆盖：

- 快照来源枚举与可信来源；
- 无时间证明的 fallback 只能 NONE / PRICE_ONLY；
- 未来时间 / 过期时间不得判为完整；
- 同信号拆单去重（独立样本组）；
- 重复快照检测（DUPLICATED_SIGNAL_EVIDENCE）；
- 台账装载把来源/时间/策略版本/证据等级带出来；
- 资产乘数边界（越界拒绝、过期、损坏、revision 变化）；
- R 与 ATR 语义不可混用。
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from scripts.evolution import observability as obs  # noqa: E402
from scripts.brain.decisions import load_asset_multiplier_state  # noqa: E402

NOW = datetime.datetime.now(datetime.timezone.utc)


def _full_strategy_snapshot(**over):
    snap = {
        "strategy_version": "trend_following@1",
        "snapshot_source": "direct_signal_journal",
        "captured_at": NOW.isoformat(),
        "signal_time": (NOW - datetime.timedelta(seconds=5)).isoformat(),
        "open_time": (NOW - datetime.timedelta(seconds=4)).isoformat(),
        "rsi_15m": 55.0, "adx_5m": 22.0, "adx_1h": 25.0, "atr_1h": 2.0,
        "jerk_15m": 0.4, "regime": "BULL_TREND",
        "risk": {"initial_stop": 98.0, "initial_risk_px": 2.0, "leverage": 3.0},
        "price": 100.0,
    }
    snap.update(over)
    return snap


class StrategySnapshotClassificationTests(unittest.TestCase):
    def test_complete_trusted_snapshot_is_strategy_observed(self):
        self.assertEqual(obs.classify_strategy_snapshot(_full_strategy_snapshot()),
                         "STRATEGY_OBSERVED")
        status = obs.strategy_evidence_status(_full_strategy_snapshot())
        self.assertTrue(status["complete"])
        self.assertTrue(status["time_verified"])

    def test_missing_fields_downgrade_to_partial(self):
        snap = _full_strategy_snapshot()
        snap.pop("adx_5m")
        self.assertEqual(obs.classify_strategy_snapshot(snap), "STRATEGY_PARTIAL")

    def test_untrusted_source_is_never_observed(self):
        for source in ("calculus_snapshot_fallback", "unavailable", "", None):
            with self.subTest(source=source):
                snap = _full_strategy_snapshot(snapshot_source=source)
                self.assertEqual(obs.classify_strategy_snapshot(snap), "STRATEGY_PARTIAL")
                self.assertFalse(obs.strategy_evidence_status(snap)["source_trusted"])

    def test_missing_strategy_version_is_never_observed(self):
        snap = _full_strategy_snapshot(strategy_version="")
        self.assertEqual(obs.classify_strategy_snapshot(snap), "STRATEGY_PARTIAL")

    def test_future_snapshot_time_is_not_verified(self):
        snap = _full_strategy_snapshot(
            captured_at=(NOW + datetime.timedelta(hours=2)).isoformat(),
            signal_time=(NOW + datetime.timedelta(hours=2)).isoformat())
        status = obs.strategy_evidence_status(snap)
        self.assertFalse(status["time_verified"])
        self.assertIn("未来", " ".join(status["reasons"]))
        self.assertEqual(obs.classify_strategy_snapshot(snap), "STRATEGY_PARTIAL")

    def test_expired_snapshot_time_is_not_verified(self):
        stale = (NOW - datetime.timedelta(hours=9)).isoformat()
        snap = _full_strategy_snapshot(captured_at=stale, signal_time=stale,
                                       open_time=stale)
        status = obs.strategy_evidence_status(snap)
        self.assertFalse(status["time_verified"])
        self.assertIn("过期", " ".join(status["reasons"]))

    def test_malformed_time_is_not_verified(self):
        snap = _full_strategy_snapshot(captured_at="not-a-date")
        self.assertFalse(obs.strategy_evidence_status(snap)["time_verified"])

    def test_captured_before_signal_is_invalid(self):
        snap = _full_strategy_snapshot(
            captured_at=NOW.isoformat(),
            signal_time=(NOW + datetime.timedelta(seconds=30)).isoformat())
        self.assertFalse(obs.strategy_evidence_status(snap)["time_verified"])

    def test_legacy_dynamics_snapshot_keeps_its_old_label(self):
        snap = {k: 1.0 for k in obs.DYNAMICS_FIELDS}
        self.assertEqual(obs.classify_strategy_snapshot(snap), "DYNAMICS_OBSERVED")

    def test_legacy_partial_snapshot_keeps_partial_label(self):
        self.assertEqual(obs.classify_strategy_snapshot({"velocity": 1.0}), "PARTIAL")

    def test_price_only_and_none(self):
        self.assertEqual(obs.classify_strategy_snapshot({"price": 1.0}), "PRICE_ONLY")
        self.assertEqual(obs.classify_strategy_snapshot(None), "NONE")
        self.assertEqual(obs.classify_strategy_snapshot({}), "NONE")

    def test_age_window_matches_the_join_side_constant(self):
        import self_improvement_engine as sie
        self.assertEqual(obs.STRATEGY_MAX_AGE_SECONDS, sie.SNAPSHOT_MAX_STALE_SECONDS)
        self.assertEqual(obs.STRATEGY_MAX_POST_FILL_LAG_SECONDS,
                         sie.SNAPSHOT_MAX_POST_FILL_LAG_SECONDS)


class SnapshotSourceAuditTests(unittest.TestCase):
    def test_source_distribution_counts_every_trade(self):
        trades = ([{"snapshot_source": "direct_signal_journal", "snapshot_time_verified": True}] * 2
                  + [{"snapshot_source": "matched_signal_journal", "snapshot_time_verified": True}]
                  + [{"snapshot_source": "calculus_snapshot_fallback", "snapshot_time_verified": False}]
                  + [{"snapshot_source": "unavailable"}]
                  + [{"snapshot_source": "weird"}])
        audit = obs.audit_snapshot_sources(trades)
        self.assertEqual(audit["direct_signal_journal"], 2)
        self.assertEqual(audit["matched_signal_journal"], 1)
        self.assertEqual(audit["trusted"], 3)
        self.assertEqual(audit["untrusted"], 3)
        self.assertEqual(audit["unknown"], 1)
        self.assertEqual(audit["time_unverified"], 3)
        self.assertEqual(audit["total"], 6)

    def test_observability_audit_includes_strategy_counts(self):
        trades = [{"snapshot_observability": "STRATEGY_OBSERVED"},
                  {"snapshot_observability": "STRATEGY_PARTIAL"},
                  {"snapshot_observability": "STRATEGY_PARTIAL"},
                  {"snapshot_observability": "DYNAMICS_OBSERVED"}]
        audit = obs.audit_snapshot_observability(trades)
        self.assertEqual(audit["strategy_observed"], 1)
        self.assertEqual(audit["strategy_partial"], 2)
        self.assertEqual(audit["strategy_observable"], 3)
        self.assertEqual(audit["dynamics_observed"], 1)
        self.assertEqual(audit["total"], 4)


class ReusedSnapshotTests(unittest.TestCase):
    def _trade(self, inst, open_time, **snap_over):
        snap = {"velocity": 1.0, "acceleration": 0.2, "jerk": 0.3, "rsi_15m": 55.0}
        snap.update(snap_over)
        return {"inst": inst, "open_time": open_time, "entry_snapshot": snap}

    def test_identical_signals_at_different_times_are_flagged(self):
        trades = [self._trade("BTC", "2026-09-20 09:00:00"),
                  self._trade("BTC", "2026-09-20 10:00:00"),
                  self._trade("BTC", "2026-09-20 11:00:00")]
        flagged = obs.detect_reused_snapshots(trades)
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["inst"], "BTC")
        self.assertEqual(flagged[0]["count"], 3)
        self.assertEqual(flagged[0]["reason_code"], "DUPLICATED_SIGNAL_EVIDENCE")

    def test_different_signals_are_not_flagged(self):
        trades = [self._trade("BTC", "2026-09-20 09:00:00"),
                  self._trade("BTC", "2026-09-20 10:00:00", velocity=9.9),
                  self._trade("BTC", "2026-09-20 11:00:00")]
        self.assertEqual(obs.detect_reused_snapshots(trades), [])

    def test_same_instant_split_orders_are_not_reuse_evidence(self):
        trades = [self._trade("BTC", "2026-09-20 09:00:00"),
                  self._trade("BTC", "2026-09-20 09:00:00"),
                  self._trade("BTC", "2026-09-20 09:00:00")]
        self.assertEqual(obs.detect_reused_snapshots(trades), [],
                         "同一时刻拆单属于独立样本组问题，不是复用证据")

    def test_below_threshold_is_not_flagged(self):
        trades = [self._trade("BTC", "2026-09-20 09:00:00"),
                  self._trade("BTC", "2026-09-20 10:00:00")]
        self.assertEqual(obs.detect_reused_snapshots(trades), [])


class LedgerEvidenceLoadingTests(unittest.TestCase):
    """台账装载：来源/时间/策略版本/证据等级逐单带出（Task 2 交付）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        import importlib
        import self_improvement_engine as sie
        importlib.reload(sie)
        self.sie = sie
        self._patch(sie)
        self.trade = {"inst": "BTC", "side": "long", "close_time": "2026-09-20 10:00:00",
                      "open_time": "2026-09-20 09:00:00", "pnl": 1.0, "fee": 0.1,
                      "strategy": "⚡ 趋势", "exit_reason": "止盈"}

    def _patch(self, sie):
        for name, value in (("DATA_DIR", str(self.data)),
                            ("LEDGER_JSON_FILE", str(self.data / "trading_ledger.json"))):
            import unittest.mock as mock
            p = mock.patch.object(sie, name, value)
            p.start()
            self.addCleanup(p.stop)
        import unittest.mock as mock
        p = mock.patch.dict(os.environ, {"R20_EVOLUTION_START_TIME": ""}, clear=False)
        p.start()
        self.addCleanup(p.stop)

    def _ledger(self, *trades):
        (self.data / "trading_ledger.json").write_text(
            json.dumps(list(trades)), encoding="utf-8")

    def test_direct_snapshot_is_labelled_direct(self):
        snap = _full_strategy_snapshot(captured_at=NOW.isoformat(),
                                       signal_time=NOW.isoformat(),
                                       open_time="2026-09-20 09:00:00")
        # 时间窗口内（今天）才可能 STRATEGY_OBSERVED；此处只锁来源与标签
        self._ledger(dict(self.trade, signal_snapshot=snap))
        row = self.sie.load_closed_trades()[0]
        self.assertEqual(row["snapshot_source"], "direct_signal_journal")
        self.assertEqual(row["strategy_version"], "trend_following@1")

    def test_journal_match_is_labelled_matched(self):
        (self.data / "signal_journal.json").write_text(json.dumps([
            {"name": "BTC", "entryTime": "2026-09-20 09:00:00", "side": "long",
             "snapshot": _full_strategy_snapshot()},
        ]), encoding="utf-8")
        self._ledger(self.trade)
        row = self.sie.load_closed_trades()[0]
        self.assertEqual(row["snapshot_source"], "matched_signal_journal")
        self.assertIsNotNone(row["snapshot_time_delta_seconds"])

    def test_calculus_fallback_cannot_upgrade_to_dynamics_observed(self):
        from scripts.evolution.observability import DYNAMICS_FIELDS
        (self.data / "calculus_snapshot.json").write_text(json.dumps(
            {"instruments": [{"name": "BTC", "calculus": {k: 1.0 for k in DYNAMICS_FIELDS}}]}),
            encoding="utf-8")
        self._ledger(self.trade)
        row = self.sie.load_closed_trades()[0]
        self.assertEqual(row["snapshot_source"], "calculus_snapshot_fallback")
        self.assertFalse(row["snapshot_time_verified"])
        self.assertEqual(row["snapshot_observability"], "PRICE_ONLY")
        self.assertIn("SNAPSHOT_TIME_UNVERIFIED", row["snapshot_reason_codes"])

    def test_unavailable_source_when_nothing_matches(self):
        self._ledger(self.trade)
        row = self.sie.load_closed_trades()[0]
        self.assertEqual(row["snapshot_source"], "unavailable")
        self.assertEqual(row["snapshot_observability"], "NONE")
        self.assertIsNone(row["entry_snapshot"])


class AssetMultiplierContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.file = self.data / "asset_multipliers.json"

    def _write(self, payload):
        self.file.write_text(json.dumps(payload), encoding="utf-8")

    def test_missing_file_is_unavailable_with_no_multipliers(self):
        state = load_asset_multiplier_state(str(self.data))
        self.assertEqual(state["multipliers"], {})
        self.assertEqual(state["status"], "UNAVAILABLE")

    def test_corrupt_file_is_invalid_and_uses_no_values(self):
        self.file.write_text("{ broken", encoding="utf-8")
        state = load_asset_multiplier_state(str(self.data))
        self.assertEqual(state["multipliers"], {})
        self.assertIn("ASSET_MULTIPLIER_INVALID", state["reason_codes"])

    def test_expired_ttl_falls_back_to_one(self):
        self._write({"multipliers": {"BTC": 1.4}, "ttl_days": 7,
                     "timestamp": (NOW - datetime.timedelta(days=30)).isoformat(),
                     "source_ledger_revision": "rev-1"})
        state = load_asset_multiplier_state(str(self.data))
        self.assertEqual(state["multipliers"], {})
        self.assertIn("ASSET_MULTIPLIER_EXPIRED", state["reason_codes"])

    def test_expires_at_in_the_past_falls_back_to_one(self):
        self._write({"multipliers": {"BTC": 1.4},
                     "expires_at": (NOW - datetime.timedelta(seconds=60)).isoformat()})
        state = load_asset_multiplier_state(str(self.data))
        self.assertEqual(state["multipliers"], {})
        self.assertIn("ASSET_MULTIPLIER_EXPIRED", state["reason_codes"])

    def test_ledger_revision_change_requires_reconfirmation(self):
        (self.data / "self_improvement_report.json").write_text(
            json.dumps({"ledger_revision": "rev-NEW"}), encoding="utf-8")
        self._write({"multipliers": {"BTC": 1.4}, "source_ledger_revision": "rev-OLD",
                     "expires_at": (NOW + datetime.timedelta(days=1)).isoformat()})
        state = load_asset_multiplier_state(str(self.data))
        self.assertEqual(state["multipliers"], {})
        self.assertIn("ASSET_MULTIPLIER_EXPIRED", state["reason_codes"])

    def test_matching_revision_applies(self):
        (self.data / "self_improvement_report.json").write_text(
            json.dumps({"ledger_revision": "rev-1"}), encoding="utf-8")
        self._write({"multipliers": {"BTC": 1.4}, "source_ledger_revision": "rev-1",
                     "expires_at": (NOW + datetime.timedelta(days=1)).isoformat(),
                     "status": "REVIEWED"})
        state = load_asset_multiplier_state(str(self.data))
        self.assertEqual(state["multipliers"], {"BTC": 1.4})
        self.assertIn("ASSET_MULTIPLIER_APPLIED", state["reason_codes"])

    def test_pending_review_multiplier_does_not_apply(self):
        self._write({"multipliers": {"BTC": 1.4},
                     "expires_at": (NOW + datetime.timedelta(days=1)).isoformat(),
                     "approval": {"required": True, "status": "PENDING_REVIEW"}})
        state = load_asset_multiplier_state(str(self.data))
        self.assertEqual(state["multipliers"], {"BTC": 1.0})
        self.assertEqual(state["status"], "PENDING_REVIEW")

    def test_out_of_range_values_are_dropped_not_clamped(self):
        self._write({"multipliers": {"BTC": 9.0, "ETH": 0.1}, "status": "REVIEWED",
                     "expires_at": (NOW + datetime.timedelta(days=1)).isoformat()})
        state = load_asset_multiplier_state(str(self.data))
        self.assertEqual(state["multipliers"], {})
        self.assertEqual(len(state["rejected"]), 2)

    def test_legacy_file_without_provenance_still_applies(self):
        self._write({"multipliers": {"BTC": 1.2}})
        state = load_asset_multiplier_state(str(self.data))
        self.assertEqual(state["multipliers"], {"BTC": 1.2})
        self.assertEqual(state["status"], "LEGACY_UNVERIFIED")


class RVsAtrSemanticsTests(unittest.TestCase):
    def test_documented_metrics_mark_each_number_kind(self):
        from scripts import strategy_rules as sr
        kinds = {k: v["kind"] for k, v in sr.DOCUMENTED_METRICS.items()}
        self.assertEqual(kinds["breakeven"], "CURRENT_CONFIG")
        self.assertEqual(kinds["rsi_extreme"], "TARGET_STRATEGY_PARAMETER")
        self.assertEqual(kinds["asset_multiplier"], "CURRENT_CONFIG")

    def test_breakeven_uses_atr_convention_in_current_ruleset(self):
        from scripts import strategy_rules as sr
        rules = sr.RULESETS["trend_following@1"]
        self.assertEqual(rules["breakeven_mode"], "ATR_MULTIPLE")
        self.assertEqual(rules["breakeven_trigger_atr"], 0.8)
        # 0.8 × ATR 与 0.8R（R=ATR×2）不是同一个价 —— 两者都记录，但只生效一个。
        self.assertAlmostEqual(
            sr.breakeven_trigger_px_atr(is_long=True, entry_px=100.0, atr=2.0, atr_mult=0.8),
            101.6)
        self.assertAlmostEqual(
            sr.breakeven_trigger_px(is_long=True, entry_px=100.0, initial_stop_px=96.0,
                                    trigger_r=0.8), 103.2)


if __name__ == "__main__":
    unittest.main()
