"""`/api/v1/admin/about` 在 **git 不可用**时必须照常 200（降级而非 500）。

背景（离线套件实测）：about 的 `repository.branch/commit` 直接调 `git(...)`，
未做任何兜底。git 缺失 / 工作区不是仓库 / tar 部署 / 子进程被离线护栏拦下 →
整个自检接口 500（离线套件里 2 例 ERROR 的真因）。同文件的 `update_status()`
早就 catch 了，唯独这个字段裸奔。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import r20_backend.app as app_module
from r20_backend.admin_auth import AdminAuthStore
from r20_backend.version import __version__


class AboutDegradationTests(unittest.TestCase):
    def setUp(self):
        from tests.config_sandbox import isolate_config
        isolate_config(self)
        self.temp = tempfile.TemporaryDirectory()
        self.original = app_module.admin_auth
        app_module.admin_auth = AdminAuthStore(Path(self.temp.name) / "admin.db")
        app_module.admin_auth.initialize_from_legacy("InitialAdmin123456")
        self.client = TestClient(app_module.app)

    def tearDown(self):
        app_module.admin_auth = self.original
        self.temp.cleanup()

    def _login(self) -> dict[str, str]:
        response = self.client.post("/api/v1/admin/auth/login",
                                    json={"username": "admin", "password": "InitialAdmin123456"})
        self.assertEqual(response.status_code, 200, response.text)
        return {"X-R20-Session": response.json()["session_token"]}

    def test_about_survives_git_unavailable(self):
        headers = self._login()
        # `app_attr("git", …)` 优先解析 `r20_backend.app.git`（config_sandbox 已把它
        # stub 成回显桩）—— 本用例把它替换成**必抛**的 git，模拟 git 不可用。
        with patch.object(app_module, "git", side_effect=RuntimeError("git unavailable")):
            response = self.client.get("/api/v1/admin/about", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["product"]["version"], __version__)
        self.assertEqual(body["repository"]["branch"], "")
        self.assertEqual(body["repository"]["commit"], "")
        self.assertIsInstance(body["update"], dict)

    def test_about_survives_platform_probe_failure(self):
        """`platform.platform()` 在 macOS 上会 fork `file -b` 探处理器型号：
        受限环境（离线护栏/只读容器）下子进程被拦，异常会直接穿透整个 /about。
        纯诊断字段必须降级，不许 500。"""
        headers = self._login()
        with patch("r20_backend.routers.system.platform.platform",
                   side_effect=RuntimeError("subprocess blocked")):
            response = self.client.get("/api/v1/admin/about", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["runtime"]["platform"])


if __name__ == "__main__":
    unittest.main()
