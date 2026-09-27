"""自进化复盘的**上下文装配**（`scripts/evolution/` 部件，从门面搬出）。

| 函数 | 职责 |
|---|---|
| `summarize_closed_trades` | 平仓统计汇总（笔数/胜负/胜率/净利/手续费）+ 数理快照可观测性审计摘要 |
| `build_host_constitution` | **宿主宪章**文本：代码层硬约束，profile 只能调措辞风格，永远无法删改证据纪律与基准心法保护（Code is Law，2026-09-10） |

## 为什么单独成模块

宿主宪章是**安全语义文本**：它规定"字段缺失不得解读为证据""基准心法不得静默删除"
"证据不足必须 NO_CHANGE"。埋在 95 行提示词装配里时，改错一行不会有人发现；
独立成函数后，门可以直接断言四条硬约束**逐条存在**且 `observability_brief` 真的被插值。

两个函数都是**纯函数**（零副作用、零模块全局读取 —— 依赖全部显式入参）。
"""


from __future__ import annotations

import json

from typing import Any, Dict, List, Tuple


def summarize_closed_trades(*,
        audit_snapshot_observability,
        closed_trades,
        render_observability_brief):
    total = len(closed_trades)
    wins = [t for t in closed_trades if t["net_pnl"] > 0]
    losses = [t for t in closed_trades if t["net_pnl"] <= 0]
    win_rate = round(len(wins) / total * 100, 1) if total > 0 else 0.0
    total_net = round(sum(t["net_pnl"] for t in closed_trades), 2)
    total_fees = round(sum(t["fee"] for t in closed_trades), 2)
    snapshot_audit = audit_snapshot_observability(closed_trades)
    observability_brief = render_observability_brief(snapshot_audit)
    return (total, wins, losses, win_rate, total_net, total_fees, snapshot_audit, observability_brief)


def build_host_constitution(*,
        observability_brief):
    host_constitution = (
        "\n\n======================= 【宿主宪章·代码层硬约束（任何提示词风格档案不可覆盖）】 =======================\n"
        f"1. 数理快照可观测性审计（宿主确定性统计，非模型推断）：{observability_brief}。\n"
        "2. 逐单标注含义：DYNAMICS_OBSERVED=开仓动力学/积分/概率链完整，可作数理因果归因；"
        "PARTIAL=仅可引用 entry_snapshot 中实际非空字段；PRICE_ONLY / NONE=数理快照不可观测，"
        "严禁编造或倒推 v/a/j/I、energy_integral、deviation_area_integral、延续/击穿概率、VaR/CVaR 因果，"
        "字段缺失本身不得解读为任何证据。\n"
        "3. ai_long_term_memory 给出生效后完整清单时必须原样包含全部现有基准心法（is_baseline）："
        "省略条目会被宿主原样补回并留痕；认定基准失效只能写入 diagnosis_insights 交人工复核，禁止静默删除。\n"
        "4. 证据不足必须 NO_CHANGE；NO_CHANGE 永不覆盖或清空长期记忆。\n"
    )
    return (host_constitution)


def parse_review_json(*,
        content):
    if content.startswith("```json"):
        content = content[7:]
    if content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]

    review_json = json.loads(content.strip())
    if not isinstance(review_json, dict):
        review_json = {}
    return content, review_json


def summarize_evidence_stats(*, closed_trades, audit_snapshot_sources,
                            detect_reused_snapshots, baseline_consistency=None) -> Dict[str, Any]:
    """台账证据统计（规划文档 §4.2-3）：不只胜率，还要成本、来源、抽样独立性。

    零副作用纯函数；外部依赖全部显式入参（与 `summarize_closed_trades` 同风格）。
    """
    trades = [t for t in (closed_trades or []) if isinstance(t, dict)]
    source_audit = audit_snapshot_sources(trades)
    venues: Dict[str, int] = {}
    modes: Dict[str, int] = {}
    versions: Dict[str, int] = {}
    for t in trades:
        venue = str(t.get("venue") or "unknown").lower()
        venues[venue] = venues.get(venue, 0) + 1
        mode = str(t.get("strategy_mode") or "unknown")
        modes[mode] = modes.get(mode, 0) + 1
        version = str(t.get("strategy_version") or "unknown")
        versions[version] = versions.get(version, 0) + 1
    group_count, groups = independent_sample_groups(trades)
    return {
        "total_trades": len(trades),
        "gross_pnl": round(sum(float(t.get("gross_pnl") or 0.0) for t in trades), 2),
        "net_pnl": round(sum(float(t.get("net_pnl") or 0.0) for t in trades), 2),
        "fees": round(sum(float(t.get("fee") or 0.0) for t in trades), 2),
        "funding": round(sum(float(t.get("funding") or 0.0) for t in trades), 4),
        "slippage": round(sum(float(t.get("slippage_estimate") or 0.0) for t in trades), 4),
        "protection_failures": len([t for t in trades if t.get("protection_failed")]),
        "snapshot_sources": {k: v for k, v in source_audit.items()},
        "strategy_modes": modes,
        "strategy_versions": versions,
        "venues": venues,
        "independent_sample_groups": group_count,
        "sample_groups": groups,
        "duplicated_signal_groups": detect_reused_snapshots(trades),
        "baseline_consistency": dict(baseline_consistency or {}),
    }


