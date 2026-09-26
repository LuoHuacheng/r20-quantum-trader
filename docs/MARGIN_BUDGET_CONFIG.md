# 保证金预算配置指南（单笔顶 × 总额）

> 适用范围：把「**单笔保证金上限 = X U**、**全场在途保证金总额 = Y U**」落成一组可改、可验、可缩放的配置。
> 现状示例档：`10U/笔 + 100U 总额`（权益 400U）；上一档：`100U/笔 + 1000U 总额`（权益 4000U）。
> 本文只讲**保证金口径**的配置；止盈止损、分批止盈、插件风控不在此列。

---

## 0. 三条铁律（改任何东西之前先读这三条）

### 铁律 1 · 单笔顶只能用 `R20_MAX_MARGIN_EQUITY_RATIO` 实现

它**同时夹张数与记账**；`R20_MAX_SINGLE_ASSET_MARGIN_USDT` 在 OKX 直下路径上**只夹记账**。

| 键 | 夹什么 | 机制 |
|---|---|---|
| `R20_MAX_MARGIN_EQUITY_RATIO` | **张数**（+ 记账） | `max_size_within_margin()` 在 `size_for_decision()` 里是**最后一步 `min()`，只砍不放** ⇒ 真的把仓位压下来 |
| `R20_MAX_SINGLE_ASSET_MARGIN_USDT` | **只夹记账**（OKX 路） | 只在 `order_margin_gate()` 的 `min()` 里，夹的是写进 `venue_ctx["margin_usdt"]` 的**数字** |

**后果对比**：想「每笔 10U」却只改 `R20_MAX_SINGLE_ASSET_MARGIN_USDT=10` ⇒ 实际下单张数照旧（例：SUI 仍 306 张 / 36U），而预算预留、面板、提示词统统记成 10U ⇒ **账面 10U、实际 36U 的错账**，总额闸以为还剩很多额度。

> 例外：走 Gate/Binance 的 `execution_router.open_protected_position()` 是 `notional = margin × leverage` **由保证金反推张数**，那条路径上绝对封顶**会**真夹张数。三所口径因此不一致，跨所部署时要注意。

### 铁律 2 · 唯一的"总额"闸是 `R20_PORTFOLIO_RISK_BUDGET_USDT`（口径 = 保证金）

判据：`已占 + 本笔 > 总额 ⇒ fail-closed 拒单`（`0` = 不封顶）。
它**只拒绝、不夹取** —— 超了就是整单不发，不是砍小。

### 铁律 3 · 撤单/平仓**不会**立即释放预算占用

释放只有一个入口：预留对账器，且要**超 TTL（7200s）**。
TTL 从 `updated_at` 起算，而 `updated_at` 是 **UTC**。
⇒ **把 TOTAL 改小之前，必须先确认占用已经回落**，否则新配额会被自己的旧占用堵死（见 §5 SOP）。

---

## 1. 三个参数与不变式

| 符号 | 含义 | 本档 | 说明 |
|---|---|---|---|
| `PER_TRADE` | 单笔可用保证金上限 | 10 U | 由 `R20_MAX_MARGIN_EQUITY_RATIO × 可用余额` 实现 |
| `TOTAL` | 全场在途保证金总额上限 | 100 U | `R20_PORTFOLIO_RISK_BUDGET_USDT` |
| `E` | 账户可用权益（真实入金） | 400 U | **推荐 `E = 4 × TOTAL`** |

四条约束（推荐配比同时满足）：

1. `E ≥ TOTAL` —— 预算不能超过可用资金；
2. `R20_MARGIN_EQUITY_RATIO = PER_TRADE / E`；
3. `R20_SINGLE_ASSET_EQUITY_RATIO × E ≥ 4 × PER_TRADE` —— 单标的累计顶不低于 4 倍单笔（含加仓空间）；
4. `R20_MAX_MARGIN_EQUITY_RATIO × E` 应落在"能负担起一笔加仓"的量级 —— 本档取 `E = 4×TOTAL` 时，`0.025 × 400 = 10U`，10 笔并发刚好把 `TOTAL` 用满，自洽。

