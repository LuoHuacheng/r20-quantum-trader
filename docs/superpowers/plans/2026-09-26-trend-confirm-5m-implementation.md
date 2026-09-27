# 5m 趋势确认跟随策略实施方案

> **执行说明：** 本文是实施规格与任务计划，不是已上线能力。后续执行可使用 `executing-plans` 技能逐项实施；各任务以复选框跟踪。默认直接执行，不因本文自动委派子代理，不自动部署或启动交易。

**Goal：** 将 R20 扩展为可选择的 5m 收盘趋势确认执行模式，使行情证据、模型提示词、开仓门禁、仓位预算、持仓退出及审计记录使用相同规则，同时保持旧策略行为可回归、存量仓位可继续管理。

**Architecture：** 新增一个无 I/O 的策略计算模块，复用现有行情、交易所适配器、订单保护、持仓追踪和台账。由代码确认信号及管理退出，LLM 对有效候选作辅助裁决；新旧规则通过持久化的执行模式分支隔离，不重写通用交易框架。

**Tech Stack：** Python、现有交易所 REST 适配器、JSON 状态文件与文件锁、unittest；必要时补充现有 Vue/TypeScript 后台字段透传，不新增指标库、数据库或任务队列。

**Spec：** 原策略为 [`r20-strategy-5分钟趋势确认跟随版-2026-09-25.json`](../../../data/prompt_strategies/r20-strategy-5分钟趋势确认跟随版-2026-09-25.json)。本文第 3 节补充原策略中尚不精确的算法口径，后续实现以本文件明确标注的推荐 v1 口径为准。

**编制日期：** 2026-09-26。

**证据范围：** 前序分析使用 CodeGraph MCP 追踪主脑行情、拦截和持仓管理调用关系，并读取相关源码；本轮补查提示词导入、两个调度入口和回测入口。没有运行收益回测，没有读取运行中的凭据或 `.env`，没有验证当前实盘参数，也没有修改交易行为。本文提及的默认参数是源码默认值，不代表线上实际配置。

## 1. 目标、范围与全局约束

### 1.1 交付结果

1. 可以显式选择 `legacy` 或 `trend_confirm_5m` 执行模式。
2. 5m 模式仅在已收盘突破、双周期 ADX 和 1H 方向同时成立时允许申请开仓。
3. 下单前重新检查有效期、最新报价、风险额度和保护几何。
4. 持仓退出由确定性规则执行，不依赖模型是否返回 CLOSE_MARKET。
5. 每笔信号、订单和仓位均可追溯至执行模式、规则版本、原始收盘时间和策略快照。
6. 模型失败、开仓熔断和策略切换不阻止已有仓位的风险管理。
7. 可先影子运行，再模拟交易，最后在独立授权后小范围实盘。

### 1.2 推荐首发范围

- 首发自动开仓只支持 OKX 的 USDT 线性永续、现有池内的加密资产。
- 首发固定单所执行，显式禁止该模式自动路由到 Binance/Gate。
- 首发加仓上限为 0；原策略允许声明禁用加仓，因此这不构成隐藏功能缺失。
- 新仓执行 5m 规则，已有仓位保留原模式。相同账户、场所、标的、方向不能混合两种策略持仓。
- 首发采用受保护限价单，可在滑点上限内成为可立即成交的限价单；纯市价入场不作为首发条件。
- 不重写旧因子、旧策略、新闻、自进化或交易所适配器；仅在必要入口增加分支和附加字段。
- 多所执行、加仓和纯市价属于明确的第二阶段交付，未完成时不能对外宣称支持。

### 1.3 不可放松的约束

- 资金、杠杆、同向敞口、并发仓位、日亏熔断、交易所最小数量与步长约束继续有效。
- 核心信号门禁不可通过关闭可编辑插件绕过。
- 缺失、损坏、非有限数字、过期或无法核验的数据不视为 0 或合法值。
- 始终保留交易所端灾难止损与保护性止盈；保护覆盖无法确认时复用现有安全退出流程。
- 订单受理不等于成交，撤单受理不等于已撤，改单受理不等于保护已更新。
- 状态采用现有锁与原子写入方式。写入失败必须禁止新增风险并告警，不能伪报成功。
- 调用期注入可热重载配置、文件路径和测试替换函数，不在子模块 import 期绑定门面状态。
- 不安装依赖、不更改生产配置、不切换实盘、不代用户提交 Git；实施与上线授权分开。

## 2. 已核验现状与改造原因

下表是当前源码事实；行号会随实施变化，定位优先使用函数名。

| 位置 | 已核验行为 | 5m 模式要求 |
|---|---|---|
| `scripts/brain/packages.py::fetch_single_instrument_package` | 采集 15m/1H/4H；1H 取 24 根；无 5m Donchian | 增加收盘 5m/1H 证据与足够预热历史 |
| `scripts/trader/factors.py::fetch_single_instrument_data` | 另取 1H 35 根；趋势为 EMA/局部高低点；ATR 有现价比例地板 | 共用新策略快照，不能以旧字段替代新通道和真实 ATR |
| `scripts/market_data_service.py::_local_math_indicators` | 已有纯 Python ADX 计算，但直接使用返回 K线 | 复用算法思路，明确收盘过滤、Wilder 初始化与预热 |
| `r20_backend/exchanges/{okx,binance,gate}.py::fetch_candles` | 统一数组通常只保留六列；时间单位及源格式有差异 | 扩展新策略取数入口，不破坏既有六列调用契约 |
| `scripts/brain/prompt.py::construct_full_market_prompt` | 输出旧数学证据及旧任务 | 5m 模式专用行情证据与任务文本 |
| `scripts/prompt_library.py::apply_module_layout` | trading_user 基座空模块可恢复实时旧任务 | 显式选择新基座，防止旧任务混入 |
| `scripts/prompt_library.py::import_profile` | 导入后生成 `custom-*` 新 ID | 不依赖导出 `profile_id` 作为执行模式 |
| `plugins/interceptors/03_adx_volatility_filter.py` | 仅拦截 `0 < adx_1h < 18` | 双周期 ADX≥20 且缺失拒绝 |
| `plugins/interceptors/01_macro_trend_filter.py` | 检查 4H 方向 | 本模式使用 1H Donchian 55 方向 |
| `scripts/trader/order_intent.py::resolve_entry_prices` | 强制 1.2% 回踩，并同步平移止损 | 新模式不套用回踩地板或平移结构止损 |
| `scripts/trader/sizing.py::size_for_decision` | 按保证金和旧基准仓位的 0.5～2 倍夹取 | 最终数量须满足最终止损风险上限 |
| `scripts/trader/pyramiding.py::pyramiding_gate` | 盈利或保本，加速度/概率门禁 | 第二阶段改为保本、非浮亏、新 10 周期突破 |
| `scripts/trader/position_exit.py`、`scale_out.py` | 时间止损、阶梯锁利、动能回撤、分批止盈 | 新模式只保留规定的退出与账户安全处置 |
| `scripts/trader/protection.py::ai_tightens_stop` | 1.2 ATR 盈利、0.7 ATR 缓冲、跨入场价等旧限制 | 不直接用它裁定新通道线 |
| `scripts/trader/position_mgmt.py` | AI 平仓要求 confidence≥85；实际刷新字典为 OKX 持仓 | 确定性退出不受 AI 置信度约束；扩展多所前补齐归属 |
| `r20_backend/execution_router.py::open_protected_position` | 将远端 TP 按现有上限收窄 | 第二阶段多所需识别保护性 TP，不能静默变成近端目标 |
| `r20_backend/execution/cooldowns.py` | 冷却按标的＋方向 | 本模式止损后同标的双方向冷却 |
| `r20_backend/scheduler.py` | 交易任务同步运行，完成后等 15 分钟；超时 600 秒 | 5m 边界触发与延迟隔离 |
| `r20_gateway/scheduler.py` | 15m 边界调度；交易超时 1260 秒 | 同样适配 5m，限制推理占用时间 |
| `scripts/ai_factor_trader.py` | 使用 900 秒槽和短时间重复启动保护 | 改为模式相关槽与信号持久幂等 |
| `scripts/trader/signal_snapshot.py`、`scripts/evolution/observability.py` | 快照及可观测性围绕动力学 | 增加趋势证据与相应审计，不删除旧标签 |
| `scripts/backtest_engine.py::run_full_portfolio_backtest` | 行情缺失生成合成序列；当前入口不等同于新策略回放 | 新模式回放缺数据失败，不能混入合成收益 |

