"""OKX 账户模式预检：只回答一件事 —— **这个账户现在能不能下合约单**。

## 为什么要单独有这一层

2026-09-22 实盘事故：OKX demo 账户被切回 `acctLv=1`（简单/现货模式），而执行层
恒发 `instId=*-USDT-SWAP` + `tdMode=cross` 的永续单 ⇒ OKX 每单回

    51010 You can't complete this request under your current account mode

同一时刻 `venue_execution_ready()` 只校「凭证齐 + 环境一致」就判 okx 就绪，
于是均衡路由（sha256 哈希轮换）每轮仍把 BTC/SOL/XRP/SUI 派给 OKX，**每 15 分钟
白烧一单**；OKX 侧 `orders-pending`/`orders-history`/`positions` 长期全 0，
而日志里只有一行 `OKX 51010`，看不出根因。

## 判据

- `acctLv` ∈ {2 单币种保证金, 3 跨币种保证金, 4 组合保证金} → 支持合约；
- `acctLv == 1`（简单/现货模式）→ **确证不支持**，摘除本所执行资格；
  这条有实盘证据（上例），故为**硬闸**；
- `acctLv` 字段缺失/探测失败 → **fail-open**（判为可交易）：本层防的是「确证模式
  错 → 每轮白烧单」，不是网络探活闸 —— 读配置失败绝不能让交易停摆；
- `posMode` 与执行层 `posSide` 策略不符只 **warn**（见下）。

## 为什么 posMode 只告警不拦

执行层恒发 `posSide=long/short`（`scripts/trader/order_intent.py`），账户若是
`net_mode` 语义上不匹配；但「net_mode + posSide=long 是否真被 OKX 拒」**尚无实测
证据**（事故时 `acctLv` 与 `posMode` 两个变量是混淆的）。拿未验证的假设当闸门
= 有误杀真交易的风险，故先告警、留证据，待实测后再决定是否升为硬闸。

## 一条反直觉的坑（2026-09-22 实测）

**不要用 `/api/v5/trade/order-precheck` 当探针**：本账户对它连**现货**单都回
`51010`，切账户模式前后无差别 —— 拿它自检只会得到「永远不可交易」的假信号，
拿它"复现故障"也会把结论带偏。唯一可信的读数是 `/api/v5/account/config`。

## 使用

    from scripts.okx_account_mode import account_mode_ready
    if not account_mode_ready(current_environment()):
        ...  # 摘除本所执行资格（路由会把 executable=False 的候选硬筛掉）

命令行：`python -m scripts.okx_account_mode` 打印当前账户模式判定。
"""
from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Callable, Dict, Optional, Tuple

#: 账户模式的唯一权威读数（私有签名 GET）。
ACCOUNT_CONFIG_PATH = "/api/v5/account/config"

#: 支持衍生品（FUTURES/SWAP）的账户模式：2 单币种保证金 / 3 跨币种保证金 / 4 组合保证金。
#: 1 = 简单交易模式（现货）——**不支持合约**，即 2026-09-22 每单 51010 的根因。
DERIVATIVES_ACCT_LV = frozenset({"2", "3", "4"})

#: 执行层期望的持仓模式（执行层恒发 posSide=long/short ⇒ 需双向持仓）。
EXECUTOR_POS_MODE = "long_short_mode"

#: 账户模式是慢变量（人工切换）。TTL 缓存避免「每个候选所 × 每个标的」都打一次私有端点。
DEFAULT_TTL_S = 300.0

#: 探测超时独立于下单超时：读配置卡住不该拖慢整轮巡检。
DEFAULT_TIMEOUT_S = 5.0

_CACHE: Dict[str, Tuple[float, Optional[Dict[str, str]]]] = {}
_LOGGED: Dict[str, str] = {}


def _env_key(env: Any) -> str:
    """缓存键：与凭证身份绑定（换 API Key = 换账户，缓存必须失效）。"""
    identity = str(getattr(env, "identity", "") or "").strip()
    if identity:
        return identity
    if env is None:
        try:
            from scripts.okx_runtime import current_environment

            return str(current_environment().identity)
        except Exception:
            return "unknown"
    return f"{getattr(env, 'mode', '')}:{str(getattr(env, 'api_key', ''))[:8]}"


