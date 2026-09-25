"""OKX 公共只读限流器（`scripts/okx_public_guard.py`，第二百四十四刀）。

| 语义 | 纪律 |
|---|---|
| ★ 档名有界 | `bucket()` 只回前缀（`/api/v5/rubik/` …），**绝不**把带 ccy/instId 的整条 URL 当标签 |
| ★ 分档间隔 | rubik（OKX 最紧的 5req/2s 档）必须比 candles/ticker 明显的慢 |
| ★ 并发摊平 | `acquire()` 持锁睡 ⇒ 同档请求按间隔**串行发出**（限流器要的就是这个） |
| ★ 429 ⇒ 按档冷却 | 被限流的档静默 N 秒，期间 `acquire()` 返回 False（调用方落备源/缺省） |
| ★ 冷却**不跨档** | rubik 被限流不得把蜡烛一起饿死（蜡烛缺失会打 invalid 触发 P0 拦单） |
| ★ Retry-After | 认秒数，下限 2s、上限 60s；HTTP-date/垃圾值/缺头一律回落默认 10s |
| 绝不抛 | 限流器自身出错必须放行（`True`）——它不能成为新的故障源 |

时间用假钟（`time.time` / `time.sleep` 同时打桩）⇒ 断言精确到秒且用例不真睡。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import okx_public_guard as guard  # noqa: E402  （与 market_data_service 同一运行时身份）


class _Clock:
    """假钟：`time()` 返回受控值，`sleep()` 只推进它并记账（不真睡）。"""

    def __init__(self, now: float = 1_000_000.0):
        self.now = now
        self.slept: list = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class _GuardTest(unittest.TestCase):
    def setUp(self):
        guard.reset()
        self.addCleanup(guard.reset)
        # 测试会话默认关掉节拍（tests/__init__.py 置 R20_PUBLIC_GUARD_PACE=0，否则万余例
        # 被 0.12~0.6s 的等待拖慢几分钟）—— 但**本文件就是量节拍的地方**，显式打开。
        p = patch.object(guard, "_pacing_enabled", lambda: True)
        p.start()
        self.addCleanup(p.stop)
        self.clock = _Clock()
        for attr, fn in (("time", self.clock.time), ("sleep", self.clock.sleep)):
            p = patch.object(guard.time, attr, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)


class BucketTest(_GuardTest):
    def test_bucket_is_a_bounded_prefix(self):
        """标签基数必须固定：ccy/instId/limit 不许进档名。"""
        self.assertEqual(guard.bucket("/api/v5/rubik/stat/taker-volume?ccy=BTC&period=5m"),
                         "/api/v5/rubik/")
        self.assertEqual(guard.bucket("/api/v5/market/candles?instId=BTC-USDT-SWAP&limit=300"),
                         "/api/v5/market/")
        self.assertEqual(guard.bucket("/api/v5/public/funding-rate?instId=BTC-USDT-SWAP"),
                         "/api/v5/public/")
        self.assertEqual(guard.bucket(""), "default")
        self.assertEqual(guard.bucket(None), "default")

    def test_a_full_url_is_bucketed_by_its_path(self):
        """★ `urlopen(request)` 传进来的是**整条 URL**（`request.full_url`）。

        不剥 scheme+host ⇒ 全部落 `default` 档：rubik 的 0.6s 与它的冷却**双双失效**。
        实测抓到过：一次 rubik 429 被记到 `default` 档，而该档被其他路径共用 ⇒
        一个 rubik 限流连带掐断了 ticker/funding/OI。
        """
        self.assertEqual(
            guard.bucket("https://www.okx.com/api/v5/rubik/stat/taker-volume?ccy=XRP"),
            "/api/v5/rubik/")
        self.assertEqual(
            guard.bucket("https://aws.okx.com/api/v5/market/ticker?instId=BTC-USDT-SWAP"),
            "/api/v5/market/")
        self.assertAlmostEqual(
            guard.min_interval("https://www.okx.com/api/v5/rubik/stat/x"), 0.60, places=3)
        self.assertEqual(guard.bucket("https://www.okx.com"), "default")

    def test_rubik_is_the_tightest_documented_bucket(self):
        """数值必须反映交易所限额：rubik 5req/2s ⇒ 间隔 ≥0.4s；candles 40req/2s ⇒ ≤0.12s。"""
        self.assertGreaterEqual(guard.min_interval("/api/v5/rubik/stat/x"), 0.4)
        self.assertLessEqual(guard.min_interval("/api/v5/market/candles"), 0.12)
        self.assertGreater(guard.min_interval("/api/v5/rubik/stat/x"),
                           guard.min_interval("/api/v5/market/candles"),
                           "rubik 比 candles 紧，间隔必须更大")


class SpacingTest(_GuardTest):
    def test_same_bucket_is_serialised_by_its_interval(self):
        with patch.object(guard, "_INTERVAL_BY_PREFIX", (("/api/v5/rubik/", 0.42),)), \
             patch.object(guard, "_GLOBAL_INTERVAL", 0.0):
            self.assertTrue(guard.acquire("/api/v5/rubik/stat/x"))
            self.assertTrue(guard.acquire("/api/v5/rubik/stat/x"))
        self.assertEqual(len(self.clock.slept), 1, "第二次必须等一个间隔")
        self.assertAlmostEqual(self.clock.slept[0], 0.42, places=3)

    def test_the_global_floor_spreads_across_buckets(self):
        """每档都不超、合起来仍可能把 IP 打爆 ⇒ 全局地板兜底。"""
        with patch.object(guard, "_GLOBAL_INTERVAL", 0.05), \
             patch.object(guard, "_INTERVAL_BY_PREFIX", ()), \
             patch.object(guard, "_DEFAULT_INTERVAL", 0.0):
            guard.acquire("/api/v5/market/candles")
            guard.acquire("/api/v5/public/funding-rate")
        self.assertEqual(len(self.clock.slept), 1, "两档各自的间隔都为 0 ⇒ 唯一能拦的是全局地板")
        self.assertAlmostEqual(self.clock.slept[0], 0.05, places=3)


class CooldownTest(_GuardTest):
    def test_429_blocks_only_its_own_bucket(self):
        seconds = guard.note_429("/api/v5/rubik/stat/x")
        self.assertAlmostEqual(seconds, 10.0, places=3)
        self.assertTrue(guard.in_cooldown("/api/v5/rubik/stat/taker-volume"))
        self.assertFalse(guard.acquire("/api/v5/rubik/stat/taker-volume"),
                         "同档必须被拦下（不发请求）")
        self.assertTrue(guard.acquire("/api/v5/market/candles"),
                        "别的档不得被 rubik 的限流连坐（蜡烛缺 → P0 拦单）")

    def test_a_blocked_acquire_costs_no_time(self):
        guard.note_429("/api/v5/market/candles")
        guard.acquire("/api/v5/market/candles")
        self.assertEqual(self.clock.slept, [], "冷却期应立刻返回，不该睡")

    def test_cooldown_expires_after_the_window(self):
        guard.note_429("/api/v5/market/candles")
        self.assertFalse(guard.acquire("/api/v5/market/candles"))
        self.clock.now += 11.0                      # 越过默认 10s
        self.assertTrue(guard.acquire("/api/v5/market/candles"))
        self.assertFalse(guard.in_cooldown("/api/v5/market/candles"))

    def test_retry_after_is_honoured(self):
        self.assertAlmostEqual(guard.note_429("/api/v5/x", {"Retry-After": "3"}), 3.0, places=3)

    def test_retry_after_is_clamped_on_both_ends(self):
        self.assertAlmostEqual(guard.note_429("/api/v5/x", {"Retry-After": "0.1"}),
                               guard._COOLDOWN_MIN, places=3)
        self.assertAlmostEqual(guard.note_429("/api/v5/x", {"Retry-After": "9999"}),
                               guard._COOLDOWN_MAX, places=3)

    def test_a_bad_or_missing_header_falls_back_to_the_default(self):
        for headers in (None, {}, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
                        {"Retry-After": ""}, {"Retry-After": "-5"}):
            with self.subTest(headers=headers):
                guard.reset()
                self.assertAlmostEqual(guard.note_429("/api/v5/x", headers),
                                       guard._COOLDOWN_DEFAULT, places=3,
                                       msg="解析不了就取默认，绝不猜、也绝不放大")

    def test_note_429_never_raises(self):
        class _Boom:
            def get(self, *_a, **_k):
                raise RuntimeError("headers 坏了")
        guard.note_429("/api/v5/x", _Boom())        # 不得抛
        self.assertAlmostEqual(guard.cooldown_remaining("/api/v5/x"), 0.0, places=3)


class FailOpenTest(_GuardTest):
    def test_the_pacing_switch_only_stops_the_sleep(self):
        """测试开关只关**节拍**：冷却（安全属性）必须照旧生效。"""
        with patch.object(guard, "_pacing_enabled", lambda: False):
            guard.acquire("/api/v5/rubik/stat/x")
            guard.acquire("/api/v5/rubik/stat/x")
            self.assertEqual(self.clock.slept, [], "关掉节拍后不得再睡")
            guard.note_429("/api/v5/rubik/stat/x")
            self.assertFalse(guard.acquire("/api/v5/rubik/stat/x"), "冷却不受开关影响")

    def test_internal_breakage_lets_the_request_through(self):
        """限流器自己出错必须放行 —— 否则它就成了新的故障源（行情全拿不到）。"""
        with patch.object(guard, "bucket", side_effect=RuntimeError("boom")):
            self.assertTrue(guard.acquire("/api/v5/market/candles"))
        self.assertAlmostEqual(guard.cooldown_remaining("/api/v5/market/candles"), 0.0, places=3)

    def test_reset_clears_both_clock_and_cooldowns(self):
        guard.acquire("/api/v5/rubik/stat/x")
        guard.note_429("/api/v5/rubik/stat/x")
        guard.reset()
        self.assertFalse(guard.in_cooldown("/api/v5/rubik/stat/x"))
        with patch.object(guard, "_INTERVAL_BY_PREFIX", (("/api/v5/rubik/", 0.42),)):
            self.assertTrue(guard.acquire("/api/v5/rubik/stat/x"))
        self.assertEqual(self.clock.slept, [], "reset 后第一次不该等间隔")


class UrlopenWrapperTest(_GuardTest):
    """包装器本身：过闸、429 记到**本档**、冷却期不发请求、非 429 不开冷却。"""

    RUBIK = "https://www.okx.com/api/v5/rubik/stat/taker-volume?ccy=XRP&instType=CONTRACTS&period=5m"

    def _http_error(self, url, code=429):
        import urllib.error
        return urllib.error.HTTPError(url, code, "Too Many Requests", {"Retry-After": "7"}, None)

    def test_a_429_is_charged_to_its_own_bucket_and_reraised(self):
        import urllib.error
        with patch.object(guard.urllib.request, "urlopen",
                          side_effect=self._http_error(self.RUBIK)):
            with self.assertRaises(urllib.error.HTTPError):
                with guard.urlopen(self.RUBIK):
                    pass
        self.assertTrue(guard.in_cooldown(self.RUBIK), "429 必须开本档冷却")
        self.assertFalse(guard.in_cooldown("/api/v5/market/candles"),
                         "rubik 被限流不得连坐蜡烛档（那会把 data_quality 打成 invalid）")
        self.assertAlmostEqual(guard.cooldown_remaining(self.RUBIK), 7.0, places=1,
                               msg="Retry-After 必须被尊重")

    def test_a_cooldown_skips_the_network_entirely(self):
        guard.note_429(self.RUBIK)
        with patch.object(guard.urllib.request, "urlopen") as fake:
            with self.assertRaises(guard.RateLimited):
                with guard.urlopen(self.RUBIK):
                    pass
        self.assertFalse(fake.called, "冷却期一个请求都不许发")

    def test_a_normal_response_passes_through(self):
        sentinel = object()

        class _Ctx:
            def __enter__(self):
                return sentinel

            def __exit__(self, *a):
                return False

        with patch.object(guard.urllib.request, "urlopen", return_value=_Ctx()) as fake:
            with guard.urlopen("https://www.okx.com/api/v5/market/ticker") as resp:
                self.assertIs(resp, sentinel)
        self.assertTrue(fake.called)

    def test_a_non_429_http_error_is_not_charged_a_cooldown(self):
        import urllib.error
        err = self._http_error(self.RUBIK, code=500)
        with patch.object(guard.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(urllib.error.HTTPError):
                with guard.urlopen(self.RUBIK):
                    pass
        self.assertFalse(guard.in_cooldown(self.RUBIK), "只有 429 才开冷却")


class NoOkxCallBypassesTheGateTest(unittest.TestCase):
    """静态闸：`scripts/**` 里**凡指向 okx.com 的裸 `urllib.request.urlopen`** 都不允许。

    为什么需要它（第二百四十四刀）：同一个 rubik 统计被 4 个模块各取一遍，而它们
    全部是裸 `urlopen` —— 一处漏接闸，突发就回来了（主脑十个币并行、因子库每 60s
    一轮、舆情每 10 分钟一串），而**单元测试全绿**。本门按仓库既有手法用文本扫描
    （与 `test_directory_docs_current` / `test_no_dead_aggregate_overwrite` 同族）：

    - 判据：某行含 `urllib.request.urlopen(`（且不是 `_public_urlopen(`），
      回溯它上面 4 行内出现 `okx.com` ⇒ 报违规；
    - 边界（如实）：只管 `urllib` 这一族；`market_data_service` 走 `requests`，
      它的闸在 `_guarded()` 里，本门看不到也不需要看到；
    - 自检：扫描面必须够大（≥100 个文件），否则“零命中”可能只是因为没扫到东西。
    """

    ROOT = Path(__file__).resolve().parents[2]
    LOOKBACK = 4
    MIN_FILES = 100

    def _offenders(self):
        bad = []
        files = sorted((self.ROOT / "scripts").rglob("*.py"))
        self.assertGreaterEqual(len(files), self.MIN_FILES, "扫描面太小 ⇒ 本门会永远绿")
        for path in files:
            lines = path.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines):
                if "urllib.request.urlopen(" not in line or "_public_urlopen(" in line:
                    continue
                window = "\n".join(lines[max(0, i - self.LOOKBACK): i + 1])
                if "okx.com" in window:
                    bad.append(f"{path.relative_to(self.ROOT)}:{i + 1}")
        return bad

    def test_no_okx_public_call_bypasses_the_guard(self):
        self.assertEqual(self._offenders(), [],
                         "这些 OKX 公共出网绕过了限流闸（改成 _public_urlopen）")

    def test_the_scan_has_teeth(self):
        """有牙齿自检：造一个“裸 urlopen + okx.com”的临时文件，必须被扫出来。"""
        import tempfile
        probe = self.ROOT / "scripts" / "_guard_scan_probe.py"
        probe.write_text(
            'import urllib.request\n'
            'with urllib.request.urlopen("https://www.okx.com/api/v5/x") as r:\n'
            '    pass\n', encoding="utf-8")
        self.addCleanup(lambda: probe.unlink(missing_ok=True))
        try:
            offenders = self._offenders()
        finally:
            probe.unlink(missing_ok=True)
        self.assertTrue([o for o in offenders if "_guard_scan_probe" in o],
                        "合成样本没被抓到 ⇒ 本门没有牙齿")


if __name__ == "__main__":
    unittest.main(verbosity=2)