已完成的只读行为探针：

- 原 JSON 格式为 `r20-prompt-profile` v4，模块编译结果与其平铺文本一致。
- 空 `base-ts-task` 会带回旧版任务。
- 现有 ADX 插件放行 ADX=0 和 ADX=19。
- 现价 100、入场 100、TP 110、SL 95，经旧定价函数变为入场 98.8、TP 110、SL 93.8。

这些探针只证明兼容性缺口，不证明策略盈利，也不替代最终集成测试。

## 3. 推荐 v1 策略规格

本节对原提示词的模糊描述给出可编码定义。它们属于建议的执行口径，必须在更新后的策略 JSON、风险预算文本、测试和规则版本中一起体现；不能声称是原文件已经精确规定的内容。

### 3.1 时间与已收盘数据

- 内部时间统一为 UTC 毫秒；界面和报告可显示北京时间。
- `open_time_ms` 表示 K线开始，`close_time_ms = open_time_ms + timeframe_ms` 表示半开区间结束边界。
- OKX 必须 `confirm == "1"` 且边界时间不在未来；其他来源按交易所结束时间和校准后的服务器时间判断。
- 时间以明确的来源格式转换，不能把 Gate 的秒当成毫秒；Binance 原生结束时间归一到区间边界。
- 5m 决策时只允许使用 `close_time_ms <= decision_asof_ms` 的 1H K线。
- 推荐每周期收盘后 3 秒首次读取；最迟 15 秒内未得到该根确认数据则本轮不开新仓，不拿上一根冒充。
- 原始行排序、去重；重复时间内容冲突、OHLC 非法、历史缺口均判无效。OHLC 必须正且有限，`low <= open/close <= high`，volume 必须有限且非负。
- 单一计算窗口只能使用同场所、同合约、同价格来源的数据。首发 OKX 新开仓不允许将外所 K线当作 OKX 突破证据。
- 每次计算固定使用截至该时点最近 120 根已收盘 5m 和最近 120 根已收盘 1H；先请求 150 根，去掉未收盘行后验证并截取。少于 120 根判无效。每次都在这 120 根内按第 3.4 节初始化，不混用跨周期累积的另一套 ADX 状态；回放使用完全相同窗口。这一选择保证重启和回放一致，代价是数值不保证与交易所使用更长历史的 ADX 完全相等。

### 3.2 Donchian 定义

令 `t` 为刚收盘的 5m K线，`C_t/H_t/L_t/V_t` 分别为其收盘价、最高价、最低价、成交量。

```text
U20(t) = max(H[t-20 : t])
L20(t) = min(L[t-20 : t])
U10(t) = max(H[t-10 : t])
L10(t) = min(L[t-10 : t])
```

切片右端不包含 `t`，即所有通道均由信号 K线之前的已收盘 K线计算。

```text
首发做多候选：C_t > U20(t)
首发做空候选：C_t < L20(t)
10 周期向上延续：C_t > U10(t)
10 周期向下延续：C_t < L10(t)
```

等于边界不算突破。不能用盘中 high/low 代替 close。连续不同 K线满足突破可各自生成候选，但同一仓位、在途订单和信号状态仍约束实际下单。

### 3.3 1H 方向

令 `h` 为截至 `t` 收盘时最后一根已收盘 1H K线。

```text
U55(h) = max(H_1h[h-55 : h])
L55(h) = min(L_1h[h-55 : h])
M55(h) = (U55(h) + L55(h)) / 2
C_1h[h] > M55(h)：BULL
C_1h[h] < M55(h)：BEAR
C_1h[h] == M55(h)：NEUTRAL
```

这是本文选择的“最后一根 1H 收盘价相对前 55 根通道中线”口径。原文的“1H 价格运行在上/下半区”没有明确排除正在形成的 K线，本方案明确排除。

零宽通道、样本不足或非法数据为 INVALID；NEUTRAL/INVALID 均禁止首发及加仓。

### 3.4 ADX 与 ATR

- 两周期 ADX 均采用 Wilder ADX(14)，只使用已收盘 OHLC。
- `TR_i = max(H_i-L_i, abs(H_i-C_(i-1)), abs(L_i-C_(i-1)))`。
- `+DM_i`：`H_i-H_(i-1)` 为正且大于 `L_(i-1)-L_i` 时取该值，否则 0；`-DM_i` 对称。相等时两者均为 0。
- TR、DM 使用前 14 个有效变动值初始化，再按 Wilder 平滑；首个 ADX 是前 14 个有效 DX 的均值，此后 `ADX = (13*ADX_prev + DX)/14`。
- DI 分母为 0 时该步 DX 取 0；合法平坦行情 ADX=0 是低强度，不能视为缺失后放行。
- ATR(14) 使用 Wilder TR 平滑，初值为前 14 个 TR 的均值。
- ADX 阈值用未四舍五入的值比较；输出展示才保留小数。19.999 不得因显示为 20.00 放行。
- 开仓与加仓要求 `ADX_5m >= 20 and ADX_1h >= 20`；“强趋势”采用两者均≥25。强趋势只允许在现有资金预算内提高申请额，不能自动抬高资金上限。
- 防追价和初始止损统一使用信号时点的真实 `ATR_1h`，不能使用执行侧旧 `max(ATR, price*1.2%)` 代理值。

### 3.5 辅助证据

```text
突破量比 = V_t / mean(V[t-20 : t])
相对通道宽度 = (U20(t)-L20(t)) / ((U20(t)+L20(t))/2)
```

保存最近 5 个相对通道宽度，展示收窄/放宽轨迹；均量为 0 时量比为 null 并披露不可计算。

首发不设置额外成交量倍数或通道收窄硬阈值，因为原策略没有给出可验证数值。更新提示词说明：这两项只能用于减仓或 WAIT，不得替代突破、方向和 ADX。不得使用“真突破已经被证明”等确定性结论。

### 3.6 信号有效期与防追价

- 候选有效期：`expires_at_ms = signal_close_time_ms + 600_000`；到期等号即失效。
- 同一个标的、方向只保留最新候选，较新的反向突破或方向/ADX 门禁失败使旧候选失效。
- 在信号有效期内允许等待限价成交，不要求后续每根都再次突破 20 周期；但不得在状态失效后继续挂单。
- 多头参考最新可执行卖一价 ask，空头参考买一价 bid。报价时间距校准当前时间不超过 5 秒；无法获取则不开仓。
- 防追价以突破 K线收盘价 `C_t` 为锚：`abs(executable_price-C_t) <= 0.8*ATR_1h_at_signal`。
- 同时要求多头最新可执行价仍 `> U20(t)`，空头仍 `< L20(t)`，否则突破已经失效。
- 下单前重验最新收盘证据的 ADX 与方向，不使用模型返回的指标值。
- 同一信号最多提交一次被交易所受理的开仓订单。明确拒单且已证实未受理才允许在原有效期内重试；未知状态先对账。

