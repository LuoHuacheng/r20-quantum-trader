#!/usr/bin/env python3
"""R20 Quantum Trader · 服务控制台（一套命令，两套后端）。

用户口径（2026-09）：把「起 / 停 / 重启」做成系统服务命令 —— ``r20-start``、
``r20-stop``、``r20-restart``、``r20-status``、``r20-logs``，外加
``r20-install`` / ``r20-uninstall`` 装卸服务。

| 平台 | 服务后端 | 单元 |
|---|---|---|
| macOS (darwin) | launchd **LaunchAgent**（用户级，免 sudo，登录自启 + KeepAlive 自愈） | ``com.r20.trader`` |
| Linux | systemd（复用既有 ``deploy/*.service``） | ``r20-quantum`` |

## 为什么本机只起**一个**服务

前端 dist 由后端 ``serve_vue_spa`` 下发；网关 worker 由后端内的
``r20_gateway.supervisor`` 托管（见 ``r20_backend/app.py``）。多起一份网关会在
``r20_gateway/worker.py`` 撞锁自退（``gateway worker already running; exiting``），
故服务面 = 后端单进程，与 systemd 侧的三单元拆分不冲突（那边由 systemd 各自托管）。

## 安装位置（免 sudo）

- plist → ``~/Library/LaunchAgents/com.r20.trader.plist``
- 命令 → ``~/.local/bin/r20-{start,stop,restart,status,logs,install,uninstall}``
  （软链到本脚本，靠 **argv[0]** 派发子命令，因此 ``r20-stop`` 无需再带参数）

环境变量可覆盖（测试与多机部署用）：``R20_ROOT`` / ``R20_FORCE_PLATFORM`` /
``R20_UID`` / ``R20_PORT`` / ``R20_LABEL`` / ``R20_UNIT`` /
``R20_LAUNCH_AGENT_DIR`` / ``R20_BIN_DIR`` / ``R20_NO_SUDO``。

``--dry-run`` 只打印**将要执行的命令**，不落盘、不调用服务管理器
（自动化测试靠它钉命令序列，绝不允许测试真的停掉生产服务）。
"""
from __future__ import annotations

import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

COMMANDS = ("start", "stop", "restart", "status", "logs", "install", "uninstall")
DEFAULT_LABEL = "com.r20.trader"
DEFAULT_UNIT = "r20-quantum"
DEFAULT_PORT = 8080
DEFAULT_LINES = 50


# ── 环境解析（全部可注入，测试据此封闭）──────────────────────────────
def repo_root() -> Path:
    forced = os.environ.get("R20_ROOT")
    return Path(forced).expanduser().resolve() if forced else Path(__file__).resolve().parents[1]


def platform() -> str:
    forced = str(os.environ.get("R20_FORCE_PLATFORM") or "").strip().lower()
    if forced in ("darwin", "linux"):
        return forced
    return "darwin" if sys.platform == "darwin" else "linux"


def uid() -> str:
    return str(os.environ.get("R20_UID") or os.getuid())


def label() -> str:
    return str(os.environ.get("R20_LABEL") or DEFAULT_LABEL)


def unit() -> str:
    return str(os.environ.get("R20_UNIT") or DEFAULT_UNIT)


def port() -> int:
    return int(os.environ.get("R20_PORT") or DEFAULT_PORT)


def launch_agent_dir() -> Path:
    forced = os.environ.get("R20_LAUNCH_AGENT_DIR")
    return Path(forced).expanduser() if forced else Path.home() / "Library" / "LaunchAgents"


def bin_dir() -> Path:
    forced = os.environ.get("R20_BIN_DIR")
    return Path(forced).expanduser() if forced else Path.home() / ".local" / "bin"


def plist_path() -> Path:
    return launch_agent_dir() / f"{label()}.plist"


def template_path() -> Path:
    return repo_root() / "deploy" / "macos" / "com.r20.trader.plist.tmpl"


def log_file() -> Path:
    forced = os.environ.get("R20_LOG_FILE")
    return Path(forced).expanduser() if forced else repo_root() / "logs" / "r20_backend.log"


def backend_mode() -> str:
    """auto（默认）｜launchd（强制，失败即报错，绝不偷偷回落）｜manual（pidfile supervisor）。"""
    forced = str(os.environ.get("R20_FORCE_BACKEND") or "auto").strip().lower()
    return forced if forced in ("auto", "launchd", "manual") else "auto"


def pidfile() -> Path:
    forced = os.environ.get("R20_PIDFILE")
    return Path(forced).expanduser() if forced else repo_root() / "data" / "r20_manual.pid"


