"""进化复盘的心法合并与发布（`scripts/evolution/` 部件，从门面搬出）。

承载**宪法级保护**（基准心法不得被进化输出物理删除）与"发布失败即保留既有权威"逻辑 ——
见函数 docstring。
"""

from __future__ import annotations

from typing import Any, Dict, List


def apply_memory_review(*,
        change_status,
        constitution_readded,
        log_msg,
        long_term_memory,
        memory_service,
        memory_snapshot,
        merge_memory_with_constitution,
        preserve_existing_memory,
        retired_lessons,
        total_trades):
    """按宪法合并复盘心法 → 记录补回/停用 → 发布到记忆服务。

    ## 三处 in-out（都是**调用方预先初始化**的跨分支状态）

    `constitution_readded`（门面 L636 `= []`）、`retired_lessons`（本块前一行 `= []`）、
    `preserve_existing_memory`（上一句 `resolve_memory_update` 的产物）——
    它们只在 `if not preserve_existing_memory:` 分支里被赋值，分支跳过时保持原值，
    块后又会被报告消费。**只按"段内是否赋值"判必然绑定会误判**，故一律 in-out。

    ⚠️ 本块是**宪法级保护**：基准心法不允许被进化输出物理删除（2026-09-10），
    遗漏/试图删除的基准心法由宿主补回并计数（`baseline_memory_protected` 报告位）。

    段体 **AST 逐字**（对拍门 `tests/extraction/test_evolution_memory_review_extraction.py`）。
    """
    if not preserve_existing_memory:
        # Safe extraction: convert potential dicts {"rule_text": "..."} to string safely
        safe_long_term = []
        for item in long_term_memory:
            if isinstance(item, dict):
                val = str(item.get("rule_text") or item.get("text") or item.get("lesson") or "").strip()
            else:
                val = str(item or "").strip()
            if val:
                safe_long_term.append(val)
        # 宪法级保护：基准心法不允许被进化输出物理删除（2026-09-10）
        safe_long_term, constitution_readded = merge_memory_with_constitution(
            change_status, safe_long_term, memory_snapshot.get("lessons") or [])
        if constitution_readded:
            log_msg(f"🛡️ 进化输出遗漏/试图删除 {len(constitution_readded)} 条基准心法，宿主已按宪法补回保留")
        # 审计 P1-8c：被模型省略的**已学（非基准）**心法不再是"静默消失"，而是停用存档；
        # 这里把条数写进日志，报告口径不再只统计基准补回。
        _already = {t.strip() for t in safe_long_term}
        _dropped = [str(l.get("rule_text") or "").strip() for l in (memory_snapshot.get("lessons") or [])
                    if l.get("enabled") and not l.get("is_baseline")
                    and str(l.get("rule_text") or "").strip() and str(l.get("rule_text") or "").strip() not in _already]
        if _dropped:
            retired_lessons = _dropped
            log_msg(f"📦 {len(_dropped)} 条既学心法本轮未被复述：已按停用存档保留（不注入提示词，可在面板复核恢复）")

        try:
            published = memory_service.publish_review(
                safe_long_term, expected_version=memory_snapshot["version"],
                sample_size=total_trades, change_status=change_status)
            preserve_existing_memory = not published
        except Exception as exc:
            preserve_existing_memory = True
            log_msg(f"Memory publication rejected; retaining authority: {exc}")
    return (constitution_readded, preserve_existing_memory, retired_lessons)


# =============================================================================
# 规则提案门禁（规划文档 §4.5）—— 新增函数，不触碰上面被 AST 对拍门钉住的
# `apply_memory_review`（它仍是"只合并与发布"的那段逐字实现）。
# =============================================================================

#: 提案至少需要多少个独立样本组 / 独立时间窗口才算「已审核启发式」。
MIN_INDEPENDENT_GROUPS = 2
MIN_TIME_WINDOWS = 2


def _evidence_ids(proposal) -> List[str]:
    raw = proposal.get("evidence_ids") if isinstance(proposal, dict) else None
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x).strip()]
    return []


def _time_windows(evidence_ids, trades_by_id) -> set:
    windows = set()
    for eid in evidence_ids:
        trade = trades_by_id.get(eid) or {}
        stamp = str(trade.get("open_time") or trade.get("time") or "")
        if stamp:
            windows.add(stamp[:13])  # YYYY-MM-DD HH
    return windows