### 3.7 入场、止损和数量

首发使用保护性限价单：模型可在合法区间内提供 `entry_price`，代码只接受同时满足突破边界、防追价、止损几何与执行成本约束的值；无合法报价时 WAIT，不自动变成回踩单。

滑点价限推荐固定为报价的 0.1%，并叠加 0.8 ATR 距离限制：多头不能高于最新 ask 的 1.001 倍，空头不能低于最新 bid 的 0.999 倍。此参数是 v1 执行假设，必须写入规则版本并在模拟盘评估。

初始止损的推荐公式：

```text
做多：SL = min(L20(t)-tick_size, entry_price-2.0*ATR_1h)
做空：SL = max(U20(t)+tick_size, entry_price+2.0*ATR_1h)
```

向更保守的价格步长舍入，并使用舍入后的距离计算数量。这表示“通道对侧之外，且至少 2 ATR 距离”；通道较宽时可以超过 2.2 ATR。它是本文对原文“对侧边界之外，参考 1.8～2.2 ATR”的明确解释，不是 2.2 ATR 硬上限。

数量以最终订单和止损计算：

```text
R_budget = 现有 effective_risk_per_trade 得到的单笔风险预算
unit_risk = ctVal * (abs(entry_price-SL) + entry_fee_per_base
                    + exit_fee_per_base + slippage_allowance_per_base)
size_risk_cap = floor_to_step(R_budget / unit_risk)
size_final = min(size_risk_cap, 模型申请额允许数量, 保证金允许数量,
                 单标的累计限额允许数量, 组合预算允许数量)
```

- 手续费按保守 taker 双边估计；滑点预算首发按入场和退出各 0.1% 名义价值估计，回放应增加高成本情景。
- fee 和 slippage 项先换算为“每单位基础币价格损失”，再乘 `ctVal`，不得混用张数、币数和 USDT。
- 不使用旧基准数量的 0.5 倍下限将数量重新抬高。
- 低于最小下单量直接 WAIT，禁止缩窄 SL 或扩大预算迁就最小数量。
- 报价发生变化后重新计算几何、R:R 和风险；不平移结构边界。
- 风险预算是正常成交假设下的计划损失，不保证跳空、穿仓或交易所故障时实际损失不超出。

### 3.8 保护性 TP

首发保留现有 TP 字段与 OCO 契约，推荐：

```text
R_price = abs(entry_price-SL)
protective_rr = max(4.0, 当前最小 R:R 硬底线)
多头 TP = entry_price + protective_rr*R_price
空头 TP = entry_price - protective_rr*R_price
```

TP 非正、超过交易所合法价格范围或舍入后不满足几何时拒绝开仓。

- 该模式不套用旧 `MAX_RISK_REWARD_RATIO` / `MAX_TAKE_PROFIT_ATR` 的主动收窄行为；改成明确的保护 TP 规则分支。
- 最小 R:R、有效数字和几何检查继续保留，不全局关闭通用校验。
- TP 触发仍会结束趋势仓位，这是保留有限远端保护单的明确代价。
- 这里的 R:R 是保护报价比例，不能当作跟踪退出的期望收益或已实现盈亏比。
- 提示词、风险预算、后台展示须反映此差异，禁止一边承诺 4R、一边宣称仍使用旧最大 3.5R 默认值。

### 3.9 策略跟踪线与云端保护线分离

维护两个字段：

- `strategy_stop_px`：5m 收盘策略退出线。
- `emergency_sl_px`：交易所盘中触发的灾难止损线。

初始两者都等于初始 SL。首发云端 SL 维持初始灾难线，策略线根据通道棘轮推进；不把每次策略线推进直接同步到云端，否则会在盘中插针时退出，与“等待收盘”冲突。

对新收盘 K线，严格按下面顺序：

1. 使用该 K线到来前已持久化的 `strategy_stop_px` 判断退出：多头 `C_t <= stop`，空头 `C_t >= stop`。边界等号也退出，这是推荐执行口径。
2. 如果触发，发起经确认的市价减仓/平仓流程；未确认平仓前不删除 tracker、不释放风险预算、不标记 CLOSED。
3. 未触发时，若多头 `C_t > U10(t)`，候选跟踪线为 `L10(t)-tick_size`；空头 `C_t < L10(t)`，候选为 `U10(t)+tick_size`。
4. 多头新线 `max(old_stop, candidate)`，空头新线 `min(old_stop, candidate)`；舍入后再检查单调性和价格几何。
5. 持久化成功后更新 `last_managed_close_time_ms`。失败则告警并禁止新增风险，下一次继续恢复。

这是“同向 10 周期突破触发，反向 10 周期边界决定止损位置”的解释。它不同于每根 K线都滚动更新通道线，回测和实盘必须一致。

风险处置优先级：交易所云端已成交事件、保护缺失安全处置、账户级强制减险优先于策略规则。新模式关闭旧时间止损、分批止盈、0.8 ATR 保本、2.2 ATR 锁利及峰值 0.75 ATR 回撤退出。

LLM 对该模式的 `HOLD` 不得阻止机械退出；`UPDATE_SL` 不能自行指定另一条价格；`CLOSE_MARKET` 不能凭自由文本绕过已定义退出条件。模型建议可以记录，但只有确定性规则或既有账户安全机制可触发交易。保留原 JSON 字段以兼容 UI，不能将其解释为模型仍可自由改变此模式。

### 3.10 冷却与可选加仓

首发止损后按同一账户、场所、标的记录冷却，同时拦多空；时长读取现有风险配置，不擅自改短。负收益策略退出、灾难 SL 和保护失效退出均登记冷却；正收益通道退出不因名称含“止损线”自动算亏损止损。

第二阶段加仓要求全部满足：

- 配置允许次数大于 0，且累计受理/成交加仓次数未超上限。
- 底仓当前净浮盈非负，且真实云端 SL 已覆盖含费用的保本价，不以本地策略线代替保护证据。
- 新的 5m K线突破 10 周期同向边界，双 ADX≥20、1H 方向一致、防追价通过。
- 加仓信号时间晚于首次入场及上次加仓信号；首发 20 周期突破同一根不得同时触发加仓。
- 加仓前先将云端灾难 SL 收紧至保本并读取确认；确认失败不加仓。此处属于明确的盘中保本例外，应在策略文本披露。
- 加仓后按新均价、全仓数量、共享 SL 重算总风险，原仓＋新增量满足单标的累计预算，OCO 覆盖全量。

首发不实现上述例外，持有期间只运行机械管理，不申请加仓。

## 4. 数据与状态契约

### 4.1 执行模式与规则版本

在 profile 内新增可持久化、可导入导出的字段：

```json
{
  "execution_policy": {
    "mode": "trend_confirm_5m",
    "revision": 1
  }
}
```

