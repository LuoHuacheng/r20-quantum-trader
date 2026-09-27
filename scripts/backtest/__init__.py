"""回测引擎的领域实现（结构优化阶段 4·B3 第三十九刀起）。

门面仍是单文件 `scripts/backtest_engine.py`（`BacktestEngine` / `TradeRecord` /
`BacktestSummary` / `run_full_portfolio_backtest` / CLI `main`）。
本子包承接从那个文件里抽出的**成块领域逻辑**。

## 模块清单

| 模块 | 职责 | 注入面 |
|---|---|---|
| `evidence.py` | **证据化绩效与影子规则**（规划文档 §12.4）：毛/净盈亏、手续费、资金费、滑点、PF、最大回撤、平均/中位 R、连续亏损、按市场状态/策略模式/交易所分组、规则触发次数、被拒后本来结果、反例数、缺失字段不可验证标记；`shadow_rule_report` 只计算不拦截 | 无（纯计算；价格与规则集由调用方传参） |
| `lifecycle.py` | 持仓生命周期：保本锁定 / 止损止盈判定 / 结算（手续费、盈亏、`R` 倍数） | 无（纯计算；费率与滑点由调用方传参） |
| `metrics.py` | 入场装配（方向 / ATR 止损 / `rr` 倍止盈 / 张数）+ 绩效统计（胜率 / 盈亏比 / 夏普 / 索提诺 / 卡玛 / 平均 `R`）+ **组合层等权汇总**（`aggregate_portfolio`） | 无（纯计算；资本与费率由调用方传参） |

## 约定

1. **不原地改调用方的资金**：`settle_exit` **返回** `pnl`，由 `run()` 自增
   `self.capital`。标量无法按引用改，塞进容器只会更难读。
2. **会原地改 `pos`**：`evaluate_position_exit` 的保本锁定上移 `pos["stop_loss"]`
   是**有意**的 —— 下一次评估要用新止损，且调用方持有同一个 dict。
3. **`r_dist` 必须用"锁定前"的止损**：`ExitDecision.initial_stop_loss` 就是为此存在。
   用 `pos["stop_loss"]` 会在锁定发生后让 `R` 倍数退化成 0.0
   （第三十九刀第一版就踩了这个坑，见 `tests/extraction/test_backtest_lifecycle_extraction.py`）。
4. **`rr` 必须是显式形参**，不能图省事写成 `sig.get("rr")`：缺键时后者得 `None`
   而非 `0.0`，`None * float` 直接 `TypeError`（见 `metrics.build_entry_candidate`）。
5. **开仓滑点与平仓滑点方向相反**（`lifecycle` 的多头平仓 `×(1-s)`；
   `metrics` 的多头开仓 `×(1+s)`）—— 两者都取其**不利**方向，**勿"统一"成同号**。
6. **组合层标量是等权平均**（`sum / len(symbols)`），不是加权；
   除数是 `len(symbols)` 而**不是** `len(asset_results)` ——
   故 `aggregate_portfolio` 显式接收 `symbols`。
"""
