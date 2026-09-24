"""r20ctl 双平台服务控制台契约测试（系统服务化）。

## 这个门钉什么

用户要求把「起/停/重启」做成系统服务命令（``r20-start`` / ``r20-stop`` / …）。
本机是 macOS（launchd），仓库既有 ``deploy/*.service`` 是 systemd（Linux）——
故一套命令、两套后端：

| 子命令 | macOS (launchd) | Linux (systemd) |
|---|---|---|
| start | ``launchctl kickstart -k gui/<uid>/com.r20.trader`` | ``sudo systemctl start r20-quantum`` |
| stop | ``launchctl bootout gui/<uid>/com.r20.trader`` | ``sudo systemctl stop r20-quantum`` |
| restart | ``launchctl kickstart -k …`` | ``sudo systemctl restart r20-quantum`` |
| status | ``launchctl print …`` + 端口 + /api/all 探活 | ``systemctl status r20-quantum`` |

**为什么用 ``--dry-run`` 钉**：本门绝不能真的 stop 生产服务（那是真停机）。
``--dry-run`` 只把**将要执行的命令**打出来，于是命令序列可以被逐字断言，
而服务本身毫发无伤。平台用 ``R20_FORCE_PLATFORM`` 钉死，uid 用 ``R20_UID`` 钉死
（否则断言里嵌真实 uid，换台机器就红）。

封闭三律：零网络、零真实 launchctl/systemctl 调用、零生产文件写入
（``R20_LAUNCH_AGENT_DIR`` / ``R20_BIN_DIR`` 全部指到临时目录）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CTL = ROOT / "deploy" / "r20ctl.py"

from tests.config_sandbox import skip_if_offline_suite  # noqa: E402


class _OfflineSkipped(unittest.TestCase):
    """本门以「spawn 子进程跑 r20ctl」为**被测对象**。

    离线套件的审计 hook 拦一切外部子进程（tests/offline_suite.py:209），两类用例
    天然冲突 —— 在离线环境它们"测不了"而非"测不过"。仓规约是在 spawn **之前**
    自行 skip（tests/config_sandbox.skip_if_offline_suite），故整族用例共用此基类。
    """

    def setUp(self):
        skip_if_offline_suite(self)
UID = "501"
LABEL = "com.r20.trader"
UNIT = "r20-quantum"


#: 封闭三律·宿主无关：默认把 LaunchAgent 目录/命令目录钉到临时目录并预置已安装形态，
#: 否则「start 会红/会绿」取决于本机有没有装过服务 —— 那种测试换台机器就变味。
_SEED = Path(tempfile.mkdtemp(prefix="r20ctl-seed-"))
(_SEED / "agents").mkdir()
(_SEED / "agents" / f"{LABEL}.plist").write_text("seed", encoding="utf-8")
(_SEED / "bin").mkdir()


def _env(platform: str, extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env.update({
        "R20_FORCE_PLATFORM": platform,
        "R20_UID": UID,
        "PYTHONPATH": str(ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "R20_LAUNCH_AGENT_DIR": str(_SEED / "agents"),
        "R20_BIN_DIR": str(_SEED / "bin"),
        # pidfile/日志同样钉到临时目录（不存在）：否则 auto 模式的 stop/status 会
        # 读到**生产** data/r20_manual.pid 而改道 manual，断言就随机器状态漂移。
        "R20_PIDFILE": str(_SEED / "manual.pid"),
        "R20_LOG_FILE": str(_SEED / "backend.log"),
    })
    if extra:
        env.update(extra)
    return env


def run_ctl(*args: str, platform: str = "darwin", extra: dict | None = None,
            prog: str | None = None):
    """跑 r20ctl；prog 非空时用 argv[0] 派发（模拟 r20-stop 这类软链）。"""
    if prog:
        tmp = tempfile.mkdtemp(prefix="r20ctl-prog-")
        shim = Path(tmp) / prog
        shim.symlink_to(CTL)
        exe = str(shim)
    else:
        exe = str(CTL)
    return subprocess.run(
        [sys.executable, exe, *args] if not prog else [exe, *args],
        capture_output=True, text=True, env=_env(platform, extra), cwd=str(ROOT))


class DarwinPlanTest(_OfflineSkipped):
    """macOS：命令必须落到 launchctl，且 Service 目标形如 gui/<uid>/<label>。"""

    def _dry(self, sub: str):
        r = run_ctl(sub, "--dry-run", platform="darwin")
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_start_kickstarts_the_agent(self):
        self.assertIn(f"launchctl kickstart -k gui/{UID}/{LABEL}", self._dry("start"))

    def test_stop_boots_the_agent_out(self):
        self.assertIn(f"launchctl bootout gui/{UID}/{LABEL}", self._dry("stop"))

    def test_restart_kickstarts_the_agent(self):
        self.assertIn(f"launchctl kickstart -k gui/{UID}/{LABEL}", self._dry("restart"))

    def test_status_reports_launchd_state_and_probes_the_api(self):
        out = self._dry("status")
        self.assertIn(f"launchctl print gui/{UID}/{LABEL}", out)
        self.assertIn("/api/all", out)

    def test_logs_tails_the_configured_backend_log(self):
        # 断言的是**配置解析结果**（R20_LOG_FILE），不是写死路径 —— 否则换个日志位置就假红
        out = self._dry("logs")
        self.assertIn("tail -n 50", out)
        self.assertIn(str(_SEED / "backend.log"), out)


class LinuxPlanTest(_OfflineSkipped):
    """Linux：复用既有 systemd 单元，非 root 时自动前置 sudo（否则必然权限失败）。"""

    def _dry(self, sub: str):
        r = run_ctl(sub, "--dry-run", platform="linux")
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_start_uses_systemctl(self):
        self.assertIn(f"sudo systemctl start {UNIT}", self._dry("start"))

    def test_stop_uses_systemctl(self):
        self.assertIn(f"sudo systemctl stop {UNIT}", self._dry("stop"))

    def test_restart_uses_systemctl(self):
        self.assertIn(f"sudo systemctl restart {UNIT}", self._dry("restart"))

    def test_status_uses_systemctl(self):
        self.assertIn(f"sudo systemctl status {UNIT}", self._dry("status"))

    def test_logs_uses_journalctl(self):
        self.assertIn(f"sudo journalctl -u {UNIT}", self._dry("logs"))


class DispatchTest(_OfflineSkipped):
    def test_argv0_symlink_dispatches_subcommand(self):
        # 软链名 r20-stop 不带子命令参数也必须等价于 stop
        r = run_ctl("--dry-run", platform="darwin", prog="r20-stop")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"launchctl bootout gui/{UID}/{LABEL}", r.stdout)

    def test_argv0_r20_restart(self):
        r = run_ctl("--dry-run", platform="linux", prog="r20-restart")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"sudo systemctl restart {UNIT}", r.stdout)

    def test_explicit_subcommand_beats_argv0(self):
        r = run_ctl("status", "--dry-run", platform="darwin", prog="r20-stop")
        self.assertIn("launchctl print", r.stdout)

    def test_unknown_subcommand_exits_2(self):
        r = run_ctl("frobnicate", platform="darwin")
        self.assertEqual(r.returncode, 2)
        self.assertIn("frobnicate", r.stderr)

    def test_help_lists_every_command(self):
        r = run_ctl("--help", platform="darwin")
        self.assertEqual(r.returncode, 0)
        for sub in ("start", "stop", "restart", "status", "logs", "install", "uninstall"):
            with self.subTest(sub=sub):
                self.assertIn(sub, r.stdout)


class FailClosedTest(_OfflineSkipped):
    """launchd 未安装就必须明确拒动并指路，而不是静默报成功（UI 不说谎）。"""

    def test_forced_launchd_without_installed_agent_fails_with_guidance(self):
        """显式要 launchd 时，未安装必须**明确拒动并指路**（不许偷偷改道）。"""
        with tempfile.TemporaryDirectory() as tmp:
            r = run_ctl("start", "--dry-run", platform="darwin",
                        extra={"R20_LAUNCH_AGENT_DIR": tmp, "R20_FORCE_BACKEND": "launchd"})
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("install", (r.stderr + r.stdout).lower())

    def test_auto_without_installed_agent_falls_back_instead_of_failing(self):
        """auto：launchd 没装不是致命错误 —— 应该说明并改走 manual supervisor。"""
        with tempfile.TemporaryDirectory() as tmp:
            r = run_ctl("start", "--dry-run", platform="darwin",
                        extra={"R20_LAUNCH_AGENT_DIR": tmp, "R20_FORCE_BACKEND": "auto"})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("manual", r.stdout.lower())

    def test_install_dry_run_targets_launch_agents_and_bin_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            agents = os.path.join(tmp, "agents")
            bindir = os.path.join(tmp, "bin")
            os.makedirs(agents)
            os.makedirs(bindir)
            r = run_ctl("install", "--dry-run", platform="darwin",
                        extra={"R20_LAUNCH_AGENT_DIR": agents, "R20_BIN_DIR": bindir})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(os.path.join(agents, f"{LABEL}.plist"), r.stdout)
            for cmd in ("r20-start", "r20-stop", "r20-restart", "r20-status", "r20-logs"):
                with self.subTest(cmd=cmd):
                    self.assertIn(os.path.join(bindir, cmd), r.stdout)
            self.assertIn("launchctl bootstrap", r.stdout)

    def test_uninstall_dry_run_boots_out_and_removes_files(self):
        r = run_ctl("uninstall", "--dry-run", platform="darwin")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("launchctl bootout", r.stdout)


class _ManualHarness(_OfflineSkipped):
    """pidfile 兜底 supervisor（方案 c）：不依赖 launchd 也能 start/stop/restart。

    用 ``R20_START_CMD=/bin/sleep 45`` 替换真进程 —— 于是 supervisor 的
    建 pid / 幂等 / SIGTERM 收停 / restart 换 pid 全都能真跑，却不碰生产 uvicorn。
    """

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="r20ctl-manual-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        self.pidfile = Path(self.tmp) / "r20_manual.pid"
        self.logfile = Path(self.tmp) / "manual.log"
        self.extra = {
            "R20_FORCE_BACKEND": "manual",
            "R20_PIDFILE": str(self.pidfile),
            "R20_LOG_FILE": str(self.logfile),
            "R20_START_CMD": "/bin/sleep 45",
            # 探活端口指向无人监听的 8099：status 用例绝不碰生产 8080，也不触发 DNS
            "R20_PORT": "8099",
        }
        self.addCleanup(self._reap)

    def _reap(self):
        pid = self._pid()
        if pid and _alive(pid):
            subprocess.run(["kill", "-9", str(pid)], capture_output=True)

    def _pid(self):
        try:
            return int(self.pidfile.read_text().strip())
        except (OSError, ValueError):
            return None

    def _ctl(self, *args):
        return run_ctl(*args, platform="darwin", extra=self.extra)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


class ManualSupervisorTest(_ManualHarness):
    def test_start_writes_pidfile_and_spawns(self):
        r = self._ctl("start")
        self.assertEqual(r.returncode, 0, r.stderr)
        pid = self._pid()
        self.assertIsNotNone(pid, "start 必须写 pidfile")
        self.assertTrue(_alive(pid), "start 之后进程必须活着")

    def test_start_is_idempotent(self):
        self._ctl("start")
        first = self._pid()
        r = self._ctl("start")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._pid(), first, "已在运行就不得换 pid（不许起第二份）")

    def test_stop_terminates_and_removes_pidfile(self):
        self._ctl("start")
        pid = self._pid()
        r = self._ctl("stop")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.pidfile.exists(), "stop 后 pidfile 必须清掉")
        for _ in range(40):
            if not _alive(pid):
                break
            time.sleep(0.1)
        self.assertFalse(_alive(pid), "SIGTERM 之后进程必须退出")

    def test_stop_when_not_running_is_benign(self):
        r = self._ctl("stop")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_stale_pidfile_is_reported_and_replaced(self):
        # 陈旧 pidfile（进程早死了）不得让 start 变成哑炮
        self.pidfile.write_text("999999", encoding="utf-8")
        r = self._ctl("start")
        self.assertEqual(r.returncode, 0, r.stderr)
        pid = self._pid()
        self.assertNotEqual(pid, 999999)
        self.assertTrue(_alive(pid))

    def test_restart_replaces_pid(self):
        self._ctl("start")
        old = self._pid()
        r = self._ctl("restart")
        self.assertEqual(r.returncode, 0, r.stderr)
        new = self._pid()
        self.assertIsNotNone(new)
        self.assertNotEqual(old, new, "restart 必须换一个新进程")
        self.assertTrue(_alive(new))
        for _ in range(40):
            if not _alive(old):
                break
            time.sleep(0.1)
        self.assertFalse(_alive(old), "restart 必须收掉旧进程，不许双起")

    def test_status_names_the_manual_backend_and_pid(self):
        self._ctl("start")
        r = self._ctl("status")
        out = (r.stdout + r.stderr).lower()
        self.assertIn("manual", out)
        self.assertIn(str(self._pid()), out)

    def test_dry_run_manual_prints_plan_without_spawning(self):
        r = self._ctl("start", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("manual", r.stdout.lower())
        self.assertFalse(self.pidfile.exists(), "dry-run 绝不能真的起进程")


class AutoFallbackTest(_ManualHarness):
    """auto：launchd 起不来就落 manual，并把崩溃循环的 agent 摘掉。"""

    def _auto_extra(self):
        extra = dict(self.extra)
        extra.update({
            "R20_FORCE_BACKEND": "auto",
            "R20_PORT": "8099",              # 无人监听 → launchd 校验必失败
            "R20_LAUNCH_VERIFY_SECONDS": "1",
            # 执行期把 launchctl 换成必失败的可执行文件：绝不碰真实 launchd（测试不触真资源）
            "R20_LAUNCHCTL_BIN": "/usr/bin/false",
        })
        return extra

    def test_auto_falls_back_to_manual_when_launchd_never_binds(self):
        r = run_ctl("start", platform="darwin", extra=self._auto_extra())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("manual", (r.stdout + r.stderr).lower())
        pid = self._pid()
        self.assertIsNotNone(pid, "回落 manual 后必须写好 pidfile")
        self.assertTrue(_alive(pid))

    def test_dry_run_auto_does_not_spawn(self):
        r = run_ctl("start", "--dry-run", platform="darwin", extra=self._auto_extra())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.pidfile.exists())

    def test_forced_launchd_never_silently_falls_back(self):
        extra = dict(self._auto_extra())
        extra["R20_FORCE_BACKEND"] = "launchd"
        r = run_ctl("start", "--dry-run", platform="darwin", extra=extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("launchctl", r.stdout)
        self.assertNotIn("manual", r.stdout.lower(), "显式指定 launchd 时不得偷偷改走 manual")


#: 一个**真的占住端口**的假后端。这是本门的关键：`_wait_port_open` 只问“端口通不通”，
#: 旧进程自己也在听 ⇒ 探活立刻为真 ⇒ 旧实现会在这个形状下空转并谎报成功。
#: ⚠️ `listen` 的 backlog 必须**大**：这个假后端从不 `accept()`，backlog=1 时第二次
#: 探活的 SYN 会被丢、`connect_ex` 超时返假 —— 那会让本门自己变成约约约（实测踩过）。
_LISTENER_CMD = (
    "{py} -c \"import socket,time; s=socket.socket(); "
    "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); "
    "s.bind(('127.0.0.1', 8099)); s.listen(128); time.sleep(60)\""
)


def _port_open(port: int = 8099) -> bool:
    import socket as _s
    with _s.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


class AutoRestartMustReplaceTheManualBackendTest(_ManualHarness):
    """★ 2026-09-24 事故回归：auto 模式的 `restart` 必须先收掉 manual 后端。

    事故形状（现场实测）：manual supervisor 占着 8080 → auto 路径走 launchd
    （仓库在 ``~/Documents``，TCC 让 launchd job 在 exec 前就死，EX_CONFIG 78）→
    那条路的 `_wait_port_open` 被**旧进程自己**骗过 → 打“✅ 已重启”而旧进程
    原封不动，还留下一个带 KeepAlive 的死 agent 无限重试。

    本门用"**真的监听端口**的假后端"复现这个假阳：`launchctl` 钉成退出码 0
    但从不监听的 `/usr/bin/true`。旧实现必定把旧进程留下 ⇒ 本门翻红。
    """

    def setUp(self):
        super().setUp()
        self.extra.update({
            "R20_START_CMD": _LISTENER_CMD.format(py=sys.executable),
            "R20_LAUNCH_VERIFY_SECONDS": "1",
            "R20_LAUNCHCTL_BIN": "/usr/bin/true",   # “成功”但从不监听
        })

    def _auto(self, *args):
        return run_ctl(*args, platform="darwin",
                       extra=dict(self.extra, R20_FORCE_BACKEND="auto"))

    def _wait_dead(self, pid) -> bool:
        for _ in range(60):
            if not _alive(pid):
                return True
            time.sleep(0.1)
        return False

    def _start_manual_and_wait_for_the_port(self) -> int:
        r = run_ctl("start", platform="darwin", extra=self.extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        pid = self._pid()
        self.assertIsNotNone(pid, "manual start 必须写 pidfile")
        for _ in range(50):
            if _port_open():
                return pid
            time.sleep(0.1)
        self.fail("假后端没占住端口 ⇒ `_wait_port_open` 不会被骗过，本门失去牙齿")

    def test_restart_replaces_the_backend_even_when_the_port_is_already_open(self):
        old = self._start_manual_and_wait_for_the_port()

        r = self._auto("restart")
        self.assertEqual(r.returncode, 0, r.stderr)
        new = self._pid()
        self.assertIsNotNone(new, "restart 后必须有新 pidfile")
        self.assertNotEqual(old, new, "restart 必须换进程（旧实现在这里被旧端口骗过而空转）")
        self.assertTrue(self._wait_dead(old), "旧后端必须先被收掉，不许与新的并存")
        self.assertTrue(_alive(new), "新后端必须活着")

    def test_port_held_by_an_unmanaged_orphan_refuses_instead_of_lying(self):
        """端口有人听、pidfile 却空 ⇒ 本工具够不着 ⇒ 必须拒动并指路，绝不谎报成功。"""
        pid = self._start_manual_and_wait_for_the_port()
        self.addCleanup(subprocess.run, ["kill", "-9", str(pid)], capture_output=True)
        self.pidfile.unlink()                     # 只删账本，进程照旧占着端口

        r = run_ctl("restart", platform="darwin",
                    extra=dict(self.extra, R20_FORCE_BACKEND="auto",
                               R20_LAUNCHCTL_BIN="/usr/bin/false"))
        self.assertNotEqual(r.returncode, 0,
                            f"够不着旧进程时必须拒动，不许报成功：rc={r.returncode}\n"
                            f"stdout={r.stdout}\nstderr={r.stderr}")
        out = r.stdout + r.stderr
        self.assertIn(str(8099), out, "拒动时要指出是哪个端口被占")
        self.assertFalse(self.pidfile.exists(), "拒动时不得写 pidfile（那不是我们起的）")


if __name__ == "__main__":
    unittest.main()