---

## 2. 配置表（规则为准，示例档为参考）

### 2.1 ratio 系（随权益自动缩放，权益不变则**不用改**）

| 环境变量（后台 `/admin/risk` 页） | 规则 | 10U/100U 档 | 100U/1000U 档 | 今天现值 |
|---|---|---|---|---|
| `R20_MAX_MARGIN_EQUITY_RATIO` | `PER_TRADE / E` | **0.025** | 0.025 | 0.25 |
| `R20_SINGLE_ASSET_EQUITY_RATIO` | `4 × PER_TRADE / E` | **0.10** | 0.10 | 0.40 |
| `R20_DAILY_LOSS_EQUITY_RATIO` | `TOTAL / E` | **0.25** | 0.25 | 0.08 |
| `R20_RISK_PER_TRADE_RATIO` | 不变（跟随 E 自动缩放） | 0.03 | 0.03 | 0.03 |

### 2.2 绝对数（**换档必须逐个改**）

| 环境变量 | 规则 | 10U/100U 档 | 100U/1000U 档 | 今天现值 | 不改会怎样 |
|---|---|---|---|---|---|
| `R20_PORTFOLIO_RISK_BUDGET_USDT` | `= TOTAL` | **100** ⏳见 §5 | 1000 | 100000 | 总额闸形同不存在 |
| `R20_MAX_SINGLE_ASSET_MARGIN_USDT` | `4 × PER_TRADE`（下限 `= PER_TRADE`） | **40** | 400 | 800 | 小于 PER_TRADE 时制造"账面 vs 实际"错账 |
| `R20_MAX_DAILY_LOSS_USDT` | `= TOTAL` | **100** | 1000 | 300 | 被小值绑死，缩不到 10× |
| `R20_MAX_TOTAL_EXPOSURE_USDT` | `0`（OKX 不读）或 `30 × PER_TRADE` | **0** | 0 | 3000 | 只影响 Gate/Binance 与提示词文本 |
| `data/instrument_pool.json` × **每条**标的的 `risk_per_trade_usd` | `PER_TRADE / 2` | **5.0** | **50.0** | 15.0 | ★ 100U 档不改：2×基准仓仅 ~65U ⇒ **永远到不了 100U** |

### 2.3 不需要跟着改的

| 键 | 值 | 为什么 |
|---|---|---|
| `R20_MAX_CONCURRENT_POSITIONS` | 10 | `10 × PER_TRADE = TOTAL`，与总额自洽 |
| `R20_MAX_SAME_DIRECTION_POSITIONS` | 5 | 同上（多/空各自封顶） |
| `R20_MIN_LEVERAGE` / `R20_MAX_LEVERAGE` | 5 / 20 | 但注意 `PER_TRADE × 杠杆 = 单笔名义额`（10U@10x = 100U；@20x = 200U） |
| `R20_SCALE_OUT_*` / `R20_TIME_STOP_*` / `R20_STOP_LOSS_ATR_MULT` | 不变 | **比例**逻辑，仓位等比缩小后触发点不变 |

---

## 3. 生效值与推导链（改完用这四行核对）

```
单笔张数 = clamp( AI计划张数,
                 0.5×base_sz ~ 2.0×base_sz,
                 max_size_within_margin = R20_MAX_MARGIN_EQUITY_RATIO × 可用余额 )   ← 最后一步 min，只砍不放
base_sz   = risk_per_trade_usd / (ctVal × ATR × sl_atr_mult)
risk_per_trade_usd = min(池内该标的的值, 可用余额 × R20_RISK_PER_TRADE_RATIO)

单标的累计顶 = min(R20_MAX_SINGLE_ASSET_MARGIN_USDT,
                    max(可用余额 × R20_SINGLE_ASSET_EQUITY_RATIO, 1.0))
日亏熔断线   = min(R20_MAX_DAILY_LOSS_USDT,
                    max(可用余额 × R20_DAILY_LOSS_EQUITY_RATIO, 1.0))
```

