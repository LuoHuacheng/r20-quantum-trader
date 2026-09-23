"""Offline isolated unit tests for core safety floor, interceptor pipeline and final quote verification.
Strictly local, temporary mocked directory, zero network, zero real exchange calls.
"""
from __future__ import annotations

import copy
import math
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.order_risk import validate_quote_geometry_and_rr
import r20_backend.interceptor_manager as im


class CoreRiskAndInterceptorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.mock_root = Path(self.temp_dir.name)
        self.mock_plugins = self.mock_root / "plugins" / "interceptors"
        self.mock_plugins.mkdir(parents=True, exist_ok=True)
        self.mock_config = self.mock_root / "data" / "interceptor_plugins.json"

        # Patch paths in interceptor_manager
        self.patch_plugins_dir = patch.object(im, "PLUGINS_DIR", self.mock_plugins)
        self.patch_config_file = patch.object(im, "CONFIG_FILE", self.mock_config)
        self.patch_plugins_dir.start()
        self.patch_config_file.start()
        self.addCleanup(self.patch_plugins_dir.stop)
        self.addCleanup(self.patch_config_file.stop)

    def test_quote_geometry_and_rr_valid(self):
        # Long valid 2.5R
        ok, reason, rr = validate_quote_geometry_and_rr("BUY_LONG", 100.0, 125.0, 90.0)
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        self.assertAlmostEqual(rr, 2.5)

        # Short valid 2.0R
        ok, reason, rr = validate_quote_geometry_and_rr("SELL_SHORT", 100.0, 80.0, 110.0)
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        self.assertAlmostEqual(rr, 2.0)

    def test_quote_geometry_and_rr_invalid_geometry(self):
        # Long: sl >= entry
        ok, reason, _ = validate_quote_geometry_and_rr("BUY_LONG", 100.0, 120.0, 105.0)
        self.assertFalse(ok)
        self.assertIn("买多几何不合法", reason)

        # Short: tp >= entry
        ok, reason, _ = validate_quote_geometry_and_rr("SELL_SHORT", 100.0, 105.0, 110.0)
        self.assertFalse(ok)
        self.assertIn("卖空几何不合法", reason)

    def test_quote_geometry_and_rr_insufficient_rr(self):
        # Long: RR = (115 - 100) / (100 - 90) = 1.5 < 2.0
        ok, reason, rr = validate_quote_geometry_and_rr("BUY_LONG", 100.0, 115.0, 90.0)
        self.assertFalse(ok)
        self.assertIn("盈亏比不足 2.0", reason)
        self.assertAlmostEqual(rr, 1.5)

    def test_quote_geometry_and_rr_non_finite_or_nan(self):
        ok, reason, _ = validate_quote_geometry_and_rr("BUY_LONG", float("nan"), 120.0, 90.0)
        self.assertFalse(ok)
        self.assertIn("有限数值", reason)

        ok, reason, _ = validate_quote_geometry_and_rr("BUY_LONG", 100.0, float("inf"), 90.0)
        self.assertFalse(ok)
        self.assertIn("有限数值", reason)

    def test_pipeline_core_floor_active_when_all_plugins_disabled(self):
        # Set all plugins to disabled in config
        im.save_config({"pipeline_order": [], "enabled": {}})

        pkg = {"instId": "BTC-USDT-SWAP", "data_quality": "valid"}
        ctx = {"active_inst_ids": set(), "active_position_sides": {}}

        # Low confidence (< 75)
        dec = {"action": "BUY_LONG", "confidence": 70.0, "entry_price": 100.0, "take_profit_price": 130.0, "stop_loss_price": 90.0}
        act, reason, _ = im.run_interceptor_pipeline(pkg, dec, ctx)
        self.assertEqual(act, "WAIT")
        self.assertIn("置信度低于安全底线", reason)

        # Insufficient RR (< 2.0)
        dec = {"action": "BUY_LONG", "confidence": 85.0, "entry_price": 100.0, "take_profit_price": 110.0, "stop_loss_price": 90.0}
        act, reason, rr = im.run_interceptor_pipeline(pkg, dec, ctx)
        self.assertEqual(act, "WAIT")
        self.assertIn("盈亏比不足 2.0", reason)

        # DOGE confidence floor 80
        pkg_doge = {"instId": "DOGE-USDT-SWAP", "data_quality": "valid"}
        dec_doge = {"action": "BUY_LONG", "confidence": 78.0, "entry_price": 0.10, "take_profit_price": 0.13, "stop_loss_price": 0.09}
        act, reason, _ = im.run_interceptor_pipeline(pkg_doge, dec_doge, ctx)
        self.assertEqual(act, "WAIT")
        self.assertIn("80.0%", reason)

        # Valid trade passes core check even with no plugins enabled
        dec_valid = {"action": "BUY_LONG", "confidence": 85.0, "entry_price": 100.0, "take_profit_price": 125.0, "stop_loss_price": 90.0}
        act, reason, rr = im.run_interceptor_pipeline(pkg, dec_valid, ctx)
        self.assertEqual(act, "BUY_LONG")
        self.assertEqual(reason, "")
        self.assertAlmostEqual(rr, 2.5)

    def test_pipeline_fail_closed_when_plugin_missing_file_or_entry(self):
        # Configure an enabled plugin that does not exist on disk
        im.save_config({
            "pipeline_order": ["missing_filter.py", "bad_syntax.py"],
            "enabled": {"missing_filter.py": True, "bad_syntax.py": False}
        })

        pkg = {"instId": "BTC-USDT-SWAP", "data_quality": "valid"}
        ctx = {"active_inst_ids": set(), "active_position_sides": {}}
        dec = {"action": "BUY_LONG", "confidence": 85.0, "entry_price": 100.0, "take_profit_price": 125.0, "stop_loss_price": 90.0}

        act, reason, _ = im.run_interceptor_pipeline(pkg, dec, ctx)
        self.assertEqual(act, "WAIT")
        self.assertIn("文件缺失", reason)

        # Now create file but omit check_risk function
        no_entry = self.mock_plugins / "no_entry.py"
        no_entry.write_text("def other_function(): pass\n", encoding="utf-8")
        im.save_config({
            "pipeline_order": ["no_entry.py"],
            "enabled": {"no_entry.py": True}
        })
        act, reason, _ = im.run_interceptor_pipeline(pkg, dec, ctx)
        self.assertEqual(act, "WAIT")
        self.assertIn("缺少 check_risk 入口", reason)

    def test_pipeline_plugin_input_mutation_isolation(self):
        # Plugin attempts to mutate decision object
        mutating_plugin = self.mock_plugins / "mutator.py"
        mutating_plugin.write_text(
            "def check_risk(package, decision, context):\n"
            "    decision['confidence'] = 999.0\n"
            "    decision['entry_price'] = 0.0\n"
            "    return True, ''\n",
            encoding="utf-8"
        )
        im.save_config({
            "pipeline_order": ["mutator.py"],
            "enabled": {"mutator.py": True}
        })

        pkg = {"instId": "BTC-USDT-SWAP", "data_quality": "valid"}
        ctx = {"active_inst_ids": set(), "active_position_sides": {}}
        dec = {"action": "BUY_LONG", "confidence": 85.0, "entry_price": 100.0, "take_profit_price": 125.0, "stop_loss_price": 90.0}
        dec_copy = copy.deepcopy(dec)

        act, _, _ = im.run_interceptor_pipeline(pkg, dec, ctx)
        self.assertEqual(act, "BUY_LONG")
        # Ensure dec was not mutated by the plugin
        self.assertEqual(dec, dec_copy)

    def test_documented_flat_dynamics_reach_the_plugins(self):
        """文档契约：99 模板与后台页面承诺的扁平动力学字段必须真的到插件手里。

        历史缺陷：两处文档都写 `package['velocity_v']` / `acceleration_a` / `jerk_j`，
        但 packager 只把动力学放在 `package['calculus']['timeframes']` 里 ——
        照文档写的规则静默读到 0.0，永不触发（fail-open）。
        """
        probe = self.mock_plugins / "probe.py"
        probe.write_text(
            "def check_risk(package, decision, context):\n"
            "    seen = {k: package.get(k, 'MISSING') for k in\n"
            "            ('velocity_v', 'acceleration_a', 'jerk_j')}\n"
            "    return False, repr(seen)\n",
            encoding="utf-8"
        )
        im.save_config({"pipeline_order": ["probe.py"], "enabled": {"probe.py": True}})

        pkg = {
            "instId": "BTC-USDT-SWAP",
            "data_quality": "valid",
            "calculus": {"valid": True, "timeframes": {
                "15M": {"velocity": -0.42, "acceleration": 0.94, "jerk": 1.46},
                "1H": {"velocity": -0.87, "acceleration": 0.40, "jerk": 0.42},
            }},
        }
        ctx = {"active_inst_ids": set(), "active_position_sides": {}}
        dec = {"action": "BUY_LONG", "confidence": 85.0, "entry_price": 100.0,
               "take_profit_price": 125.0, "stop_loss_price": 90.0}

        act, reason, _ = im.run_interceptor_pipeline(pkg, dec, ctx)
        self.assertEqual(act, "WAIT")
        # 取 1H（与提示词「1H 三大数理基石」同源），不是 15M
        self.assertEqual(reason, repr({"velocity_v": -0.87, "acceleration_a": 0.4, "jerk_j": 0.42}))
        # 规范化副本不得回写调用方的包
        self.assertNotIn("velocity_v", pkg)

        # 数据不可用时显式 None（而不是 0.0），插件可据此 fail-closed
        act, reason, _ = im.run_interceptor_pipeline({"instId": "X", "data_quality": "valid"}, dec, ctx)
        self.assertEqual(act, "WAIT")
        self.assertEqual(reason, repr({"velocity_v": None, "acceleration_a": None, "jerk_j": None}))