- 旧 profile 字段缺失解释为 `legacy`，不改写其历史行为。
- 新策略源文件显式增加该字段，保持 v4 包装并保证清洗器与导入导出透传；老版本程序会忽略新字段，因此禁止使用老程序运行该模式。
- 未知 mode、revision 或非法字段必须拒绝激活/新开仓，不静默退回 legacy。
- v1 参数作为策略模块中的固定规则集，不新增一整套通用策略 DSL。
- 将 mode、revision、实际风险参数摘要、规则参数及部署代码版本纳入执行快照指纹，防止提示词相同而执行口径变化却仍显示同一版本。
- `execution_policy_hash` 标识冻结的执行规则与风险配置参数；`policy_hash` 继续代表既有综合策略快照，二者均记录。余额、最新报价、动态推导的可用风险金额不参与执行规则哈希，单独保存在周期快照中，避免同一规则因行情或余额变化产生新的身份。
- 交易周期开始冻结策略配置；周期中若激活配置改变，旧周期不得再提交开仓。

### 4.2 收盘 K线归一结构

建议新增模块 `scripts/strategies/trend_confirm_5m.py` 使用以下类型，不强制替换旧数组格式：

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class ClosedBar:
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
```

场所、合约、获取时间与确认依据放在快照外层，避免每根重复存储。

### 4.3 统一趋势快照

下列示例说明结构，数值仅为示例，不能当交易参数：

```json
{
  "schema_version": 1,
  "mode": "trend_confirm_5m",
  "revision": 1,
  "market_venue": "okx",
  "instId": "BTC-USDT-SWAP",
  "asof_ms": 1790294700000,
  "fetched_at_ms": 1790294703000,
  "quality": "valid",
  "invalid_reasons": [],
  "bar_5m": {
    "close_time_ms": 1790294700000,
    "close": 100.0,
    "upper20": 99.0,
    "lower20": 95.0,
    "upper10": 99.0,
    "lower10": 96.0,
    "adx14": 27.1,
    "volume_ratio20": 1.8,
    "width20_history": [0.05, 0.046, 0.043, 0.041, 0.0412],
    "breakout20": "long",
    "breakout10": "long"
  },
  "bar_1h": {
    "close_time_ms": 1790294400000,
    "close": 99.5,
    "upper55": 105.0,
    "lower55": 90.0,
    "mid55": 97.5,
    "direction": "BULL",
    "adx14": 24.0,
    "atr14": 2.0
  },
  "warmup": {"5m_closed_count": 120, "1h_closed_count": 120}
}
```

快照生产失败时允许诊断字段为 null，但 `quality=invalid`，不得传进有效候选。模型文本不能成为快照数据源。

### 4.4 共享接口

在新增策略模块定义并测试以下接口；它们均无网络、磁盘及账户查询。表格为完整签名约定，函数算法由第 3 节定义，对应 Task 2、3、5 实现：

| 函数签名 | 返回类型 |
|---|---|
| `adx14(bars: list[ClosedBar])` | `float \| None` |
| `atr14(bars: list[ClosedBar])` | `float \| None` |
| `donchian_before_last(bars: list[ClosedBar], period: int)` | `tuple[float, float]` |
| `build_trend_snapshot(*, bars_5m: list[ClosedBar], bars_1h: list[ClosedBar], inst_id: str, venue: str, asof_ms: int, fetched_at_ms: int)` | `dict` |
| `check_entry(*, signal: dict, current_snapshot: dict, action: str, quote: dict, now_ms: int)` | `tuple[bool, str]` |
| `initial_stop(*, is_long: bool, entry: float, lower20: float, upper20: float, atr_1h: float, tick_size: float)` | `float` |
| `next_position_action(*, tracker: dict, snapshot: dict, tick_size: float)` | `dict` |

明确返回约定：

- `adx14/atr14`：样本不够返回 None；输入非法由归一层拒绝，不吞异常伪造数值。
- `donchian_before_last`：返回 `(upper, lower)`；不足 `period+1` 根抛 ValueError。
- `check_entry`：返回 bool 和第一个稳定拒绝码；通过时拒绝码为空串。
- `quote`：`{"bid": float, "ask": float, "ts_ms": int, "venue": str}`。
- `signal`：保存原确认快照、`side`、`signal_close_time_ms`、`expires_at_ms`、`signal_id`、`execution_policy_hash`；下单有效期不得从当前时间重新计算。
- `next_position_action`：返回 `{"action": "HOLD|ADVANCE_STOP|CLOSE_MARKET", "reason_code": str, "new_stop_px": float|null, "bar_close_time_ms": int}`。不直接修改输入 tracker。

### 4.5 身份、幂等与持久状态

```text
signal_id = SHA256(account_scope, environment, execution_venue, instId, side,
                   signal_close_time_ms, entry_kind, execution_policy_hash)
entry_kind = initial 或 scale_in
```

按固定顺序序列化再哈希，不使用 Python 的非稳定内建 hash。account_scope 复用现有非敏感账户标识，不存 API key。

订单意图增加：`signal_id`、`expires_at_ms`、`entry_kind`、`execution_policy`、`execution_policy_hash`、`signal_snapshot`、`venue`、`environment`、`account_scope`、`client_order_id`、`order_id`、`state`。

```text
CANDIDATE -> RESERVED -> SUBMITTING -> ACKNOWLEDGED -> FILLED
                                  -> REJECTED
                                  -> UNKNOWN
