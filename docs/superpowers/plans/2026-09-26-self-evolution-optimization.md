# R20 自进化系统正确优化实施规格

**文档状态：** 待实施

**目标：** 将当前自进化系统从“复盘文本驱动提示词”优化为“证据可追溯、基准受保护、软启发式可控、硬规则代码执行、策略与记忆版本一致”的自进化体系。

**适用范围：** 自进化复盘、结构化长期记忆、交易提示词、风险拦截器、资产乘数、策略快照、后台展示、调度、回测、迁移与回滚。

**不包含：** 不在本阶段直接修改真实交易参数，不直接替换当前结构化记忆，不自动启用 RSI/Jerk 硬门禁，不自动把文档中的 3.0x 杠杆写入全局配置。

**当前项目事实：** 当前仓库已经具备结构化记忆权威源、Markdown 镜像、防基准删除逻辑、NO_CHANGE、快照可观测性审计、资产乘数和策略快照。现有自进化相关测试 240 项通过。需要修复的是权威记忆与代码基准可能脱节、历史快照 fallback 的证据边界、文档与当前数据不一致，以及软规则和硬规则边界不清。

## 1. 设计结论

### 1.1 四层规则模型

系统将规则分为四层，任何一层都不能冒充另一层：

| 层级 | 内容 | 运行时权限 |
|---|---|---|
| L0 宿主硬约束 | 数据有效性、账户安全、杠杆/保证金上限、OCO、冷却、禁止马丁格尔 | 只能由代码执行，提示词不可覆盖 |
| L1 策略硬规则 | 某策略的 RSI/Jerk/Donchian/ADX/止损/退出规则 | 由版本化策略代码执行，必须有测试和策略版本 |
| L2 已审核启发式 | 多个独立样本支持的方向、成本、仓位、市场状态经验 | 可注入提示词，不能直接放宽 L0/L1 |
| L3 待验证假设 | 单次异常、样本少、证据不完整、因果不明确的观察 | 只能进入复盘报告和观察清单，不注入交易提示词 |

`docs/self-evolution.md` 默认属于复盘报告，不能直接作为 L0/L1 配置源。

### 1.2 数据源权威顺序

```text
代码硬规则
  > 策略执行配置
  > 结构化基准心法
  > 已审核启发式记忆
  > 复盘报告
  > LLM 输出文本
```

模型输出永远不能提升自身权限。模型可以提出 L2/L3 内容，不能直接创建 L0/L1 内容。

### 1.3 当前文档的处理方式

现有 [`docs/self-evolution.md`](../../self-evolution.md) 中的内容需要拆分：

- 历史交易事实：保留，但补充 `ledger_revision`、统计周期、样本数和快照可观测性。
- “完全由某原因引发”“高度正期望”“彻底隔绝回撤”等结论：改为“待验证假设”或删除过度因果表述。
- 黄金心法：迁移为带 ID、版本、状态的基准/启发式条目。
- 行动清单：拆为“代码已实现”“提案待审核”“仅观察”三类。
- 3.0x、1.8x～2.2x ATR、0.8R～1.0R 等数值：必须标明是历史观察、当前配置还是目标策略参数，不能混写。

## 2. 当前问题与优化目标

### 2.1 基准心法可能脱离权威记忆

代码在 [`scripts/evolution_shield.py`](../../../scripts/evolution_shield.py) 中定义了 `BASELINE_LESSONS`，但 `merge_memory_with_constitution()` 主要保护当前权威快照里已有的 `is_baseline=true` 条目。

如果当前权威 JSON 中基准条目已不存在，普通发布流程没有足够信息将代码基准重新补回。优化后必须保证：

```text
代码 BASELINE_LESSONS
    与
结构化记忆中启用的 baseline
    每次发布前后都一致
```

### 2.2 历史证据可能被错误 fallback

[`scripts/self_improvement_engine.py`](../../../scripts/self_improvement_engine.py) 在没有逐笔 `signal_snapshot` 时，会尝试读取全局 `calculus_snapshot.json`。如果该文件缺少严格的交易时间戳，当前指标可能被错误当成开仓时证据。

优化后：

- 无时间证明的 fallback 只能标记 `NONE` 或 `PRICE_ONLY`；
- 不得因为字段完整就提升为 `DYNAMICS_OBSERVED`；
- 每个快照必须记录来源和时间差；
- 复盘模型只能使用宿主已经确认的证据等级。

### 2.3 复盘文本和交易硬规则混在一起

当前系统会将长期记忆注入交易提示词，资产乘数会影响模型申请的保证金，但 RSI/Jerk、止损和保本等建议不会自动成为硬规则。

优化后：

- L2/L3 仍可进入提示词；
- L0/L1 必须从代码策略配置读取；
- LLM 只能提交规则提案；
- 新硬规则需要人工审核或显式策略版本发布；
- 提示词必须展示“当前生效规则”和“待验证观察”，避免模型自行升级规则权限。

### 2.4 当前数值语义不统一

必须明确以下口径：

- 杠杆是固定 3.0x，还是可配置低杠杆区间；
- 初始止损按 ATR，还是按初始 R；
- `0.8R` 与 `0.8 * ATR` 不能混写；
- RSI 极值是否只限制趋势追随，不能误伤均值回归；
- Jerk 阈值是 1.8、2.5 还是其他值；
- 资产乘数影响保证金还是风险额；
- 同一交易的多条台账记录是否属于独立样本。

这些值必须在策略版本中冻结，并出现在决策快照和回测报告中。

## 3. 目标数据模型

### 3.1 记忆条目

扩展结构化记忆条目。旧字段保持兼容，新字段缺失时按旧条目读取，但新发布必须写完整字段。

