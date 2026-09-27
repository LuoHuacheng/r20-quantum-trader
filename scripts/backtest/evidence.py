"""回测的证据化绩效与影子规则报告（规划文档 §12.4 / Task 8）。

## 为什么不能只统计胜率

优化前回测只输出胜率/PF/夏普。规划文档 §12.4 要求至少还要有：毛盈亏、净盈亏、
手续费、资金费、滑点、Profit Factor、最大回撤、平均 R、中位数 R、连续亏损、
按市场状态/策略模式/交易所分组、规则触发次数、**被规则拒绝后本来会产生的结果**、
反例数量。

## 两条纪律

1. **缺数据标记不可验证**：`data_completeness()` 逐字段检查，缺失即列入
   `unverifiable`，绝不静默按 0 计入统计（0 与"读不到"是两件事）。
2. **影子不计实际拦截**：`shadow_rule_report()` 只**计算**规则会不会拒绝，
   不改变任何交易结果（阶段 C 影子硬规则的定义）—— 拒绝样本的盈亏作为
   "如果当时执行了会怎样"的对照。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

try:
    from scripts.strategy_rules import (RULESETS, RULE_SET_BY_MODE, evaluate_strategy_hard_rules,
                                       extract_gate_inputs)
except ImportError:  # pragma: no cover - scripts/ 单独在 sys.path 上
    from strategy_rules import (RULESETS, RULE_SET_BY_MODE, evaluate_strategy_hard_rules,
                               extract_gate_inputs)

__all__ = ["compute_extended_metrics", "data_completeness", "signals_with_evidence_signature",
           "shadow_rule_report"]

#: §12.4 要求逐笔可对账的字段（缺失即不可验证）。
REQUIRED_TRADE_FIELDS = (
    "pnl", "fee", "entry_time", "exit_time", "direction", "entry_price", "exit_price",
)


def data_completeness(trades: Sequence[Dict[str, Any]],
                      required: Iterable[str] = REQUIRED_TRADE_FIELDS) -> Dict[str, Any]:
    """逐字段完整度：返回 `{complete, missing_fields, unverifiable_trades}`。"""
    rows = [t for t in (trades or []) if isinstance(t, dict)]
    missing_fields: Dict[str, int] = {}
    for row in rows:
        for field in required:
            if row.get(field) in (None, ""):
                missing_fields[field] = missing_fields.get(field, 0) + 1
    unverifiable = [i for i, row in enumerate(rows)
                    if any(row.get(f) in (None, "") for f in required)]
    return {"complete": not missing_fields, "missing_fields": missing_fields,
            "unverifiable_trades": unverifiable, "total_trades": len(rows)}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


#: 同一语义在两个域里的字段名不同：台账用 `pnl/fee/funding/slippage_estimate`，
#: 回测 `TradeRecord` 用 `pnl_usd/fee_usd/funding_usd/slippage_usd`。统计层同时认。
_FIELD_ALIASES = {
    "pnl": ("pnl", "pnl_usd", "net_pnl"),
    "gross_pnl": ("gross_pnl", "pnl_gross"),
    "fee": ("fee", "fee_usd", "fees"),
    "funding": ("funding", "funding_usd"),
    "slippage": ("slippage_estimate", "slippage_usd", "slippage"),
}


def _pick(row: Dict[str, Any], key: str, default: Any = None) -> Any:
    for name in _FIELD_ALIASES.get(key, (key,)):
        if row.get(name) is not None:
            return row.get(name)
    return default


def _max_drawdown(equity: List[float]) -> float:
    peak = None
    worst = 0.0
    for value in equity:
        peak = value if peak is None else max(peak, value)
        if peak and peak > 0:
            worst = min(worst, (value - peak) / peak)
    return abs(worst) * 100.0


def _consecutive_losses(trades: Sequence[Dict[str, Any]]) -> int:
    worst = current = 0
    for row in trades:
        if _num(_pick(row, "pnl")) <= 0:
            current += 1
            worst = max(worst, current)
        else:
            current = 0
    return worst


def _group_stats(trades: Sequence[Dict[str, Any]], key: str) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for row in trades:
        bucket = str(row.get(key) or "unknown")
        entry = out.setdefault(bucket, {"trades": 0, "net_pnl": 0.0, "wins": 0, "gross_pnl": 0.0})
        entry["trades"] += 1
        entry["net_pnl"] = round(entry["net_pnl"] + _num(_pick(row, "pnl")), 4)
        entry["gross_pnl"] = round(entry["gross_pnl"] + _num(_pick(row, "gross_pnl", _pick(row, "pnl"))), 4)
        if _num(_pick(row, "pnl")) > 0:
            entry["wins"] += 1
    for entry in out.values():
        entry["win_rate_pct"] = (round(entry["wins"] / entry["trades"] * 100, 2)
                                 if entry["trades"] else 0.0)
    return out


def compute_extended_metrics(trades: Sequence[Dict[str, Any]], *,
                             initial_capital: float = 10000.0,
                             rule_triggers: Optional[Dict[str, int]] = None,
                             rejected_outcomes: Optional[Sequence[Dict[str, Any]]] = None,
                             ) -> Dict[str, Any]:
    """§12.4 的完整绩效面板（纯函数；缺失字段单独披露）。"""
    rows = [t for t in (trades or []) if isinstance(t, dict)]
    gross = sum(_num(_pick(r, "gross_pnl", _pick(r, "pnl"))) for r in rows)
    fees = sum(abs(_num(_pick(r, "fee"))) for r in rows)
    funding = sum(_num(_pick(r, "funding")) for r in rows)
    slippage = sum(_num(_pick(r, "slippage")) for r in rows)
    net = sum(_num(_pick(r, "pnl")) for r in rows)
    wins = [r for r in rows if _num(_pick(r, "pnl")) > 0]
    losses = [r for r in rows if _num(_pick(r, "pnl")) <= 0]
    gross_profit = sum(_num(_pick(r, "pnl")) for r in wins)
    gross_loss = abs(sum(_num(_pick(r, "pnl")) for r in losses))
    r_multiples = sorted(_num(r.get("r_multiple")) for r in rows if r.get("r_multiple") is not None)
    median_r = 0.0
    if r_multiples:
        mid = len(r_multiples) // 2
        median_r = (r_multiples[mid] if len(r_multiples) % 2
                    else (r_multiples[mid - 1] + r_multiples[mid]) / 2)
    equity = [initial_capital]
    for row in rows:
        equity.append(equity[-1] + _num(_pick(row, "pnl")))
    rejected = list(rejected_outcomes or [])
    return {
        "total_trades": len(rows),
        "gross_pnl": round(gross, 4),
        "net_pnl": round(net, 4),
        "fees": round(fees, 4),
        "funding": round(funding, 4),
        "slippage": round(slippage, 4),
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else (
            99.0 if gross_profit > 0 else 0.0),
        "max_drawdown_pct": round(_max_drawdown(equity), 4),
        "avg_r": round(sum(r_multiples) / len(r_multiples), 4) if r_multiples else 0.0,
        "median_r": round(median_r, 4),
        "r_samples": len(r_multiples),
        "max_consecutive_losses": _consecutive_losses(rows),
        "win_rate_pct": round(len(wins) / len(rows) * 100, 2) if rows else 0.0,
        "by_regime": _group_stats(rows, "regime"),
        "by_strategy_mode": _group_stats(rows, "strategy_mode"),
        "by_venue": _group_stats(rows, "venue"),
        "rule_triggers": dict(rule_triggers or {}),
        "rejected_then_outcomes": {
            "count": len(rejected),
            "would_be_net_pnl": round(sum(_num(_pick(r, "pnl")) for r in rejected), 4),
            "would_be_wins": len([r for r in rejected if _num(_pick(r, "pnl")) > 0]),
        },
        "counterexamples": len(losses),
        "data_completeness": data_completeness(rows),
    }


def shadow_rule_report(trades: Sequence[Dict[str, Any]], *,
                       strategy_mode: str = "trend_confirm_5m",
                       ) -> Dict[str, Any]:
    """**影子硬规则**报告（阶段 C）：只计算规则会拒绝哪些交易，不真正拦截。

    - `evaluated`：逐笔给出 `allowed` / `reason` / `setup_kind`；
    - `would_reject`：按原因聚合的触发次数（§12.4「规则触发次数」）；
    - `rejected_then_outcomes`：被拒样本的**实际结果**（如果当时执行了会怎样）；
    - 按市场状态（trend / range）分开统计 —— §16 要求影子报告覆盖趋势与震荡环境。
    """
    rows = [t for t in (trades or []) if isinstance(t, dict)]
    rule_set = RULE_SET_BY_MODE.get(strategy_mode, "legacy@1")
    rules = RULESETS.get(rule_set, {})
    triggers: Dict[str, int] = {}
    evaluated: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    by_regime: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        action = str(row.get("direction") or row.get("action") or "").upper()
        if action in {"LONG", "多"}:
            action = "BUY_LONG"
        elif action in {"SHORT", "空"}:
            action = "SELL_SHORT"
        allowed, reason, setup_kind = evaluate_strategy_hard_rules(
            action=action, strategy_mode=strategy_mode,
            rsi=row.get("rsi_15m", row.get("rsi")), jerk=row.get("jerk_15m", row.get("jerk")),
            setup_kind=row.get("setup_kind"), rules=rules)
        regime = str(row.get("regime") or "unknown")
        bucket = by_regime.setdefault(regime, {"evaluated": 0, "would_reject": 0,
                                              "rejected_net_pnl": 0.0})
        bucket["evaluated"] += 1
        evaluated.append({"inst": row.get("inst") or row.get("symbol"),
                          "action": action, "allowed": allowed,
                          "reason": reason, "setup_kind": setup_kind, "regime": regime})
        if not allowed:
            triggers[reason.split(":")[0]] = triggers.get(reason.split(":")[0], 0) + 1
            rejected.append(row)
            bucket["would_reject"] += 1
            bucket["rejected_net_pnl"] = round(
                bucket["rejected_net_pnl"] + _num(_pick(row, "pnl")), 4)
    return {
        "strategy_mode": strategy_mode,
        "rule_set": rule_set,
        "rule_set_hash": _rule_hash(rule_set),
        "evaluated": evaluated,
        "would_reject": len(rejected),
        "rule_triggers": triggers,
        "rejected_then_outcomes": {
            "count": len(rejected),
            "would_be_net_pnl": round(sum(_num(_pick(r, "pnl")) for r in rejected), 4),
            "would_be_wins": len([r for r in rejected if _num(_pick(r, "pnl")) > 0]),
            "would_be_losses": len([r for r in rejected if _num(_pick(r, "pnl")) <= 0]),
        },
        "by_regime": by_regime,
        # §16：影子报告必须覆盖趋势与震荡两类环境。本仓 regime 词表用
        # BULL_*/BEAR_* 表示趋势、RANGE_*/CHOP 表示震荡，故按词表判定。
        "covers_trend_and_range": _covers_trend_and_range(by_regime),
    }


def _covers_trend_and_range(by_regime: dict) -> bool:
    names = " ".join(by_regime).upper()
    has_trend = any(token in names for token in ("TREND", "BULL", "BEAR"))
    has_range = any(token in names for token in ("RANGE", "CHOP"))
    return has_trend and has_range


def _rule_hash(rule_set: str) -> str:
    try:
        try:
            from scripts.strategy_rules import rule_set_hash
        except ImportError:  # pragma: no cover
            from strategy_rules import rule_set_hash
        return rule_set_hash(rule_set)
    except Exception:
        return ""


def signals_with_evidence_signature(signals: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """回测信号补充策略证据签名（供 `extract_gate_inputs` 复用）。"""
    out = []
    for row in signals or []:
        item = dict(row) if isinstance(row, dict) else {}
        gate = extract_gate_inputs(item, item, {"strategy_mode": item.get("strategy_mode")})
        item.setdefault("rsi_15m", gate.get("rsi_15m"))
        item.setdefault("jerk_15m", gate.get("jerk"))
        out.append(item)
    return out