ACKNOWLEDGED -> CANCEL_REQUESTED -> CANCELLED
```

- 发请求前写 SUBMITTING；交易所超时后为 UNKNOWN，以 client_order_id 对账，不重复发单。
- 不确定订单是否受理时，保留预留预算，不凭本地 TTL 自动释放。
- 部分成交：为已成交量确认保护、保留真实仓位，撤销剩余量；不能记为整单取消。
- 幂等检查与预算预留放在同一临界区，复用现有意图文件/风险预留机制，不另建第二套账本。
- 额外按 `(account_scope, environment, venue, instId, side, signal_close_time_ms, entry_kind)` 检查是否已消费；即使修改风险配置导致执行哈希变化，同一根同向信号也不能再次受理。新模式同一净仓不允许跨版本并行开仓。
- 订单到期后撤销；`KEEP` 及旧归属豁免不能延长新策略信号有效期。

tracker 增加：

```text
execution_policy / execution_policy_hash / account_scope / venue / environment
entry_signal_id / entry_signal_close_time_ms / entry_snapshot
strategy_stop_px / emergency_sl_px / protective_tp_px
last_managed_close_time_ms / last_scale_signal_close_time_ms
scale_count / close_intent_id / state
```

必须与现有 tracker 的清洗、裁剪、恢复、序列化兼容，不能新增字段后又被 `data_shape.py` 丢弃。

### 4.6 重启与模式切换

- 无 mode 的旧 tracker 标记 legacy，只补身份，不重算旧仓位的策略止损。
- 已有真实仓位却无可核验归属时，不凭当前 active profile 猜测；保留云端保护，禁止同标的新增风险并告警。
- 切换 active profile 只影响新信号；旧仓使用入场时冻结的模式与规则版本，账户级更严格安全限制仍即时适用。
- 5m 仓位尚未归零时，即使新开仓切回 legacy，也必须继续运行 5m 持仓管理节奏。
- 恢复时顺序重放遗漏的已收盘 K线更新策略线。若历史已触发退出但没有订单确认，当前价平仓并记录延迟，不能伪造在历史价格成交。
- 历史缺口无法补齐时，保留灾难保护并告警，停止新开仓；策略线不得凭空跳到新值。

## 5. 执行流程与失败语义

### 5.1 单周期顺序

1. 获取进程锁，冻结账户环境、执行模式、风险预算与策略版本。
2. 对账订单、成交和云端保护；处理到期入场单与 UNKNOWN 订单。
3. 获取真实持仓，按 tracker 模式分组。
4. 采集一份收盘行情，先处理所有持仓的机械退出和保护异常。
5. 运行账户级开仓闸门。日亏熔断只禁止新增风险，不跳过步骤 2～4。
6. 计算候选。无有效候选则记录 WAIT，不调用 LLM。
7. 将合法候选和共同快照交给 LLM；最多等待规定的推理预算。
8. 验证模型动作、方向、置信度、资金申请；不得由模型新增未授权候选。
9. 刷新报价与最新收盘快照，复验信号、配置版本、追价、风险和幂等。
10. 写入意图并提交订单，核实成交状态与保护覆盖，落审计证据。

不得继续沿用“先运行旧机械退出，再调用主脑，最后才知道这是 5m 模式”的顺序。模式必须在持仓管理前可用。

### 5.2 LLM 角色与预算

- 保留 BUY_LONG / SELL_SHORT / WAIT 和原解释字段，减少前端兼容成本。
- 首发只有已确认候选才能进入模型。模型可 WAIT 或申请更低风险；不得输出相反方向、更宽松门槛或超预算仓位。
- 置信度继续满足现有安全门槛；它不代表经校准的盈利概率，不得用 confidence 替代 ADX。
- `calculus_dynamics` 填双 ADX，`math_prob_rationale` 填通道宽度/量比证据；修正原 JSON 中另一模块将 ADX 指向 math_prob_rationale 的冲突。
- 模型遗漏、超时、非法 JSON、返回无关标的均不下单；机械退出照常。
- 首发使用单一 trader 模型；暂不启用可能跨越多个 5m 周期的委员会多轮辩论。
- 推荐 LLM 整体墙钟预算 45 秒，包含所有 provider 回退和重试；不能每次重试各给 45 秒。外层交易周期预算 90 秒，超时应保留意图供下轮对账。
- 不为本次改造新增长期常驻推理服务或线程池框架。

### 5.3 失败处置表

| 失败 | 新开仓 | 存量仓位与订单 |
|---|---|---|
| 5m/1H 数据过期、断档或源不一致 | 拒绝 | 保留云端保护，告警；不凭坏数据移动策略线 |
| LLM 超时或解析失败 | 拒绝 | 机械管理已经独立执行 |
| 日亏熔断、池不可读、预算不足 | 拒绝 | 继续对账、退出和保护检查 |
| 订单请求超时 | 禁止重复提交该信号 | UNKNOWN，对账 client_order_id，预留保持 |
| 撤单超时或撤单后恰好成交 | 暂停同标的新单 | 查询终态，按真实成交量接管保护 |
| 平仓失败或部分平仓 | 禁止同标的新单 | 保留 tracker 和剩余量，继续保护与重试 |
| 本地状态写入失败 | 拒绝 | 保留已确认云端保护，告警和恢复对账 |
| 策略切换时旧周期尚在推理 | 旧周期不得提交 | 已有订单/仓位按原冻结规则管理 |
| 不支持的交易所/非加密标的 | 拒绝该模式入场 | 原仓仍由原有管理链路处理 |

## 6. 文件改动地图

所有下列“新增”均为计划，不代表当前文件已经存在。

| 类别 | 文件 | 改动职责 |
|---|---|---|
| 新增 | `scripts/strategies/__init__.py` | 空包入口，不导入带 I/O 的交易门面 |
| 新增 | `scripts/strategies/trend_confirm_5m.py` | 规则版本、收盘类型、ADX/ATR/通道、信号与退出纯函数 |
| 修改 | `scripts/market_data_service.py` | 新策略收盘采集和来源/时间校验，保留旧 API 兼容 |
| 修改 | `scripts/brain/packages.py`、`scripts/trader/factors.py` | 接受/传递同一趋势快照，避免各算一份 |
| 修改 | `scripts/ai_brain_trader.py`、`scripts/ai_factor_trader.py` | 调用期接线、周期模式冻结、快照传递、调度槽 |
| 修改 | `scripts/prompt_library.py`、`scripts/prompt_templates.py` | execution_policy 清洗/校验/保留；按模式装配基座 |
| 修改 | `scripts/brain/prompt.py`、`scripts/brain/decisions.py`、`scripts/brain/cycle_parts.py` | 新任务、预算文本、候选约束、缓存与历史透传 |
| 修改 | 原策略 JSON | 增加执行模式，修正规则歧义及字段冲突，同步模块和平铺文本 |
| 修改 | `r20_backend/schemas.py`、`r20_backend/routers/strategy/prompts.py` | 模式更新/导入边界校验，不允许未知模式激活 |
| 修改 | `frontend/src/views/admin/promptStudioLogic.ts`、`PromptStudioPage.vue` | 保存时保留 execution_policy，展示模式与未就绪状态；不新建策略面板 |
| 修改 | `r20_backend/policy/{schema,fingerprints,capture,restore}.py` | 模式与执行快照指纹、归档恢复兼容 |
| 修改 | `r20_backend/interceptor_manager.py` | 核心信号门禁及模式对应插件筛选 |
| 修改 | `scripts/trader/{entry_execution,order_intent,sizing,order_submit,brackets}.py` | 入场规则、实际风险数量、最终复验与保护 TP |
| 修改 | `scripts/trader/{order_lifecycle,ledger_writer,reservation_reconcile,data_shape}.py` | 信号过期、幂等状态、保留新字段和崩溃恢复 |
| 修改 | `scripts/trader/{position_exit,position_mgmt,cycle_stages,scale_out}.py` | 模式分流、独立退出、阻止旧减仓规则干预 |
| 修改 | `r20_backend/execution/{sizing,cooldowns}.py` | 共享风险计算与同标的双向冷却；legacy 分支不变 |
| 修改 | `r20_backend/scheduler.py`、`r20_gateway/scheduler.py` | 两入口 5m 调度、超时与状态展示一致 |
| 修改 | `scripts/trader/signal_snapshot.py`、`scripts/evolution/{observability,review_context}.py` | 趋势证据与复盘可观测性 |
| 修改 | `scripts/backtest_engine.py`、`scripts/backtest/{lifecycle,metrics}.py` | 新模式历史回放，成本与真实样本报告 |
| 第二阶段 | `scripts/trader/pyramiding.py` | 新加仓规则、共享风险复验 |
| 第二阶段 | `r20_backend/execution_router.py`、`scripts/trader/venue_protection.py`、交易所适配器 | 多所下单/撤单/平仓/改单与信号场所一致 |

插件不必全部重写。5m 模式跳过内置旧 4H/VWAP/单周期 ADX 策略过滤，保留公共资金和价格约束；用户自定义插件继续允许拒绝，但不能放宽核心门禁。必须记录实际启用插件集合，不能静默改变全局启用配置。

## 7. 实施任务与验证

每个任务遵循“编写失败用例、运行确认、最小实现、运行相关回归、审查 diff”。测试使用现有 unittest，不新增测试框架。新增测试名是本计划规定的交付路径。

### Task 1：执行模式与兼容契约

**文件：** prompt_library、schemas、策略路由、policy 子包、原 JSON、Prompt Studio 两文件。

**输入：** 原 v4 profile 和旧 profile。

**输出：** 经验证的 `execution_policy`；导入/导出/保存/恢复后不丢失；执行快照能区分模式和 revision。

- [ ] 新建 `tests/llm/test_trend_5m_profile.py`：缺字段为 legacy；合法新字段往返保留；未知模式/版本拒绝；导入生成新 ID 但模式不变。
- [ ] 扩展前端现有逻辑测试：编辑文本后保存不能丢 execution_policy；预览显示执行模式，不仅显示名称。
- [ ] 扩展 profile 清洗字段和 API schema，增加严格校验；激活新模式时校验代码版本能力。
- [ ] 更新指纹投影、导出、归档与恢复；旧档案缺字段继续可读，未知新规则档案不激活。
- [ ] 更新原 JSON：添加 execution_policy，替换第 3 节有歧义的文本，保持模块和平铺字符串一致。
- [ ] 运行新测试及 `tests.llm.test_prompt_import_export`、`tests.core.test_policy_fingerprints`。

验证示例：

```python
def test_import_preserves_execution_policy(self):
    import json
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from unittest.mock import patch
    from scripts import prompt_library as prompts

    project = Path(__file__).resolve().parents[2]
    source = project / "data/prompt_strategies/r20-strategy-5分钟趋势确认跟随版-2026-09-25.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    policy = {"mode": "trend_confirm_5m", "revision": 1}
    payload["profile"]["execution_policy"] = policy
    with TemporaryDirectory() as directory:
        root = Path(directory)
        with patch.object(prompts, "ROOT", root), \
             patch.object(prompts, "LIBRARY_FILE", root / "library.json"):
            imported = prompts.import_profile(payload)
            exported = prompts.export_profile(imported["id"])
    self.assertEqual(exported["profile"]["execution_policy"], policy)
    self.assertNotEqual(imported["id"], "trend_confirm_5m")