```json
{
  "id": "lesson_anti_extreme_chase",
  "category": "TACTICAL",
  "rule_text": "趋势追随多单在15M RSI>75时不追价；该规则不限制独立的均值回归多单。",
  "enabled": true,
  "is_baseline": false,
  "status": "ACTIVE",
  "evidence_level": "REVIEWED_HEURISTIC",
  "health_score": 90.0,
  "created_at": "2026-09-26T08:00:00+00:00",
  "updated_at": "2026-09-26T08:00:00+00:00",
  "ttl_days": 14,
  "sample_size": 12,
  "independent_sample_groups": 4,
  "counterexample_count": 3,
  "source_ledger_revision": "sha256:...",
  "source_strategy_versions": ["legacy-v1"],
  "scope": {
    "strategy_modes": ["trend_following"],
    "timeframes": ["15m", "1h"],
    "directions": ["long"]
  },
  "shield_status": "PASSED",
  "approval": {
    "required": false,
    "status": "NOT_REQUIRED",
    "approved_by": "",
    "approved_at": ""
  }
}
```

`evidence_level` 只允许：

```text
BASELINE_HARD_RULE
REVIEWED_HEURISTIC
PROPOSED_HEURISTIC
OBSERVATION_ONLY
```

`status` 只允许：

```text
ACTIVE
DISABLED
RETIRED
REJECTED
```

### 3.2 基准心法一致性报告

新增确定性报告结构：

```json
{
  "code_baseline_count": 4,
  "authority_baseline_count": 4,
  "missing_ids": [],
  "mismatched_ids": [],
  "unexpected_ids": [],
  "healthy": true,
  "checked_at": "2026-09-26T08:00:00+00:00"
}
```

`healthy=false` 时：

- 禁止自进化发布；
- 禁止把记忆提案标记为已生效；
- 交易继续使用代码硬规则；
- 后台显示 CRITICAL；
- 允许人工恢复，不允许模型自动覆盖。

### 3.3 逐笔证据快照

扩展当前 `entry_snapshot`，至少包含：

```json
{
  "schema_version": 2,
  "snapshot_source": "direct_signal_journal",
  "snapshot_observability": "STRATEGY_OBSERVED",
  "captured_at": "2026-09-26T08:00:03+00:00",
  "signal_time": "2026-09-26T08:00:00+00:00",
  "open_time": "2026-09-26T08:00:05+00:00",
  "age_seconds_at_fill": 5,
  "strategy_version": "trend_confirm_5m@1",
  "policy_hash": "abcd1234",
  "venue": "okx",
  "instId": "BTC-USDT-SWAP",
  "side": "long",
  "price": 100.0,
  "atr_1h": 2.0,
  "rsi_15m": 64.0,
  "adx_5m": 27.0,
  "adx_1h": 24.0,
  "jerk_15m": 0.8,
  "regime": "BULL_TREND",
  "donchian": {
    "upper20": 99.0,
    "lower20": 95.0,
    "upper10": 99.0,
    "lower10": 96.0,
    "breakout": "long"
  },
  "risk": {
    "leverage": 3.0,
    "margin_usdt": 10.0,
    "initial_stop": 89.9,
    "initial_risk_px": 10.1
  }
}
```

### 3.4 快照来源枚举

```text
direct_signal_journal
matched_signal_journal
legacy_signal_snapshot
calculus_snapshot_fallback
unavailable
```

只有以下来源可以用于完整策略因果归因：

```text
direct_signal_journal
matched_signal_journal
```

`calculus_snapshot_fallback` 只有在存在合法 `captured_at`、`signal_time` 和交易关联 ID 时才可以升级，否则只能作为普通观测。

### 3.5 可观测性分类

保留当前 `DYNAMICS_OBSERVED/PARTIAL/PRICE_ONLY/NONE`，新增策略专属分类：

```text
STRATEGY_OBSERVED
STRATEGY_PARTIAL
DYNAMICS_OBSERVED
DYNAMICS_PARTIAL
PRICE_ONLY
NONE
```

分类规则：

- `STRATEGY_OBSERVED`：目标策略所需的全部字段、时间和版本完整；
- `STRATEGY_PARTIAL`：策略字段部分存在，但不能完成全链路归因；
- `DYNAMICS_OBSERVED`：旧动力学字段完整，但不一定包含目标策略字段；
- `PRICE_ONLY`：只有价格、盈亏等普通字段；
- `NONE`：没有可用快照。

## 4. 代码改动地图

### 4.1 `scripts/evolution_shield.py`

修改功能：

1. 增加 `baseline_manifest()`，将代码中的 `BASELINE_LESSONS` 转成稳定的 ID、文本和 hash。
2. 增加 `check_baseline_consistency(lessons)`。
3. 修改 `publish_review()`：发布前先合并代码基准，发布后再次校验。
4. 修改 `save_structured_memory()`：普通接口不允许删除、停用或修改 baseline 文本。
5. 修改 `admin_mutate()`：
   - 删除 baseline 返回明确错误；
   - 停用 baseline 要求人工确认 token；
   - 普通 `replace` 不得绕过基准校验。
6. 保留 `rollback_to_baseline()`，但使其使用同一个 baseline 合并函数。
7. 将 `is_lesson_expired()`、`render_lessons()` 和 `injection_report()` 的过期、启用、注入数量统一到一个筛选函数。
8. 记忆文件损坏时继续 fail-closed，不能用 `BASELINE_LESSONS` 静默覆盖原文件。

建议接口：

```python
def baseline_manifest() -> dict[str, dict]: ...
def check_baseline_consistency(lessons: list[dict]) -> dict: ...
def merge_code_baselines(lessons: list[dict]) -> tuple[list[dict], dict]: ...
def select_injected_lessons(lessons: list[dict], now=None) -> dict: ...
```

