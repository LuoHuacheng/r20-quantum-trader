"""自进化**报告载荷**的形状（从 `self_improvement_engine.run_self_evolution` 搬出）。

这段 20 行的字典字面量是**与前端/看板之间的契约**：键名即前端读取的字段。
原先埋在 154 行编排函数的中后段（前后是落盘与通知），改动它没有任何提示；
独立成函数后，门可以把**键集精确钉住**，新人删/改字段时会立刻被拦下。

几处**不是"看起来那样"的字段**（原样保留，勿"优化"）：

- `insights` 与 `diagnosis_insights` **是同一个列表**（历史字段名并存，前端两者都在读）；
- `retired_count` = `len(retired_lessons)`、`baseline_memory_protected` = `len(constitution_readded)`
  —— 是**计数快照**，不是明细；
- `memory_preserved` 直接取 `preserve_existing_memory`（不是"是否保留"的再判断）；
- `llm_error` 把 `__llm_error__` 转成字符串，缺省 `""`（前端据此显示上游失败，而非静默 NO_CHANGE）；
- `mode` 是固定文案（启发式长期记忆模式）。

零副作用、零模块全局读取：全部依赖由调用方注入。
"""
def build_evolution_report(*,
        actions_taken,
        change_status,
        constitution_readded,
        insights,
        ledger_revision,
        llm_review,
        long_term_memory,
        preserve_existing_memory,
        profit_factor,
        retired_lessons,
        snapshot_audit,
        timestamp_str,
        total_trades,
        win_rate):
    report_payload = {
        "timestamp": timestamp_str,
        "ledger_revision": ledger_revision,
        "total_trades": total_trades,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "mode": "R20 Native Heuristic Memory (启发式长期记忆)",
        "change_status": change_status,
        "retired_lessons": retired_lessons,
        "retired_count": len(retired_lessons),
        "memory_preserved": preserve_existing_memory,
        "insights": insights,
        "diagnosis_insights": insights,
        "memory_overwrites_reason": llm_review.get("memory_overwrites_reason", ""),
        "actions_taken": actions_taken,
        "core_lessons": long_term_memory,
        "snapshot_audit": snapshot_audit,
        "baseline_memory_protected": len(constitution_readded),
        "llm_error": str(llm_review.get("__llm_error__") or ""),
    }
    return report_payload


def build_evolution_report_v2(*, report_payload, ledger_revision, memory_revision, policy_hash,
                              baseline_hash, baseline_consistency, evidence_stats,
                              review_contract, proposal_result, asset_multiplier_status,
                              review_input_hash="", review_output_hash="",
                              pending_proposals=None, llm_failed=False,
                              mirror_synced=None) -> dict:
    """在 v1 报告之上追加证据/版本字段（规划文档 §4.2 建议结构）。

    v1 的 18 个键由 `build_evolution_report` 原样提供（那是前端契约，被对拍门钉住），
    本函数**只增不改**：事实/假设/提案分层、基线一致性、台账与记忆版本、
    输入/输出 hash、资产乘数状态、待审批队列。

    零副作用纯函数：不读文件，全部由调用方注入。
    """
    contract = review_contract if isinstance(review_contract, dict) else {}
    proposals = proposal_result if isinstance(proposal_result, dict) else {}
    payload = dict(report_payload)

    def _texts(rows):
        out = []
        for row in rows or []:
            if isinstance(row, dict):
                out.append(str(row.get("text") or row.get("rule_text") or ""))
            elif isinstance(row, str):
                out.append(row)
        return [t for t in out if t]

    facts = contract.get("facts") or []
    hypotheses = contract.get("hypotheses") or []
    accepted = proposals.get("accepted") or []
    observation_only = proposals.get("observation_only") or []
    requires_approval = proposals.get("requires_approval") or []
    rejected = proposals.get("rejected") or []

    payload.update({
        "report_schema_version": 2,
        "ledger_revision": ledger_revision,
        "memory_revision": memory_revision,
        "policy_hash": policy_hash,
        "baseline_hash": baseline_hash,
        "baseline_consistency": dict(baseline_consistency or {}),
        "evidence_stats": dict(evidence_stats or {}),
        "snapshot_source_audit": dict((evidence_stats or {}).get("snapshot_sources") or {}),
        "reused_snapshot_groups": list((evidence_stats or {}).get("duplicated_signal_groups") or []),
        "facts": facts,
        "fact_texts": _texts(facts),
        "hypotheses": hypotheses,
        "hypothesis_texts": _texts(hypotheses),
        "rule_proposals": list(contract.get("rule_proposals") or []),
        "heuristics": accepted,
        "heuristic_texts": _texts(accepted),
        "proposals": observation_only,
        "observation_only_texts": _texts(observation_only),
        "requires_approval": requires_approval,
        "requires_approval_count": len(requires_approval),
        "rejected_proposals": rejected,
        "proposal_reason_codes": list(proposals.get("reason_codes") or []),
        "pending_proposals": list(pending_proposals or []),
        "asset_multiplier_status": str(asset_multiplier_status or "UNAVAILABLE"),
        "review_input_hash": review_input_hash,
        "review_output_hash": review_output_hash,
        "llm_failed": bool(llm_failed),
        # §10.2：Markdown 镜像失败不影响结构化权威，但必须在报告里如实披露。
        "markdown_mirror_synced": (None if mirror_synced is None else bool(mirror_synced)),
    })
    return payload


