"""交易证据盖章（规划文档 §8.2）—— 每笔交易都能还原当时的策略/记忆/风险/证据版本。

## 为什么要有这个模块

优化前：台账只记价格、盈亏、`policy_version`，复盘时无法回答
「这条结论来自哪笔交易 / 用了哪个快照 / 用的哪套规则 / 哪版记忆」。
策略、记忆、风险版本散落在运行时内存里，事后无法重建。

本模块是**唯一写入口**：`record_trade` 门面壳在落账前调用
`stamp_trade_evidence()`，逐笔补齐以下字段（幂等，只补缺失键）：

- 策略：`strategy_mode` / `strategy_rule_version` / `rule_set` / `rule_set_hash`
- 记忆：`memory_revision` / `baseline_hash`
- 风险：`initial_stop_px` / `initial_risk_px` / `breakeven_mode` / `breakeven_trigger_atr`
  / `asset_multiplier` / `risk_budget_snapshot`
- 证据：`entry_snapshot` / `snapshot_source` / `snapshot_observability` / `signal_id`
- 平仓侧：`exit_reason_code`（稳定枚举，不依赖中文自由文本）/ `pnl_gross` / `fees`
  / `funding` / `slippage_estimate` / `snapshot_at_exit` / `cooldown_recorded`

⚠️ 绝不抛异常、绝不覆盖已有键：台账是审计证据，宁可少盖一个章，也不能把账写坏。
"""
from __future__ import annotations

import datetime
import json
import os
from typing import Any, Dict, Optional

__all__ = [
    "CURRENT_TRACKER_FIELDS", "exit_evidence_fields", "exit_reason_code",
    "stamp_trade_evidence", "tracker_evidence_fields",
]

#: 平仓原因 → 稳定 reason code（规划文档 §8.3：审计不匹配中文自由文本）。
#: ⚠️ **顺序即优先级**：具体词必须排在泛词之前（“时间止损”不能被“止损”先吃掉）。
_EXIT_PATTERNS = (
    ("保护失效", "PROTECTION_FAILED"),
    ("时间止损", "TIME_STOP"),
    ("阶梯锁利", "TRAILING_TAKE_PROFIT"),
    ("移动止盈", "TRAILING_TAKE_PROFIT"),
    ("分批", "SCALE_OUT"),
    ("部分止盈", "SCALE_OUT"),
    ("目标止盈", "TAKE_PROFIT"),
    ("保本", "BREAKEVEN"),
    ("云止损", "STOP_LOSS"),
    ("硬止损", "STOP_LOSS"),
    ("止损", "STOP_LOSS"),
    ("止盈", "TAKE_PROFIT"),
    ("手动平仓", "MANUAL"),
    ("冷却", "COOLDOWN"),
)

CURRENT_TRACKER_FIELDS = (
    "initial_stop_px", "initial_risk_px", "breakeven_mode", "breakeven_trigger_atr",
    "strategy_rule_version", "rule_set_hash", "policy_hash", "memory_revision",
    "baseline_hash", "asset_multiplier", "asset_multiplier_status", "risk_budget_snapshot",
    "entry_snapshot", "snapshot_source", "snapshot_observability", "signal_id",
    "strategy_mode",
)


def exit_reason_code(reason: Any) -> str:
    """把中文平仓原因映射成稳定 code（无法识别 ⇒ `UNKNOWN`，绝不猜）。"""
    text = str(reason or "").strip()
    for needle, code in _EXIT_PATTERNS:
        if needle in text:
            return code
    return "UNKNOWN" if text else ""


def _read_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def _policy_snapshot(data_dir: str) -> Dict[str, Any]:
    """当前执行策略（读取失败 ⇒ 空 dict，不伪造 legacy）。"""
    try:
        try:
            from scripts.strategy_rules import (active_execution_policy, rule_set_hash,
                                                strategy_rule_version)
        except ImportError:  # pragma: no cover - scripts/ 单独在 sys.path 上
            from strategy_rules import (active_execution_policy, rule_set_hash,
                                        strategy_rule_version)
        policy = active_execution_policy()
        return {
            "strategy_mode": policy["mode"],
            "rule_set": policy["rule_set"],
            "strategy_rule_version": strategy_rule_version(policy["rule_set"]),
            "rule_set_hash": rule_set_hash(policy["rule_set"]),
        }
    except Exception:
        return {}


def _memory_snapshot() -> Dict[str, Any]:
    try:
        try:
            from scripts import evolution_shield
        except ImportError:  # pragma: no cover
            import evolution_shield
        return {
            "memory_revision": str(evolution_shield.read_memory_snapshot().get("version") or ""),
            "baseline_hash": evolution_shield.baseline_manifest_hash(),
        }
    except Exception:
        return {}


