"""OKX **公共只读**行情限流与 429 冷却（第二百四十四刀）。

## 为什么需要它（2026-09-25 实测）

主脑每轮把标的池（10 币）**并行**取数，单币种约 9 次公共请求（ticker / 15m·1H·4H 蜡烛 /
funding / openInterest / ls_ratio / taker_volume / ADX），一轮就是 **约 90 个请求**
几乎同一瞬间发出。而 OKX 公共接口按 IP 分档限频（`market/candles` 40req/2s、
`market/ticker` 与 `public/*` 20req/2s、`rubik/stat/*` 只有 **5req/2s**）
⇒ 批量尾部必然 429；实测最先倒的正是 `okx_ls_ratio` / `okx_taker_volume`（rubik 档）。

更糟的是当时的失败路径**放大**流量：urllib3 对 429 自身重试 2 次 × www/aws 双域
⇒ 一次"逻辑调用"最多打 6 个请求 ⇒ **被限流越狠，请求越猛**（旧注释写"重试吸收突发"，
实际效果相反）。

## 两道闸（只作用于公共只读行情，绝不碰私有交易链路）

1. **间隔**：按路径分档，全局串行发出（持锁睡）—— 把一个突发摊平成节拍，
   每档都落回自己的限额之内（rubik 档按文档限额留余量，但它**实测更紧**：
   真正解决稳态超限靠减少重复取数，见常量表上方注释）；
2. **429 冷却**：某档被限流 ⇒ **该档**静默 N 秒（听 `Retry-After`，默认 10s），
   期间 `acquire()` 直接返回 `False`，调用方落备源 / 本地数学 / 缺省。
   按档冷却（而非全局）是有意的：rubik 被限流**不该**把蜡烛一起饿死 ——
   蜡烛缺失会把 `data_quality` 打成 invalid，直接触发主脑 P0 拦单。

## 边界

- 纯 stdlib、只读时钟；**绝不抛异常**（限流器不能成为新的故障源）；
- 状态是**进程内**的：本机还有别的进程（因子库 / 面板）共享同一出口 IP，跨进程共享
  冷却需要落文件，本轮不做（见改动报告里的遗留风险）；
- `reset()` 只给测试用 —— 模块级状态会跨用例残留。
"""
from __future__ import annotations

import contextlib
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

#: 路径档 → 最小间隔（秒）。按 OKX 文档限额留余量（等效 req/s 降低），
#: 目的是把**一个突发**摊平成节拍，而不是保证任何需求都能被满足。
#: ⚠️ rubik 档 2026-09-25 本机实测：文档写 5req/2s，实测却是“一个短窗内约 5 发之后
#: 就 429”，且违规后要等分钟级才恢复（怀疑同一出口 IP 还有别的进程，或违规罚时）。
#: 所以 0.60s（1.67req/s）只是**不再一瞬间打完**；真正的稳态超限来自**重复取数**：
#: `brain/packages.py`、`factors/smart_money.py`、`factor_library.py` **三个模块各取
#: 同一批 rubik 统计**，而后者每 60s 跑一轮（≈50 发/分钟）—— 已列为后续项（见报告）。
#: 两个 rubik 端点共用本档 ⇒ 单端点实际 ≈0.83req/s，比文档的 2.5req/s 保守一倍。
_INTERVAL_BY_PREFIX: Tuple[Tuple[str, float], ...] = (
    ("/api/v5/rubik/", 0.60),     # 文档 5req/2s 档（实测更紧，最紧；最先 429 的就是它）
    ("/api/v5/market/", 0.12),    # candles 40req/2s、ticker 20req/2s
    ("/api/v5/public/", 0.12),    # funding-rate / open-interest 20req/2s
    ("/api/v5/aigc/", 0.12),      # 指标批量 POST（未见公开限额，按 20req/2s 保守）
)
_DEFAULT_INTERVAL = 0.12
#: 全局地板：所有档**合计** ≤20req/s（防"每档都不超、合起来把 IP 打爆"）。
_GLOBAL_INTERVAL = 0.05
#: 429 冷却：默认 10s；带 `Retry-After` 就听它的（下限 2s、上限 60s）。
_COOLDOWN_DEFAULT = 10.0
_COOLDOWN_MIN = 2.0
_COOLDOWN_MAX = 60.0

_LOCK = threading.Lock()
_LAST: Dict[str, float] = {}
_COOLDOWN_UNTIL: Dict[str, float] = {}
_GLOBAL_KEY = "__global__"


class RateLimited(RuntimeError):
    """该档正处于 429 冷却 ⇒ 本次**没有发出请求**（拿不到数据 = 一次失败）。

    单独一个类型是为了可诊断：日志/指标里能一眼分清“被交易所限流后主动停手”
    与“请求发出去但失败了”（后者是 `MarketDataResponseError` / 传输异常）。
    """


def _pacing_enabled() -> bool:
    """节拍开关（**调用时**读环境，避开 import 顺序陷阱）。

    生产从不设 `R20_PUBLIC_GUARD_PACE` ⇒ 真节拍。测试会话由 `tests/__init__.py` 置 `0`：
    全量万余例会反复走这几条取数路径，真睡 0.12~0.60s 会把套件拖慢几分钟，而且那**不是在
    验证节拍**。节拍本身由 `tests/venues/test_okx_public_guard.py` 用假钟单独钉住
    （那里显式打开）。**冷却不受本开关影响**（它是安全属性，不是性能调节）。
    """
    try:
        raw = str(os.environ.get("R20_PUBLIC_GUARD_PACE", "1")).strip().lower()
        return raw not in ("0", "off", "false", "no")
    except Exception:
        return True