```

此时只能保存配置，不允许开启新策略交易；就绪条件在 Task 8 汇总后成立。

### Task 2：纯指标与收盘快照

**文件：** 新策略模块、market_data_service、packages、factors。

**输入：** 原始 K线、明确来源、截至时间。

**输出：** 第 4.3 节统一趋势快照和第 4.4 节指标接口。

- [ ] 新建 `tests/trading/test_trend_5m_indicators.py`，覆盖闭合过滤、时间单位、乱序/重复/断档、OHLC 非法、样本不足和未来 1H 排除。
- [ ] 用手算序列和固定已验证数值夹具验证 Wilder ATR/ADX 初值、平滑和非提前舍入；不以复制实现作为预期值。
- [ ] 实现通道、ATR、ADX 和快照纯函数，再接入已收盘数据采集。
- [ ] 同一周期采集结果注入主脑/执行器，保留旧模式旧函数行为。
- [ ] 运行新测试与现有市场数据/主脑包相关测试。

可直接落实的通道用例：

```python
from scripts.strategies.trend_confirm_5m import ClosedBar, donchian_before_last

bars = [ClosedBar(i*300000, (i+1)*300000, 98, 100, 95, 98, 10)
        for i in range(20)]
bars.append(ClosedBar(6000000, 6300000, 99, 105, 98, 101, 20))
self.assertEqual(donchian_before_last(bars, 20), (100, 95))
self.assertGreater(bars[-1].close, 100)
```

### Task 3：核心门禁、提示词与缓存

**文件：** interceptor_manager、brain/prompt、brain/decisions、brain/cycle_parts、主脑门面。

**输入：** 统一快照、当前持仓/在途单、冻结模式。

**输出：** 已验证候选、精确提示词、带证据的决策缓存。

- [ ] 新建 `tests/trading/test_trend_5m_entry_gate.py`，使用明确矩阵：双 ADX=(20,20) 放行；(19.999,25)/(25,0)/缺失/NaN 拒绝；中线相等及反向拒绝；到期等号拒绝；报价过期拒绝。
- [ ] 实现不可编辑核心 gate，旧插件筛选按模式生效，不修改全局插件配置。
- [ ] 新建 `tests/llm/test_trend_5m_prompt.py`，捕获实际发送给模型的 system/user 文本，确认没有旧预测任务或矛盾预算。
- [ ] 限制 decisions 只消费合法候选；模型相反方向、伪造快照或未知标的均记录拒绝。
- [ ] 在 standard_cache/history 中保存原确认快照和 signal_id，保持原解释字段兼容。
- [ ] 测试 legacy 模式仍走旧 prompt 与旧插件组合。

### Task 4：定价、数量、幂等与到期撤单

**文件：** trader 的 entry_execution、order_intent、sizing、order_submit、order_lifecycle、ledger_writer、reservation_reconcile、data_shape；execution/sizing。

**输入：** 合法信号、最新报价、规格和风险预算。

**输出：** 受保护订单意图与可恢复状态；数量满足最终止损风险。

- [ ] 新建 `tests/trading/test_trend_5m_execution.py`，覆盖现价 100 不再强制改成 98.8、SL 不随意平移、宽 SL 降低数量、低于最小量拒绝、最终价格复验。
- [ ] 实现模式分支，保留旧 `resolve_entry_prices` 回踩行为。
- [ ] 将 `signal_id` 幂等检查接入现有预留临界区，提交前持久化 client_order_id 和 SUBMITTING。
- [ ] 加入 accepted/unknown/partial/cancel 状态恢复，禁止 UNKNOWN 重发。
- [ ] 挂单归属 KEEP 不豁免信号到期；最新方向失效也触发撤单并核实终态。
- [ ] 模式绑定 OKX；模拟路由选择外所时必须拒单，不能静默改走外所。
- [ ] 复用 fake adapter 验证重复周期、并发触发、重启恢复只产生一次被受理订单。

止损纯函数数值验收：

```python
from scripts.strategies.trend_confirm_5m import initial_stop
self.assertEqual(initial_stop(is_long=True, entry=100, lower20=90,
                              upper20=99, atr_1h=2, tick_size=0.1), 89.9)
self.assertEqual(initial_stop(is_long=False, entry=100, lower20=101,
                              upper20=110, atr_1h=2, tick_size=0.1), 110.1)