def launch_verify_seconds() -> float:
    try:
        return float(os.environ.get("R20_LAUNCH_VERIFY_SECONDS") or 10.0)
    except ValueError:
        return 10.0


def start_argv() -> list:
    """manual 模式要 spawn 的命令。测试用 R20_START_CMD 换成 /bin/sleep，不碰真进程。"""
    raw = os.environ.get("R20_START_CMD")
    if raw:
        return shlex.split(raw)
    return [_python_bin(), "-m", "uvicorn", "r20_backend.app:app",
            "--host", "0.0.0.0", "--port", str(port())]


def _bin(name: str) -> str:
    """服务管理器可执行文件的**执行期**替换位（dry-run 仍打印逻辑名，断言才稳定）。

    测试把 R20_LAUNCHCTL_BIN 指到 /bin/false，就能在不碰真实 launchd 的前提下
    验证「launchd 起不来 → 回落 manual」这条路。
    """
    return str(os.environ.get(f"R20_{name.upper()}_BIN") or name)


def _read_manual_pid():
    try:
        return int(pidfile().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True


def service_target() -> str:
    return f"gui/{uid()}/{label()}"


def _sudo() -> list[str]:
    """Linux 系统级单元需要 root；已在 root 或显式关掉时不前置 sudo。"""
    if platform() == "linux" and os.geteuid() != 0 and os.environ.get("R20_NO_SUDO") != "1":
        return ["sudo"]
    return []


def _python_bin() -> str:
    venv = repo_root() / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


# ── 输出与执行 ────────────────────────────────────────────────────
def _echo(cmd) -> None:
    print(shlex.join([str(x) for x in cmd]))


def _run(cmd) -> int:
    if os.environ.get("R20_QUIET") != "1":
        _echo(cmd)
    real = [_bin(str(cmd[0]))] + [str(x) for x in cmd[1:]]
    return subprocess.run(real).returncode


def _wait_port_open(seconds: float) -> bool:
    deadline = time.time() + max(0.0, seconds)
    while True:
        if _port_open():
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.5)


def _launchctl(*args) -> list:
    return [_bin("launchctl")] + [str(a) for a in args]


def _fail(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 1


# ── 计划（dry-run 与真跑共用同一份命令序列，避免两套真相）────────────
def _darwin_plan(sub: str, lines: int):
    if sub in ("start", "restart"):
        return [["launchctl", "kickstart", "-k", service_target()]]
    if sub == "stop":
        return [["launchctl", "bootout", service_target()]]
    if sub == "status":
        return [["launchctl", "print", service_target()],
                ["GET", f"http://127.0.0.1:{port()}/api/all"]]
    if sub == "logs":
        return [["tail", "-n", str(lines), str(log_file())]]
    if sub == "install":
        cmds = [["install", "-m", "0644", str(template_path()), str(plist_path())]]
        cmds += [["ln", "-sf", str(repo_root() / "deploy" / "r20ctl.py"), str(bin_dir() / c)]
                 for c in _link_names()]
        cmds.append(["launchctl", "bootstrap", f"gui/{uid()}", str(plist_path())])
        return cmds
    if sub == "uninstall":
        cmds = [["launchctl", "bootout", service_target()],
                ["rm", "-f", str(plist_path())]]
        cmds += [["rm", "-f", str(bin_dir() / c)] for c in _link_names()]
        return cmds
    return []


def _linux_plan(sub: str, lines: int):
    s = _sudo()
    if sub in ("start", "stop", "restart", "status"):
        return [s + ["systemctl", sub, unit()]]
    if sub == "logs":
        return [s + ["journalctl", "-u", unit(), "-n", str(lines), "--no-pager"]]
    if sub == "install":
        return [s + ["systemctl", "enable", "--now", unit()]]
    if sub == "uninstall":
        return [s + ["systemctl", "disable", "--now", unit()]]
    return []


def plan(sub: str, lines: int = DEFAULT_LINES):
    return _darwin_plan(sub, lines) if platform() == "darwin" else _linux_plan(sub, lines)


def _link_names() -> tuple:
    return tuple(f"r20-{c}" for c in COMMANDS)


# ── 各子命令 ──────────────────────────────────────────────────────
def manual_start(dry: bool, restart: bool = False) -> int:
    """pidfile supervisor 起服务（方案 c：不依赖 launchd 也能 start/stop/restart）。

    为什么需要它：本机仓库在 ``~/Documents``（macOS TCC 保护目录），launchd 子进程
    连 ``chdir`` 进去都被拒，job 以 ``EX_CONFIG(78)`` 在 exec 之前就死掉（实测 runs=72、
    日志零输出）。此时命令不该变成哑炮：改由本进程 ``setsid`` 起一个**脱离**的子进程并记 pid。

    取舍说清：这不是系统服务（没有登录自启），但 start/stop/restart/status/logs 全都真能用。
    """
    argv = start_argv()
    if dry:
        print("[backend] manual（pidfile supervisor，非 launchd 托管）")
        print(f"[pidfile] {pidfile()}")
        print(f"[spawn] {shlex.join(argv)}   # detached，日志 {log_file()}")
        return 0

    if restart:
        manual_stop(dry=False)

    running = _read_manual_pid()
    if _alive(running) and not restart:
        print(f"ℹ️  已在运行（manual pid {running}）")
        return 0
    if running and not _alive(running):
        print(f"ℹ️  陈旧 pidfile（pid {running} 不存在），按未运行处理")

    pidfile().parent.mkdir(parents=True, exist_ok=True)
    log_file().parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_file(), "ab")
    proc = subprocess.Popen(argv, cwd=str(repo_root()), stdout=handle,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            start_new_session=True)
    pidfile().write_text(str(proc.pid), encoding="utf-8")
    print(f"✅ 已启动（manual supervisor，pid {proc.pid}，日志 {log_file()}）")
    return 0


