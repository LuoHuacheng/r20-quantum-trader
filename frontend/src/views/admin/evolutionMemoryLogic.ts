/**
 * 自进化记忆/复盘页的**纯逻辑**（规划文档 §9.1 / §9.2 / §9.3）。
 *
 * 抽成独立模块的理由与 `promptStudioLogic.ts` 同款：页面里的这类"计数与告警口径"
 * 是不可直接观察的（算错了页面照样渲染），必须能在 node 里逐条断言。
 *
 * | 函数 | 决定什么 |
 * |---|---|
 * | `summarizeMemoryInventory` | 记忆管理页要显示的计数（总数/启用/注入/未注入原因/baseline/等级分布/过期/版本） |
 * | `memoryAlerts` | 管理员告警清单（§9.3 的八类），返回稳定 code + 严重级 |
 * | `summarizeReviewLayers` | 复盘报告页的**事实/假设/提案**三层 + 反例/样本/来源 |
 *
 * 纪律：入参缺失一律降级为「未知」而不是 0 —— 页面把"读不到"显示成 0 就是谎报。
 */

export type TFn = (path: string, fallback?: string, params?: Record<string, string | number>) => string

export interface InventoryRow {
  key: string
  label: string
  value: string
  tone: 'ok' | 'warn' | 'bad' | 'muted'
}

export interface MemoryAlert {
  code: string
  severity: 'CRITICAL' | 'WARNING' | 'INFO'
  detail: string
}

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function show(value: number | null): string {
  return value === null ? '--' : String(value)
}

/** 记忆管理页的计数行（§9.1）。 */
export function summarizeMemoryInventory(payload: unknown, t?: TFn): InventoryRow[] {
  const data = (payload || {}) as Record<string, any>
  const inv = (data.inventory || {}) as Record<string, any>
  const consistency = (inv.baseline_consistency || {}) as Record<string, any>
  const healthy = consistency.healthy
  const injected = num(inv.injected)
  const active = num(inv.active)
  const limit = num(inv.limit)
  const notInjected = Array.isArray(inv.not_injected) ? inv.not_injected : []
  const expired = num(inv.expired)
  return [
    {
      key: 'total',
      label: t ? t('admin.evolution.inventory.total', '总记忆条数') : '总记忆条数',
      value: show(num(inv.total)),
      tone: 'muted',
    },
    {
      key: 'enabled',
      label: t ? t('admin.evolution.inventory.enabled', '启用条数') : '启用条数',
      value: show(num(inv.enabled)),
      tone: 'ok',
    },
    {
      key: 'injected',
      label: t ? t('admin.evolution.inventory.injected', '实际注入条数') : '实际注入条数',
      value: limit === null ? show(injected) : `${show(injected)} / ${limit}`,
      tone: injected !== null && limit !== null && injected > limit ? 'bad' : 'ok',
    },
    {
      key: 'active',
      label: t ? t('admin.evolution.inventory.active', '生效（未过期）条数') : '生效（未过期）条数',
      value: show(active),
      tone: 'muted',
    },
    {
      key: 'not_injected',
      label: t ? t('admin.evolution.inventory.notInjected', '未注入条数') : '未注入条数',
      value: String(notInjected.length),
      tone: notInjected.length > 0 ? 'warn' : 'ok',
    },
    {
      key: 'baseline',
      label: t ? t('admin.evolution.inventory.baseline', '基准心法条数') : '基准心法条数',
      value: show(num(inv.baseline_count)),
      tone: 'muted',
    },
    {
      key: 'baseline_consistency',
      label: t ? t('admin.evolution.inventory.baselineConsistency', '基准一致性') : '基准一致性',
      value: healthy === undefined ? '--' : healthy ? 'HEALTHY' : 'CRITICAL',
      tone: healthy === undefined ? 'muted' : healthy ? 'ok' : 'bad',
    },
    {
      key: 'reviewed_heuristics',
      label: t ? t('admin.evolution.inventory.heuristics', '已审核启发式') : '已审核启发式',
      value: show(num(inv.reviewed_heuristics)),
      tone: 'muted',
    },
    {
      key: 'observations',
      label: t ? t('admin.evolution.inventory.observations', '待验证观察') : '待验证观察',
      value: show(num(inv.observations)),
      tone: 'muted',
    },
    {
      key: 'expired',
      label: t ? t('admin.evolution.inventory.expired', '过期条数') : '过期条数',
      value: show(expired),
      tone: expired && expired > 0 ? 'warn' : 'ok',
    },
    {
      key: 'ledger_revision',
      label: t ? t('admin.evolution.inventory.ledgerRevision', '最近 ledger revision') : '最近 ledger revision',
      value: String(inv.ledger_revision || '--').slice(0, 12),
      tone: 'muted',
    },
    {
      key: 'memory_revision',
      label: t ? t('admin.evolution.inventory.memoryRevision', '最近 memory revision') : '最近 memory revision',
      value: String(inv.memory_revision || data.version || '--').slice(0, 12),
      tone: 'muted',
    },
  ]
}