def _signature_of(entry_snapshot: Any) -> str:
    if not isinstance(entry_snapshot, dict):
        return ""
    payload = {k: v for k, v in entry_snapshot.items()
               if k in ("velocity", "acceleration", "jerk", "impulse", "regime",
                        "rsi", "rsi_15m", "adx", "adx_1h", "atr_1h", "jerk_15m")
               and v is not None}
    return json.dumps(payload, sort_keys=True, default=str) if payload else ""


def independent_sample_groups(closed_trades) -> Tuple[int, Dict[str, List[int]]]:
    """独立样本组（§4.5 / §6.1-4）：同一信号拆成多笔订单只算**一组**。

    分组键优先级：显式 `signal_id` ⇒ 信号签名（同一标的/方向/同一小时）⇒ 兜底
    「标的+方向+小时」。同一信号的拆单共享同一键，因此不会把「一信号 3 单」
    当成 3 个独立样本。
    """
    groups: Dict[str, List[int]] = {}
    for idx, t in enumerate(closed_trades or []):
        trade = t if isinstance(t, dict) else {}
        inst = str(trade.get("inst") or "")
        side = str(trade.get("side") or "").strip().lower()
        signal_id = str(trade.get("signal_id") or "")
        signature = _signature_of(trade.get("entry_snapshot"))
        open_time = str(trade.get("open_time") or trade.get("time") or "")
        hour_key = open_time[:13]  # YYYY-MM-DD HH
        key = f"{inst}|{side}|{signal_id or signature or hour_key}"
        groups.setdefault(key, []).append(idx)
    return len(groups), groups


def split_lessons_by_level(lessons, *, now=None) -> Dict[str, List[Dict[str, Any]]]:
    """把当前记忆拆成四块（规划文档 §4.4-4）。

    桶：

    - `active_baselines`：代码基线（硬规则，提示词里单独区块，模型不得改）；
    - `active_reviewed_heuristics`：已审核启发式（可注入提示词，不得覆盖硬规则）；
    - `observation_only`：待验证观察（不得单独构成开仓理由）；
    - `pending_approval`：未审核/待人工审批的提案（**绝不**注入交易提示词，§6.4）；
    - `retired_lessons`：停用/退役/过期条目（**不**注入提示词）。
    """
    import datetime as _dt
    now = now or _dt.datetime.now(_dt.timezone.utc)
    rows = [i for i in (lessons or []) if isinstance(i, dict)]
    buckets: Dict[str, List[Dict[str, Any]]] = {
        "active_baselines": [], "active_reviewed_heuristics": [],
        "observation_only": [], "pending_approval": [], "retired_lessons": [],
    }
    for item in rows:
        level = str(item.get("evidence_level") or (
            "BASELINE_HARD_RULE" if item.get("is_baseline") else "REVIEWED_HEURISTIC"))
        status = str(item.get("status") or ("ACTIVE" if item.get("enabled") else "DISABLED"))
        expired = False
        created = item.get("created_at")
        ttl = item.get("ttl_days")
        if item.get("enabled") and created and ttl and not item.get("is_baseline"):
            try:
                text = str(created)
                if text.endswith("Z"):
                    text = text[:-1] + "+00:00"
                created_dt = _dt.datetime.fromisoformat(text)
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=_dt.timezone.utc)
                expired = (now - created_dt).total_seconds() > float(ttl) * 86400
            except (TypeError, ValueError):
                expired = False
        approval = item.get("approval") if isinstance(item.get("approval"), dict) else {}
        needs_approval = bool(approval.get("required")) and approval.get("status") != "APPROVED"
        if not item.get("enabled") or status in {"DISABLED", "RETIRED", "REJECTED"} or expired:
            buckets["retired_lessons"].append(item)
        elif level == "PROPOSED_HEURISTIC" or needs_approval:
            buckets["pending_approval"].append(item)
        elif level == "BASELINE_HARD_RULE":
            buckets["active_baselines"].append(item)
        elif level == "OBSERVATION_ONLY":
            buckets["observation_only"].append(item)
        else:
            buckets["active_reviewed_heuristics"].append(item)
    return buckets