实现要求：

- 文本 hash 使用 SHA-256 截断值；
- 结果按 ID 排序；
- 同一 baseline ID 文本变化视为不一致；
- 不在启动时自动写盘；
- 所有发布写入都通过文件锁和原子替换。

测试：

```text
tests/llm/test_evolution_shield.py
 tests/ops/test_self_improvement_engine.py
 tests/core/test_memory_routes_isolated.py
```

新增场景：

- 权威文件缺少 baseline 时发布被拒绝或补回；
- 删除 baseline 被拒绝；
- baseline 文本被修改时报告不一致；
- 记忆文件损坏时不被自动覆盖；
- 8 条注入上限与后台显示一致。

### 4.2 `scripts/self_improvement_engine.py`

修改功能：

1. `load_closed_trades()` 增加快照来源、时间差、策略版本和证据状态。
2. 删除无时间证明的全局 snapshot 因果 fallback，或只能以 `OBSERVATION_ONLY` 保存。
3. 账单统计增加：
   - 毛盈亏；
   - 净盈亏；
   - 手续费；
   - 资金费；
   - 滑点；
   - 保护失败次数；
   - 快照来源分布；
   - 策略版本分布；
   - 交易所分布。
4. 复盘报告保存：
   - `ledger_revision`；
   - `memory_revision`；
   - `policy_hash`；
   - `snapshot_audit`；
   - `baseline_consistency`；
   - `review_input_hash`；
   - `review_output_hash`。
5. 资产乘数写盘时增加来源、有效期、证据 revision 和审批状态。
6. 资产乘数只影响保证金申请，不得修改风险常量、杠杆上限、止损倍数和熔断线。
7. 资产乘数读取失败时使用 1.0，并记录 `adaptive_multiplier_status=UNAVAILABLE`，不能使用旧文件中的未知值。
8. 无新台账证据时不调用 LLM，继续保留旧配置和旧报告。
9. LLM 返回 `__llm_error__` 时不能生成新的自进化规则；只写失败报告。

建议报告结构：

```json
{
  "report_schema_version": 2,
  "generated_at": "2026-09-26T08:00:00+00:00",
  "ledger_revision": "sha256:...",
  "memory_revision": "sha256:...",
  "policy_hash": "abcd1234",
  "total_trades": 52,
  "snapshot_audit": {
    "strategy_observed": 0,
    "strategy_partial": 0,
    "dynamics_observed": 30,
    "price_only": 12,
    "none": 10
  },
  "baseline_consistency": {
    "healthy": true,
    "missing_ids": [],
    "mismatched_ids": []
  },
  "facts": [],
  "heuristics": [],
  "proposals": [],
  "asset_multiplier_status": "REVIEWED",
  "change_status": "NO_CHANGE"
}
```

### 4.3 `scripts/evolution/observability.py`

修改功能：

1. 增加策略字段集合，例如：

```python
STRATEGY_FIELDS = (
    "strategy_version",
    "snapshot_source",
    "captured_at",
    "signal_time",
    "rsi_15m",
    "adx_5m",
    "adx_1h",
    "atr_1h",
    "jerk_15m",
    "regime",
    "initial_stop",
    "initial_risk_px",
)
```

2. 增加 `classify_strategy_snapshot()`。
3. `audit_snapshot_observability()` 同时输出旧动力学和策略证据统计。
4. 增加来源统计，不只统计字段数量。
5. 将“字段存在”与“字段可信”分开：时间不合法、来源不可信、版本缺失时不得判定完整。
6. 增加重复快照检测：同一标的、不同时间、完全相同的信号指标超过阈值时，标记 `DUPLICATED_SIGNAL_EVIDENCE`。

新增接口：

```python
def classify_strategy_snapshot(snapshot: dict) -> str: ...
def audit_snapshot_sources(closed_trades: list[dict]) -> dict: ...
def detect_reused_snapshots(closed_trades: list[dict]) -> list[dict]: ...
```

### 4.4 `scripts/evolution/review_context.py`

修改提示词上下文装配：

1. 统计中增加证据来源和独立样本组数。
2. `build_host_constitution()` 增加以下硬约束：
   - 没有时间证明的快照不得作因果证据；
   - 同一信号拆成多笔订单不算多个独立样本；
   - 资产乘数不是硬风控；
   - 复盘输出不能直接改变交易参数；
   - baseline 一致性异常时只能输出报告，不能发布。
3. `normalize_asset_multipliers()` 增加：
   - 缺失默认为 1.0；
   - 超过 `0.5～1.5` 拒绝；
   - 不能为未知标的生成条目；
   - 保存提案来源和审核状态。
4. 将当前心法拆成：
   - `active_baselines`；
   - `active_reviewed_heuristics`；
   - `observation_only`；
   - `retired_lessons`。

### 4.5 `scripts/evolution/memory_review.py`

修改功能：

1. `apply_memory_review()` 只负责合并和发布，不负责把模型文本变成硬规则。
2. 引入提案校验结果：

```python
proposal_result = {
    "accepted": True,
    "evidence_level": "REVIEWED_HEURISTIC",
    "rejection_reasons": [],
    "requires_approval": False,
}
```

3. 新增规则提案必须满足：
   - 至少 2 个独立样本；
   - 至少 2 个独立时间窗口；
   - 样本不能只是同一信号的拆单；
   - 有盈利和亏损反例检查；
   - 不得与 L0/L1 冲突；
   - 文本必须声明适用范围；
   - 不得引入新的风险上限或放宽阈值。
4. 只满足样本数但没有独立性时，降级为 `OBSERVATION_ONLY`。
5. `REVISE` 和 `INVALIDATE` 不得物理删除 baseline；非基准旧条目可以退役，但保留审计字段。
6. 模型声称某个 baseline 失效时，只进入 `requires_approval=true` 的人工审核队列。

