"""自进化 ↔ 交易链路集成（规划文档 Task 8 / §16 完成定义）。

覆盖三件端到端的事：

1. **台账证据章**（`scripts/trader/evidence_stamp.py`）：落账前补齐策略/记忆/风险/证据版本，
   幂等、绝不覆盖已有键、绝不抛异常；
2. **tracker 证据回填**（规划文档 §8.2 开仓侧）：新建 tracker 带初始止损/初始风险/保本口径；
3. **影子规则报告**（`scripts/backtest/evidence.py`，阶段 C）：只计算不拦截，
   覆盖趋势与震荡环境，并给出被拒样本的实际结果。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from scripts.backtest.evidence import (  # noqa: E402
    compute_extended_metrics, data_completeness, shadow_rule_report)
from scripts.trader.evidence_stamp import (  # noqa: E402
    exit_reason_code, stamp_trade_evidence, tracker_evidence_fields)


class ExitReasonCodeTests(unittest.TestCase):
    def test_known_reasons_map_to_stable_codes(self):
        for reason, code in (("🛑 触发云端止损", "STOP_LOSS"), ("🎯 目标止盈达成", "TAKE_PROFIT"),
                             ("保护失效退出", "PROTECTION_FAILED"),
                             ("浮盈保本移损", "BREAKEVEN"),
                             ("时间止损离场", "TIME_STOP"),
                             ("移动止盈跟踪", "TRAILING_TAKE_PROFIT"),
                             ("分批止盈 50%", "SCALE_OUT"), ("手动平仓", "MANUAL")):
            with self.subTest(reason=reason):
                self.assertEqual(exit_reason_code(reason), code)

    def test_unknown_text_is_not_guessed(self):
        self.assertEqual(exit_reason_code("某种未知原因"), "UNKNOWN")
        self.assertEqual(exit_reason_code(""), "")


class TrackerEvidenceTests(unittest.TestCase):
    def test_tracker_fields_are_derived_from_the_entry(self):
        entry = {"entryPx": 100.0, "trailingStopPx": 96.0, "side": "long",
                 "signal_snapshot": {"snapshot_source": "direct_signal_journal",
                                     "snapshot_observability": "STRATEGY_OBSERVED"}}
        fields = tracker_evidence_fields({"risk_budget_snapshot": {"asset_multiplier": 1.1}}, {}, entry)
        self.assertEqual(fields["initial_stop_px"], 96.0)
        self.assertEqual(fields["initial_risk_px"], 4.0)
        self.assertEqual(fields["breakeven_mode"], "ATR_MULTIPLE")
        self.assertEqual(fields["asset_multiplier"], 1.1)
        self.assertEqual(fields["snapshot_source"], "direct_signal_journal")

    def test_missing_entry_price_is_tolerated(self):
        self.assertEqual(tracker_evidence_fields({}, {}, {}), {})


class StampTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)

    def _trade(self, **over):
        trade = {"inst": "BTC", "direction": "平多", "pnl": 12.5, "gross_pnl": 14.0,
                 "fee": -1.5, "exit_reason": "🎯 目标止盈达成", "venue": "okx"}
        trade.update(over)
        return trade

    def _trackers(self):
        return {"BTC-USDT-SWAP_long": {
            "instId": "BTC-USDT-SWAP", "side": "long", "entryPx": 100.0, "trailingStopPx": 96.0,
            # 平仓侧 §8.2：策略棘轮止损与场所侧紧急保护止损
            "emergency_stop_px": 94.5,
            "initial_stop_px": 96.0, "initial_risk_px": 4.0, "breakeven_mode": "ATR_MULTIPLE",
            "breakeven_trigger_atr": 0.8, "strategy_rule_version": "trend_following@1",
            "rule_set_hash": "abc", "policy_hash": "deadbeef",
            "asset_multiplier": 1.1, "asset_multiplier_status": "REVIEWED",
            "risk_budget_snapshot": {"leverage": 3.0, "initial_risk_px": 4.0},
            "entry_snapshot": {"price": 100.0}, "snapshot_source": "direct_signal_journal",
            "snapshot_observability": "STRATEGY_OBSERVED", "signal_id": "sig-1"}}

    def test_stamp_fills_strategy_memory_and_risk_evidence(self):
        trade = stamp_trade_evidence(self._trade(), data_dir=str(self.data),
                                     trackers=self._trackers())
        for key in ("strategy_mode", "strategy_rule_version", "rule_set_hash", "policy_hash",
                    "memory_revision", "baseline_hash", "initial_stop_px", "initial_risk_px",
                    "breakeven_mode", "asset_multiplier", "entry_snapshot", "snapshot_source",
                    "snapshot_observability", "signal_id", "risk_budget_snapshot",
                    "strategy_stop_px", "emergency_stop_px"):
            self.assertIn(key, trade, f"台账缺证据字段: {key}")
        self.assertEqual(trade["strategy_stop_px"], 96.0)
        self.assertEqual(trade["emergency_stop_px"], 94.5)
        self.assertEqual(trade["initial_risk_px"], 4.0)
        self.assertEqual(trade["strategy_rule_version"], "trend_following@1")

    def test_stamp_fills_exit_side_fields(self):
        trade = stamp_trade_evidence(self._trade(), data_dir=str(self.data))
        self.assertEqual(trade["exit_reason_code"], "TAKE_PROFIT")
        self.assertEqual(trade["pnl_gross"], 14.0)
        self.assertEqual(trade["fees"], 1.5)
        self.assertEqual(trade["funding"], 0.0)
        self.assertIsNone(trade["slippage_estimate"])
        self.assertIsNone(trade["snapshot_at_exit"])
        self.assertFalse(trade["cooldown_recorded"])

    def test_cooldown_is_recorded_for_a_stop_loss(self):
        (self.data / ".stop_cooldown.json").write_text(json.dumps({"BTC|long": 1}), encoding="utf-8")
        trade = stamp_trade_evidence(self._trade(exit_reason="🛑 触发云端止损"),
                                     data_dir=str(self.data))
        self.assertTrue(trade["cooldown_recorded"])
        self.assertEqual(trade["exit_reason_code"], "STOP_LOSS")

    def test_stamp_is_idempotent_and_never_overwrites(self):
        trade = self._trade(exit_reason_code="MANUAL", funding=-0.5)
        stamp_trade_evidence(trade, data_dir=str(self.data), trackers=self._trackers())
        first = json.dumps(trade, sort_keys=True, ensure_ascii=False)
        stamp_trade_evidence(trade, data_dir=str(self.data), trackers=self._trackers())
        self.assertEqual(json.dumps(trade, sort_keys=True, ensure_ascii=False), first)
        self.assertEqual(trade["exit_reason_code"], "MANUAL")
        self.assertEqual(trade["funding"], -0.5)

    def test_stamp_swallows_everything(self):
        trade = {"inst": "BTC", "pnl": float("nan")}
        with patch("json.load", side_effect=RuntimeError("boom")):
            out = stamp_trade_evidence(trade, data_dir=str(self.data))
        self.assertIs(out, trade)

    def test_no_tracker_still_stamps_policy_evidence(self):
        trade = stamp_trade_evidence(self._trade(), data_dir=str(self.data), trackers={})
        self.assertIn("strategy_mode", trade)
        self.assertNotIn("initial_stop_px", trade)


class ShadowRuleReportTests(unittest.TestCase):
    def _trades(self):
        return [
            {"inst": "BTC", "direction": "BUY_LONG", "rsi": 80.0, "jerk": 0.1,
             "regime": "BULL_TREND", "pnl": -12.0, "strategy_mode": "trend_confirm_5m"},
            {"inst": "ETH", "direction": "BUY_LONG", "rsi": 55.0, "jerk": -3.0,
             "regime": "BULL_TREND", "pnl": -8.0, "strategy_mode": "trend_confirm_5m"},
            {"inst": "SOL", "direction": "BUY_LONG", "rsi": 55.0, "jerk": 0.2,
             "regime": "RANGE_CHOP", "pnl": 9.0, "strategy_mode": "trend_confirm_5m"},
            {"inst": "DOGE", "direction": "SELL_SHORT", "rsi": 20.0, "jerk": -0.2,
             "regime": "RANGE_CHOP", "pnl": -3.0, "strategy_mode": "trend_confirm_5m"},
        ]

    def test_shadow_report_counts_triggers_and_outcomes(self):
        report = shadow_rule_report(self._trades(), strategy_mode="trend_confirm_5m")
        self.assertEqual(report["would_reject"], 3)
        outcomes = report["rejected_then_outcomes"]
        self.assertEqual(outcomes["count"], 3)
        self.assertEqual(outcomes["would_be_net_pnl"], -23.0)
        self.assertEqual(outcomes["would_be_wins"], 0)
        self.assertTrue(report["rule_triggers"])

    def test_shadow_report_covers_trend_and_range(self):
        report = shadow_rule_report(self._trades(), strategy_mode="trend_confirm_5m")
        self.assertIn("BULL_TREND", report["by_regime"])
        self.assertIn("RANGE_CHOP", report["by_regime"])
        self.assertTrue(report["covers_trend_and_range"])
        self.assertEqual(report["by_regime"]["RANGE_CHOP"]["would_reject"], 1)

    def test_shadow_report_does_not_change_results(self):
        trades = self._trades()
        before = json.dumps(trades, sort_keys=True)
        shadow_rule_report(trades)
        self.assertEqual(json.dumps(trades, sort_keys=True), before, "影子报告不得改写交易")

    def test_legacy_mode_rejects_nothing(self):
        report = shadow_rule_report(self._trades(), strategy_mode="legacy")
        self.assertEqual(report["would_reject"], 0)
        self.assertEqual(report["rule_set"], "legacy@1")


class ExtendedMetricsTests(unittest.TestCase):
    def _trades(self):
        return [
            {"pnl": 10.0, "gross_pnl": 11.0, "fee": 1.0, "funding": 0.1,
             "slippage_estimate": 0.05, "r_multiple": 2.0, "regime": "BULL_TREND",
             "strategy_mode": "trend_confirm_5m", "venue": "okx",
             "entry_time": "t1", "exit_time": "t2", "direction": "long",
             "entry_price": 1.0, "exit_price": 1.1},
            {"pnl": -5.0, "gross_pnl": -4.0, "fee": 1.0, "funding": -0.1,
             "slippage_estimate": 0.05, "r_multiple": -1.0, "regime": "RANGE_CHOP",
             "strategy_mode": "trend_confirm_5m", "venue": "binance",
             "entry_time": "t3", "exit_time": "t4", "direction": "short",
             "entry_price": 2.0, "exit_price": 2.1},
            {"pnl": -3.0, "gross_pnl": -2.0, "fee": 1.0, "r_multiple": -0.5,
             "regime": "RANGE_CHOP", "strategy_mode": "legacy", "venue": "okx",
             "entry_time": "t5", "exit_time": "t6", "direction": "long",
             "entry_price": 3.0, "exit_price": 2.9},
        ]

    def test_costs_and_profit_factor(self):
        m = compute_extended_metrics(self._trades())
        self.assertEqual(m["gross_pnl"], 5.0)
        self.assertEqual(m["net_pnl"], 2.0)
        self.assertEqual(m["fees"], 3.0)
        self.assertEqual(m["profit_factor"], 1.25)  # 10 / (5+3)（按净盈亏口径）
        self.assertEqual(m["total_trades"], 3)

    def test_r_statistics_and_consecutive_losses(self):
        m = compute_extended_metrics(self._trades())
        self.assertEqual(m["avg_r"], 0.1667)
        self.assertEqual(m["median_r"], -0.5)
        self.assertEqual(m["max_consecutive_losses"], 2)
        self.assertEqual(m["counterexamples"], 2)

    def test_grouping_by_regime_mode_and_venue(self):
        m = compute_extended_metrics(self._trades())
        self.assertEqual(sorted(m["by_regime"]), ["BULL_TREND", "RANGE_CHOP"])
        self.assertEqual(sorted(m["by_strategy_mode"]), ["legacy", "trend_confirm_5m"])
        self.assertEqual(sorted(m["by_venue"]), ["binance", "okx"])
        self.assertEqual(m["by_venue"]["okx"]["trades"], 2)

    def test_rule_triggers_and_rejected_outcomes_are_passed_through(self):
        m = compute_extended_metrics(
            self._trades(), rule_triggers={"RSI 极值追多拦截": 2},
            rejected_outcomes=[{"pnl": -7.0}])
        self.assertEqual(m["rule_triggers"], {"RSI 极值追多拦截": 2})
        self.assertEqual(m["rejected_then_outcomes"]["would_be_net_pnl"], -7.0)
        self.assertEqual(m["rejected_then_outcomes"]["would_be_wins"], 0)

    def test_max_drawdown_is_computed_on_the_equity_curve(self):
        trades = [{"pnl": -100.0}, {"pnl": 50.0}, {"pnl": -25.0}]
        m = compute_extended_metrics(trades, initial_capital=1000.0)
        self.assertAlmostEqual(m["max_drawdown_pct"], 10.0, places=4)  # 峰值 1000 → 谷 900

    def test_incomplete_data_is_marked_unverifiable(self):
        trades = [{"pnl": 1.0}, {"pnl": 2.0, "fee": 0.1, "entry_time": "t",
                                 "exit_time": "t", "direction": "long",
                                 "entry_price": 1.0, "exit_price": 1.1}]
        audit = data_completeness(trades)
        self.assertFalse(audit["complete"])
        self.assertIn("fee", audit["missing_fields"])
        self.assertEqual(audit["unverifiable_trades"], [0])
        self.assertEqual(compute_extended_metrics(trades)["data_completeness"]["complete"], False)

    def test_empty_input_is_safe(self):
        m = compute_extended_metrics([])
        self.assertEqual(m["total_trades"], 0)
        self.assertEqual(m["profit_factor"], 0.0)
        self.assertTrue(m["data_completeness"]["complete"])


if __name__ == "__main__":
    unittest.main()


class BacktestShadowIntegrationTests(unittest.TestCase):
    """回测引擎 → 影子规则（规划文档 §12.4 / Task 8）：逐笔证据 + 规则触发统计。"""

    @staticmethod
    def _candles():
        rows = []
        for i in range(25):
            close = 100.0 if i <= 15 else 100.0 + (i - 15) * 2.0
            rows.append({"symbol": "BTC-USDT-SWAP", "timestamp": f"t{i:02d}",
                         "ts_ms": i * 3600000, "open": close, "high": close + 1.0,
                         "low": close - 1.0, "close": close, "volume": 10.0})
        return rows

    def _summary(self):
        import scripts.backtest_engine as be
        engine = be.BacktestEngine(initial_capital=1000.0, strategy_mode="trend_confirm_5m")
        signals = [{"timestamp": "t15", "action": "BUY", "confidence": 0.9, "rr": 2.5,
                    "atr": 1.0, "rsi": 80.0, "jerk": 0.1, "regime": "BULL_TREND",
                    "strategy_mode": "trend_confirm_5m"}]
        return engine.run(self._candles(), signals=signals)

    def test_engine_records_shadow_inputs_and_cost_breakdown(self):
        summary = self._summary()
        self.assertTrue(summary.all_trades, "无成交 ⇒ 影子统计无从谈起")
        trade = summary.all_trades[0]
        self.assertEqual(trade["rsi_15m"], 80.0)
        self.assertEqual(trade["jerk_15m"], 0.1)
        self.assertEqual(trade["regime"], "BULL_TREND")
        self.assertEqual(trade["strategy_mode"], "trend_confirm_5m")
        self.assertGreater(trade["fee_usd"], 0.0)
        self.assertGreater(trade["slippage_usd"], 0.0)
        self.assertEqual(trade["funding_usd"], 0.0, "回测不建模资金费：结构性 0")

    def test_shadow_rules_flag_the_extreme_chase_trade(self):
        report = shadow_rule_report(self._summary().all_trades,
                                    strategy_mode="trend_confirm_5m")
        self.assertEqual(report["would_reject"], 1)
        self.assertTrue(any("极值追多" in reason for reason in report["rule_triggers"]))
        self.assertEqual(report["rejected_then_outcomes"]["count"], 1)

    def test_extended_metrics_cover_the_engine_trades(self):
        metrics = compute_extended_metrics(self._summary().all_trades, initial_capital=1000.0)
        self.assertEqual(metrics["total_trades"], 1)
        self.assertGreater(metrics["fees"], 0.0)
        self.assertIn("BULL_TREND", metrics["by_regime"])
        self.assertEqual(metrics["r_samples"], 1)


if __name__ == "__main__":
    unittest.main()


class StopRiskSizingTests(unittest.TestCase):
    """规划文档 §5.4：`sizing.py 使用最终止损距离计算风险数量`（只减不增）。"""

    def test_a_too_wide_stop_shrinks_the_size(self):
        from scripts.trader.sizing import cap_size_by_stop_risk
        size, note = cap_size_by_stop_risk(size=10.0, entry_px=100.0, stop_px=90.0,
                                           ct_val=1.0, risk_budget_usd=20.0)
        self.assertEqual(size, 2.0)          # 20U / (10 × 1)
        self.assertIn("收紧", note)

    def test_a_safe_size_is_untouched(self):
        from scripts.trader.sizing import cap_size_by_stop_risk
        size, note = cap_size_by_stop_risk(size=1.0, entry_px=100.0, stop_px=98.0,
                                           ct_val=1.0, risk_budget_usd=15.0)
        self.assertEqual((size, note), (1.0, ""))

    def test_size_below_exchange_minimum_is_refused(self):
        from scripts.trader.sizing import cap_size_by_stop_risk
        size, note = cap_size_by_stop_risk(size=10.0, entry_px=100.0, stop_px=99.0,
                                           ct_val=1.0, risk_budget_usd=0.5, min_sz=1.0)
        self.assertEqual(size, 0.0)
        self.assertIn("最小下单量", note)

    def test_quantisation_never_rounds_up(self):
        from scripts.trader.sizing import cap_size_by_stop_risk
        size, _ = cap_size_by_stop_risk(size=100.0, entry_px=100.0, stop_px=99.0,
                                        ct_val=1.0, risk_budget_usd=5.9, min_sz=1.0)
        self.assertEqual(size, 5.0)

    def test_missing_inputs_leave_the_size_alone(self):
        from scripts.trader.sizing import cap_size_by_stop_risk
        for kwargs in ({"stop_px": 100.0}, {"ct_val": 0.0}, {"risk_budget_usd": 0.0},
                       {"size": 0.0}, {"entry_px": "x"}):
            base = {"size": 5.0, "entry_px": 100.0, "stop_px": 98.0, "ct_val": 1.0,
                    "risk_budget_usd": 15.0}
            base.update(kwargs)
            with self.subTest(**kwargs):
                self.assertEqual(cap_size_by_stop_risk(**base)[0], base["size"])

    def test_stop_risk_limits_read_the_pool(self):
        from scripts.trader.sizing import stop_risk_limits
        limits = stop_risk_limits("BTC-USDT-SWAP", usdt_available=1000.0)
        self.assertGreater(limits["risk_budget_usd"], 0)
        self.assertGreater(limits["ct_val"], 0)
        self.assertEqual(stop_risk_limits("NOPE-USDT-SWAP"), {})


class SubmitShellRiskCapTests(unittest.TestCase):
    """下单壳：最终止损距离的风险张数闸门（venue_ctx 驱动，缺数据不改旧行为）。"""

    def setUp(self):
        import scripts.ai_factor_trader as aft
        self.aft = aft
        self.sent: list = []
        self._patch("_order_submit_protected",
                    lambda *a, **k: (self.sent.append(a), (True, "ok"))[1])
        self._patch("verify_order_intent_now", lambda ctx: (True, ""))

    def _patch(self, name, value):
        from unittest.mock import patch as _p
        patcher = _p(f"scripts.ai_factor_trader.{name}", value, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _submit(self, *, size, stop_px, ctx):
        with patch("scripts.strategy_rules.verify_order_intent_now", lambda c: (True, "")):
            return self.aft.submit_protected_limit_order(
                "BTC-USDT-SWAP", "buy", "long", size, 100.0, 110.0, stop_px, venue_ctx=ctx)

    def test_a_wide_stop_is_capped_before_the_order_leaves(self):
        ok, note = self._submit(size=10.0, stop_px=90.0,
                                ctx={"ct_val": 1.0, "risk_budget_usd": 20.0, "min_sz": 0.1})
        self.assertTrue(ok, note)
        self.assertEqual(self.sent[0][3], 2.0, "发单张数未按风险预算收紧")

    def test_a_cap_below_the_exchange_minimum_refuses_to_send(self):
        ok, note = self._submit(size=10.0, stop_px=99.0,
                                ctx={"ct_val": 1.0, "risk_budget_usd": 0.5, "min_sz": 1.0})
        self.assertFalse(ok)
        self.assertIn("风险预算", note)
        self.assertEqual(self.sent, [], "被拒的单不得发出")

    def test_without_risk_data_the_size_is_unchanged(self):
        ok, _ = self._submit(size=7.0, stop_px=50.0, ctx={"margin_usdt": 100.0})
        self.assertTrue(ok)
        self.assertEqual(self.sent[0][3], 7.0)