def manual_stop(dry: bool) -> int:
    """停 pidfile supervisor：SIGTERM → 最多 20s → SIGKILL；清理 pidfile。

    网关 worker 由后端内的 supervisor 托管（uvicorn 优雅退出时会带走它），故此处
    **不直接动网关 pidfile** —— 那条路会越过进程所有权边界，也会让测试碰到生产网关。
    """
    pid = _read_manual_pid()
    if dry:
        print("[backend] manual（pidfile supervisor，非 launchd 托管）")
        print(f"[pidfile] {pidfile()}")
        print(f"[stop] kill -TERM {pid if pid else '<无进程>'}")
        return 0
    if not pid:
        print("ℹ️  manual supervisor 未运行（无 pidfile）")
        return 0
    if not _alive(pid):
        pidfile().unlink(missing_ok=True)
        print(f"ℹ️  pidfile 陈旧（pid {pid} 不存在），已清理")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pidfile().unlink(missing_ok=True)
        return 0
    for _ in range(200):
        if not _alive(pid):
            break
        time.sleep(0.1)
    if _alive(pid):
        print(f"⚠️  pid {pid} 未在 20s 内退出，改用 SIGKILL")
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        time.sleep(0.3)
    pidfile().unlink(missing_ok=True)
    print(f"✅ 已停止（manual pid {pid}）")
    return 0