### 4.6 `scripts/evolution_shield.py::audit_proposed_lesson`

现有文本红线检查继续保留，并增加结构化检查：

- 禁止以“胜率锁死”“彻底避免亏损”“保证正期望”等词作为已验证结论；
- 禁止单笔或单窗口结论标记为 `REVIEWED_HEURISTIC`；
- 禁止没有 `scope` 的规则进入交易提示词；
- 禁止规则文本修改 L0/L1 参数；
- 禁止提案把“建议”写成“当前硬规则”；
- 禁止模型直接生成 `is_baseline=true`；
- `is_baseline` 只能来自代码 manifest。

## 5. 硬规则与交易代码改动

### 5.1 新增策略规则注册表

新增文件：

```text
scripts/strategy_rules.py
```

该模块只保存版本化规则和纯函数，不读网络、不读 LLM、不写文件。

建议结构：

```python
TREND_FOLLOWING_RULES = {
    "version": "trend_following@1",
    "rsi_long_chase_ceiling": 75.0,
    "rsi_short_chase_floor": 28.0,
    "reverse_jerk_abs_floor": 2.5,
    "breakeven_mode": "R_MULTIPLE",
    "breakeven_trigger_r": 0.8,
}
```

这些值只是目标配置示例，实施前必须通过回测和策略审核确认，不能直接照搬文档数字。

新增纯函数：

```python
def reject_extreme_chase(*, action: str, rsi: float | None,
                         setup_kind: str, rules: dict) -> tuple[bool, str]: ...

def reject_reverse_shock(*, action: str, jerk: float | None,
                         rules: dict) -> tuple[bool, str]: ...

def breakeven_trigger_px(*, is_long: bool, entry_px: float,
                         initial_stop_px: float, trigger_r: float) -> float: ...
```

### 5.2 RSI/Jerk 只作用于指定策略

不能全局写成：

```text
RSI > 75 禁止任何多单
RSI < 28 禁止任何空单
```

应按 `setup_kind` 区分：

```text
trend_following_long / trend_following_short：启用极值追价门禁
mean_reversion_long / mean_reversion_short：不使用同一门禁
unknown：fail-closed，禁止开仓
```

如果系统切换到 5m 趋势确认策略，5m 策略必须显式使用 `trend_following`，不能依赖名称推断。

### 5.3 核心拦截层

修改：

```text
r20_backend/interceptor_manager.py
```

核心校验顺序：

1. 数据有效性；
2. 账户和持仓冲突；
3. 结构化策略硬规则；
4. 价格几何和 R:R；
5. 资金、杠杆和敞口；
6. 用户可编辑插件；
7. 最终下单前再次复验。

用户插件可以进一步拒绝，不能放宽核心规则。

### 5.4 入口路径

修改：

```text
scripts/trader/entry_execution.py
scripts/trader/signals.py
scripts/trader/order_submit.py
scripts/trader/sizing.py
scripts/trader/brackets.py
```

要求：

- `signals.py` 可继续生成解释和候选，但不作为最终安全判断；
- `entry_execution.py` 使用统一策略规则做硬门禁；
- `order_submit.py` 下单前重新获取当前数据和策略 hash；
- `sizing.py` 使用最终止损距离计算风险数量；
- `brackets.py` 保持价格几何检查；
- 任何策略规则读取失败时拒绝新开仓；
- 旧 `legacy` 模式继续走旧规则；
- 新模式使用明确的 `strategy_mode`，不靠提示词名称识别。

### 5.5 止损与保本语义

必须在代码和提示词中选择一个统一口径。

推荐采用 R 口径：

```text
initial_risk_px = abs(entry_price - initial_stop_price)
profit_px >= 0.8 * initial_risk_px
```

tracker 增加：

```text
initial_stop_px
initial_risk_px
breakeven_trigger_r
breakeven_mode
strategy_rule_version
```

修改：

```text
scripts/trader/position_exit.py
scripts/trader/protection.py
scripts/trader/position_mgmt.py
```

如果项目决定继续使用 ATR，则必须把文档中的 `0.8R` 全部改成 `0.8x ATR`，不能继续混用。

### 5.6 资产乘数边界

当前资产乘数可以影响 `margin_usdt`。优化后：

- 资产乘数只能影响模型申请保证金的软缩放；
- 最终数量仍受实际止损风险、余额、单标的上限和组合上限约束；
- 不能提高杠杆；
- 不能扩大止损；
- 不能降低置信度门槛；
- 不能覆盖冷却；
- 不能自动生成新的交易标的；
- 7 天 TTL 到期后自动回到 1.0；
- 版本或台账 revision 变化后必须重新确认是否继续有效。

修改：

```text
scripts/brain/decisions.py
scripts/risk_constants.py
scripts/trader/sizing.py
scripts/self_improvement_engine.py
```

### 5.7 相关标的敞口

文档中的相关标的同向敞口建议不能只停留在心法文本。

分两步：

第一步，使用现有全局同向持仓和总敞口限制，并增加相关组配置：

```json
{
  "correlation_groups": {
    "large_cap_crypto": ["BTC", "ETH"],
    "high_beta_crypto": ["SOL", "DOGE", "SUI"]
  }
}
```

第二步，执行层计算：

```text
同一相关组、同一方向的已有风险 + 新单风险 <= group_risk_cap
```

相关组风险配置属于 L0/L1，不能由 LLM 或记忆文本修改。

修改：

```text
scripts/risk_constants.py
scripts/trader/entry_execution.py
r20_backend/execution_router.py
r20_backend/execution/risk_gates.py
```

## 6. 自进化提示词改动