```

### Task 5：持仓管理、冷却和恢复

**文件：** position_exit、position_mgmt、cycle_stages、scale_out、data_shape、execution/cooldowns。

**输入：** 带原执行模式的 tracker、已收盘快照、真实账户及云端保护。

**输出：** 可恢复的 HOLD/推进策略线/确认平仓；同标的冷却。

- [ ] 新建 `tests/trading/test_trend_5m_position_management.py`：先判断旧线再推进；多空单调；盘中刺穿不触发策略平仓；收盘触发不受 confidence=0 或 LLM 失败影响。
- [ ] 新建 `tests/trading/test_trend_5m_recovery.py`：重启重放、遗漏 K线、部分平仓、写入失败、旧仓兼容、来源不明仓位拒绝新增风险。
- [ ] 按 tracker 模式在旧 position_exit 之前分流；禁用该模式旧分批/时间/阶梯/回撤逻辑。
- [ ] 对入场 OCO 及平仓使用既有确认函数；明确不同字段分别代表云端灾难线和本地策略线。
- [ ] 从云端成交回执和本地确认平仓统一登记冷却，不能只在本地触发分支记冷却。
- [ ] 模式切换时仍管理原 5m 仓位；禁止同一净仓位被两个策略同时加减仓。
- [ ] 验证账户开仓熔断期间仍调用此管理链。

数值验收：旧多头策略线 95，当前收盘 96，U10=99，不创新高，HOLD；下一根收盘 101、U10=100、L10=97、tick=0.1，推进至 96.9；再一根收盘 96.9，触发 CLOSE_MARKET。云端灾难线在这三步均不自动跟随。

### Task 6：5m 调度与推理预算

**文件：** 两个 scheduler、ai_factor_trader、brain/runtime、brain/dispatch 中实际调用/超时接线。

**输入：** active 新开仓模式、在管仓位模式、持久运行状态。

**输出：** 不重复的 5m 收盘周期，推理失败不堵塞下一周期。

- [ ] 新建 `tests/core/test_trend_5m_scheduler.py`，覆盖北京时间/UTC 同边界、收盘+3 秒、错过窄触发窗、任务未完成、重启和切回 legacy 仍有 5m 仓位。
- [ ] 用冻结时间函数测试，不真实 sleep。
- [ ] 两调度入口共享模式相关周期选择：新模式或存量 5m 仓位存在时 300 秒，否则保留 900 秒。
- [ ] 不仅修改 JOBS 常量，同时修改 due 判断、状态展示、超时、入口槽和重复触发处理。
- [ ] 串行入口复用标准库已有进程管理方式，使慢交易任务不阻塞维护任务；同一 trader 仍只运行一个实例。
- [ ] 任务超出 90 秒停止本轮等待并记录失败；已提交订单保持 UNKNOWN/已确认状态，下轮先对账，不能因超时清空意图。
- [ ] 对 LLM provider 回退链应用共同 45 秒截止时间。退出和对账安排在推理之前，无候选直接跳过推理。
- [ ] 验证只启用一个调度所有者，禁止 standalone 与 gateway 同时管理交易作业。

首发不要求新增独立高频进程。每根 5m 收盘处理一次本地策略退出；盘中突发风险由交易所灾难保护承担。若测得单周期持续超预算，先减少候选和推理开销，再决定拆分服务，不能通过放宽信号时效掩盖性能问题。

### Task 7：审计、复盘与真实历史回放

**文件：** signal_snapshot、evolution/observability、review_context、backtest_engine、backtest/lifecycle、metrics。

**输入：** 冻结信号、真实成交/退出、历史 5m/1H 数据。

**输出：** 可追溯策略统计和不混入合成数据的验证报告。

- [ ] 新建 `tests/trading/test_trend_5m_observability.py`：记录双 ADX、通道、ATR、信号年龄、下单前价格、两条止损线、拒绝码、手续费、执行模式。
- [ ] 增加 `TREND_CONFIRM_OBSERVED` / `TREND_CONFIRM_PARTIAL` 分类，保留旧 DYNAMICS 分类。缺快照禁止事后补造。
- [ ] 新建 `tests/backtest/test_trend_5m_replay.py`，验证 1H as-of join、下一根最早成交、费率滑点、同根止盈止损先后不明的保守处理、缺数据失败。
- [ ] 新模式调用同一套纯策略函数回放，不能重新写一份近似门禁。
- [ ] 旧展示回测可以保留原兼容行为，但新模式明确禁止合成行情回退。
- [ ] 回放处理全部交易和完整权益曲线；不能把展示用 recent_trades 或抽样曲线用于收益分布与最大回撤。
- [ ] 分开报告“确定性候选策略结果”和“包含真实 LLM 决策的完整系统结果”。没有历史模型输出时不声称已复现完整系统。

### Task 8：集成验收、影子运行与发布材料

**文件：** 新增 `tests/trading/test_trend_5m_integration.py`；维护本文验收记录及既有策略文档。

**输入：** Task 1～7 已通过的实现。

**输出：** 可审查的发布候选，不自动启用实盘。

- [ ] 用 fake market/LLM/adapter 走完整生产接线：行情、候选、模型、最终 gate、受理、成交、保护、退出、冷却。
- [ ] 对原策略 JSON 做真实导入及实际提示词渲染，验证没有未替换变量或旧任务混入。
- [ ] 以 legacy profile 运行同一用例，确认旧入场、插件、退出和调度仍符合既有测试。
- [ ] 运行第 9 节检查，记录命令、提交版本和实际结果，不填预期通过作为已通过。
- [ ] 完成影子与模拟阶段的报告，再提交新开仓开关、标的清单和风险额度供上线授权。
- [ ] 发布材料列明当前仅 OKX、禁用加仓、限价入场；不把第二阶段能力写成已支持。

### Task 9：第二阶段加仓与多所支持

本任务不是首发依赖；不开启对应能力时，不应提前实现通用多策略框架。

- [ ] 在 `pyramiding.py` 加入第 3.10 节新模式分支，测试盈利但未云端保本仍拒绝、同根不能首发并加仓、共享止损后的总风险。
- [ ] 新建 `tests/trading/test_trend_5m_pyramiding.py`，覆盖多空和加仓保护失败恢复。
- [ ] 多所行情使用目标场所的收盘数据；修改适配器时保持旧六列返回值兼容，新增带元数据接口或显式参数。
- [ ] 将 position_management、pending CANCEL 和 CLOSE 路由扩展为账户/环境/场所/标的/方向唯一身份，消除仅 instId 查找的歧义。
- [ ] 在 execution_router 中透传执行模式、冻结快照、ATR、最终风险量；保留公共校验，按模式处理保护 TP。
- [ ] 每所独立验收部分成交、撤单竞态、改单回读、最小量、费用和错误恢复。Gate/Binance 通过前不解除首发场所限制。

## 8. 推荐的影子与上线顺序

### 8.1 影子运行

只计算信号和虚拟持仓，不调用订单/撤单/改单 API，不写真实 tracker 或预留账本。输出写入明确隔离目录，例如 `data/shadow/trend_confirm_5m/`，沿用同一策略纯函数。

至少观察连续 7 天的调度、数据和幂等稳定性；若未覆盖趋势与震荡，延长观察。7 天是工程稳定性观察窗口，不是盈利能力证明。

记录每根是否按时完成、候选数量、拒绝原因、模型耗时和费用、信号到提交的延迟、预计费率与滑点。

### 8.2 历史回放

使用至少覆盖趋势和震荡的连续真实数据，推荐先取 90 天以上；预热数据不计收益，最后一段保留作不调参样本。

- 只用在当时可见的 1H 收盘数据，不用最终完整 1H K线回填小时内 5m 信号。
- 5m 收盘信号最早在收盘后的可成交价格执行，不能按产生信号的同根最低/最高价成交。
- 有 1m 或逐笔数据时用于撮合和保护触发；只有 5m OHLC 时，同根 TP/SL 都触及采用保守次序并报告歧义次数。
- 包含 taker 双边费用、资金费率、滑点、未成交、部分成交、跳空和故障情景。
- 缺少历史资金费率或成交微观数据时，明确成本假设，不能标为完整实盘重演。
- 输出净收益、最大回撤、收益回撤比、成交数、期望 R、持仓时长、手续费占比、浮盈回吐、连续亏损和分行情结果；不只看胜率。

### 8.3 模拟盘

使用明确隔离的模拟环境，先 1～2 个流动性较好的合约，禁用加仓。验证交易所受理、成交、保护回读、断线恢复和真实持仓归属。

模拟盘与真实行情差异较大时不得用旧 demo rescale 将价格整体平移后假称通道突破成立。应使用环境一致行情，或将该次判为无法验证；记录数据与执行环境差异。

### 8.4 小范围实盘

仅在单独获得实盘切换授权后执行，使用明确的标的白名单和低于常规额度的风险预算。不以“已经写好文档”或“测试通过”推定用户授权交易。

上线前保存可恢复的代码版本、profile、风险配置和路由配置；操作备份须沿用现有敏感信息保护机制，不把凭据写入文档或 Git。

上线后存在以下任一事件即停止新开仓：重复受理同一信号、保护覆盖不明、状态账本不可读、两个周期连续无法获得新鲜确认数据、执行模式错配、最终风险计算不一致。继续管理存量仓位。

## 9. 测试命令与验收标准

### 9.1 隔离要求

所有新增测试必须使用临时文件路径和 fake adapter/market/LLM，不加载真实账户凭据、不联网、不落真实 data。涉及门面 import 时遵循现有依赖注入/AST 隔离测试模式，不能为了跑测试自动读取 `.env`。

如全量既有测试存在真实配置或网络副作用，应先在隔离 checkout 和非生产测试配置中运行。不得为了得到绿色结果删除测试、绕过安全检查或降低阈值。

### 9.2 运行命令

实施完成后，使用项目现有虚拟环境；下列新测试文件在对应任务创建后执行：

```bash
.venv/bin/python -m unittest \
  tests.llm.test_trend_5m_profile \
  tests.llm.test_trend_5m_prompt \
  tests.trading.test_trend_5m_indicators \
  tests.trading.test_trend_5m_entry_gate \
  tests.trading.test_trend_5m_execution \
  tests.trading.test_trend_5m_position_management \
  tests.trading.test_trend_5m_recovery \
  tests.trading.test_trend_5m_observability \
  tests.core.test_trend_5m_scheduler \
  tests.backtest.test_trend_5m_replay \
  tests.trading.test_trend_5m_integration -v