class VwapPremiumGateTests(unittest.TestCase):
    """`plugins/interceptors/05_vwap_premium_gate.py`：入场位置门禁。

    背景：账本 26 笔做多入场全部发生在 VWAP 上方（中位 +1.19%），
    而已知的亏损多单集中在 +1.26%~+1.99%（溢价区），两笔止盈成功则在 +0.3% 以内。
    本类从磁盘加载真插件（不复制一份逻辑），钉住四种判据。
    """

    @classmethod
    def setUpClass(cls):
        import importlib.util as u
        path = Path(im.__file__).resolve().parent.parent / "plugins" / "interceptors" / "05_vwap_premium_gate.py"
        spec = u.spec_from_file_location("vwap_gate", path)
        cls.mod = u.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def _check(self, action, bias):
        return self.mod.check_risk({"vwap_bias": bias}, {"action": action}, {})

    def test_premium_zone_long_is_blocked(self):
        ok, reason = self._check("BUY_LONG", 2.4)
        self.assertFalse(ok, "溢价区追多必须被拦")
        self.assertIn("溢价区追高", reason)

    def test_near_vwap_long_passes(self):
        """近 VWAP 的回踩（两组历史盈利样本都在 +0.3% 以内）必须放行。"""
        for bias in (0.0, 0.3, -1.2):
            ok, reason = self._check("BUY_LONG", bias)
            self.assertTrue(ok, f"bias={bias} 不该被拦: {reason}")

    def test_accepted_long_fill_always_lands_below_vwap(self):
        """验收条件：被放行的多单，成交价必在 VWAP 之下。

        这是阈值取 1.0（而非 1.5）的**唯一**理由：溢价区的均值回归就是逆风行程，
        若成交仍在 VWAP 之上，回到 VWAP 这段路就先吃掉一截止损距离。

        验收条件 = `(1 - 回踩地板) × (1 + 阈值) < 1`。两个常量分居两个模块
        （plugins/ 与 scripts/trader/order_intent.py），没有这道断言就会各调各的。
        """
        from scripts.trader import order_intent

        floor = order_intent.MIN_ENTRY_PULLBACK_RATIO
        limit = self.mod.PREMIUM_LIMIT_PCT / 100.0
        self.assertLess((1.0 - floor) * (1.0 + limit), 1.0,
                        "入场地板与溢价阈值胶合破口：成交可能落在 VWAP 之上")

        # 取「刚刚好被放行」的最坏情形：现价 = VWAP × (1 + 阈值)，向内挤万分之一
        vwap = 100.0
        px = vwap * (1.0 + limit) * 0.9999
        self.assertTrue(self._check("BUY_LONG", (px / vwap - 1.0) * 100)[0],
                        "边界内侧应放行（阀值口径与实现不一致）")
        lp, _, _ = order_intent.resolve_entry_prices(
            is_long=True, ai_decision={"entry_price": px},
            f={"price": px, "bidPx": px, "askPx": px},
            prec=4, tp_dist=8.0, sl_dist=4.0)
        self.assertLess(lp, vwap, f"成交价 {lp} 不在 VWAP {vwap} 之下")

    def test_short_side_mirrors(self):
        ok, reason = self._check("SELL_SHORT", -2.4)
        self.assertFalse(ok, "折价区追空是同一失效模式的镜像，同样拦")
        self.assertIn("折价区追空", reason)
        self.assertTrue(self._check("SELL_SHORT", -0.3)[0])

    def test_zero_bias_is_a_valid_reading_not_missing_data(self):
        """0.0 是合法读数（现价正落在 VWAP 上），不是缺省值 —— 不得当成数据缺失。"""
        self.assertTrue(self._check("BUY_LONG", 0.0)[0])

    def test_missing_bias_fails_closed(self):
        for pkg in ({}, {"vwap_bias": None}, {"vwap_bias": "N/A"}, {"vwap_bias": True}):
            ok, reason = self.mod.check_risk(pkg, {"action": "BUY_LONG"}, {})
            self.assertFalse(ok, f"{pkg} 应 fail-closed")
            self.assertIn("不可用", reason)

    def test_wait_never_touches_the_gate(self):
        """WAIT 直接放行（下游不变更成开仓），且不要求任何字段存在。"""
        self.assertTrue(self.mod.check_risk({}, {"action": "WAIT"}, {})[0])

    def test_registered_in_default_pipeline(self):
        """新插件必须真的进默认管线（否则就是一个永不执行的摆设）。"""
        self.assertIn("05_vwap_premium_gate.py", im.DEFAULT_ORDER)
        self.assertTrue(im.DEFAULT_ENABLED.get("05_vwap_premium_gate.py"))


if __name__ == "__main__":
    unittest.main()
