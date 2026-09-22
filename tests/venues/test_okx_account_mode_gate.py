"""OKX 账户模式预检闸回归（2026-09-22 每单 51010 事故）。

守两件事：

1. **判据本身**（`scripts/okx_account_mode.py`）：`acctLv=1` 摘除、`2/3/4` 放行、
   探测失败 fail-open、TTL 缓存不重复出网、`posMode` 不符只告警不拦；
2. **门面接线**（`scripts/ai_factor_trader.py::venue_execution_ready`）：
   这道闸真的能否决 okx 的就绪判定，且**不碰外所**判据。

零出网：探测一律走注入的 `fetch`，接线用例 patch 掉子模块的底层读取。
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import okx_account_mode as oam  # noqa: E402
from scripts import okx_rest  # noqa: E402


def _env():
    """够用的假环境对象（`identity` 是缓存键的来源）。"""
    return types.SimpleNamespace(
        mode="demo", configured=True, api_key="k" * 8, identity="okx:demo:deadbeef")


class AccountModeVerdictTests(unittest.TestCase):
    def setUp(self):
        oam.reset_cache()
        self.addCleanup(oam.reset_cache)

    def _ready(self, info, **kw):
        return oam.account_mode_ready(_env(), fetch=lambda env, timeout: info, **kw)

    def test_spot_mode_is_blocked(self):
        """事故本体：acctLv=1（简单/现货）不支持合约单，必须摘除。"""
        self.assertFalse(self._ready({"acctLv": "1", "posMode": "long_short_mode"}))

    def test_derivatives_modes_pass(self):
        for lv in ("2", "3", "4"):
            with self.subTest(acctLv=lv):
                oam.reset_cache()
                self.assertTrue(self._ready({"acctLv": lv, "posMode": "long_short_mode"}))

    def test_probe_failure_fails_open(self):
        """读配置失败 ≠ 不可交易：不得因为探测失败让交易停摆。"""
        def boom(env, timeout):
            raise RuntimeError("network down")
        self.assertTrue(oam.account_mode_ready(_env(), fetch=boom))

    def test_missing_fields_fail_open(self):
        self.assertTrue(self._ready({}))

    def test_net_mode_only_warns(self):
        """未实测的假设不当闸门：net_mode 只告警，不摘除（误杀真交易更贵）。"""
        self.assertTrue(self._ready({"acctLv": "2", "posMode": "net_mode"}))

    def test_not_configured_env_still_probed(self):
        """环境对象没有 identity 时缓存键退化为 mode+key 前缀，结论不变。"""
        env = types.SimpleNamespace(mode="demo", api_key="abcdefgh")
        self.assertFalse(oam.account_mode_ready(
            env, fetch=lambda e, t: {"acctLv": "1", "posMode": "long_short_mode"}))

    def test_ttl_cache_avoids_repeat_probe(self):
        calls = []

        def fetch(env, timeout):
            calls.append(timeout)
            return {"acctLv": "2", "posMode": "long_short_mode"}

        env = _env()
        self.assertTrue(oam.account_mode_ready(env, fetch=fetch, now=100.0, ttl=300.0))
        self.assertTrue(oam.account_mode_ready(env, fetch=fetch, now=101.0, ttl=300.0))
        self.assertEqual(len(calls), 1, "TTL 内重复出网 ⇒ 每个候选/标的都打一次私有端点")

    def test_ttl_expiry_reprobes(self):
        calls = []

        def fetch(env, timeout):
            calls.append(1)
            return {"acctLv": "1", "posMode": "long_short_mode"}

        env = _env()
        self.assertFalse(oam.account_mode_ready(env, fetch=fetch, now=0.0, ttl=10.0))
        self.assertFalse(oam.account_mode_ready(env, fetch=fetch, now=11.0, ttl=10.0))
        self.assertEqual(len(calls), 2)

    def test_cache_key_follows_credentials(self):
        """换 API Key = 换账户：旧缓存不得复用（否则切账户后闸门读的是上一个账户）。"""
        seen = {"acct-a": {"acctLv": "1", "posMode": "long_short_mode"},
                "acct-b": {"acctLv": "2", "posMode": "long_short_mode"}}

        def fetch(env, timeout):
            return seen[env.identity]

        env_a = types.SimpleNamespace(mode="demo", api_key="a" * 8, identity="acct-a")
        env_b = types.SimpleNamespace(mode="demo", api_key="b" * 8, identity="acct-b")
        self.assertFalse(oam.account_mode_ready(env_a, fetch=fetch))
        self.assertTrue(oam.account_mode_ready(env_b, fetch=fetch))


class FacadeGateWiringTests(unittest.TestCase):
    """接线门：闸门必须真的作用在门面就绪判定上。"""

    def setUp(self):
        oam.reset_cache()
        self.addCleanup(oam.reset_cache)
        import scripts.ai_factor_trader as aft
        self.aft = aft

    def test_okx_removed_when_mode_unsupported(self):
        aft = self.aft
        with patch.object(aft.venue_registry, "is_registered", lambda k: True), \
             patch.object(aft, "current_environment", lambda: _env()), \
             patch.object(oam, "_fetch_account_config",
                          staticmethod(lambda env, timeout: {"acctLv": "1",
                                                             "posMode": "long_short_mode"})):
            self.assertFalse(aft.venue_execution_ready("okx", "demo"),
                             "acctLv=1 时 okx 仍判就绪 ⇒ 每轮继续白烧 51010 单")

    def test_okx_kept_when_mode_supported(self):
        aft = self.aft
        with patch.object(aft.venue_registry, "is_registered", lambda k: True), \
             patch.object(aft, "current_environment", lambda: _env()), \
             patch.object(oam, "_fetch_account_config",
                          staticmethod(lambda env, timeout: {"acctLv": "2",
                                                             "posMode": "long_short_mode"})):
            self.assertTrue(aft.venue_execution_ready("okx", "demo"))

    def test_other_venues_unaffected_by_okx_gate(self):
        """外所判据不得被这道 OKX 专有闸碰到（否则等于单点故障扩面）。"""
        aft = self.aft
        with patch.object(aft.venue_registry, "is_registered", lambda k: True), \
             patch.object(aft.venue_registry, "execution_open", lambda v, e: True), \
             patch.object(aft, "okx_account_mode_ready", lambda env: False), \
             patch.object(aft, "_BROKEN_VENUES", set()):
            self.assertTrue(aft.venue_execution_ready("gate", "demo"))
            self.assertTrue(aft.venue_execution_ready("binance", "demo"))

    def test_credentials_still_gate_first(self):
        """凭证不齐时连探测都不该发生（保持原有 fail-closed 语义）。"""
        aft = self.aft
        calls = []
        with patch.object(aft.venue_registry, "is_registered", lambda k: True), \
             patch.object(aft, "current_environment",
                          lambda: types.SimpleNamespace(mode="demo", configured=False)), \
             patch.object(aft, "okx_account_mode_ready",
                          lambda env: calls.append(1) or True):
            self.assertFalse(aft.venue_execution_ready("okx", "demo"))
        self.assertEqual(calls, [], "凭证不齐却仍探测账户模式")


class SCodeHintTests(unittest.TestCase):
    """51010 的中文根因必须出现在异常里（下次排障不用再翻 OKX 文档）。"""

    def test_prefix_contract_kept(self):
        err = okx_rest._business_error("51010", "You can't complete this request", "x")
        text = str(err)
        self.assertTrue(text.startswith("OKX 51010: You can't complete this request"),
                        "既有前缀契约被破坏（测试/外部解析按前缀匹配）")
        self.assertIn("账户模式", text)

    def test_unknown_code_adds_nothing(self):
        err = okx_rest._business_error("59999", "mystery", "业务请求失败")
        self.assertEqual(str(err), "OKX 59999: mystery")

    def test_missing_msg_uses_default(self):
        err = okx_rest._business_error("51008", "", "业务请求失败")
        self.assertIn("OKX 51008: 业务请求失败", str(err))
        self.assertIn("保证金不足", str(err))

    def test_hint_table_covers_the_incident_code(self):
        self.assertTrue(okx_rest.scode_hint("51010"))
        self.assertEqual(okx_rest.scode_hint("nope"), "")


if __name__ == "__main__":
    unittest.main()