```

相关旧回归至少包括：

```bash
.venv/bin/python -m unittest \
  tests.llm.test_prompt_import_export \
  tests.llm.test_prompt_rendering_isolated \
  tests.core.test_policy_fingerprints \
  tests.core.test_scheduler \
  tests.ops.test_gateway_scheduler \
  tests.trading.test_ai_position_mgmt_paths \
  tests.extraction.test_trader_pyramiding_extraction \
  tests.extraction.test_trader_order_intent_extraction -v
```

在隔离环境中执行仓库约定的最终回归：

```bash
.venv/bin/python -m unittest discover -s tests -t .
git diff --check
```

前端若改动，执行其 `package.json` 当前定义的相关测试、类型检查及构建脚本，实施时先读取实际脚本，不假设 pnpm/npm 命令名。源码锚点如需更新，必须同时保留 legacy 行为断言并补新模式运行时断言，不能只删除不再匹配的测试。

### 9.3 功能验收清单

- [ ] 当前未收盘 K线即使大幅刺穿通道，也不能产生开仓或收盘退出。
- [ ] 通道不含信号 K线，ADX/ATR 不含未来数据，1H as-of 对齐正确。
- [ ] 任一 ADX<20、无效或方向不符均由代码拒绝，不依赖模型服从。
- [ ] 实际发给模型的提示词没有旧微积分开仓/加仓任务，预算与执行参数一致。
- [ ] 突破价不会被改成固定 1.2% 回踩价，结构 SL 不被任意平移。
- [ ] 最终数量按最终 SL 风险限制，低于最小数量拒绝，不向上补量。
- [ ] 同一信号在并发、超时、重启条件下最多一次被受理；未知请求先对账。
- [ ] 信号到期/失效后不新发单，已挂单发起撤销并核实成交与终态。
- [ ] 策略线只朝有利方向移动；云端灾难线保持规定行为，不混为同一条线。
- [ ] LLM 故障、无候选、日亏熔断期间仍管理仓位和保护。
- [ ] 分批止盈、旧锁利和旧时间退出不会作用于 5m 仓位。
- [ ] 新旧仓位按入场模式管理，切换后仍能处理旧模式仓位。
- [ ] 未完成多所支持时外所入场被明确拒绝；非支持资产不能误进入该模式。
- [ ] 复盘缺失证据保持不可观测，不以当前数据回填历史。
- [ ] 回放没有合成行情收益，没有同根前视成交，没有将抽样权益曲线作为精确回撤来源。

## 10. 影响评估与观测指标

### 10.1 成本和性能

15m 理想固定频率每天 96 轮，5m 为 288 轮。只有“每轮均调用同样模型、相同 token、相同席位数”时调用成本才近似 3 倍；候选前置过滤后，LLM 调用可显著少于 288 次。行情获取与本地管理仍需要每轮运行。

1H 数据只在新小时收盘更新，5m 数据按收盘缓存；同一周期共享快照。保留已有请求限流和冷却机制，不能因频率提高增加无界重试。

建议影子阶段工程目标：

| 指标 | 首发目标 | 未达标处置 |
|---|---|---|
| 机械管理收盘至开始处理延迟 | 正常情况下≤15 秒 | 记录调度/行情原因，持续超标停止新开仓 |
| 整体 LLM 墙钟时间 | ≤45 秒 | WAIT，不能延长信号有效期 |
| 交易周期墙钟时间 | ≤90 秒 | 标记异常、恢复对账，保留保护 |
| 同一信号重复受理 | 0 | 立即停新开仓 |
| 无保护成交/保护覆盖不明 | 0 未处置事件 | 执行既有安全退出与告警 |
| 不同消费者快照不一致 | 0 | 拒绝对应周期入场 |

这些数值是建议的工程预算，不是已测性能或收益保证。

### 10.2 交易行为

- 检查频率增加不代表成交更多；三门禁可能使策略长时间空仓。
- 5m 突破与 1H ADX 带来确认滞后，防追价可能拒绝最强的快速行情。
- 通道退出可能捕捉长趋势，也可能比旧锁利规则回吐更多浮盈。
- 灾难线不随本地策略线移动时，进程故障下的最大回吐可能增大；需通过可靠调度和模拟故障测试量化这一代价。
- 保护性 TP 会在极端顺势行情提前结束仓位，不是真正无上限持有。
- taker 成交比例可能上升，必须报告扣费后结果。
- 宽结构止损使小账户更多信号因最小下单量而跳过，这是正确风险约束，不是程序故障。
- 90 分钟冷却在 5m 下覆盖 18 根，较原 15m 的 6 根更严格，不默认降低。

## 11. 回滚方案

### 11.1 行为回滚

1. 关闭 `trend_confirm_5m` 新开仓入口，保留该模式管理能力运行。
2. 查询并处理该模式全部在途开仓订单，核实已撤、部分成交或全成交，不能只清空本地意图。
3. 保持每笔 5m 存量仓位的灾难保护和收盘管理，等待正常退出；若选择立即平仓，单独确认该操作。
4. active profile 可恢复 legacy，但旧 5m tracker 不能改写为 legacy。
5. 直到不存在 5m 仓位、未知订单或保护单归属问题，才恢复纯 15m 管理节奏。

### 11.2 代码回滚

- 含新 schema 的状态仍存在时，不回滚到无法理解 execution_policy 或双止损字段的旧程序。
- 推荐回退至仍支持新状态、仅关闭新开仓的兼容版本。
- 确认 5m 状态清理完毕后，才允许恢复更早程序版本；历史交易和审计记录保留。
- 配置恢复不撤销已经发生的交易，不能将配置快照当交易状态快照覆盖真实成交。

## 12. 交付完成定义

首发完成须同时满足：Task 1～8 实现、相关与隔离全量回归通过、实际提示词验证通过、影子/模拟报告具备真实证据、回滚步骤可演练。首发可保持禁用加仓和多所，这两个限制必须明确展示。

代码验收与收益验收分开：工程检查通过只证明规则按设计运行，不证明长期正期望；没有足够真实样本时，结论必须写“收益优势尚未验证”。

本次文档交付不包含代码实施、配置激活、实盘操作或 Git 提交。后续实施从 Task 1 开始，按依赖推进；上线授权安排在代码与验证材料完整后。
