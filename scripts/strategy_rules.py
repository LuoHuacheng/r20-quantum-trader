"""版本化策略硬规则注册表（规划文档 §5.1 / §5.2）。

本模块是 **L1 策略硬规则**的唯一落点：只保存版本化规则与纯函数 ——
不读网络、不读 LLM、不写文件（配置由调用方显式传入）。

## 为什么必须存在

优化前：RSI/Jerk/保本这些"规则"只活在复盘文档与长期记忆提示词里，
模型可以（并且确实会）把它们当成建议改写，执行层没有任何确定性门禁。
优化后：硬规则由**代码**执行，模型只能提交提案（`reviewed_heuristic` 上限）。

## setup_kind 不能靠名字猜（§5.2）

`RSI > 75 禁止任何多单` 是错的：它会误伤独立的均值回归多单。
门禁必须按 `setup_kind` 区分：

```text
trend_following_long / trend_following_short : 启用极值追价 + 反向冲击门禁
mean_reversion_long  / mean_reversion_short  : 不使用同一门禁
unknown                                      : fail-closed（拒绝开仓）
```

策略模式 → setup 家族的映射是**显式**的（`MODE_SETUP_FAMILY`），
5m 趋势确认策略必须显式声明为 `trend_following`，不允许用名字/提示词推断。

## 数值口径（§2.4 / §5.5）

`TREND_FOLLOWING_RULES` 里的数字是**目标配置示例**，实施时必须先过回测与策略审核；
本仓当前冻结的口径见 `DOCUMENTED_METRICS`（杠杆/止损/保本各自写明是
历史观察、当前配置还是目标参数），提示词与台账都从这里取值，不得混写。

保本语义选择 **ATR 口径**（`breakeven_mode="ATR_MULTIPLE"`）：本仓
`position_exit.BREAKEVEN_LOCK_ATR = 0.8` 是 2026-09-24 经实盘证据下调后的现行配置，
改成 R 口径等于在无回测证据的情况下修改真实交易参数（规划文档"不包含"清单明确禁止）。
R 口径所需的 `initial_risk_px` 仍逐笔记录，供回测与后续切换评估使用。
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Dict, Optional, Tuple

__all__ = [
    "DEFAULT_EXECUTION_POLICY", "DOCUMENTED_METRICS", "LEGACY_RULES",
    "MODE_SETUP_FAMILY", "RULE_SET_BY_MODE", "RULESETS", "SETUP_KINDS",
    "StrategyRuleError", "TREND_FOLLOWING_RULES", "active_execution_policy",
    "breakeven_trigger_px", "breakeven_trigger_px_atr", "evaluate_strategy_hard_rules",
    "extract_gate_inputs", "final_order_intent_check", "verify_order_intent_now",
    "render_host_hard_rules_block",
    "reject_extreme_chase", "reject_reverse_shock", "resolve_execution_policy",
    "rule_set_hash", "setup_kind_for", "strategy_rule_version",
]

#: 趋势追随规则集（`trend_following@1`）。改动必须同时改 version —— 版本号是
#: 决策快照与回测报告的一部分，静默改数字等于伪造历史。
TREND_FOLLOWING_RULES: Dict[str, Any] = {
    "version": "trend_following@1",
    "gates_enabled": True,
    "rsi_long_chase_ceiling": 75.0,
    "rsi_short_chase_floor": 28.0,
    "reverse_jerk_abs_floor": 2.5,
    "breakeven_mode": "ATR_MULTIPLE",
    "breakeven_trigger_atr": 0.8,
    "breakeven_trigger_r": 0.8,
    "stop_loss_atr_mult": 2.0,
}

#: 旧模式规则集：**不新增任何门禁**，行为与历史一致（§5.4「旧 legacy 模式继续走旧规则」）。
LEGACY_RULES: Dict[str, Any] = {
    "version": "legacy@1",
    "gates_enabled": False,
    "breakeven_mode": "ATR_MULTIPLE",
    "breakeven_trigger_atr": 0.8,
    "breakeven_trigger_r": 0.8,
    "stop_loss_atr_mult": 2.0,
}

RULESETS: Dict[str, Dict[str, Any]] = {
    TREND_FOLLOWING_RULES["version"]: TREND_FOLLOWING_RULES,
    LEGACY_RULES["version"]: LEGACY_RULES,
}

#: 策略模式 → setup 家族。缺字段的旧 profile 解释为 legacy（§7.1）。
MODE_SETUP_FAMILY: Dict[str, str] = {
    "legacy": "legacy",
    "trend_confirm_5m": "trend_following",
    "trend_following": "trend_following",
    "mean_reversion": "mean_reversion",
}

#: 策略模式 → 默认规则集。
RULE_SET_BY_MODE: Dict[str, str] = {
    "legacy": LEGACY_RULES["version"],
    "trend_confirm_5m": TREND_FOLLOWING_RULES["version"],
    "trend_following": TREND_FOLLOWING_RULES["version"],
    "mean_reversion": TREND_FOLLOWING_RULES["version"],
}

DEFAULT_EXECUTION_POLICY: Dict[str, Any] = {
    "mode": "legacy",
    "revision": 1,
    "rule_set": LEGACY_RULES["version"],
}

SETUP_KINDS = (
    "trend_following_long",
    "trend_following_short",
    "mean_reversion_long",
    "mean_reversion_short",
    "unknown",
)

#: 数值口径说明书（§2.4）：每个数字必须标明它是什么，禁止混写。
DOCUMENTED_METRICS = {
    "leverage": {
        "semantics": "AI 在风控页配置区间 [MIN_LEVERAGE, MAX_LEVERAGE] 内自主裁决，执行层强制钳制",
        "kind": "CURRENT_CONFIG",
    },
    "stop_loss": {
        "semantics": "initial_stop = 入场价 ∓ ATR × stop_loss_atr_mult（本仓执行层使用 15M ATR，回测须显式注明周期）",
        "kind": "CURRENT_CONFIG",
    },
    "breakeven": {
        "semantics": "峰值浮盈 >= breakeven_trigger_atr × ATR 时把止损推到保本（ATR 口径，不是 R 口径）",
        "kind": "CURRENT_CONFIG",
    },
    "rsi_extreme": {
        "semantics": "趋势追随多单 15M RSI > 75 拒绝追价；趋势追随空单 15M RSI < 28 拒绝追空；均值回归不受此门禁约束",
        "kind": "TARGET_STRATEGY_PARAMETER",
    },
    "reverse_jerk": {
        "semantics": "趋势追随开仓时，反向 |Jerk| >= 2.5 拒绝入场；缺失时 fail-closed",
        "kind": "TARGET_STRATEGY_PARAMETER",
    },
    "asset_multiplier": {
        "semantics": "只缩放模型申请的保证金（margin_usdt），不改变杠杆/止损/熔断/风险常量",
        "kind": "CURRENT_CONFIG",
    },
}


class StrategyRuleError(ValueError):
    """规则集/策略模式不可用时抛错；调用方必须 fail-closed（拒绝新开仓）。"""


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def rule_set_hash(rule_set: str) -> str:
    """规则集内容的稳定 hash（进决策快照/台账/回测报告）。"""
    rules = RULESETS.get(str(rule_set))
    if rules is None:
        raise StrategyRuleError(f"未知规则集: {rule_set}")
    return hashlib.sha256(_canonical(rules).encode("utf-8")).hexdigest()[:16]


def strategy_rule_version(rule_set: str) -> str:
    rules = RULESETS.get(str(rule_set))
    if rules is None:
        raise StrategyRuleError(f"未知规则集: {rule_set}")
    return str(rules["version"])


def resolve_execution_policy(profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """从 prompt profile 解析执行策略（§7.1）。

    - 缺字段的旧 profile ⇒ `legacy`（向后兼容，不改变现有交易行为）；
    - 未知 mode / revision / rule_set ⇒ 抛错（调用方 fail-closed 拒绝激活）。
    """
    raw = (profile or {}).get("execution_policy") if isinstance(profile, dict) else None
    if not isinstance(raw, dict) or not raw:
        return copy.deepcopy(DEFAULT_EXECUTION_POLICY)
    mode = str(raw.get("mode") or "legacy").strip()
    if mode not in MODE_SETUP_FAMILY:
        raise StrategyRuleError(f"未知执行模式: {mode}")
    try:
        revision = int(raw.get("revision", 1))
    except (TypeError, ValueError) as exc:
        raise StrategyRuleError(f"非法 revision: {raw.get('revision')!r}") from exc
    if revision < 1:
        raise StrategyRuleError(f"非法 revision: {revision}")
    rule_set = str(raw.get("rule_set") or f"{mode}@{revision}").strip()
    if rule_set not in RULESETS:
        raise StrategyRuleError(f"未知规则集: {rule_set}")
    return {"mode": mode, "revision": revision, "rule_set": rule_set}


def active_execution_policy() -> Dict[str, Any]:
    """读取**当前激活 profile** 的执行策略（规划文档 §7.1）。

    - 旧 profile 缺字段 ⇒ `legacy@1`（行为不变）；
    - 未知 mode / rule_set ⇒ 抛 `StrategyRuleError`（调用方 fail-closed 拒绝新开仓）。
    """
    profile: Optional[Dict[str, Any]] = None
    try:
        from scripts.prompt_library import active_profile as _active_profile
    except ImportError:  # pragma: no cover - scripts/ 单独在 sys.path 上时
        from prompt_library import active_profile as _active_profile
    profile = _active_profile()
    return resolve_execution_policy(profile)


def setup_kind_for(strategy_mode: str, action: str, *, explicit: Optional[str] = None) -> str:
    """把「策略模式 + 方向」确定性翻译成 setup_kind（§5.2）。

    `explicit`（模型声明的 setup_kind）只能**确认**策略模式给出的家族/方向；
    跨家族声明（例如 trend_confirm_5m 下自称 mean_reversion）一律降级为
    `unknown` ⇒ 上游 fail-closed，杜绝"声明一个不受门禁约束的 kind 来绕过"。
    """
    act = str(action or "").upper()
    direction = "long" if act == "BUY_LONG" else ("short" if act == "SELL_SHORT" else "")
    family = MODE_SETUP_FAMILY.get(str(strategy_mode or "legacy"))
    if family is None or family == "legacy" or not direction:
        return "unknown"
    expected = f"{family}_{direction}"
    if explicit:
        return expected if str(explicit).strip() == expected else "unknown"
    return expected


def _rules_for(setup_kind: str, rules: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return TREND_FOLLOWING_RULES if rules is None else rules


def reject_extreme_chase(*, action: str, rsi: Optional[float], setup_kind: str,
                         rules: Dict[str, Any]) -> Tuple[bool, str]:
    """极值追价门禁。返回 `(是否放行, 中文原因)`。

    - 仅 `trend_following_*` 受约束；均值回归显式豁免（§5.2）；
    - 边界是**严格**的：RSI=75.0 放行、75.01 拒绝；RSI=28.0 放行、27.99 拒绝；
    - 未知 setup_kind ⇒ 拒绝（fail-closed）；
    - RSI 不可解析 ⇒ 拒绝（缺数据不得当成"没触发"）。
    """
    if not rules.get("gates_enabled", False):
        return True, ""
    if setup_kind == "unknown":
        return False, "策略 setup_kind 未知，按 fail-closed 拒绝新开仓"
    if setup_kind not in ("trend_following_long", "trend_following_short"):
        return True, ""
    try:
        value = float(rsi)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False, "RSI 缺失或非法，趋势追随门禁 fail-closed 拒绝开仓"
    if setup_kind == "trend_following_long":
        ceiling = float(rules.get("rsi_long_chase_ceiling", 75.0))
        if value > ceiling:
            return False, f"RSI 极值追多拦截: 15M RSI {value:g} > {ceiling:g}"
        return True, ""
    floor = float(rules.get("rsi_short_chase_floor", 28.0))
    if value < floor:
        return False, f"RSI 极值追空拦截: 15M RSI {value:g} < {floor:g}"
    return True, ""


def reject_reverse_shock(*, action: str, jerk: Optional[float],
                         rules: Dict[str, Any]) -> Tuple[bool, str]:
    """反向动能冲击门禁：反向 |Jerk| >= 阈值拒绝入场。

    - jerk 与开仓方向一致（多头 jerk>=0 / 空头 jerk<=0）⇒ 放行；
    - jerk 缺失/非法 ⇒ 拒绝（fail-closed：缺证据不等于没风险）。
    """
    if not rules.get("gates_enabled", False):
        return True, ""
    try:
        value = float(jerk)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False, "反向 Jerk 缺失或非法，动能门禁 fail-closed 拒绝开仓"
    floor = float(rules.get("reverse_jerk_abs_floor", 2.5))
    act = str(action or "").upper()
    is_long = act == "BUY_LONG"
    reverse = (value < 0) if is_long else (value > 0)
    if reverse and abs(value) >= floor:
        return False, (f"反向动能冲击拦截: |Jerk| {abs(value):g} >= {floor:g}，"
                       f"方向与{'多' if is_long else '空'}单相反")
    return True, ""


def breakeven_trigger_px(*, is_long: bool, entry_px: float, initial_stop_px: float,
                         trigger_r: float) -> float:
    """R 口径保本触发价：`initial_risk_px = |entry - initial_stop|`。

    ⚠️ 当前生产口径是 ATR（见 `breakeven_trigger_px_atr`）；本函数供回测/评估使用，
    两种口径**不得**混用（调用方必须显式选择）。
    """
    entry = float(entry_px)
    risk = abs(float(entry_px) - float(initial_stop_px))
    offset = risk * float(trigger_r)
    return entry + offset if is_long else entry - offset


def breakeven_trigger_px_atr(*, is_long: bool, entry_px: float, atr: float,
                             atr_mult: float) -> float:
    """ATR 口径保本触发价（当前生效口径）。"""
    entry = float(entry_px)
    offset = abs(float(atr)) * float(atr_mult)
    return entry + offset if is_long else entry - offset


def _first_number(payload: Any) -> Optional[float]:
    if isinstance(payload, (int, float)) and not isinstance(payload, bool):
        return float(payload)
    if isinstance(payload, str) and payload.strip():
        try:
            return float(payload)
        except ValueError:
            return None
    return None


def _dig(payload: Any, *path: str) -> Any:
    node = payload
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def extract_gate_inputs(package: Optional[Dict[str, Any]],
                        decision: Optional[Dict[str, Any]],
                        context: Optional[Dict[str, Any]] = None,
                        ) -> Dict[str, Any]:
    """从提示词包 / 决策 / 上下文里**确定性**取门禁输入（不做名字推断）。

    取值优先级固定，缺失时返回 None（由门禁 fail-closed）：

    - `strategy_mode`：context → package → decision，缺省 `legacy`；
    - `rsi_15m`：decision → package（本仓 `rsi` 就是 15M RSI，`rsi_15m` 是它的显式别名）；
    - `jerk`：decision 的 15M 字段 → package 的 15M 字段 → calculus 多周期 15M →
      聚合 `max_abs_jerk`（并返回 `jerk_source` 供台账/审计区分口径）。
    """
    pkg = package if isinstance(package, dict) else {}
    dec = decision if isinstance(decision, dict) else {}
    ctx = context if isinstance(context, dict) else {}

    strategy_mode = (ctx.get("strategy_mode") or pkg.get("strategy_mode")
                     or dec.get("strategy_mode") or "legacy")
    rsi = _first_number(dec.get("rsi_15m"))
    if rsi is None:
        rsi = _first_number(pkg.get("rsi_15m"))
    if rsi is None:
        rsi = _first_number(pkg.get("rsi"))

    jerk = _first_number(dec.get("jerk_15m"))
    jerk_source = "decision.jerk_15m" if jerk is not None else ""
    if jerk is None:
        jerk = _first_number(pkg.get("jerk_15m"))
        if jerk is not None:
            jerk_source = "package.jerk_15m"
    if jerk is None:
        timeframes = _dig(pkg, "calculus", "timeframes")
        if isinstance(timeframes, dict):
            for key in ("15M", "15m"):
                jerk = _first_number(_dig(timeframes.get(key), "jerk"))
                if jerk is not None:
                    jerk_source = f"calculus.timeframes.{key}.jerk"
                    break
    if jerk is None:
        jerk = _first_number(_dig(pkg, "calculus", "max_abs_jerk"))
        if jerk is not None:
            jerk_source = "calculus.max_abs_jerk(aggregate)"

    return {"strategy_mode": str(strategy_mode), "rsi_15m": rsi, "jerk": jerk,
            "jerk_source": jerk_source,
            "setup_kind": dec.get("setup_kind")}


def final_order_intent_check(venue_ctx: Optional[Dict[str, Any]],
                             current_rule_set_hash: Optional[str] = None,
                             ) -> Tuple[bool, str]:
    """下单前最终复验（§5.3 第 7 步 / §5.4）。

    - 非 AI 信号（无 `execution_policy`）⇒ 不归策略门禁管，放行；
    - 意图里声明的 `rule_set_hash` 与当前读到的规则不一致 ⇒ 拒绝（周期中途改规则
      不得影响已生成的订单意图）；
    - 策略门禁结果标记为阻断 ⇒ 拒绝。
    """
    ctx = venue_ctx if isinstance(venue_ctx, dict) else {}
    policy = ctx.get("execution_policy")
    if not isinstance(policy, dict) or not policy:
        return True, ""
    verdict = ctx.get("strategy_gate") if isinstance(ctx.get("strategy_gate"), dict) else {}
    if verdict.get("allowed") is False:
        return False, f"策略硬规则拦截（下单前复验）: {verdict.get('reason') or '未通过'}"
    declared = str(policy.get("rule_set_hash") or verdict.get("rule_set_hash") or "")
    if current_rule_set_hash:
        if not declared:
            return False, "策略规则 hash 缺失，fail-closed 拒绝下单"
        if declared != current_rule_set_hash:
            return False, ("策略规则集在本周期内发生变化 "
                           f"({declared} != {current_rule_set_hash})，拒绝本单")
    return True, ""


def verify_order_intent_now(venue_ctx: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    """下单前**重新读取**当前策略 hash 并复验意图（§5.3 第 7 步 / §5.4）。

    - 非 AI 信号（无 execution_policy）⇒ 不归策略门禁管，放行；
    - 规则集不可读 ⇒ fail-closed（拒绝新开仓，宁可不发）；
    - 意图冻结的 hash 与当前 hash 不同 ⇒ 拒绝（周期中途换规则不得影响已生成的意图）。
    """
    ctx = venue_ctx if isinstance(venue_ctx, dict) else {}
    policy = ctx.get("execution_policy")
    if not isinstance(policy, dict) or not policy:
        return True, ""
    try:
        current = active_execution_policy()
        current_hash = rule_set_hash(current["rule_set"])
    except Exception as exc:
        return False, f"策略规则读取失败，fail-closed 拒绝下单: {exc}"
    return final_order_intent_check(ctx, current_rule_set_hash=current_hash)


def render_host_hard_rules_block() -> str:
    """交易提示词用的**【宿主硬规则】区块**（规划文档 §6.3）。

    区块内容全部来自**代码事实**：当前策略模式/规则版本/规则集 hash/风险配置 hash/
    baseline hash，以及数值口径说明。模型不得修改，也不靠提示词自称。

    读取失败一律写字面 `unavailable`（不臆造），键名保持稳定供前端/grep 断言。
    """
    try:
        policy = active_execution_policy()
        rule_set = policy["rule_set"]
        rule_hash = rule_set_hash(rule_set)
        rule_version = strategy_rule_version(rule_set)
        mode = policy["mode"]
    except Exception:
        rule_set, rule_hash, rule_version, mode = "unavailable", "", "", "legacy"
    try:
        try:
            from scripts import evolution_shield as _shield
        except ImportError:  # pragma: no cover
            import evolution_shield as _shield
        baseline_hash = _shield.baseline_manifest_hash()
    except Exception:
        baseline_hash = ""
    try:
        from r20_backend.risk_config import current_values as _risk_values
        risk_hash = hashlib.sha256(
            json.dumps(_risk_values(), sort_keys=True, separators=(",", ":"),
                       default=str).encode("utf-8")).hexdigest()[:16]
    except Exception:
        risk_hash = ""
    return (
        "======================= 【宿主硬规则（代码与冻结策略版本，模型不得修改）】 =======================\n"
        "- 数据缺失时一律 WAIT\n"
        f"- 当前策略模式: {mode} | 策略规则版本: {rule_set}\n"
        f"- RSI/Jerk 规则集版本: {rule_version} | rule_set_hash: {rule_hash}\n"
        f"- 当前风险配置 hash: {risk_hash or 'unavailable'}\n"
        f"- 当前 baseline hash: {baseline_hash or 'unavailable'}\n"
        "- 数值口径：初始止损=入场价∓ATR×倍数；保本=峰值浮盈≥ATR×倍数 时移损"
        "（现行 ATR 口径，**不是** R 口径）；资产乘数只缩放模型申请的保证金，"
        "不改变杠杆/止损/熔断/置信度门槛。\n"
    )


def evaluate_strategy_hard_rules(*, action: str, strategy_mode: str,
                                 rsi: Optional[float] = None, jerk: Optional[float] = None,
                                 setup_kind: Optional[str] = None,
                                 rules: Optional[Dict[str, Any]] = None,
                                 ) -> Tuple[bool, str, str]:
    """统一入口：`(是否放行, 人类原因, setup_kind)`。

    调用顺序（对应 §5.3 核心拦截顺序的第 3 步）：

    1. 未知策略模式 ⇒ fail-closed（拒绝新开仓）——模式是代码事实，不能由提示词命名推断；
    2. `legacy@1` ⇒ 不新增任何门禁（旧模式行为不变）；
    3. 未知 setup_kind ⇒ fail-closed；
    4. `trend_following_*` ⇒ 极值追价门禁 + 反向冲击门禁；
    5. `mean_reversion_*` ⇒ **不**适用上述两条门禁（避免误伤独立策略）。

    规则集读取失败时由调用方捕获 `StrategyRuleError` 并 fail-closed。
    """
    act = str(action or "").upper()
    if act not in {"BUY_LONG", "SELL_SHORT"}:
        return True, "", "unknown"
    mode = str(strategy_mode or "legacy")
    family = MODE_SETUP_FAMILY.get(mode)
    if family is None:
        return False, f"未知策略模式 {mode}，fail-closed 拒绝新开仓", "unknown"
    active = rules if rules is not None else RULESETS[RULE_SET_BY_MODE[mode]]
    resolved = setup_kind_for(mode, act, explicit=setup_kind)
    if not active.get("gates_enabled", False):
        return True, "", resolved
    if resolved == "unknown":
        return False, "策略 setup_kind 未知，按 fail-closed 拒绝新开仓", resolved
    ok, reason = reject_extreme_chase(action=act, rsi=rsi, setup_kind=resolved, rules=active)
    if not ok:
        return False, reason, resolved
    if resolved.startswith("trend_following"):
        # 反向冲击门禁只对趋势追随生效；均值回归显式豁免（§5.2）。
        ok, reason = reject_reverse_shock(action=act, jerk=jerk, rules=active)
        if not ok:
            return False, reason, resolved
    return True, "", resolved