def render_self_evolution_doc(*, report_payload) -> str:
    """把报告渲染成 `docs/self-evolution.md` 的生成式正文（规划文档 §7.2）。

    文档必须能回答「这条结论来自哪笔交易 / 用了哪个快照 / 是否独立样本 /
    是否已审核 / 是否进入提示词 / 是否由代码硬执行」。**禁止**写死累计盈亏、
    禁止给历史参数冒充当前配置、禁止把观察写成硬风控。
    """
    payload = report_payload if isinstance(report_payload, dict) else {}
    stats = payload.get("evidence_stats") or {}
    consistency = payload.get("baseline_consistency") or {}

    def _bullets(rows, *, empty="- （无）"):
        lines = []
        for row in rows or []:
            if isinstance(row, dict):
                text = row.get("text") or row.get("rule_text") or ""
                extra = []
                if row.get("evidence_ids"):
                    extra.append("证据=" + ",".join(str(x) for x in row["evidence_ids"]))
                if row.get("independent_sample_groups") is not None:
                    extra.append(f"独立样本组={row['independent_sample_groups']}")
                if row.get("counterexample_count") is not None:
                    extra.append(f"反例={row['counterexample_count']}")
                if row.get("scope"):
                    extra.append(f"适用范围={row['scope']}")
                lines.append(f"- {text}" + (f"（{'；'.join(extra)}）" if extra else ""))
            else:
                lines.append(f"- {row}")
        return "\n".join(lines) if lines else empty

    sources = stats.get("snapshot_sources") or {}
    source_line = "；".join(f"{k}={v}" for k, v in sources.items()) or "（无）"
    return f"""# 自进化复盘报告（生成物）

> ⚠️ 本文档由 `scripts/evolution/report.py::render_self_evolution_doc` 生成。
> 手写内容必须带 `author` / `created_at` / `source` / `status` / `reviewed_by`，
> 否则下一轮生成会被覆盖。**禁止**手工写死累计盈亏、禁止把历史参数写成当前配置。

## 报告时间与版本

- 报告时间：{payload.get('timestamp', '--')}
- 报告 schema：{payload.get('report_schema_version', 1)}
- 台账 revision：`{payload.get('ledger_revision', '--')}`
- 记忆 revision：`{payload.get('memory_revision', '--')}`
- 策略 hash：`{payload.get('policy_hash', '--')}` / baseline hash：`{payload.get('baseline_hash', '--')}`

## 台账起止与观测性

- 有效交易数：{payload.get('total_trades', 0)}
- 不可观测交易数（PRICE_ONLY + NONE）：{(payload.get('snapshot_audit') or {}).get('PRICE_ONLY', 0) + (payload.get('snapshot_audit') or {}).get('NONE', 0)}
- 快照来源分布：{source_line}
- 独立样本组：{stats.get('independent_sample_groups', '--')} | 重复信号证据组：{len(payload.get('reused_snapshot_groups') or [])}
- 成本：手续费 {stats.get('fees', '--')} / 资金费 {stats.get('funding', '--')} / 滑点 {stats.get('slippage', '--')}（单位 USDT，来自台账字段）

## 基准一致性

- 健康：{('是' if consistency.get('healthy') else '否') if consistency.get('healthy') is not None else '--'}
- missing：{consistency.get('missing_ids') or []}
- mismatched：{consistency.get('mismatched_ids') or []}
- unexpected：{consistency.get('unexpected_ids') or []}

## 事实（可观测）

{_bullets(payload.get('facts'))}

## 已审核启发式（可注入提示词，不得覆盖硬规则）

{_bullets(payload.get('heuristics'))}

## 待验证观察（不得单独构成开仓理由）

{_bullets(payload.get('proposals'))}

## 规则提案（需人工审核，不自动生效）

{_bullets((payload.get('requires_approval') or []) + (payload.get('rejected_proposals') or []))}

## 当前执行规则引用

- 执行规则版本：{((payload.get('evidence_stats') or {}).get('strategy_versions') or {})}
- 资产乘数状态：{payload.get('asset_multiplier_status', 'UNAVAILABLE')}
- 成功发布：{'否（保留既有权威）' if payload.get('memory_preserved') else '是'}

## 回滚说明

1. 记忆：`POST /api/v1/admin/memory/rollback`（带 `expected_version`）→ 原子回滚到代码基准；
2. 硬规则：关闭新策略模式的新开仓，已有仓位继续用入场时冻结的规则版本保护退出；
3. 资产乘数：视为 1.0（`data/asset_multipliers.json` 保留原文件作审计）。
"""