**`risk_per_trade_usd = PER_TRADE / 2` 的来历**（用 SUI 的 ATR 0.0279、sl_mult 2.2 实算）：

| 池内 risk 值 | 基准仓 | 基准仓保证金 @10x | 结论 |
|---|---|---|---|
| **5U**（本档） | 81 张 | 9.4 U | 正好 ≈ 单笔顶 10U，加仓 0.5–2× 语义不失真 |
| 15U（现状） | 244 张 | 28 U | 被 10U 顶夹到 10U，基准仓与实际差 2.8× |
| 15U @100U 档 | 244 张 | 28 U（2×=57U） | **到不了 100U** |
| **50U** @100U 档 | 943 张 | 109 U（2×=218U） | 可达 100U ✓ |

---

## 4. 总额闸（`R20_PORTFOLIO_RISK_BUDGET_USDT`）的行为细节

- **口径 = 保证金**（不是名义额）。路由层刻意不做预筛，裁决单点在预留层的原子 `reserve()`；
- **占用来源两类**：已确认成交的持仓预留 + **在途未成交挂单的预留**（未成交单**全额**占用计划保证金）。
  实测比例参考：某日 3 笔在途入场单占 410U，持仓只占 36U ⇒ **吃预算的主要是 pending，不是持仓**；
- **释放**：只有预留对账器会释放，条件三条全满足
  1. 该标的**无持仓**、且不在在途挂单集合里；
  2. 预留年龄 `≥ TTL(7200s)`（从 `updated_at` 起算，UTC）；
  3. 本周期**跨所实况核验成功**（持仓 + 挂单枚举都成功）——任一失败则**本轮一笔都不释放**；
- **保守保留（会拖慢回笼）**：
  - 同一标的只要还有**任何**在途单（**不分方向**），该标的**全部**旧预留都保留；
  - 时间戳不可解析 → 视为"未到窗口"，永不释放；
- ⚠️ **上一条的推论（实测踩过，必须知道）**：AI 会**持续滚单**（撤掉的入场单下一个周期就按原尺寸补挂），所以
  **只要同一标的一直有在途单或持仓，该标的的旧预留就被无限期阻塞**。
  "撤单 + 等 TTL" 这条路径在 AI 持续交易时**走不通**。实测：20:30 撤 XRP → 20:45 又挂上；同时 21:00 新挂的 LINK 单把 20:03 那条 LINK 旧预留（125U）继续锁住。
  ⇒ 换档时请走 §5 的**迁移窗口**路径；
- **热生效**：`reservation_manager()` 每次调用新建实例 ⇒ 改 `TOTAL` **不需要重启**（下一周期即生效）。

---

## 5. 改动 SOP（含清占用的两条路径）

### 5.1 前两步（无副作用，随时可做）

```
① 压单笔：改 ratio 系 3 个 + MAX_SINGLE_ASSET_MARGIN + MAX_DAILY_LOSS + risk_per_trade_usd
   ⇒ TOTAL 暂时保留大值
   为什么：此刻台账里可能压着几百 U 旧占用，先设小 TOTAL 会立刻把新开仓全拒掉
   收益：此后 AI 再挂的每笔只占 PER_TRADE（例 10U），占用不再以 100~190U/笔 的速度增长

② 确认新尺寸已生效：看下一笔新入场单的预留额是否 ≈ PER_TRADE
```

### 5.2 清旧占用：两条路径，**只有 A 是确定性的**

> ⚠️ 为什么不能只靠"撤单 + 等 2h"：释放条件里有一条 **"该标的无仓无挂"**，而 AI 会持续滚单、持仓也会占着 ——
> 旧预留会被**无限期阻塞**。实测：20:30 撤掉 XRP 入场单 → 20:45 周期按原尺寸又挂上；21:00 新挂的 LINK 单把 20:03 的 LINK 旧预留（125U）继续锁住。