@contextlib.contextmanager
def urlopen(request, timeout: float = 3.0):
    """裸 `urllib.request.urlopen` 的 drop-in 包装：过闸 → 出网 → 429 记账。

    调用方（`brain/packages.py`、`factors/smart_money.py`、`factor_library.py`、
    `news_sentiment_harvester.py`）只需把 `urllib.request.urlopen` 换成本函数，
    其余行一字不动 —— 这几处都各有逐行形挑或行为门钉着，改得越少越安全。

    - 冷却中：抛 `RateLimited`（**不发请求**；调用方的 `except Exception` 照旧留痕）；
    - 429：`note_429` 开该档冷却后**原样上抛**（调用方的 except 语义不变）；
    - 仍然调 `urllib.request.urlopen`（属性查找在调用时）⇒ 既有的
      `patch.object(urllib.request, "urlopen")` 类打桩全部继续生效。

    ⚠️ 边界：本闸的表只认 **OKX 公共路径**。非 OKX 请求（如币安 `fapi.binance.com`）
    不要接进来 —— 它们的限额与档位不在表里，一旦 429 会被记到共享的 `default` 档上。
    """
    url = getattr(request, "full_url", None) or str(request)
    if not acquire(url):
        raise RateLimited(f"429 冷却中，跳过请求: {url}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            yield resp
    except urllib.error.HTTPError as exc:
        if getattr(exc, "code", None) == 429:
            note_429(url, getattr(exc, "headers", None))
        raise


def _path_of(path: str) -> str:
    """把「路径」或「整条 URL」归一到 path。

    ⚠️ 实测踩过的坑（第二百四十四刀）：`urlopen(request)` 传进来的是
    `request.full_url`（`https://www.okx.com/api/v5/...`），不剥 scheme+host 的话
    `startswith("/api/v5/rubik/")` 永远不成立 ⇒ 所有经过 urlopen 的请求都落进 `default`
    档：**rubik 的 0.6s 与它的冷却全部失效**（现场抓到“rubik 429 被记到 default 档”，
    而 default 档又被其他路径共用 ⇒ 一个 rubik 429 会连带掐断 ticker/funding/OI）。
    """
    raw = str(path or "")
    if "://" in raw:
        raw = raw.split("://", 1)[1]
        raw = ("/" + raw.split("/", 1)[1]) if "/" in raw else ""
    return raw.split("?", 1)[0].split("#", 1)[0]


def bucket(path: str) -> str:
    """请求路径（或整条 URL）→ 限流档名（有界，可直接进日志/指标标签，绝不放整条 URL）。"""
    clean = _path_of(path)
    for prefix, _interval in _INTERVAL_BY_PREFIX:
        if clean.startswith(prefix):
            return prefix
    return "default"


def min_interval(path: str) -> float:
    """该档的最小间隔（秒）。未知路径按最保守的默认档。"""
    clean = _path_of(path)
    for prefix, interval in _INTERVAL_BY_PREFIX:
        if clean.startswith(prefix):
            return interval
    return _DEFAULT_INTERVAL


def cooldown_remaining(path: str) -> float:
    """该档剩余冷却秒数（0 = 可以发）。"""
    try:
        with _LOCK:
            return max(0.0, _COOLDOWN_UNTIL.get(bucket(path), 0.0) - time.time())
    except Exception:
        return 0.0


def in_cooldown(path: str) -> bool:
    return cooldown_remaining(path) > 0.0


def acquire(path: str) -> bool:
    """出网前过闸。返回 `False` = **该档正在 429 冷却，别发请求**。

    `True` 表示已占到一个发送时隙（必要时**持锁睡**等间隔 —— 串行化正是限流器要的
    效果：把突发摊平成节拍）。任何内部意外一律放行（`True`），限流器不能因为自己
    出错把整条行情链路掐死。
    """
    try:
        key = bucket(path)
        with _LOCK:
            now = time.time()
            if now < _COOLDOWN_UNTIL.get(key, 0.0):
                return False
            ready_at = max(_LAST.get(key, 0.0) + min_interval(path),
                           _LAST.get(_GLOBAL_KEY, 0.0) + _GLOBAL_INTERVAL)
            if ready_at > now and _pacing_enabled():
                time.sleep(ready_at - now)
            stamp = time.time()
            _LAST[key] = stamp
            _LAST[_GLOBAL_KEY] = stamp
            return True
    except Exception:
        return True


def note_429(path: str, headers: Optional[Any] = None) -> float:
    """记一次 429：开该档冷却并返回冷却秒数（供调用方打日志/上报）。

    `Retry-After` 只认正整数秒（HTTP-date 形式与奇葩值一律忽略后取默认）——
    解析失败绝不猜、也不用它放大冷却。
    """
    seconds = _COOLDOWN_DEFAULT
    try:
        raw = None if headers is None else headers.get("Retry-After")
        if raw is not None:
            try:
                parsed = float(str(raw).strip())
                if parsed > 0:
                    seconds = parsed
            except (TypeError, ValueError):
                seconds = _COOLDOWN_DEFAULT
        seconds = min(max(seconds, _COOLDOWN_MIN), _COOLDOWN_MAX)
        with _LOCK:
            _COOLDOWN_UNTIL[bucket(path)] = time.time() + seconds
        return seconds
    except Exception:
        return 0.0


def reset() -> None:
    """清空节拍与冷却（**只给测试用**；生产路径没有任何调用点）。"""
    with _LOCK:
        _LAST.clear()
        _COOLDOWN_UNTIL.clear()