/**
 * 管理员告警（§9.3）。只对**已有证据**告警：读不到的字段不臆测成告警。
 *
 * @param payload `/api/v1/admin/memory` 返回体
 * @param report  自进化报告（可为空）
 */
export function memoryAlerts(payload: unknown, report?: unknown): MemoryAlert[] {
  const data = (payload || {}) as Record<string, any>
  const inv = (data.inventory || {}) as Record<string, any>
  const rep = (report || {}) as Record<string, any>
  const alerts: MemoryAlert[] = []
  const consistency = (inv.baseline_consistency || rep.baseline_consistency || {}) as Record<string, any>

  if (consistency.healthy === false) {
    alerts.push({
      code: 'MEMORY_BASELINE_MISMATCH',
      severity: 'CRITICAL',
      detail: `baseline 不一致：missing=${JSON.stringify(consistency.missing_ids || [])} mismatched=${JSON.stringify(consistency.mismatched_ids || [])}`,
    })
  }
  if (data.legacy_read_only === true) {
    alerts.push({
      code: 'MEMORY_AUTHORITY_MISSING',
      severity: 'CRITICAL',
      detail: '结构化权威记忆不存在（当前只读回退到 legacy markdown）',
    })
  }
  if (data.inventory === undefined && data.error) {
    alerts.push({ code: 'MEMORY_READ_FAILED', severity: 'CRITICAL', detail: String(data.error) })
  }

  const sources = (rep.snapshot_source_audit || {}) as Record<string, any>
  const total = num(sources.total)
  const untrusted = num(sources.untrusted)
  if (total && untrusted !== null && untrusted / total > 0.5) {
    alerts.push({
      code: 'SNAPSHOT_UNOBSERVABLE_RATIO',
      severity: 'WARNING',
      detail: `不可信来源快照占比 ${(untrusted / total * 100).toFixed(1)}%（${untrusted}/${total}）`,
    })
  }
  const reused = Array.isArray(rep.reused_snapshot_groups) ? rep.reused_snapshot_groups : []
  if (reused.length > 0) {
    alerts.push({
      code: 'SNAPSHOT_REUSED',
      severity: 'WARNING',
      detail: `${reused.length} 组重复信号快照被标记（DUPLICATED_SIGNAL_EVIDENCE）`,
    })
  }
  if (typeof rep.asset_multiplier_status === 'string' &&
      ['UNAVAILABLE', 'EXPIRED', 'INVALID', 'PENDING_REVIEW'].includes(rep.asset_multiplier_status)) {
    alerts.push({
      code: 'ASSET_MULTIPLIER_INVALID',
      severity: 'WARNING',
      detail: `资产乘数状态 ${rep.asset_multiplier_status}（本周期按 1.0 处理）`,
    })
  }
  if (rep.llm_failed === true) {
    alerts.push({ code: 'LLM_REVIEW_FAILED', severity: 'WARNING', detail: String(rep.llm_error || '复盘模型失败') })
  }
  const pending = Array.isArray(rep.pending_proposals) ? rep.pending_proposals : []
  if (pending.length > 0) {
    alerts.push({
      code: 'RULE_PROPOSAL_REQUIRES_APPROVAL',
      severity: 'INFO',
      detail: `${pending.length} 条硬规则/基线提案等待人工审批`,
    })
  }
  const memoryRevision = String(rep.memory_revision || '')
  const reportLedger = String(rep.ledger_revision || '')
  const currentLedger = String(inv.ledger_revision || '')
  if (memoryRevision && reportLedger && currentLedger && currentLedger !== reportLedger) {
    alerts.push({
      code: 'REPORT_REVISION_STALE',
      severity: 'WARNING',
      detail: `报告台账 revision(${reportLedger.slice(0, 8)}) 落后于当前(${currentLedger.slice(0, 8)})`,
    })
  }
  return alerts
}

