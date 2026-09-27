"""策略硬规则门禁回归（规划文档 §12.3 逐条）。

这些断言是**风险行为**的钉子：RSI/Jerk 门禁必须按 setup_kind 生效、
边界值必须精确、均值回归不得被趋势追随门禁误伤、未知模式必须 fail-closed。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from scripts import strategy_rules as sr  # noqa: E402


def _gate(action, *, mode="trend_confirm_5m", rsi=None, jerk=0.0, explicit=None):
    return sr.evaluate_strategy_hard_rules(
        action=action, strategy_mode=mode, rsi=rsi, jerk=jerk, setup_kind=explicit)


class RsiExtremeGateTests(unittest.TestCase):
    def test_trend_long_at_ceiling_passes(self):
        ok, reason, kind = _gate("BUY_LONG", rsi=75.0)
        self.assertTrue(ok, reason)
        self.assertEqual(kind, "trend_following_long")

    def test_trend_long_above_ceiling_is_rejected(self):
        ok, reason, _ = _gate("BUY_LONG", rsi=75.01)
        self.assertFalse(ok)
        self.assertIn("极值追多", reason)

    def test_trend_short_at_floor_passes(self):
        ok, reason, kind = _gate("SELL_SHORT", rsi=28.0)
        self.assertTrue(ok, reason)
        self.assertEqual(kind, "trend_following_short")

    def test_trend_short_below_floor_is_rejected(self):
        ok, reason, _ = _gate("SELL_SHORT", rsi=27.99)
        self.assertFalse(ok)
        self.assertIn("极值追空", reason)

    def test_missing_rsi_is_fail_closed_for_trend(self):
        ok, reason, _ = _gate("BUY_LONG", rsi=None)
        self.assertFalse(ok)
        self.assertIn("RSI 缺失", reason)

    def test_mean_reversion_long_is_not_hurt_by_the_trend_gate(self):
        ok, reason, kind = _gate("BUY_LONG", mode="mean_reversion", rsi=25.0)
        self.assertTrue(ok, reason)
        self.assertEqual(kind, "mean_reversion_long")

    def test_mean_reversion_short_is_not_hurt_either(self):
        ok, reason, kind = _gate("SELL_SHORT", mode="mean_reversion", rsi=85.0)
        self.assertTrue(ok, reason)
        self.assertEqual(kind, "mean_reversion_short")


class ReverseJerkGateTests(unittest.TestCase):
    def test_missing_jerk_is_rejected(self):
        ok, reason, _ = _gate("BUY_LONG", rsi=50.0, jerk=None)
        self.assertFalse(ok)
        self.assertIn("Jerk 缺失", reason)

    def test_reverse_jerk_at_threshold_is_rejected(self):
        ok, reason, _ = _gate("BUY_LONG", rsi=50.0, jerk=-2.5)
        self.assertFalse(ok)
        self.assertIn("反向动能冲击", reason)

    def test_reverse_jerk_just_below_threshold_passes(self):
        self.assertTrue(_gate("BUY_LONG", rsi=50.0, jerk=-2.49)[0])

    def test_short_reverse_jerk_is_rejected(self):
        self.assertFalse(_gate("SELL_SHORT", rsi=50.0, jerk=2.5)[0])

    def test_same_direction_jerk_passes(self):
        self.assertTrue(_gate("BUY_LONG", rsi=50.0, jerk=3.0)[0])
        self.assertTrue(_gate("SELL_SHORT", rsi=50.0, jerk=-3.0)[0])

    def test_mean_reversion_ignores_missing_jerk(self):
        self.assertTrue(_gate("BUY_LONG", mode="mean_reversion", rsi=20.0, jerk=None)[0])

    def test_legacy_mode_keeps_old_behaviour(self):
        # §5.4：旧 legacy 模式继续走旧规则 —— 门禁不生效，RSI/Jerk 都不拦。
        for action in ("BUY_LONG", "SELL_SHORT"):
            for rsi, jerk in ((99.0, None), (1.0, -9.0)):
                with self.subTest(action=action, rsi=rsi, jerk=jerk):
                    ok, reason, kind = _gate(action, mode="legacy", rsi=rsi, jerk=jerk)
                    self.assertTrue(ok, reason)
                    self.assertEqual(kind, "unknown")


class SetupKindTests(unittest.TestCase):
    def test_unknown_strategy_mode_is_rejected(self):
        ok, reason, kind = _gate("BUY_LONG", mode="brand_new_mode", rsi=50.0, jerk=0.0)
        self.assertFalse(ok)
        self.assertEqual(kind, "unknown")
        self.assertIn("未知", reason)

    def test_explicit_setup_kind_cannot_bypass_the_family(self):
        # trend_confirm_5m 下自称 mean_reversion ⇒ unknown ⇒ fail-closed
        ok, reason, kind = _gate("BUY_LONG", rsi=99.0, jerk=0.0,
                                 explicit="mean_reversion_long")
        self.assertFalse(ok)
        self.assertEqual(kind, "unknown")

    def test_explicit_setup_kind_confirming_the_family_is_allowed(self):
        ok, _, kind = _gate("BUY_LONG", rsi=50.0, jerk=0.0,
                            explicit="trend_following_long")
        self.assertTrue(ok)
        self.assertEqual(kind, "trend_following_long")

    def test_unknown_setup_kind_is_rejected(self):
        ok, reason, kind = sr.evaluate_strategy_hard_rules(
            action="BUY_LONG", strategy_mode="trend_confirm_5m", rsi=50.0, jerk=0.0,
            setup_kind="unknown")
        self.assertFalse(ok)
        self.assertEqual(kind, "unknown")

    def test_no_action_yields_unknown(self):
        self.assertEqual(sr.setup_kind_for("trend_confirm_5m", "HOLD"), "unknown")


class ExecutionPolicyTests(unittest.TestCase):
    def test_missing_field_means_legacy(self):
        policy = sr.resolve_execution_policy({"name": "旧档案"})
        self.assertEqual(policy["mode"], "legacy")
        self.assertEqual(policy["rule_set"], "legacy@1")

    def test_none_profile_means_legacy(self):
        self.assertEqual(sr.resolve_execution_policy(None)["mode"], "legacy")

    def test_explicit_policy_round_trips(self):
        policy = sr.resolve_execution_policy(
            {"execution_policy": {"mode": "trend_confirm_5m", "revision": 1,
                                  "rule_set": "trend_following@1"}})
        self.assertEqual(policy, {"mode": "trend_confirm_5m", "revision": 1,
                                  "rule_set": "trend_following@1"})

    def test_unknown_mode_is_refused(self):
        with self.assertRaises(sr.StrategyRuleError):
            sr.resolve_execution_policy({"execution_policy": {"mode": "yolo"}})

    def test_unknown_rule_set_is_refused(self):
        with self.assertRaises(sr.StrategyRuleError):
            sr.resolve_execution_policy(
                {"execution_policy": {"mode": "trend_confirm_5m", "rule_set": "nope@9"}})

    def test_rule_reading_failure_is_fail_closed_at_gate_level(self):
        # §5.4：任何策略规则读取失败时拒绝新开仓。
        from unittest.mock import patch
        with patch.object(sr, "RULESETS", {}):
            with self.assertRaises(sr.StrategyRuleError):
                sr.rule_set_hash("trend_following@1")
            self.assertFalse(sr.reject_extreme_chase(
                action="BUY_LONG", rsi=10.0, setup_kind="trend_following_long",
                rules={"gates_enabled": True, "rsi_long_chase_ceiling": 75.0}) is None)

    def test_rule_set_hash_is_stable_and_version_changes_it(self):
        self.assertEqual(sr.rule_set_hash("trend_following@1"),
                         sr.rule_set_hash("trend_following@1"))
        self.assertNotEqual(sr.rule_set_hash("trend_following@1"),
                            sr.rule_set_hash("legacy@1"))
        self.assertEqual(sr.strategy_rule_version("trend_following@1"), "trend_following@1")


class BreakevenSemanticsTests(unittest.TestCase):
    def test_r_and_atr_conventions_are_distinct(self):
        entry, stop, atr = 100.0, 90.0, 2.0
        r_level = sr.breakeven_trigger_px(is_long=True, entry_px=entry,
                                          initial_stop_px=stop, trigger_r=0.8)
        atr_level = sr.breakeven_trigger_px_atr(is_long=True, entry_px=entry, atr=atr,
                                                atr_mult=0.8)
        self.assertAlmostEqual(r_level, 108.0)   # 0.8 × 10 = 8
        self.assertAlmostEqual(atr_level, 101.6)  # 0.8 × 2 = 1.6
        self.assertNotEqual(r_level, atr_level, "R 与 ATR 口径绝不能算出同一个价")

    def test_short_side_levels_are_below_entry(self):
        self.assertAlmostEqual(
            sr.breakeven_trigger_px(is_long=False, entry_px=100.0,
                                    initial_stop_px=110.0, trigger_r=1.0), 90.0)
        self.assertAlmostEqual(
            sr.breakeven_trigger_px_atr(is_long=False, entry_px=100.0, atr=2.0,
                                        atr_mult=1.0), 98.0)

    def test_current_ruleset_freezes_the_atr_convention(self):
        rules = sr.RULESETS["trend_following@1"]
        self.assertEqual(rules["breakeven_mode"], "ATR_MULTIPLE")
        self.assertEqual(rules["breakeven_trigger_atr"], 0.8)
        self.assertEqual(sr.DOCUMENTED_METRICS["breakeven"]["kind"], "CURRENT_CONFIG")


if __name__ == "__main__":
    unittest.main()


class CorePipelineGateTests(unittest.TestCase):
    """核心拦截器管线必须真的执行策略硬规则（规划文档 §5.3 第 3 步 / §5.4）。

    开仓扫描只接受大脑缓存里 `action in {BUY_LONG, SELL_SHORT}` 的标的，
    而被拦截的决策在装配缓存时就被替换成 WAIT —— 因此**这条管线就是入口硬门禁**。
    """

    def _package(self, **over):
        pkg = {"instId": "BTC-USDT-SWAP", "name": "BTC", "data_quality": "valid",
               "rsi_15m": 55.0, "jerk_15m": 0.2, "calculus": {"timeframes": {}}}
        pkg.update(over)
        return pkg

    def _decision(self, **over):
        dec = {"action": "BUY_LONG", "confidence": 88.0, "entry_price": 100.0,
               "take_profit_price": 106.0, "stop_loss_price": 97.0}
        dec.update(over)
        return dec

    def _run(self, package, decision, context):
        """只测**核心闸门**：把用户插件管线置空，避免插件（宏观/ADX/VWAP 等）
        的拒绝掩盖策略规则本身的判定。"""
        from unittest.mock import patch as _patch
        from r20_backend.interceptor_manager import run_interceptor_pipeline
        with _patch("r20_backend.interceptor_manager.list_plugins", return_value=[]):
            return run_interceptor_pipeline(package, decision, context)

    def test_extreme_rsi_is_blocked_in_trend_mode(self):
        action, reason, _rr = self._run(
            self._package(rsi_15m=80.0), self._decision(),
            {"active_inst_ids": set(), "active_position_sides": {},
             "strategy_mode": "trend_confirm_5m"})
        self.assertEqual(action, "WAIT")
        self.assertIn("策略硬规则", reason)
        self.assertIn("极值追多", reason)

    def test_reverse_jerk_is_blocked_in_trend_mode(self):
        action, reason, _rr = self._run(
            self._package(jerk_15m=-3.0), self._decision(),
            {"active_inst_ids": set(), "active_position_sides": {},
             "strategy_mode": "trend_confirm_5m"})
        self.assertEqual(action, "WAIT")
        self.assertIn("反向动能冲击", reason)

    def test_unknown_strategy_mode_is_fail_closed(self):
        action, reason, _rr = self._run(
            self._package(), self._decision(),
            {"active_inst_ids": set(), "active_position_sides": {},
             "strategy_mode": "brand_new_mode"})
        self.assertEqual(action, "WAIT")
        self.assertIn("未知策略模式", reason)

    def test_legacy_mode_keeps_old_behaviour(self):
        action, reason, rr = self._run(
            self._package(rsi_15m=99.0, jerk_15m=-9.0), self._decision(),
            {"active_inst_ids": set(), "active_position_sides": {}})
        self.assertEqual(action, "BUY_LONG", reason)
        self.assertGreater(rr, 0)

    def test_mean_reversion_setup_is_not_hurt_by_the_trend_gate(self):
        action, reason, _rr = self._run(
            self._package(rsi_15m=99.0), self._decision(),
            {"active_inst_ids": set(), "active_position_sides": {},
             "strategy_mode": "mean_reversion"})
        self.assertEqual(action, "BUY_LONG", reason)

    def test_correlation_group_count_is_enforced(self):
        """规划文档 §5.7 第一步：同相关组同向持仓笔数超限即拒。"""
        action, reason, _rr = self._run(
            self._package(), self._decision(),
            {"active_inst_ids": set(),
             "active_position_sides": {"BTC-USDT-SWAP": "long", "ETH-USDT-SWAP": "long"}})
        self.assertEqual(action, "WAIT")
        self.assertIn("相关组同向敞口", reason)

    def test_a_different_group_is_not_penalised(self):
        action, reason, _rr = self._run(
            self._package(instId="SOL-USDT-SWAP", name="SOL"), self._decision(),
            {"active_inst_ids": set(),
             "active_position_sides": {"BTC-USDT-SWAP": "long", "ETH-USDT-SWAP": "long"}})
        self.assertEqual(action, "BUY_LONG", reason)


if __name__ == "__main__":
    unittest.main()