def cmd_start(sub: str, dry: bool, lines: int) -> int:
    """start / restart 的**后端派发**：manual 直走；auto（macOS）先试 launchd，
    验不通就摘掉崩溃循环的 agent 并回落 manual（而不是留一个哑炮命令）。"""
    mode = backend_mode()
    if mode == "manual":
        return manual_start(dry, restart=(sub == "restart"))
    if mode == "auto" and platform() == "darwin":
        if dry:
            print(f"[backend] auto：先试 launchd；{launch_verify_seconds():g}s 内未监听则回落 manual")
            print(f"[pidfile] {pidfile()}")
            for c in plan(sub, lines):
                _echo(c)
            print(f"[fallback] manual：spawn {shlex.join(start_argv())}")
            return 0
        rc = _start_service_manager(sub, False, lines)
        if rc == 0 and _wait_port_open(launch_verify_seconds()):
            return 0
        print(f"⚠️  launchd 未能在 {launch_verify_seconds():g}s 内监听端口 {port()}")
        print(f"    摘掉崩溃循环的 agent（{service_target()}），回落 manual supervisor")
        subprocess.run(_launchctl("bootout", service_target()),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return manual_start(False, restart=(sub == "restart"))
    return _start_service_manager(sub, dry, lines)


def _start_service_manager(sub: str, dry: bool, lines: int) -> int:
    """start / restart 的服务管理器路径（launchd / systemd）。未安装必须明确拒动并指路。"""
    if platform() == "darwin":
        if not plist_path().exists():
            return _fail(
                f"[r20ctl] LaunchAgent 未安装：{plist_path()} 不存在。\n"
                f"          先跑：r20-install（或 {repo_root()}/deploy/r20ctl.py install）")
        if dry:
            print(f"[precheck] plist ok: {plist_path()}")
            for c in plan(sub, lines):
                _echo(c)
            return 0
        # 已加载时 kickstart -k 即为重启；未加载则先 bootstrap
        loaded = subprocess.run(
            _launchctl("print", service_target()),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if not loaded:
            rc = _run(_launchctl("bootstrap", f"gui/{uid()}", str(plist_path())))
            if rc != 0:
                return _fail(f"[r20ctl] bootstrap 失败（rc={rc}）；检查 plist 与日志")
        rc = _run(_launchctl("kickstart", "-k", service_target()))
        if rc == 0:
            print(f"✅ R20 已{'重启' if sub == 'restart' else '启动'}（launchd {label()}，日志 {log_file()}）")
        return rc

    # Linux：systemd 既有单元
    verb = "restart" if sub == "restart" else "start"
    if dry:
        for c in plan(verb, lines):
            _echo(c)
        return 0
    rc = _run(_sudo() + ["systemctl", verb, unit()])
    if rc == 0:
        print(f"✅ R20 已{'重启' if verb == 'restart' else '启动'}（systemd {unit()}）")
    return rc


def cmd_stop(dry: bool, lines: int) -> int:
    """stop 的**后端派发**：manual 优先（有 pid 就走 pidfile 收停），否则交给服务管理器。"""
    mode = backend_mode()
    if mode == "manual":
        return manual_stop(dry)
    if mode == "auto" and platform() == "darwin" and _alive(_read_manual_pid()):
        return manual_stop(dry)
    return _stop_service_manager(dry, lines)


def _stop_service_manager(dry: bool, lines: int) -> int:
    if dry:
        for c in plan("stop", lines):
            _echo(c)
        return 0
    if platform() == "darwin":
        loaded = subprocess.run(
            _launchctl("print", service_target()),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if not loaded:
            print("ℹ️  服务本就未运行（launchd 无此 agent）")
            return 0
    rc = _run(plan("stop", lines)[0])
    if rc == 0:
        print("✅ R20 已停止（不再自动拉起；重启用 r20-start）")
    return rc


def cmd_status(dry: bool, lines: int) -> int:
    if dry:
        for c in plan("status", lines):
            _echo(c)
        return 0
    mode = backend_mode()
    manual_pid = _read_manual_pid() if mode != "launchd" else None
    if mode == "manual" or (mode == "auto" and _alive(manual_pid)):
        print(f"── backend：manual（pidfile supervisor，pid {manual_pid or '未运行'}，"
              f"pidfile {pidfile()}）──")
    elif platform() == "darwin":
        print(f"── backend：launchd（{service_target()}）──")
        subprocess.run(_launchctl("print", service_target()))
    else:
        print(f"── backend：systemd（{unit()}）──")
        subprocess.run(_sudo() + [_bin("systemctl"), "status", unit(), "--no-pager"])
    alive = _port_open()
    print(f"── 端口 {port()}：{'LISTEN' if alive else '未监听'} ──")
    body = _probe_api()
    print(f"── /api/all：{body} ──")
    return 0 if alive else 1


def cmd_logs(dry: bool, lines: int) -> int:
    if dry:
        for c in plan("logs", lines):
            _echo(c)
        return 0
    if platform() == "darwin":
        return _run(["tail", "-n", str(lines), str(log_file())])
    return _run(_sudo() + ["journalctl", "-u", unit(), "-n", str(lines), "--no-pager"])


def cmd_install(dry: bool, lines: int) -> int:
    """渲染 plist + 建命令软链 + 交给服务管理器托管（macOS）；Linux 走 systemd enable。"""
    if platform() != "darwin":
        if dry:
            for c in plan("install", lines):
                _echo(c)
            return 0
        return _run(_sudo() + ["systemctl", "enable", "--now", unit()])

    if dry:
        print(f"[precheck] root ok: {repo_root()}")
        print(f"[precheck] python: {_python_bin()}")
        print(f"[mkdir] {launch_agent_dir()}  {bin_dir()}")
        print(f"[render] {template_path()} → {plist_path()}")
        for c in plan("install", lines):
            _echo(c)
        return 0

    tmpl = template_path()
    if not tmpl.exists():
        return _fail(f"[r20ctl] 模板缺失：{tmpl}")
    rendered = (tmpl.read_text(encoding="utf-8")
                .replace("__PYTHON__", _python_bin())
                .replace("__ROOT__", str(repo_root()))
                .replace("__LOG__", str(log_file())))
    launch_agent_dir().mkdir(parents=True, exist_ok=True)
    bin_dir().mkdir(parents=True, exist_ok=True)
    log_file().parent.mkdir(parents=True, exist_ok=True)
    plist_path().write_text(rendered, encoding="utf-8")
    print(f"✅ plist → {plist_path()}")

    ctl = repo_root() / "deploy" / "r20ctl.py"
    ctl.chmod(0o755)
    for name in _link_names():
        link = bin_dir() / name
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(ctl)
        print(f"✅ 命令 → {link}")

    # 已加载则先卸载，保证是**新** plist 生效（否则 bootstrap 会报 already bootstrapped）
    subprocess.run(["launchctl", "bootout", service_target()],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rc = _run(["launchctl", "bootstrap", f"gui/{uid()}", str(plist_path())])
    if rc != 0:
        return _fail("[r20ctl] bootstrap 失败；检查 plist 语法（plutil -lint）与 logs/")
    print(f"✅ 服务已托管（RunAtLoad + KeepAlive）｜r20-status 查看，r20-logs 看日志")
    return 0


def cmd_uninstall(dry: bool, lines: int) -> int:
    if dry:
        for c in plan("uninstall", lines):
            _echo(c)
        return 0
    if platform() == "darwin":
        subprocess.run(["launchctl", "bootout", service_target()],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        plist_path().unlink(missing_ok=True)
        for name in _link_names():
            (bin_dir() / name).unlink(missing_ok=True)
        print("✅ 已卸载（plist 与命令软链已移除；data/ 与 logs/ 原样保留）")
        return 0
    return _run(_sudo() + ["systemctl", "disable", "--now", unit()])


# ── 探活（真跑时才走网络）──────────────────────────────────────────
def _port_open() -> bool:
    with socket.socket() as s:
        s.settimeout(1.5)
        return s.connect_ex(("127.0.0.1", port())) == 0


def _probe_api() -> str:
    try:
        import urllib.request
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port()}/api/all", timeout=8) as resp:
            return f"HTTP {resp.status}（{len(resp.read())} bytes）"
    except Exception as exc:  # noqa: BLE001 —— 探活失败就是探活失败
        return f"不可用：{str(exc)[:120]}"


# ── 入口与 argv[0] 派发 ────────────────────────────────────────────
def usage() -> str:
    subs = "\n".join(f"  r20-{c}" for c in COMMANDS)
    return ("R20 Quantum Trader 服务控制台\n\n"
            f"用法：r20 <{'|'.join(COMMANDS)}> [--dry-run] [-n N]\n"
            "     或直接用命令软链（r20-stop＝stop），无需再带子命令。\n\n"
            f"子命令：\n{subs}\n")


def _resolve_sub(argv: list) -> tuple:
    """返回 (sub, rest)。显式子命令优先于 argv[0]（软链名）。"""
    rest = [a for a in argv]
    if rest and rest[0] in COMMANDS:
        return rest[0], rest[1:]
    base = os.path.basename(sys.argv[0])
    if base.endswith(".py"):
        base = base[:-3]
    if base.startswith("r20-") and base[4:] in COMMANDS:
        return base[4:], rest
    if base in COMMANDS:
        return base, rest
    return None, rest


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(a in ("-h", "--help") for a in argv):
        print(usage())
        return 0
    dry = "--dry-run" in argv
    lines = DEFAULT_LINES
    args: list = []
    it = iter(argv)
    for a in it:
        if a == "--dry-run":
            continue
        if a in ("-n", "--lines"):
            try:
                lines = int(next(it))
            except StopIteration:
                return _fail("[r20ctl] -n/--lines 需要一个整数")
            continue
        args.append(a)

    sub, _rest = _resolve_sub(args)
    if sub is None:
        bad = args[0] if args else ""
        if bad:
            # 用法错误按惯例用退出码 2（区别于「执行失败」的 1），便于脚本区分
            print(f"[r20ctl] 未知子命令：{bad}\n\n{usage()}", file=sys.stderr)
            return 2
        print(usage(), file=sys.stderr)
        return 2

    if sub in ("start", "restart"):
        return cmd_start(sub, dry, lines)
    if sub == "stop":
        return cmd_stop(dry, lines)
    if sub == "status":
        return cmd_status(dry, lines)
    if sub == "logs":
        return cmd_logs(dry, lines)
    if sub == "install":
        return cmd_install(dry, lines)
    return cmd_uninstall(dry, lines)


if __name__ == "__main__":
    raise SystemExit(main())