### 6.1 `EVOLUTION_SYSTEM_PROMPT`

修改：

```text
scripts/self_improvement_engine.py
scripts/evolution/review_context.py
```

新增明确要求：

1. 先报告事实，再报告解释，再报告提案。
2. `PRICE_ONLY/NONE` 不得进行指标因果归因。
3. `calculus_snapshot_fallback` 未完成时间验证时只能作为观察。
4. 同一信号拆成多笔订单只能算一个独立样本组。
5. 结论必须标明适用策略、周期、方向、交易所和样本窗口。
6. 不得使用“证明正期望”“完全由”“彻底隔绝”等绝对表述，除非属于代码行为事实。
7. 不得把软启发式写成硬规则。
8. 不得直接生成或修改 baseline。
9. 不得修改杠杆、保证金、熔断、止损和持仓上限。
10. 证据不足时输出 `NO_CHANGE` 或 `OBSERVATION_ONLY`。

### 6.2 新复盘 JSON 契约

```json
{
  "change_status": "NO_CHANGE",
  "facts": [
    {
      "text": "截至本轮台账 revision 的可观测事实",
      "evidence_ids": ["trade:123"],
      "evidence_level": "OBSERVED",
      "sample_size": 12,
      "independent_sample_groups": 4
    }
  ],
  "hypotheses": [
    {
      "text": "待验证解释",
      "evidence_ids": ["trade:123", "trade:456"],
      "counterexample_count": 3,
      "scope": {
        "strategy_modes": ["trend_following"],
        "timeframes": ["15m"]
      }
    }
  ],
  "rule_proposals": [
    {
      "rule_id": "anti_extreme_chase",
      "text": "建议趋势追随多单在 RSI>75 时拒绝追价",
      "scope": {
        "strategy_modes": ["trend_following"],
        "directions": ["long"]
      },
      "requested_level": "REVIEWED_HEURISTIC",
      "requires_approval": false,
      "evidence_ids": ["trade:123", "trade:456"]
    }
  ],
  "asset_multipliers": {
    "BTC": 1.0
  },
  "ai_long_term_memory": [],
  "memory_overwrites_reason": "证据不足，保留当前记忆"
}
```

### 6.3 提示词中的硬规则注入

交易主脑提示词必须将硬规则以单独区块注入：

```text
【宿主硬规则】
以下规则来自代码和冻结策略版本，模型不得修改：
- 数据缺失时 WAIT
- 当前策略版本: trend_following@1
- RSI/Jerk 规则版本: rule-set@1
- 当前风险配置 hash: ...
- 当前 baseline hash: ...
```

长期记忆区块必须改为：

```text
【已审核启发式】
仅作为辅助证据，不能覆盖宿主硬规则。

【待验证观察】
不得单独构成开仓理由。
```

修改：

```text
scripts/brain/prompt.py
scripts/prompt_library.py
scripts/prompt_templates.py
scripts/ai_brain_trader.py
```

### 6.4 自进化提示词与交易提示词隔离

自进化模型可以看到：

- 交易事实；
- 证据来源；
- 规则版本；
- 交易结果；
- 反例和成本。

交易模型可以看到：

- 已审核启发式；
- 当前硬规则摘要；
- 当前风险预算；
- 当前市场数据。

交易模型不应看到：

- 未审核的规则提案；
- 过期或未注入的历史记忆；
- 复盘模型的内部推理；
- 其他策略模式的规则。

## 7. 策略配置与提示词库改动

### 7.1 Profile 增加策略模式

修改：

```text
scripts/prompt_library.py
r20_backend/schemas.py
r20_backend/routers/strategy/prompts.py
r20_backend/policy/schema.py
r20_backend/policy/fingerprints.py
r20_backend/policy/capture.py
r20_backend/policy/restore.py
```

新增：

```json
{
  "execution_policy": {
    "mode": "legacy",
    "revision": 1,
    "rule_set": "legacy@1"
  }
}
```

5m 趋势策略示例：

```json
{
  "execution_policy": {
    "mode": "trend_confirm_5m",
    "revision": 1,
    "rule_set": "trend_confirm_5m@1"
  }
}
```

规则：

- 缺少字段的旧 profile 解释为 `legacy`；
- 未知 mode 或 revision 拒绝激活；
- profile ID 与 execution mode 分开；
- 导入后生成新 profile ID，但不能丢策略模式；
- policy snapshot 同时记录 prompt hash、memory hash、rule set hash、risk config hash；
- profile 文字不能覆盖代码硬规则。

### 7.2 `docs/self-evolution.md` 改造

文档改为生成式复盘报告，至少包含：

```text
报告时间
台账起止时间
ledger_revision
memory_revision
policy_hash
有效交易数
不可观测交易数
基准一致性
已审核启发式
待验证观察
规则提案
当前执行规则引用
回滚说明
```

文档中禁止：

- 手工写死当前累计盈亏；
- 不标样本量的“证明”结论；
- 将历史参数写成当前配置；
- 将观察写成硬风控；
- 声称系统已经“彻底隔绝”某种风险。

可以保留人工补充，但人工补充必须有：

```text
author
created_at
source
status
reviewed_by
```

## 8. 策略快照、日志和审计改动

### 8.1 策略快照

修改：

```text
r20_backend/policy/schema.py
r20_backend/policy/fingerprints.py
r20_backend/policy/capture.py
r20_backend/policy/restore.py
```

策略快照增加：

```json
{
  "execution_policy": {
    "mode": "trend_confirm_5m",
    "revision": 1,
    "rule_set_hash": "..."
  },
  "memory": {
    "authority_hash": "...",
    "baseline_hash": "...",
    "injected_lesson_ids": []
  },
  "risk_config_hash": "...",
  "evidence_policy_version": "2"
}
```