def _fetch_account_config(env: Any, timeout: float) -> Optional[Dict[str, str]]:
    """读 `/api/v5/account/config`。**任何失败 → None**（调用方 fail-open）。"""
    try:
        from scripts import okx_rest

        rows = okx_rest.request("GET", ACCOUNT_CONFIG_PATH, env=env, timeout=timeout)
    except Exception:
        return None
    row = rows[0] if rows and isinstance(rows[0], Mapping) else None
    if not isinstance(row, Mapping):
        return None
    return {
        "acctLv": str(row.get("acctLv") or "").strip(),
        "posMode": str(row.get("posMode") or "").strip().lower(),
    }


def read_account_mode(
    env: Any = None,
    *,
    ttl: float = DEFAULT_TTL_S,
    now: Optional[float] = None,
    fetch: Optional[Callable[[Any, float], Optional[Dict[str, str]]]] = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Optional[Dict[str, str]]:
    """账户模式 `{"acctLv", "posMode"}`，带 TTL 缓存（**失败也缓存**，防逐标的重复出网）。

    返回 None 的语义是「**未知**」，不是「不可交易」—— 判为不可交易是调用方的事。
    """
    key = _env_key(env)
    stamp = time.monotonic() if now is None else float(now)
    hit = _CACHE.get(key)
    if hit is not None and (stamp - hit[0]) < float(ttl):
        return hit[1]
    probe = fetch or _fetch_account_config
    try:
        info = probe(env, timeout)
    except Exception:
        info = None
    if not isinstance(info, Mapping):
        info = None
    normalized = dict(info) if info else None
    _CACHE[key] = (stamp, normalized)
    return normalized


def _log_once(key: str, verdict: str, message: str) -> None:
    """同一状态只吼一次：巡检每 15 分钟一轮，逐轮刷屏等于没告警。"""
    if _LOGGED.get(key) == verdict:
        return
    _LOGGED[key] = verdict
    if message:
        print(message)


def account_mode_ready(
    env: Any = None,
    *,
    ttl: float = DEFAULT_TTL_S,
    now: Optional[float] = None,
    fetch: Optional[Callable[[Any, float], Optional[Dict[str, str]]]] = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> bool:
    """False = **确证**账户模式不支持合约单（调用方应摘除该所执行资格）。

    探测失败/字段缺失 → True（fail-open，见模块 docstring 的理由）。
    """
    info = read_account_mode(env, ttl=ttl, now=now, fetch=fetch, timeout=timeout)
    key = _env_key(env)
    if info is None:
        _log_once(
            key,
            "unknown",
            "[账户模式] warn OKX 账户模式读取失败（网络/凭证/格式），按可交易处理"
            "（fail-open：读配置失败不得让交易停摆）",
        )
        return True

    acct_lv = str(info.get("acctLv") or "").strip()
    if acct_lv and acct_lv not in DERIVATIVES_ACCT_LV:
        _log_once(
            key,
            f"level:{acct_lv}",
            f"[账户模式] 摘除 OKX 执行资格：acctLv={acct_lv}（1=简单/现货模式）"
            f"不支持 SWAP 合约单，任何合约下单都会被拒 51010。"
            f"请在 OKX 模拟盘把账户切到「单币种/跨币种保证金」，或关闭 OKX 执行闸。",
        )
        return False

    pos_mode = str(info.get("posMode") or "").strip().lower()
    if pos_mode and pos_mode != EXECUTOR_POS_MODE:
        _log_once(
            key,
            f"posmode:{pos_mode}",
            f"[账户模式] warn OKX posMode={pos_mode} 与执行层恒发 posSide=long/short "
            f"不符（期望 {EXECUTOR_POS_MODE}）；单向持仓下是否被拒尚无实测证据，先放行并告警。",
        )
    else:
        _log_once(key, "ready", "")
    return True


def reset_cache() -> None:
    """清空缓存与告警去重状态（账户切换后 / 测试隔离）。"""
    _CACHE.clear()
    _LOGGED.clear()


def main() -> int:
    """`python -m scripts.okx_account_mode`：打印当前账户模式与判定。"""
    from scripts.okx_runtime import current_environment

    env = current_environment()
    info = read_account_mode(env)
    ready = account_mode_ready(env)
    print(f"OKX 环境：{env.mode}（configured={env.configured}）")
    print(f"账户模式：{info if info else '读取失败（fail-open）'}")
    print("判定：", "可下合约单" if ready else "不可下合约单（已摘除本所执行资格）")
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