def render_lesson_block(buckets, *, limit: int = 8, strategy_mode: Optional[str] = None) -> str:
    """交易主脑提示词用的分层区块（§6.3/§6.4）：已审核启发式 / 待验证观察。

    ⚠️ 只渲染「已审核启发式」与「待验证观察」——基线由宿主硬规则区块单独给出，
    退役/停用条目与未审核提案**绝不**出现。

    `strategy_mode` 给出时按 `scope.strategy_modes` 过滤：其他策略模式的规则不进本
    策略的提示词（§6.4 最后一条）。未声明 strategy_modes 的条目视为通用，仍会渲染。
    """
    def _in_scope(item) -> bool:
        if not strategy_mode:
            return True
        scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
        modes = scope.get("strategy_modes") or scope.get("modes")
        if not modes:
            return True
        if isinstance(modes, str):
            modes = [modes]
        return str(strategy_mode) in {str(m) for m in modes}

    heuristics = [i for i in (buckets.get("active_reviewed_heuristics") or []) if _in_scope(i)]
    observations = [i for i in (buckets.get("observation_only") or []) if _in_scope(i)]
    lines = ["【已审核启发式】", "仅作为辅助证据，不能覆盖宿主硬规则。"]
    for item in heuristics[:limit]:
        lines.append(f"- {item.get('rule_text')}")
    if not heuristics:
        lines.append("- （无）")
    lines.append("")
    lines.append("【待验证观察】")
    lines.append("不得单独构成开仓理由。")
    for item in observations[:limit]:
        lines.append(f"- {item.get('rule_text')}")
    if not observations:
        lines.append("- （无）")
    return "\n".join(lines)


def current_strategy_mode() -> str:
    """当前执行策略模式（读取失败 ⇒ 空串 ⇒ 调用方退化为不过滤）。"""
    try:
        try:
            from scripts.strategy_rules import active_execution_policy
        except ImportError:  # pragma: no cover
            from strategy_rules import active_execution_policy
        return str(active_execution_policy().get("mode") or "")
    except Exception:
        return ""


def parse_review_contract(*, llm_review) -> Dict[str, Any]:
    """新复盘 JSON 契约归一（规划文档 §6.2）。

    输出固定包含：`facts` / `hypotheses` / `rule_proposals` / `observation_texts` /
    `change_status` / `memory_overwrites_reason`；缺失字段归一为空（不抛错，
    由上层按「无新证据 ⇒ NO_CHANGE」处理）。
    """
    review = llm_review if isinstance(llm_review, dict) else {}

    def _rows(key: str) -> List[Dict[str, Any]]:
        raw = review.get(key)
        if not isinstance(raw, list):
            return []
        out = []
        for item in raw:
            if isinstance(item, dict):
                out.append(dict(item))
            elif isinstance(item, str) and item.strip():
                out.append({"text": item.strip()})
        return out

    status = str(review.get("change_status") or "NO_CHANGE").upper()
    if status not in {"NO_CHANGE", "ADD", "REVISE", "INVALIDATE"}:
        status = "NO_CHANGE"
    return {
        "change_status": status,
        "facts": _rows("facts"),
        "hypotheses": _rows("hypotheses"),
        "rule_proposals": _rows("rule_proposals"),
        "legacy_insights": review.get("diagnosis_insights") or [],
        "legacy_actions": review.get("evolution_actions") or [],
        "memory_overwrites_reason": str(review.get("memory_overwrites_reason") or ""),
        "asset_multipliers": review.get("asset_multipliers") if isinstance(
            review.get("asset_multipliers"), dict) else {},
        "has_llm_error": bool(review.get("__llm_error__")),
    }


