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
