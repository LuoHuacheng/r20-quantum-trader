# 自进化

> ⚠️ 本页是**生成式复盘报告的落点**。正式内容由
> `scripts/evolution/report.py::render_self_evolution_doc` 依据
> `data/self_improvement_report.json` 生成，本文件只保留**结构与口径说明**，
> 人工补充必须带 `author` / `created_at` / `source` / `status` / `reviewed_by`。

## 报告时间与版本

- 报告时间：见报告 `timestamp`（北京时间）
- 报告 schema：`report_schema_version = 2`
- 台账 revision / 记忆 revision / 策略 hash / baseline hash：逐份报告内给出
- 证据策略版本：`evidence_policy_version = 2`

## 台账起止与观测性

- 有效交易数、不可观测交易数（PRICE_ONLY + NONE）
- 快照来源分布（`direct_signal_journal` / `matched_signal_journal` /
  `legacy_signal_snapshot` / `calculus_snapshot_fallback` / `unavailable`）
- 独立样本组数量与重复信号证据组数量
- 成本分项：手续费、资金费、滑点（来自台账字段，缺失按不可验证标记）

> ⚠️ **不写死累计盈亏**。任何盈亏数字都必须来自当轮报告，不得在本文件手工固化。

## 基准一致性

- `healthy` / `missing_ids` / `mismatched_ids` / `unexpected_ids`
- 代码基准（`BASELINE_LESSONS`）与结构化记忆中启用的 baseline 每次发布前后必须一致；
  不一致时禁止发布、后台显示 CRITICAL、交易继续使用代码硬规则。

## 事实 / 已审核启发式 / 待验证观察 / 规则提案

四层**必须分开**：

| 层 | 含义 | 能否进交易提示词 |
|---|---|---|
| 事实 `facts` | 可从台账直接读出 | 不进（供复盘） |
| 已审核启发式 | ≥2 个独立样本组 + ≥2 个时间窗口 + 反例检查 + 声明 scope | 进（辅助证据，不得覆盖硬规则） |
| 待验证观察 `observation_only` | 样本/独立性不足，降级保留 | 进【待验证观察】区块，不得单独构成开仓理由 |
| 规则提案 `rule_proposals` | 触碰硬规则或声称基线失效 | **不进**，进人工审批队列 |

## 当前执行规则引用

- 执行策略：`execution_policy.mode` / `revision` / `rule_set` / `rule_set_hash`
- 风险配置 hash 与 baseline hash（与策略快照同源）
- 资产乘数状态（`REVIEWED` / `NEUTRAL` / `UNAVAILABLE` / `EXPIRED` / `INVALID` / `PENDING_REVIEW`）

## 数值口径（禁止混写）

- **杠杆**：风控页配置区间内的当前配置，不是"固定 3.0x"。
- **止损**：`initial_stop = 入场价 ∓ ATR × stop_loss_atr_mult`；本仓执行层使用 15M ATR，
  回测必须显式注明周期。
- **保本**：峰值浮盈 ≥ `breakeven_trigger_atr × ATR` 时把止损推到保本 ——
  现行口径是 **ATR**（`breakeven_mode = ATR_MULTIPLE`），**不是** R 口径；
  `initial_risk_px` 同时逐笔记录，供后续切换评估。
- **RSI/Jerk**：属**目标策略参数**，只对 `trend_following_*` 生效；
  均值回归策略不使用同一门禁；边界严格（RSI=75.0 放行、75.01 拒绝）。
- **资产乘数**：只缩放模型申请的保证金，不改变杠杆/止损/熔断/置信度门槛。
- **历史观察 / 当前配置 / 目标参数**：三者必须分别标注，历史参数不得写成当前配置。

## 回滚说明

1. **记忆**：`POST /api/v1/admin/memory/rollback?expected_version=…` 原子回滚到代码基准；
   或 `scripts/migrate_evolution_memory.py --apply --expected-version <hash>` 做 schema 迁移。
2. **硬规则**：关闭新策略模式的新开仓；已有仓位继续用入场时冻结的规则版本保护退出；
   不回滚到无法读取新字段的旧程序。
3. **资产乘数**：视为 1.0，`data/asset_multipliers.json` 保留原文件作审计，
   不覆盖历史交易记录，下一轮复盘重新生成。

## 人工补充

<!--
人工补充格式（缺任一字段则该段在下一轮生成时会被覆盖）：

author: <姓名>
created_at: <YYYY-MM-DD HH:MM:SS>
source: <台账/凭据/链接>
status: <DRAFT|REVIEWED>
reviewed_by: <复核人>
-->