### 8.2 交易台账

修改：

```text
scripts/trader/ledger_writer.py
scripts/trader/signal_snapshot.py
scripts/self_improvement_engine.py
```

每笔开仓至少记录：

- `strategy_mode`；
- `strategy_rule_version`；
- `policy_hash`；
- `memory_revision`；
- `signal_id`；
- `snapshot_source`；
- `snapshot_observability`；
- `entry_snapshot`；
- `initial_stop_px`；
- `initial_risk_px`；
- `breakeven_mode`；
- `asset_multiplier`；
- `risk_budget_snapshot`；
- `venue`；
- `environment`。

每笔平仓至少记录：

- `exit_reason_code`；
- `strategy_stop_px`；
- `emergency_stop_px`；
- `pnl_gross`；
- `fees`；
- `funding`；
- `slippage_estimate`；
- `snapshot_at_exit`；
- `cooldown_recorded`。

### 8.3 审计日志

新增稳定 reason code，不依赖中文自由文本：

```text
MEMORY_BASELINE_MISMATCH
MEMORY_PUBLISH_REJECTED
SNAPSHOT_TIME_UNVERIFIED
SNAPSHOT_REUSED
RULE_PROPOSAL_OBSERVATION_ONLY
RULE_PROPOSAL_REQUIRES_APPROVAL
HARD_RULE_BLOCKED
ASSET_MULTIPLIER_APPLIED
ASSET_MULTIPLIER_EXPIRED
ASSET_MULTIPLIER_INVALID
```

## 9. 后台与前端功能

### 9.1 记忆管理页面

修改：

```text
r20_backend/routers/strategy/prompts.py
r20_backend/schemas.py
frontend/src/views/admin/PromptStudioPage.vue
frontend/src/views/admin/promptStudioLogic.ts
```

后台应显示：

- 总记忆条数；
- 启用条数；
- 实际注入条数；
- 未注入条数及原因；
- baseline 数量；
- baseline 一致性；
- 已审核启发式数量；
- 待验证观察数量；
- 过期数量；
- 最近 ledger revision；
- 最近 memory revision。

操作权限：

| 操作 | 普通管理员 | 超级管理员 |
|---|---:|---:|
| 查看 | 是 | 是 |
| 启停普通启发式 | 是 | 是 |
| 删除普通启发式 | 是 | 是 |
| 停用 baseline | 否 | 需确认 token |
| 审核硬规则提案 | 否 | 是 |
| 修改 L0/L1 风控参数 | 走现有风险配置权限 | 走现有风险配置权限 |

### 9.2 复盘报告页面

增加：

- 事实/假设/提案三个标签页；
- 证据交易列表；
- 独立样本组数量；
- 反例数量；
- 快照来源和时间差；
- 规则提案审批状态；
- 当前生效规则版本；
- 报告与权威记忆的 hash。

不能只显示模型生成的 `diagnosis_insights` 文本。

### 9.3 告警

以下情况进入管理员告警：

- baseline 不一致；
- 权威记忆损坏；
- 复盘输入快照不可观测比例超过阈值；
- 交易台账大量重复快照；
- 资产乘数文件过期或损坏；
- 规则提案等待审批超过 TTL；
- 自进化模型多次失败；
- report revision 与 memory revision 不一致。

## 10. 调度和运行流程

### 10.1 复盘周期

修改：

```text
r20_backend/scheduler.py
r20_gateway/scheduler.py
scripts/self_improvement_engine.py
```

运行流程：

1. 读取账户和策略快照；
2. 读取台账并计算 `ledger_revision`；
3. 如果没有新平仓证据，直接 NO_CHANGE，不调用模型；
4. 检查 baseline 一致性；
5. 构造证据审计；
6. 调用 LLM 复盘；
7. 解析事实、假设、提案和资产乘数；
8. 运行宪法检查；
9. 只发布合格的 L2 记忆；
10. 资产乘数单独保存并带 TTL；
11. 同步 Markdown 镜像；
12. 生成报告；
13. 更新 policy snapshot。

### 10.2 失败语义

| 失败 | 处理 |
|---|---|
| 记忆损坏 | 不覆盖，停止发布，继续硬规则交易 |
| baseline 不一致 | 停止自进化发布，告警，继续硬规则交易 |
| LLM 超时 | NO_CHANGE，保留旧记忆 |
| LLM 输出非法 | NO_CHANGE，报告解析失败 |
| 资产乘数损坏 | 使用 1.0，告警 |
| Markdown 镜像失败 | 保留结构化权威，报告镜像失败 |
| 台账证据不完整 | 只能生成观察，不得写审核启发式 |
| 硬规则提案出现 | 进入人工审批，不直接生效 |
| report 写入失败 | 不影响交易，记录失败；下轮根据 ledger revision 重试 |

### 10.3 交易周期使用记忆

交易主脑启动时冻结：

- `policy_hash`；
- `memory_revision`；
- `baseline_hash`；
- `injected_lesson_ids`；
- `rule_set_hash`。

周期中途记忆更新不能改变本周期已经生成的订单意图。下一周期才读取新版本。

## 11. 数据迁移方案

### 11.1 不直接覆盖当前记忆

当前权威记忆包含历史启用、停用和退役条目。迁移必须先生成差异报告：

```text
当前条目数
当前启用条目数
当前 baseline 数量
代码 baseline 数量
缺失 baseline
文本不一致 baseline
重复条目
过期条目
无法验证来源的条目
```

### 11.2 迁移步骤