export interface ReviewLayer {
  key: 'facts' | 'hypotheses' | 'rule_proposals'
  title: string
  rows: Array<Record<string, any>>
}

/** 复盘报告三层（§9.2）：事实 / 假设 / 提案，附独立样本组与反例数。 */
export function summarizeReviewLayers(report: unknown, t?: TFn): ReviewLayer[] {
  const rep = (report || {}) as Record<string, any>

  const facts = Array.isArray(rep.facts) ? rep.facts
    : (Array.isArray(rep.fact_texts) ? rep.fact_texts.map((text: any) => ({ text })) : [])
  const hypotheses = Array.isArray(rep.hypotheses) ? rep.hypotheses
    : (Array.isArray(rep.hypothesis_texts) ? rep.hypothesis_texts.map((text: any) => ({ text })) : [])
  const proposals = Array.isArray(rep.rule_proposals) ? rep.rule_proposals
    : (Array.isArray(rep.heuristics) ? rep.heuristics : [])

  return [
    {
      key: 'facts',
      title: t ? t('admin.evolution.layers.facts', '事实') : '事实',
      rows: facts.map((row: any) => ({
        text: typeof row === 'string' ? row : String(row?.text || row?.rule_text || ''),
        evidence_ids: (row && row.evidence_ids) || [],
        sample_size: row?.sample_size ?? null,
        independent_sample_groups: row?.independent_sample_groups ?? null,
      })),
    },
    {
      key: 'hypotheses',
      title: t ? t('admin.evolution.layers.hypotheses', '假设') : '假设',
      rows: hypotheses.map((row: any) => ({
        text: typeof row === 'string' ? row : String(row?.text || row?.rule_text || ''),
        evidence_ids: (row && row.evidence_ids) || [],
        counterexample_count: row?.counterexample_count ?? null,
        scope: row?.scope || null,
      })),
    },
    {
      key: 'rule_proposals',
      title: t ? t('admin.evolution.layers.proposals', '规则提案') : '规则提案',
      rows: proposals.map((row: any) => ({
        text: typeof row === 'string' ? row : String(row?.text || row?.rule_text || ''),
        requested_level: row?.requested_level || row?.evidence_level || 'REVIEWED_HEURISTIC',
        requires_approval: Boolean(row?.requires_approval || row?.approval_required),
        counterexample_count: row?.counterexample_count ?? null,
        scope: row?.scope || null,
      })),
    },
  ]
}

/** 复盘报告页的版本与证据摘要（§9.2：报告与权威记忆的 hash、当前生效规则版本）。 */
export function summarizeReviewEvidence(report: unknown): Array<{ key: string; value: string }> {
  const rep = (report || {}) as Record<string, any>
  const stats = (rep.evidence_stats || {}) as Record<string, any>
  const sources = (rep.snapshot_source_audit || {}) as Record<string, any>
  return [
    { key: 'ledger_revision', value: String(rep.ledger_revision || '--') },
    { key: 'memory_revision', value: String(rep.memory_revision || '--') },
    { key: 'policy_hash', value: String(rep.policy_hash || '--') },
    { key: 'baseline_hash', value: String(rep.baseline_hash || '--') },
    { key: 'review_input_hash', value: String(rep.review_input_hash || '--') },
    { key: 'review_output_hash', value: String(rep.review_output_hash || '--') },
    { key: 'trusted_snapshots', value: String(sources.trusted ?? '--') },
    { key: 'time_unverified_snapshots', value: String(sources.time_unverified ?? '--') },
    { key: 'independent_sample_groups', value: String(stats.independent_sample_groups ?? '--') },
    { key: 'strategy_versions', value: JSON.stringify(stats.strategy_versions || {}) },
    { key: 'asset_multiplier_status', value: String(rep.asset_multiplier_status || '--') },
  ]
}