def build_host_constitution_v2(*, observability_brief, evidence_stats=None,
                               baseline_consistency=None, rule_versions=None) -> str:
    """宿主宪章续篇（规划文档 §4.4-2 / §6.1）。

    v1 的四条硬约束由 `build_host_constitution` 给出且**原样保留**（AST 对拍门）；
    本函数续写 6~10 条，把本轮新增的证据边界显式写进提示词：

    6. 没有时间证明的快照不得作因果证据；
    7. 同一信号拆成多笔订单不算多个独立样本；
    8. 资产乘数不是硬风控；
    9. 复盘输出不能直接改变交易参数；
    10. baseline 一致性异常时只能输出报告，不能发布。
    """
    stats = evidence_stats or {}
    consistency = baseline_consistency or {}
    versions = rule_versions or {}
    source_line = "；".join(f"{k}={v}" for k, v in (stats.get("snapshot_sources") or {}).items())
    duplicate_count = len(stats.get("duplicated_signal_groups") or [])
    return (
        "6. 没有时间证明的快照不得作因果证据：来源为 calculus_snapshot_fallback / unavailable "
        "或 snapshot_time_verified=false 的交易，只能当作普通观测（PRICE_ONLY / NONE），"
        "严禁用它做策略因果归因。\n"
        f"   本轮快照来源分布（宿主确定性统计）：{source_line or '无'}；"
        f"重复信号证据组：{duplicate_count}；独立样本组：{stats.get('independent_sample_groups', '未知')}。\n"
        "7. 同一信号拆成多笔订单只算**一个独立样本组**，不得当成多个独立样本；"
        "反例（同形态的亏损样本）必须与正例一起报告。\n"
        "8. 资产乘数不是硬风控：它只缩放模型申请的保证金，永远不得改变杠杆、保证金比例上限、"
        "止损宽度、熔断线与置信度门槛。\n"
        "9. 复盘输出不能直接改变交易参数：本轮只能产出事实/假设/提案，"
        "硬规则（杠杆/保证金/熔断/止损/持仓上限）只能由代码与显式策略版本发布改变。\n"
        f"10. baseline 一致性：{'健康' if consistency.get('healthy', True) else '不一致'} "
        f"(missing={consistency.get('missing_ids') or []}, mismatched={consistency.get('mismatched_ids') or []})。"
        "不一致时只能输出报告，不得发布记忆，也不得把提案标为已生效。\n"
        f"   当前策略规则版本（代码事实）：{versions.get('rule_set') or 'legacy@1'} / "
        f"risk hash {versions.get('policy_hash') or '--'} / baseline hash {versions.get('baseline_hash') or '--'}。\n"
    )


def normalize_asset_multipliers(*,
        TARGET_INSTRUMENTS,
        clamp,
        llm_review):
    raw_asset_mults = llm_review.get("asset_multipliers", {})
    if not isinstance(raw_asset_mults, dict):
        raw_asset_mults = {}
    asset_mults = {
        asset: clamp(raw_asset_mults.get(asset, 1.0), 0.5, 1.5, 1.0)
        for asset in TARGET_INSTRUMENTS
    }
    return asset_mults


def normalize_asset_multiplier_proposals(*, llm_review, target_instruments,
                                         allowed_range=(0.5, 1.5)) -> Dict[str, Any]:
    """资产乘数提案的**严格**归一（规划文档 §4.4-3 / §5.6）。

    - 缺失 ⇒ 1.0；
    - 模型数值超出 `0.5~1.5` ⇒ **拒绝**该项（不是夹到边界），记 INVALID；
    - 未知标的（不在标的池）⇒ 不生成条目，记 REJECTED；
    - 非法类型 ⇒ 拒绝；
    - 输出带来源与审核状态，供写盘时携带 TTL/证据 revision。

    与 `normalize_asset_multipliers`（历史契约：只夹取、只对池内标的取默认 1.0）
    并存：旧函数保持逐字行为供既有调用方/测试解析，新版由自进化引擎使用。
    """
    raw = (llm_review or {}).get("asset_multipliers", {}) if isinstance(llm_review, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    lo, hi = float(allowed_range[0]), float(allowed_range[1])
    pool = [str(x) for x in (target_instruments or [])]
    pool_upper = {p.upper(): p for p in pool}
    multipliers: Dict[str, float] = {p: 1.0 for p in pool}
    rejected: List[Dict[str, Any]] = []
    for key, value in raw.items():
        symbol = str(key).strip()
        canonical = pool_upper.get(symbol.upper())
        if canonical is None:
            rejected.append({"symbol": symbol, "value": value, "status": "REJECTED",
                             "reason": "UNKNOWN_INSTRUMENT"})
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            rejected.append({"symbol": canonical, "value": value, "status": "INVALID",
                             "reason": "NOT_A_NUMBER"})
            continue
        if not (lo <= numeric <= hi):
            rejected.append({"symbol": canonical, "value": numeric, "status": "INVALID",
                             "reason": f"OUT_OF_RANGE[{lo:g},{hi:g}]"})
            continue
        multipliers[canonical] = round(numeric, 4)
    status = "REVIEWED" if any(v != 1.0 for v in multipliers.values()) else "NEUTRAL"
    return {"multipliers": multipliers, "rejected": rejected, "status": status,
            "source": "self_improvement_review", "allowed_range": [lo, hi],
            "evidence_policy_version": "2"}