**路径 A · 迁移窗口（推荐，确定性）**

```
A1. 选一个整点后的空档（避开 :00/:15/:30/:45 的周期窗口，周期长度约 2~5min）
A2. 临时停网关调度（停 r20-gateway 容器）——保证窗口内没有新一轮滚单
A3. 撤掉全部在途入场单（保护单/算法单不要碰）
A4. 若有持仓：等它了结，或接受"该标的的旧行继续占"（其余标的照常清）
A5. 此时对每个"无仓无挂"的标的，把其未释放预留置闭合（见下方脚本）
A6. 设 TOTAL = 目标值
A7. 恢复网关容器
```

A5 的脚本（**只对确实无仓无挂的标的动手**；这是一次性的迁移操作，日常运维不应使用 ——
设计上只有对账器会释放预留）：

```python
# 前置校验：先确认该标的三处都为空（交易所持仓 / 在途普通挂单 / 在途算法单）
# 校验通过后再置 closed，否则跳过并打印原因
import sqlite3
from scripts import okx_rest

live_pos = {p['instId'] for p in okx_rest.request('GET', '/api/v5/account/positions', {'instType': 'SWAP'})}
live_ord = {o['instId'] for o in okx_rest.pending_orders()}
live_algo = {o['instId'] for o in okx_rest.pending_algo_orders()}
busy = live_pos | live_ord | live_algo          # 有仓/有挂单/有保护单 = 不许动

c = sqlite3.connect('data/risk_reservation.db')
cols = [d[1] for d in c.execute('pragma table_info(risk_reservations)')]
for r in c.execute("select * from risk_reservations where state='confirmed'").fetchall():
    d = dict(zip(cols, r))
    sym = str(d['intent_id']).split(':')[0]
    if sym in busy:
        print('跳过（仍在场）', d['intent_id']); continue
    c.execute("update risk_reservations set state='closed', released=1, "
              "updated_at=CURRENT_TIMESTAMP where id=?", (d['id'],))
    print('置闭合', d['intent_id'], d['amount_usdt'])
c.commit()
```

**路径 B · 自然老化（不推荐，不确定）**

不动台账，等对账器逐条放。**只在"某标的连续 2h 无仓无挂"时才可能成功**，而 AI 持续滚单时可能永远等不到。
只有在"不急着换档、且愿意长期观察"时用。

### 5.3 收尾

```
③ 核对占用已回落到 ≤ TOTAL - PER_TRADE
④ 设 TOTAL = 目标值（R20_PORTFOLIO_RISK_BUDGET_USDT）
⑤ 验收：第一笔新入场单的预留额 ≈ PER_TRADE，交易所张数 ≈ PER_TRADE×杠杆/(价×ctVal)
```

> 反例（别这么做）：先设 `TOTAL=100` 而台账里还压着 525U ⇒ `已占 + 本笔 > 100` 恒真 ⇒ **每单 fail-closed 拒开**。

---

## 6. 生效机制速查

| 改什么 | 怎么改 | 何时生效 | 是否需要重启 |
|---|---|---|---|
| 风控常量（`R20_*`） | 后台 `/admin/risk` 页保存（写入 `.env`） | trader 是**每周期 spawn 的子进程**，启动时重读 `.env` ⇒ **下一个 15min 整点** | 不需要；backend 侧保存时还会即时热重载 |
| `risk_per_trade_usd`（池） | **直接编辑 `data/instrument_pool.json`** | 同上（下一周期读文件） | 不需要 |
| 账户凭证 / 执行闸 | 后台 | 同上 | 不需要 |

⚠️ 后台 instruments 接口（`POST/DELETE /api/v1/admin/instruments`）**只支持增删标的（只收 `inst_id`）**，不暴露 `risk_per_trade_usd` ⇒ 池内风险额**必须改文件**。

---

## 7. 维护纪律

### 7.1 ratio 口径 = "单笔顶 = n% 权益"，不是"固定 N U"