def _touches_hard_rules(text: str) -> bool:
    """提案是否试图修改 L0/L1 参数（杠杆/保证金/止损/熔断/持仓上限/阈值）。"""
    import re
    patterns = (
        r"(杠杆|leverage)",
        r"(保证金|margin)",
        r"(止损|stop[_ ]?loss|sl\b)",
        r"(熔断|circuit)",
        r"(持仓上限|仓位上限|敞口上限|风险预算)",
        r"(置信度门槛|阈值|threshold)",
    )
    for pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def evaluate_rule_proposals(*, rule_proposals, closed_trades, audit_structured_lesson,
                            independent_sample_groups, hard_rule_conflicts=(),
                            min_groups: int = MIN_INDEPENDENT_GROUPS,
                            min_windows: int = MIN_TIME_WINDOWS) -> Dict[str, Any]:
    """规则提案闸门（规划文档 §4.5-2/3/4/6）。

    返回：

    ```python
    {
      "accepted": [...],          # 可发布为 L2 的提案（已审核启发式）
      "observation_only": [...],  # 样本够但独立性/反例不足 ⇒ 降级为观察
      "requires_approval": [...], # 声明基线失效 / 触碰硬规则 ⇒ 只进人工队列
      "rejected": [...],          # 文本红线 / 无 scope / 无证据
      "reason_codes": [...],      # 稳定 reason code
      "metadata": {rule_text: {...}},
    }
    ```
    """
    trades = [t for t in (closed_trades or []) if isinstance(t, dict)]
    trades_by_id = {}
    for idx, trade in enumerate(trades):
        for key in (str(trade.get("id") or ""), f"trade:{idx}", f"trade:{idx + 1}"):
            if key:
                trades_by_id.setdefault(key, trade)
    accepted, observation_only, requires_approval, rejected = [], [], [], []
    reason_codes: List[str] = []
    metadata: Dict[str, Dict[str, Any]] = {}
    conflicts = [str(c) for c in (hard_rule_conflicts or [])]

    for proposal in (rule_proposals or []):
        if not isinstance(proposal, dict):
            continue
        text = str(proposal.get("text") or proposal.get("rule_text") or "").strip()
        record = dict(proposal)
        record["text"] = text
        evidence = _evidence_ids(proposal)
        groups = proposal.get("independent_sample_groups")
        if groups is None:
            groups = independent_sample_groups
        windows = _time_windows(evidence, trades_by_id)
        counterexample_count = proposal.get("counterexample_count")
        requested_level = str(proposal.get("requested_level") or "REVIEWED_HEURISTIC")
        scope = proposal.get("scope") if isinstance(proposal.get("scope"), dict) else {}

        if not text:
            rejected.append(dict(record, rejection_reasons=["EMPTY_TEXT"]))
            continue
        if not scope:
            rejected.append(dict(record, rejection_reasons=["SCOPE_REQUIRED"]))
            reason_codes.append("RULE_PROPOSAL_SCOPE_REQUIRED")
            continue
        # 触碰 L0/L1 的提案：不直接生效，进人工审核队列（§4.5-6）。
        if _touches_hard_rules(text) or any(c and c in text for c in conflicts):
            requires_approval.append(dict(record, approval_required=True,
                                          rejection_reasons=["HARD_RULE_CONFLICT"]))
            reason_codes.append("RULE_PROPOSAL_REQUIRES_APPROVAL")
            continue
        # 模型声称某条基线失效 ⇒ requires_approval=true，绝不物理删基线（§4.5-5/6）。
        if str(proposal.get("target_lesson_id") or "").startswith("lesson_") and \
                record.get("invalidates_baseline"):
            requires_approval.append(dict(record, approval_required=True,
                                          rejection_reasons=["BASELINE_INVALIDATION"]))
            reason_codes.append("RULE_PROPOSAL_REQUIRES_APPROVAL")
            continue
        passed, reason = audit_structured_lesson(
            rule_text=text, evidence_level="OBSERVATION_ONLY", scope=scope,
            sample_size=max(len(evidence), 1), independent_sample_groups=groups)
        if not passed:
            rejected.append(dict(record, rejection_reasons=[reason]))
            continue
        sufficient = (len(evidence) >= min_groups and int(groups or 0) >= min_groups
                      and len(windows) >= min_windows and counterexample_count is not None
                      and bool(record.get("counterexamples_checked")))
        if not sufficient:
            # §4.5-4：样本/独立性/反例不足 ⇒ 降级为待验证观察，而不是丢掉这条信息。
            if len(evidence) < min_groups or int(groups or 0) < min_groups:
                why = ["INSUFFICIENT_INDEPENDENT_SAMPLES"]
            elif len(windows) < min_windows:
                why = ["INSUFFICIENT_TIME_WINDOWS"]
            else:
                why = ["COUNTEREXAMPLE_CHECK_MISSING"]
            observation_only.append(dict(record, downgraded_to="OBSERVATION_ONLY",
                                         rejection_reasons=why))
            reason_codes.append("RULE_PROPOSAL_OBSERVATION_ONLY")
            continue
        passed, reason = audit_structured_lesson(
            rule_text=text, evidence_level=requested_level, scope=scope,
            sample_size=len(evidence), independent_sample_groups=groups)
        if not passed:
            rejected.append(dict(record, rejection_reasons=[reason]))
            continue
        accepted.append(dict(record, evidence_level="REVIEWED_HEURISTIC",
                             independent_sample_groups=int(groups or 0),
                             time_windows=len(windows)))
        metadata[text] = {
            "evidence_level": "REVIEWED_HEURISTIC",
            "scope": scope,
            "independent_sample_groups": int(groups or 0),
            "counterexample_count": int(counterexample_count or 0),
            "source_strategy_versions": sorted({str(t.get("strategy_version") or "")
                                                 for t in trades if t.get("strategy_version")}),
            "approval": {"required": False, "status": "NOT_REQUIRED",
                         "approved_by": "", "approved_at": ""},
        }

    for proposal in observation_only:
        text = str(proposal.get("text") or "").strip()
        if text:
            metadata.setdefault(text, {
                "evidence_level": "OBSERVATION_ONLY",
                "scope": proposal.get("scope") or {},
                "independent_sample_groups": int(proposal.get("independent_sample_groups") or 0),
                "counterexample_count": int(proposal.get("counterexample_count") or 0),
                "approval": {"required": False, "status": "NOT_REQUIRED",
                             "approved_by": "", "approved_at": ""},
            })

    return {"accepted": accepted, "observation_only": observation_only,
            "requires_approval": requires_approval, "rejected": rejected,
            "reason_codes": reason_codes, "metadata": metadata}