1. 读取 `structured_trading_memory.json`；
2. 校验 JSON、revision、条目 schema；
3. 对照 `BASELINE_LESSONS` 生成 manifest；
4. 对每条历史心法补默认 `evidence_level`：
   - baseline 为 `BASELINE_HARD_RULE`；
   - 当前启用且有样本字段为 `REVIEWED_HEURISTIC`；
   - 无法确认来源为 `OBSERVATION_ONLY`；
5. 对历史条目补 `source_ledger_revision` 为空并标记 `LEGACY_MIGRATION`；
6. 不自动启用停用条目；
7. 不自动删除历史条目；
8. 将代码基准加入待确认迁移列表；
9. 人工确认后原子提交；
10. 生成迁移前后 hash；
11. 同步 Markdown 镜像；
12. 运行 memory consistency check。

### 11.3 迁移工具

新增：

```text
scripts/migrate_evolution_memory.py
```

命令：

```bash
.venv/bin/python scripts/migrate_evolution_memory.py --check
.venv/bin/python scripts/migrate_evolution_memory.py --report data/evolution_memory_migration.json
.venv/bin/python scripts/migrate_evolution_memory.py --apply --expected-version <hash>
```

默认只读。没有 `--apply` 不写任何数据。`--apply` 必须带读取时得到的 `expected-version`，防止覆盖并发更新。

## 12. 回测和验证

### 12.1 复盘逻辑测试

新增：

```text
tests/llm/test_evolution_memory_contract.py
tests/llm/test_evolution_baseline_consistency.py
tests/llm/test_evolution_prompt_contract.py
tests/llm/test_evolution_proposal_gates.py
tests/trading/test_strategy_rule_gates.py
tests/ops/test_evolution_migration.py
```

覆盖：

- baseline 缺失；
- baseline 文本篡改；
- baseline 删除；
- 记忆损坏；
- NO_CHANGE；
- LLM 失败；
- 快照来源不可信；
- 快照未来时间；
- 快照过期；
- 同信号拆单去重；
- 反例不足；
- 规则提案只能进入待审核；
- 资产乘数边界；
- 资产乘数过期；
- RSI/Jerk 多空对称门禁；
- 均值回归策略不被趋势追随门禁误伤；
- R 与 ATR 语义不可混用。

### 12.2 提示词测试

新增断言：

```text
交易提示词必须包含当前硬规则版本
交易提示词必须区分已审核启发式和待验证观察
交易提示词不得包含未审核规则提案
自进化提示词必须包含 ledger_revision
自进化提示词必须包含 snapshot source 统计
自进化提示词必须要求反例和独立样本组
profile 自定义模块不能删除宿主宪章
```

修改测试：

```text
tests/llm/test_prompt_rendering_isolated.py
tests/llm/test_prompt_library.py
tests/llm/test_evolution_observability.py
tests/llm/test_self_evolution_safety.py
```

### 12.3 交易规则测试

至少覆盖：

```text
趋势追随多单 RSI=75.0 不触发 >75 规则
趋势追随多单 RSI=75.01 被拒绝
趋势追随空单 RSI=28.0 不触发 <28 规则
趋势追随空单 RSI=27.99 被拒绝
均值回归多单 RSI=25 不被趋势追随门禁误拒
反向 Jerk 缺失时拒绝
反向 Jerk 达到阈值时拒绝
Jerk 方向与交易方向一致时按规则处理
策略模式未知时拒绝新开仓
规则读取失败时拒绝新开仓
旧 legacy 模式保持旧测试行为
```

### 12.4 回测要求

不能只统计胜率。至少输出：

- 毛盈亏；
- 净盈亏；
- 手续费；
- 资金费；
- 滑点；
- Profit Factor；
- 最大回撤；
- 平均 R；
- 中位数 R；
- 连续亏损；
- 按市场状态分组；
- 按策略模式分组；
- 按交易所分组；
- 规则触发次数；
- 被 RSI/Jerk 拒绝后本来会产生的结果；
- 反例数量。

回测不能使用合成行情替代缺失真实数据。缺数据必须标记不可验证。

## 13. 实施任务顺序

### Task 1：实现基准 manifest 和一致性检查

文件：

```text
scripts/evolution_shield.py
r20_backend/routers/strategy/prompts.py
tests/llm/test_evolution_baseline_consistency.py
```

交付：代码基准、权威记忆和后台状态可比较；不一致时禁止发布。

### Task 2：扩展证据快照和可观测性

文件：

```text
scripts/trader/signal_snapshot.py
scripts/evolution/observability.py
scripts/self_improvement_engine.py
scripts/evolution/review_context.py
```

交付：快照来源、时间、策略版本、证据等级和重复快照检测。

### Task 3：重写自进化 JSON 契约和提示词

文件：

```text
scripts/self_improvement_engine.py
scripts/evolution/review_context.py
scripts/prompt_library.py
```

交付：事实/假设/提案分层，禁止模型直接生成硬规则。

### Task 4：隔离资产乘数和硬风险配置

文件：

```text
scripts/evolution/review_context.py
scripts/self_improvement_engine.py
scripts/brain/decisions.py
scripts/trader/sizing.py
```

交付：资产乘数有 TTL、来源、hash 和边界；不能修改硬风控。

### Task 5：实现策略硬规则注册表

文件：

```text
scripts/strategy_rules.py
r20_backend/interceptor_manager.py
scripts/trader/entry_execution.py
scripts/trader/signals.py
```

交付：策略模式下的 RSI/Jerk/止损/保本规则由代码执行，模型不能放宽。

### Task 6：统一 tracker、台账和策略快照

文件：

```text
scripts/trader/position_exit.py
scripts/trader/protection.py
scripts/trader/ledger_writer.py
r20_backend/policy/fingerprints.py
r20_backend/policy/capture.py
```

交付：每笔交易都可以还原当时的策略、记忆、风险和证据版本。

### Task 7：后台、迁移和回滚

