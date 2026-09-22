"""网关 supervisor 跨平台判活（无 /proc 主机）。

背景（macOS 实测）：`_is_gateway_worker` 旧实现无条件读 `/proc/<pid>/cmdline`，
非 Linux 主机上 `OSError` 被当成「不是 worker」→ `current_pid()` 反手 unlink
PID 文件（活体持锁者不可见，后台网关面板永远显示「未运行」）。

判定真相始终是 flock 单持有者；PID 文件只是它旁边的缓存提示。无 /proc 时降级为
「存活即本仓 worker」，与既有 EACCES 降级分支同一语义（单 checkout 无歧义）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from r20_gateway import supervisor
from r20_gateway.pidfile import PID_FILE


class TestLivenessWithoutProc(unittest.TestCase):
    """模拟非 Linux（无 /proc）主机。"""

    def _dead_pid(self) -> int:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def test_alive_pid_reported_as_worker_without_proc(self):
        with patch.object(supervisor, "_PROC_AVAILABLE", False):
            self.assertTrue(supervisor._is_gateway_worker(os.getpid()))

    def test_dead_pid_still_reported_dead_without_proc(self):
        with patch.object(supervisor, "_PROC_AVAILABLE", False):
            self.assertFalse(supervisor._is_gateway_worker(self._dead_pid()))

    def test_current_pid_keeps_pid_file_for_live_worker(self):
        """活体持锁者自我登记的文件绝不能被判活逻辑删掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            # 路径字面量只允许留在 pidfile.py（extraction 审计）：这里复用常量名
            pid_file = Path(tmp) / PID_FILE.name
            pid_file.write_text(str(os.getpid()), encoding="utf-8")
            with patch.object(supervisor, "_PROC_AVAILABLE", False), \
                    patch.object(supervisor, "PID_FILE", pid_file):
                self.assertEqual(supervisor.current_pid(), os.getpid())
            self.assertTrue(pid_file.exists(), "活体 worker 的 pid 文件被误删")


if __name__ == "__main__":
    unittest.main()