`max_size_within_margin` 用的是**每周期实时读到的可用余额**，所以：

> **入金/出金/权益漂移后，ratio 必须按公式重算**，否则单笔顶会跟着权益一起漂。

例：设好 `0.025`（对 400U ⇒ 10U）之后账户涨到 1000U ⇒ 单笔顶自动变成 **25U**。

### 7.2 OKX 路径**没有**绝对张数闸

想"张数也钉死在 10U"，唯一组合是「**权益稳定 + ratio 重算**」。`R20_MAX_SINGLE_ASSET_MARGIN_USDT` 帮不上（只夹记账）。
另注：**同一份 `.env` 同时作用于 demo/live**，而 demo 账户权益可能极大（例：102,775U）⇒ ratio 0.025 在 demo 上等于 2569U/笔。**这套值只在"权益 ≈ 400U 的那个账户"下等于 10U**，跨账户规模测试时不要拿它当"固定 10U"。

### 7.3 改档速查（填 6 个数即可）

给定 `(PER_TRADE, TOTAL)`，且 `E = 4 × TOTAL`：

```
R20_MAX_MARGIN_EQUITY_RATIO   = PER_TRADE / E
R20_SINGLE_ASSET_EQUITY_RATIO = 4 × PER_TRADE / E
R20_DAILY_LOSS_EQUITY_RATIO   = TOTAL / E
R20_PORTFOLIO_RISK_BUDGET_USDT = TOTAL
R20_MAX_SINGLE_ASSET_MARGIN_USDT = 4 × PER_TRADE
R20_MAX_DAILY_LOSS_USDT        = TOTAL
池内 risk_per_trade_usd（×N 条） = PER_TRADE / 2
```

**ratio 系三档保持一致时，换档只改 4 个绝对数 + 入金。**

---

## 8. 验证命令

```bash
# ① 生效值与推导（容器内读真实值，不碰 .env）
docker exec r20-backend python3 -c "
from scripts import risk_constants as rc
import json
E = 400.0   # 换成真实可用余额
print('单笔顶   =', rc.MAX_MARGIN_EQUITY_RATIO * E, ' (目标', 10, ')')
print('总额     =', rc.PORTFOLIO_RISK_BUDGET_USDT)
print('单标的顶 =', rc.effective_single_asset_margin(E))
print('日亏顶   =', rc.effective_daily_loss_limit(E))
print('并发上限 =', rc.effective_max_positions(10))
print('池内风险额 =', {p['name']: p['risk_per_trade_usd']
                     for p in json.load(open('/app/data/instrument_pool.json'))})
"

# ② 总额占用（口径=保证金）
docker exec r20-backend python3 -c "
from scripts.ai_factor_trader import reservation_manager
print('占用 =', reservation_manager().gross_exposure('demo'))"

# ③ 占用明细 + 各自还要多久才够 TTL（updated_at 是 UTC）
sqlite3 data/risk_reservation.db \
  "select id,intent_id,amount_usdt,datetime(updated_at,'+8 hours') as updated_bj from risk_reservations where state='confirmed';"

# ④ 空仓位/挂单核对
docker exec r20-backend python3 -c "
from scripts import okx_rest
print('普通挂单', okx_rest.pending_orders())
print('算法单  ', okx_rest.pending_algo_orders())"

# ⑤ 周期输出（trader 是子进程，stdout 落在网关作业库，不在 logs/*.log）
sqlite3 data/r20_gateway.db \
  "select started_at,status,substr(detail,-1500) from job_runs where job_name='trader' order by rowid desc limit 1;"
```

**验收判据**：新入场单在 `risk_reservations.amount_usdt ≈ PER_TRADE`，且交易所张数 ≈ `PER_TRADE × 杠杆 / (价 × ctVal)`。

---

## 9. 已知边界与坑（清单）