文件：

```text
scripts/migrate_evolution_memory.py
r20_backend/schemas.py
r20_backend/routers/strategy/prompts.py
frontend/src/views/admin/PromptStudioPage.vue
frontend/src/views/admin/promptStudioLogic.ts
```

交付：只读迁移报告、人工确认、baseline 差异展示和安全回滚。

### Task 8：回测、影子运行和发布验收

文件：

```text
scripts/backtest_engine.py
scripts/backtest/lifecycle.py
scripts/backtest/metrics.py
tests/trading/test_strategy_rule_gates.py
tests/trading/test_self_evolution_integration.py
```

交付：旧模式回归、新模式影子统计、硬规则触发效果和回滚演练。

## 14. 上线阶段

### 阶段 A：只读审计

- 不改交易行为；
- 生成 baseline 一致性报告；
- 统计快照来源和缺失；
- 统计重复快照；
- 比较文档、结构化记忆和运行提示词；
- 运行至少 3 个复盘周期。

通过条件：没有未解释的 baseline 缺失，没有未经时间验证的完整因果快照。

### 阶段 B：记忆链路切换

- 启用新的记忆 schema；
- 允许 L2/L3 分类；
- 资产乘数仍保持观察或人工批准；
- 硬规则提案不自动生效；
- 交易规则不变。

通过条件：连续多个周期 memory revision、report revision、prompt 注入内容一致。

### 阶段 C：影子硬规则

- 计算 RSI/Jerk 规则；
- 记录如果执行会拒绝哪些交易；
- 不真正拦截；
- 回测和实盘影子结果分开记录。

通过条件：规则字段完整、时间一致、没有错误误伤均值回归策略。

### 阶段 D：模拟盘启用

- 只选小标的池；
- 禁用加仓；
- 保留现有 OCO 和冷却；
- 观察执行、保护、恢复和回滚。

### 阶段 E：小范围实盘

必须单独获得实盘授权。上线前保存：

- 代码版本；
- profile；
- policy snapshot；
- memory snapshot；
- risk config；
- venue routing；
- baseline manifest。

## 15. 回滚方案

### 15.1 记忆回滚

1. 停止自进化发布；
2. 保留结构化权威文件；
3. 使用 `expected_version` 检查当前版本；
4. 恢复到上一个经过校验的 memory snapshot；
5. 重新生成 Markdown 镜像；
6. 验证 baseline manifest；
7. 重新生成 policy snapshot。

### 15.2 硬规则回滚

1. 关闭新策略模式的新开仓；
2. 保留已有仓位的保护和退出管理；
3. 不删除 tracker；
4. 旧仓继续使用入场时冻结的规则版本；
5. 新仓恢复 legacy 前，确认没有未知订单和未归属保护单；
6. 不回滚到无法读取新字段的旧程序。

### 15.3 资产乘数回滚

- 将所有乘数临时视为 1.0；
- 保留原 JSON 作为审计；
- 不覆盖历史交易记录；
- 下一次复盘重新生成；
- 资产乘数文件损坏时不自动从旧缓存恢复未知值。

## 16. 完成定义

只有以下条件全部满足，才可宣称优化完成：

- baseline manifest 与权威记忆一致；
- 记忆损坏、并发写入和 stale version 有测试；
- 快照来源和时间验证完整；
- 复盘输出区分事实、假设和提案；
- 未审核提案不会进入交易提示词；
- 资产乘数不改变硬风险规则；
- 策略硬规则由代码执行；
- RSI/Jerk 规则按策略模式生效；
- 止损和保本语义统一；
- 交易台账包含策略和证据版本；
- policy snapshot 包含 memory/rule/risk hash；
- 后台能显示实际注入条目和 baseline 状态；
- 历史数据迁移有 dry-run 和回滚；
- 相关单元、集成、回测和旧策略回归通过；
- 影子运行报告覆盖趋势和震荡环境；
- 没有未经授权的实盘切换。

## 17. 验证命令

针对自进化链路：

```bash
.venv/bin/python -m unittest -q \
  tests.ops.test_self_improvement_engine \
  tests.llm.test_evolution_shield \
  tests.llm.test_evolution_observability \
  tests.llm.test_self_evolution_safety
```

针对提示词和策略快照：

```bash
.venv/bin/python -m unittest -q \
  tests.llm.test_prompt_rendering_isolated \
  tests.llm.test_prompt_library \
  tests.core.test_policy_fingerprints \
  tests.ops.test_policy_capture_tails \
  tests.ops.test_policy_restore_tails
```

针对交易硬规则：

```bash
.venv/bin/python -m unittest -q \
  tests.trading.test_strategy_rule_gates \
  tests.trading.test_entry_execution_gates \
  tests.trading.test_signal_snapshot_schema \
  tests.core.test_interceptor_core_safety
```

最终回归：

```bash
.venv/bin/python -m unittest discover -s tests -t .
git diff --check
```

## 18. 最终建议

实施顺序必须是：

```text
证据与权威一致性
  > baseline 保护
  > 复盘提示词分层
  > 资产乘数隔离
  > 策略硬规则代码化
  > 台账/快照/策略 hash
  > 后台与迁移
  > 影子运行
  > 模拟盘
  > 小范围实盘
```

不要从修改 `docs/self-evolution.md` 的文字开始，也不要直接把文档中的四条心法写进生产记忆。先让代码能够回答：

```text
这条结论来自哪笔交易？
使用了哪个快照？
快照是否在开仓时可见？
是否属于独立样本？
当前是否已审核？
是否进入了交易提示词？
是否由代码硬执行？
使用的是哪个策略、风险和记忆版本？
```

这些问题全部可以被确定回答后，才适合根据复盘结果优化交易行为。
