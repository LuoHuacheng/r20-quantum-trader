# deploy/

## 服务命令（一套命令，两套后端）

``deploy/r20ctl.py`` 是唯一实现；``r20-install`` 会把它软链成下面这些命令
（macOS 默认装到 ``~/.local/bin``，已在 PATH，免 sudo）：

| 命令 | 作用 |
|---|---|
| ``r20-start`` | 起服务（已运行则不动，不双起） |
| ``r20-stop`` | 停服务（SIGTERM 优雅退，最多等 20s 再 SIGKILL） |
| ``r20-restart`` | 换代码后重启（换新进程，收掉旧的） |
| ``r20-status`` | 后端身份 + pid + 端口 + 真打一次 ``/api/all``（未监听则退出码 1） |
| ``r20-logs [n]`` | tail 后端日志（默认 50 行） |
| ``r20-install`` / ``r20-uninstall`` | 装卸服务托管 |

| 平台 | 后端 | 单元 |
|---|---|---|
| macOS | launchd **LaunchAgent**（``deploy/macos/com.r20.trader.plist.tmpl`` 渲染到 ``~/Library/LaunchAgents/``），用户级免 sudo，``RunAtLoad`` + ``KeepAlive`` | ``com.r20.trader`` |
| Linux | systemd（复用 ``deploy/*.service``） | ``r20-quantum`` |

只起**一个**后端进程：前端 dist 由后端 ``serve_vue_spa`` 下发；网关 worker 由后端内的
``r20_gateway.supervisor`` 托管（多起一份会在 worker 里撞锁自退）。

## 后端选择

``R20_FORCE_BACKEND=auto|launchd|manual``（默认 ``auto``）：

- ``manual``：pidfile supervisor（``setsid`` 脱离 + ``data/r20_manual.pid``），不依赖服务管理器；
- ``launchd``：强制服务管理器，失败即明确报错（**不会**偷偷改道）；
- ``auto``（macOS）：先试 launchd，``R20_LAUNCH_VERIFY_SECONDS``（默认 10s）内未监听端口
  就摘掉 agent 并回落 manual —— 命令永远不是哑炮。

## ⚠️ macOS 已知限制：仓库在 TCC 保护目录

``~/Documents``、``~/Desktop``、``~/Downloads`` 是 macOS TCC 保护目录。launchd 起的子进程
连 ``chdir`` 进 ``~/Documents/...`` 都被静默拒绝，job 以 ``EX_CONFIG(78)`` 在 **exec 之前**就死掉
（实测 ``runs=72``、日志**零输出**）。两条出路：

1. 系统设置 → 隐私与安全性 → 完全磁盘访问权限，加入 ``.venv/bin/python3.12`；
2. 把仓库搬到 ``~/Documents`` 之外（如 ``~/r20-quantum-trader``）后重跑 ``r20-install``。

在解决之前，``r20-start`` 会自动回落 manual supervisor：命令全都能用，只是**没有登录自启**。

## 环境变量（测试与多机部署）

``R20_ROOT`` / ``R20_FORCE_PLATFORM`` / ``R20_FORCE_BACKEND`` / ``R20_UID`` / ``R20_PORT`` /
``R20_LABEL`` / ``R20_UNIT`` / ``R20_LAUNCH_AGENT_DIR`` / ``R20_BIN_DIR`` / ``R20_PIDFILE`` /
``R20_LOG_FILE`` / ``R20_START_CMD`` / ``R20_LAUNCH_VERIFY_SECONDS`` / ``R20_NO_SUDO`` /
``R20_LAUNCHCTL_BIN``（执行期替换位，测试用它避免碰真实 launchd）。

``--dry-run`` 只打印**将要执行的命令**，不落盘、不调服务管理器、不起进程 ——
契约测试靠它钉命令序列（``tests/ops/test_r20ctl.py``）。
