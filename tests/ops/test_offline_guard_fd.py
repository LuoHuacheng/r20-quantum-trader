"""`OfflineGuard.protected()` 的 **dir_fd 解析**必须跨平台。

背景（macOS 实测）：`shutil.rmtree` 走 fd 版实现时，`os.remove/os.rmdir` 的 audit
事件带的是**相对名 + dir_fd**。旧实现无条件 `os.readlink('/proc/self/fd/N')` ——
macOS 没有 /proc，`readlink` 直接抛 `OSError`，而它在 **audit hook 里抛**会被
`shutil` 当成"删不掉" ⇒ 每个用 `TemporaryDirectory` 的用例 teardown 集体
`ENOTEMPTY` 报 ERROR（离线套件实测 **716 例**）。

本门钉两件事：
1. 临时目录里的相对目标 → 判为**不受保护**（且绝不抛错）；
2. 仓内 `data/` 里的相对目标 → 仍然**受保护**（换平台不许把护栏换成摆设）。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tests.offline_suite import OfflineGuard

ROOT = Path(__file__).resolve().parents[2]


class OfflineGuardFdPathTest(unittest.TestCase):
    def test_relative_target_in_temp_dir_is_not_protected(self):
        guard = OfflineGuard()
        with tempfile.TemporaryDirectory() as tmp:
            fd = os.open(tmp, os.O_RDONLY)
            try:
                self.assertFalse(guard.protected("somefile.txt", fd))
            finally:
                os.close(fd)

    def test_relative_target_in_temp_dir_never_raises(self):
        """核心回归：旧实现在这里抛 OSError，害得 rmtree 半途而废。"""
        guard = OfflineGuard()
        with tempfile.TemporaryDirectory() as tmp:
            fd = os.open(tmp, os.O_RDONLY)
            try:
                guard.protected("x", fd)          # 不抛即通过
            except OSError as exc:                # pragma: no cover - 旧实现路径
                self.fail(f"protected() 抛了 OSError（rmtree 会因此残废）: {exc}")
            finally:
                os.close(fd)

    def test_blocked_spawn_is_attributed_in_diagnostics(self):
        """被拦的 spawn 必须留下**归因栈**（否则只能靠猜谁在 spawn）。"""
        import sys as _sys
        log = Path(OfflineGuard.DIAG_LOG)
        log.unlink(missing_ok=True)
        guard = OfflineGuard()
        with self.assertRaises(RuntimeError):
            guard.audit("subprocess.Popen",
                        (_sys.executable, [_sys.executable, "-c", "pass"], None, None))
        text = log.read_text(encoding="utf-8")
        self.assertIn("spawn-blocked", text)
        self.assertIn("origin=", text)

    def test_origin_falls_back_to_current_test_on_threads(self):
        """线程里（无测试帧）也必须能归因到触发它的用例。"""
        import threading
        from tests import offline_suite
        offline_suite.track_current_test()
        saved = offline_suite.CURRENT_TEST['id']
        # 直接构造"当前用例"记号：本用例的 run() 早在装壳前就已开始，
        # 所以壳不会覆盖到它自己（这也是真实套件里壳先于用例安装的原因）。
        offline_suite.CURRENT_TEST['id'] = (
            f"{type(self).__module__}.{type(self).__name__}.{self._testMethodName}")
        holder = {}
        try:
            worker = threading.Thread(
                target=lambda: holder.update(origin=offline_suite.OfflineGuard._origin()))
            worker.start(); worker.join()
        finally:
            offline_suite.CURRENT_TEST['id'] = saved
        self.assertIn(type(self).__name__, holder["origin"],
                      f"线程归因丢了：{holder.get('origin')}")

    def test_relative_target_inside_repo_data_is_still_protected(self):
        guard = OfflineGuard()
        data_dir = ROOT / "data"
        self.assertTrue(data_dir.is_dir(), "本仓 data/ 必须存在（否则用例无意义）")
        fd = os.open(data_dir, os.O_RDONLY)
        try:
            self.assertTrue(guard.protected("trading_state.json", fd),
                            "仓内 data/ 的相对目标必须仍被判定为受保护资源")
        finally:
            os.close(fd)


if __name__ == "__main__":
    unittest.main()
