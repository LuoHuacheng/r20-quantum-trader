"""看板缓存后台刷新线程（第一百九十三刀）。

| 语义 | 纪律 |
|---|---|
| 循环 | 起手先小睡 0.5s 让服务干净启动；**缓存未过 `WORKER_MIN_INTERVAL_SECONDS` 就不刷**（与 `/api/all` 的陈旧闸同口径）；每轮后睡 2.0s |
| ★ 单例 | `start_*` 只在「没有线程」或「线程已死」时才起新的 ⇒ **重复调用不会起出第二个刷新线程**（否则看板会被并发刷成两套数据）|
| ⚠️ 吞异常 | 本轮**实测边界**：`update_cache_cycle()` 抛任何异常都被 `except Exception: pass` **静默吞掉**，循环继续 ⇒ 刷新长期失败时**外部看不出**（面板会停在旧数据而进程毫无反应）。只钉现状、列为待议 |
| 停止 | 置标志位即返回（**不 join**）⇒ 停止后可能还有最后一轮在跑 |
"""

import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import r20_backend.dashboard_cache as dc


class WorkerLoopTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, dc, "_BG_WORKER_RUNNING", False)
        self.addCleanup(setattr, dc, "_BG_WORKER_THREAD", None)
        # 节流判据依赖 LAST_CACHE_TIME：不归零则本文件的用例结果取决于**同进程里
        # 别的测试**刚把缓存刷到多新（不可重放）。故先钉到“很旧”。
        self.addCleanup(setattr, dc, "LAST_CACHE_TIME", dc.LAST_CACHE_TIME)
        dc.LAST_CACHE_TIME = 0.0

    def test_loop_refreshes_then_exits_when_flag_cleared(self):
        calls = []

        def _cycle():
            calls.append(1)
            dc._BG_WORKER_RUNNING = False      # 一轮之后请求停止
        with patch.object(dc, "update_cache_cycle", side_effect=_cycle), \
             patch.object(dc.time, "sleep") as slept:
            dc._BG_WORKER_RUNNING = True
            dc._dashboard_background_worker_loop()
        self.assertEqual(len(calls), 1, "标志位清掉后不再刷")
        self.assertEqual([c.args[0] for c in slept.call_args_list], [0.5, 2.0],
                         "起手 0.5s、每轮 2.0s")

    def test_cache_still_fresh_skips_the_round(self):
        """★ 节流：缓存比 `WORKER_MIN_INTERVAL_SECONDS` 新 ⇒ 本轮**一个外呼都不发**。

        旧实现每 2s 无条件刷一轮，每轮固定 `balances`/`positions`/`pending_orders`
        三连 —— 本机 OKX 私有端点请求量的最大来源（≈100~150 次/分钟）。
        """
        calls = []

        def _sleep(seconds):
            # 跳过一轮后（即 2.0s 那一觉）就让循环退出 —— 否则 sleep 被 patch 掉、
            # 标志位永真 = 死循环。起手那 0.5s 不算“一轮”。
            if seconds == 2.0:
                dc._BG_WORKER_RUNNING = False
        with patch.object(dc, "update_cache_cycle", side_effect=lambda: calls.append(1)), \
             patch.object(dc.time, "sleep", side_effect=_sleep) as slept:
            dc.LAST_CACHE_TIME = time.time()          # 刚刷过
            dc._BG_WORKER_RUNNING = True
            dc._dashboard_background_worker_loop()
        self.assertEqual(calls, [], "缓存还新却仍外呼 ⇒ 节流没生效")
        self.assertEqual([c.args[0] for c in slept.call_args_list], [0.5, 2.0],
                         "跳过该轮也得照旧睡 2.0s（不能变成忙轮）")

    def test_stale_cache_is_refreshed(self):
        """反证：缓存一旦超过阈值（或进程刚起 LAST_CACHE_TIME=0）⇒ 正常刷新。"""
        calls = []

        def _cycle():
            calls.append(1)
            dc._BG_WORKER_RUNNING = False
        with patch.object(dc, "update_cache_cycle", side_effect=_cycle), \
             patch.object(dc.time, "sleep"):
            dc.LAST_CACHE_TIME = time.time() - (dc.WORKER_MIN_INTERVAL_SECONDS + 1)
            dc._BG_WORKER_RUNNING = True
            dc._dashboard_background_worker_loop()
        self.assertEqual(len(calls), 1, "过旧就必须刷，否则面板永远不更新")

    def test_a_failing_cycle_does_not_kill_the_worker(self):
        """★ 单轮失败不得让刷新线程死掉（否则看板从此不再更新，而进程看起来一切正常）。"""
        state = {"n": 0}

        def _cycle():
            state["n"] += 1
            if state["n"] >= 2:
                dc._BG_WORKER_RUNNING = False
            raise RuntimeError("这一轮炸了")
        with patch.object(dc, "update_cache_cycle", side_effect=_cycle), \
             patch.object(dc.time, "sleep"):
            dc._BG_WORKER_RUNNING = True
            dc._dashboard_background_worker_loop()      # 不应抛
        self.assertEqual(state["n"], 2, "失败之后仍然继续下一轮")

    def test_silent_swallow_is_recorded_as_a_boundary(self):
        """⚠️ **实测边界（列待议）**：异常被**静默吞掉**（无日志、无计数、无状态位）⇒
        刷新长期失败时**外部完全看不出**：面板停在旧数据，进程健康，指标也正常。

        这与本仓别处「读不到要能被看见」的取向相反；改它属可观测性变更 ⇒ 只钉现状。"""
        def _cycle():
            dc._BG_WORKER_RUNNING = False
            raise ValueError("x")
        with patch.object(dc, "update_cache_cycle", side_effect=_cycle), \
             patch.object(dc.time, "sleep"):
            dc._BG_WORKER_RUNNING = True
            with patch("builtins.print") as pr:
                dc._dashboard_background_worker_loop()
        self.assertFalse(pr.called, "连打印都没有 ⇒ 确实静默")


class WorkerLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, dc, "_BG_WORKER_RUNNING", False)
        self.addCleanup(setattr, dc, "_BG_WORKER_THREAD", None)

    def test_start_is_idempotent_while_the_thread_lives(self):
        once = MagicMock()
        once.is_alive.return_value = True
        with patch.object(dc.threading, "Thread", return_value=once) as th:
            dc._BG_WORKER_THREAD = None
            dc.start_dashboard_background_worker()
            self.assertEqual(th.call_count, 1)
            dc.start_dashboard_background_worker()      # 已有活线程
        self.assertEqual(th.call_count, 1, "★ 不得起出第二个刷新线程")
        self.assertTrue(dc._BG_WORKER_RUNNING)

    def test_start_restarts_after_the_thread_died(self):
        dead = MagicMock()
        dead.is_alive.return_value = False
        with patch.object(dc.threading, "Thread", return_value=dead) as th:
            dc._BG_WORKER_THREAD = dead
            dc.start_dashboard_background_worker()
        self.assertEqual(th.call_count, 1, "死线程 ⇒ 允许重启")

    def test_stop_only_clears_the_flag(self):
        dc._BG_WORKER_RUNNING = True
        dc.stop_dashboard_background_worker()
        self.assertFalse(dc._BG_WORKER_RUNNING)
        self.assertIsInstance(dc._BG_WORKER_THREAD, (type(None), threading.Thread))


if __name__ == "__main__":
    unittest.main()