| # | 事实 | 影响 / 规避 |
|---|---|---|
| 1 | `MAX_SINGLE_ASSET_MARGIN` 在 OKX 路只夹记账 | 见铁律 1。要夹张数只能用 ratio |
| 2 | 撤单/平仓不释放预算，TTL 2h | 见铁律 3 与 §5 SOP |
| 3 | 同一标的有任何在途单/持仓 ⇒ 该标的**全部**旧预留都保留 | 同标的反复滚单会让旧预留**无限期**占着；实测 21:00 新挂的 LINK 单锁住了 20:03 的 LINK 旧预留 ⇒ 换档走 §5.2 路径 A |
| 4 | 跨所实况核验失败 ⇒ 本轮一笔都不释放 | 占用"卡住"时先查持仓/挂单枚举是否成功，别急着改台账 |
| 5 | `R20_MAX_TOTAL_EXPOSURE_USDT` 在 OKX 直下**无消费者** | 只影响 Gate/Binance 与提示词 |
| 6 | 提示词「常规单笔保证金」按**权益比例**算，不与绝对封顶取 min | 当绝对封顶远小于权益比例区间时，同一段提示词会**上下限倒挂**（权益 102,775U 时实测倒挂 300~1200 倍）。改档时如出现倒挂需一并调整提示词口径 |
| 7 | `R20_MAX_DAILY_LOSS_USDT` 与 `DAILY_LOSS_EQUITY_RATIO` 取 min | 两把尺不等值时，真正生效的是小的那支；本档刻意取等值（100 = 0.25×400） |
| 8 | 单笔顶随可用余额**递减**（开仓占用后可用下降） | 这是特性：连续开仓的顶逐笔变小，累计自然收敛到 `TOTAL` 附近 |
| 9 | 日亏顶按 `TOTAL` 设定是刻意选择 | 单笔最大亏损 ≈ `PER_TRADE × 杠杆 × 止损距`（10U@10x、2.8% ⇒ ≈2.8U）⇒ 100U 日亏顶要 ~35 笔连亏才触及，它不再是"兜住几笔止损"的闸，而是"亏掉一个总预算就停当日" |

---

## 10. 代码锚点（排障时按图索骥）

| 关注点 | 位置 |
|---|---|
| 张数推导（4 道钳制） | `scripts/trader/sizing.py::size_for_decision` |
| 单笔余额硬顶 | `r20_backend/execution/sizing.py::max_size_within_margin` |
| 单笔风险额 | `r20_backend/execution/sizing.py::effective_risk_per_trade` |
| 记账侧保证金闸 | `scripts/trader/gates.py::order_margin_gate` |
| 意图装配（margin 进 venue_ctx） | `scripts/trader/order_intent.py::build_order_intent` |
| 保证金估算（预留用） | `scripts/trader/routing_policy.py::estimate_margin_usdt` |
| **总额闸** | `scripts/trader/routing_policy.py::portfolio_budget_guard` |
| 预留台账（SQLite） | `r20_backend/risk_reservation.py`、`data/risk_reservation.db` |
| **占用回笼（TTL 2h）** | `scripts/trader/reservation_reconcile.py::reconcile_reservation_ledger`、`RESERVATION_RECONCILE_TTL_S`（`scripts/ai_factor_trader.py`） |
| 生效值定义 / 派生上限 | `scripts/risk_constants.py`（`effective_single_asset_margin` / `effective_daily_loss_limit` / `effective_max_positions`） |
| 加仓累计顶判定 | `scripts/trader/pyramiding.py::pyramiding_gate` |
| 多所由保证金反推张数 | `r20_backend/execution_router.py`（`notional = margin × leverage`） |
| 后台风控页 / API | `r20_backend/routers/risk.py`（`/api/v1/admin/risk`）、`frontend/src/views/admin/RiskPage.vue` |
| 标的池文件 | `scripts/instrument_pool.py`（`POOL_FILE = data/instrument_pool.json`） |
| 提示词风险预算段 | `scripts/ai_brain_trader.py::build_risk_budget_text` |
| 周期调度（15min 子进程） | `r20_gateway/scheduler.py`（`JobSpec("trader", ...)`） |