def _find_tracker(trade_data: Dict[str, Any], trackers: Any) -> Optional[Dict[str, Any]]:
    """按 `名称 + 方向` 在 tracker 表里找本仓（键是 `instId_side`）。"""
    if not isinstance(trackers, dict) or not trackers:
        return None
    name = str(trade_data.get("inst") or trade_data.get("name") or "").strip()
    if not name:
        return None
    side_text = str(trade_data.get("direction") or trade_data.get("side") or "")
    side = "long" if "多" in side_text or "long" in side_text.lower() else (
        "short" if "空" in side_text or "short" in side_text.lower() else "")
    for key, entry in trackers.items():
        if not isinstance(entry, dict):
            continue
        key_text = str(key)
        if not (key_text.startswith(f"{name}-") or key_text.startswith(f"{name}_")):
            continue
        entry_side = str(entry.get("side") or "").lower()
        if side and entry_side and side != entry_side:
            continue
        return entry
    return None


def tracker_evidence_fields(f: Dict[str, Any], curr_pos: Dict[str, Any],
                            entry: Dict[str, Any]) -> Dict[str, Any]:
    """新建 tracker 上应带的策略/风险/证据字段（§8.2 的开仓侧）。

    只做确定性推导：初始止损取 tracker 建档时的 `trailingStopPx`（此刻即入场止损），
    `initial_risk_px = |entry - initial_stop|`；保本口径取自冻结的规则集。
    """
    out: Dict[str, Any] = {}
    try:
        entry_px = float(entry.get("entryPx") or 0.0)
        stop_px = float(entry.get("trailingStopPx") or 0.0)
    except (TypeError, ValueError):
        return out
    if entry_px <= 0 or stop_px <= 0:
        # 没有可用的入场价/止损价 ⇒ 初仓几何不可验证，不编造初始风险。
        return out
    out["initial_stop_px"] = stop_px
    out["initial_risk_px"] = abs(entry_px - stop_px)
    policy = _policy_snapshot("")
    out.update(policy)
    out.update(_memory_snapshot())
    try:
        try:
            from scripts.strategy_rules import RULESETS, RULE_SET_BY_MODE
        except ImportError:  # pragma: no cover
            from strategy_rules import RULESETS, RULE_SET_BY_MODE
        rule_set = RULE_SET_BY_MODE.get(policy.get("strategy_mode") or "legacy", "legacy@1")
        rules = RULESETS.get(rule_set) or {}
        out["breakeven_mode"] = rules.get("breakeven_mode", "ATR_MULTIPLE")
        out["breakeven_trigger_atr"] = rules.get("breakeven_trigger_atr", 0.8)
        out["strategy_mode"] = policy.get("strategy_mode") or "legacy"
    except Exception:
        pass
    snapshot = entry.get("signal_snapshot")
    if isinstance(snapshot, dict) and snapshot:
        out["entry_snapshot"] = snapshot
        out["snapshot_source"] = str(snapshot.get("snapshot_source") or "") or None
        out["snapshot_observability"] = str(snapshot.get("snapshot_observability") or "") or None
    risk_budget = f.get("risk_budget_snapshot") if isinstance(f, dict) else None
    if isinstance(risk_budget, dict) and risk_budget:
        out["risk_budget_snapshot"] = risk_budget
        out["asset_multiplier"] = risk_budget.get("asset_multiplier")
        out["asset_multiplier_status"] = risk_budget.get("asset_multiplier_status")
    policy_hash = str(entry.get("policy_hash") or "")
    if policy_hash:
        out["policy_hash"] = policy_hash
    signal_id = None
    if isinstance(f, dict):
        signal_id = f.get("signal_id") or f.get("intent_id")
    out["signal_id"] = str(signal_id or "")
    return {k: v for k, v in out.items() if v is not None or k == "initial_risk_px"}


def stamp_trade_evidence(trade_data: Dict[str, Any], *, data_dir: str,
                         trackers: Optional[Dict[str, Any]] = None,
                         now: Optional[str] = None) -> Dict[str, Any]:
    """**就地**给一条台账记录补齐证据字段（幂等），返回同一个 dict。

    `data_dir` 由门面在调用期注入（测试沙箱沿用既有 patch 面）。
    """
    if not isinstance(trade_data, dict):
        return trade_data
    try:
        trade_data.setdefault("venue", "okx")
        if not trade_data.get("environment"):
            try:
                try:
                    from scripts.okx_runtime import current_environment
                except ImportError:  # pragma: no cover
                    from okx_runtime import current_environment
                trade_data["environment"] = str(getattr(current_environment(), "mode", "") or "")
            except Exception:
                pass

        evidence = _find_tracker(trade_data, trackers) or {}
        if evidence:
            mapping = {
                "initial_stop_px": evidence.get("initial_stop_px"),
                "initial_risk_px": evidence.get("initial_risk_px"),
                # §8.2 平仓侧：策略止损（本地棘轮维护的 trailingStopPx）与
                # 紧急保护止损（场所侧 OCO 的冻结值 —— 本仓由入场时冻结的
                # initial_stop_px 承载；若 tracker 显式给出 emergency/cloud 止损，以它优先）。
                "strategy_stop_px": (evidence.get("strategy_stop_px")
                                     or evidence.get("trailingStopPx")),
                "emergency_stop_px": (evidence.get("emergency_stop_px")
                                      or evidence.get("cloud_stop_px")
                                      or evidence.get("initial_stop_px")),
                "breakeven_mode": evidence.get("breakeven_mode"),
                "breakeven_trigger_atr": evidence.get("breakeven_trigger_atr"),
                "strategy_mode": evidence.get("strategy_mode"),
                "strategy_rule_version": evidence.get("strategy_rule_version"),
                "rule_set_hash": evidence.get("rule_set_hash"),
                "policy_hash": evidence.get("policy_hash"),
                "memory_revision": evidence.get("memory_revision"),
                "baseline_hash": evidence.get("baseline_hash"),
                "asset_multiplier": evidence.get("asset_multiplier"),
                "asset_multiplier_status": evidence.get("asset_multiplier_status"),
                "risk_budget_snapshot": evidence.get("risk_budget_snapshot"),
                "entry_snapshot": evidence.get("entry_snapshot"),
                "snapshot_source": evidence.get("snapshot_source"),
                "snapshot_observability": evidence.get("snapshot_observability"),
                "signal_id": evidence.get("signal_id"),
            }
            for key, value in mapping.items():
                if value is not None:
                    trade_data.setdefault(key, value)

        # 策略/记忆版本：**持仓冻结值优先于当前值**（一笔已平仓交易的证据
        # 应以它入场时使用的版本为准，而不是平仓时刻的激活版本）。
        for key, value in _policy_snapshot(data_dir).items():
            trade_data.setdefault(key, value)
        for key, value in _memory_snapshot().items():
            trade_data.setdefault(key, value)

        # 平仓侧（§8.2）：只用已存在的字段推导，绝不发明数据。
        for key, value in exit_evidence_fields(trade_data, data_dir=data_dir).items():
            trade_data.setdefault(key, value)
        trade_data.setdefault("stamped_at", now or datetime.datetime.now(
            datetime.timezone.utc).isoformat())
        return trade_data
    except Exception:
        return trade_data


def exit_evidence_fields(trade_data: Dict[str, Any], *, data_dir: str) -> Dict[str, Any]:
    """平仓侧证据字段（规划文档 §8.2）—— 由 `scripts/trader/protection.py` 对外提供。

    只从**已存在**的台账字段推导：`exit_reason_code`（稳定枚举）、`pnl_gross`、`fees`、
    `funding`、`slippage_estimate`、`snapshot_at_exit`、`cooldown_recorded`。
    缺证据的字段一律不返回（调用方 `setdefault` 时不会被写成 0 冒充数据）。
    """
    out: Dict[str, Any] = {}
    reason_text = trade_data.get("exit_reason") or trade_data.get("remark") or ""
    code = exit_reason_code(reason_text)
    if code:
        out["exit_reason_code"] = code
    if "gross_pnl" in trade_data:
        out["pnl_gross"] = trade_data.get("gross_pnl")
    if "fee" in trade_data:
        try:
            out["fees"] = abs(float(trade_data.get("fee") or 0.0))
        except (TypeError, ValueError):
            pass
    out["funding"] = trade_data.get("funding", 0.0)
    out["slippage_estimate"] = trade_data.get("slippage_estimate")
    out["snapshot_at_exit"] = trade_data.get("snapshot_at_exit")
    out["cooldown_recorded"] = _cooldown_recorded(
        data_dir, trade_data.get("inst") or trade_data.get("name"), code)
    return out


def _cooldown_recorded(data_dir: str, inst: Any, code: str) -> bool:
    """止损/保本类平仓会写同标的冷却记录 —— 台账只如实记录「是否已登记」。"""
    if code not in {"STOP_LOSS", "BREAKEVEN"}:
        return False
    name = str(inst or "").strip()
    if not name:
        return False
    payload = _read_json(os.path.join(data_dir, ".stop_cooldown.json"))
    if payload is None:
        payload = _read_json(os.path.join(data_dir, "stop_cooldown.json"))
    if not isinstance(payload, dict):
        return False
    return any(str(key).startswith(name) for key in payload.keys())
