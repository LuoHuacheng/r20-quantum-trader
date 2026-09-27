#!/usr/bin/env python3
"""
R20 Quantum Multi-Asset Backtesting & Statistical Verification Engine (backtest_engine.py)
------------------------------------------------------------------------------------------
Features:
- Multi-Asset Portfolio Backtesting (Simultaneous 6 Instruments)
- Single Asset Isolation Backtesting
- OKX Real Public Candles Synchronous Ingestion
- Realistic PnL, Fees (Taker 0.05%, Maker 0.02%), Slippage (0.02%)
- Equity Curve History for Mini-chart Rendering
- Trade-by-Trade Execution Audit Log
- Risk Metrics: Sharpe, Sortino, Calmar, Max Drawdown, Win Rate, Profit Factor
- Fail-Closed Interceptor Gatekeeper Filtering Attribution
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import sys
import urllib.request

# ⚠️ 第七十九刀（bootstrap 门抓到的同类真雷）：顶层 import `scripts.` 但
# 此前**无任何仓库根 bootstrap**（下方 L9x 的 insert 只是函数内给
# `market_data_service` 的 fallback，且目录不是 repo 根）——
# `python scripts/backtest_engine.py` 直跑必 ModuleNotFoundError。
# 补 factor_library 同款（幂等）。
_BT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BT_ROOT not in sys.path:
    sys.path.insert(0, _BT_ROOT)

from scripts.backtest.lifecycle import evaluate_position_exit, settle_exit
from scripts.backtest.metrics import (
    aggregate_portfolio,
    build_entry_candidate,
    compute_performance_metrics,
)
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

WORKSPACE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = WORKSPACE_DIR / "data"

DEFAULT_SYMBOLS = [
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
    "DOGE-USDT-SWAP",
    "SUI-USDT-SWAP",
    "ASTER-USDT-SWAP",
]


@dataclass
class TradeRecord:
    symbol: str
    entry_time: str
    exit_time: str
    direction: str  # "LONG" | "SHORT"
    entry_price: float
    exit_price: float
    size: float
    pnl_usd: float
    pnl_pct: float
    exit_reason: str  # "TAKE_PROFIT" | "STOP_LOSS" | "TRAILING_STOP"
    r_multiple: float
    # ── 规划文档 §12.4：回测必须能逐笔对账成本与证据，不能只有胜率 ──
    # 默认值让旧构造点（测试夹具）无需改动。
    fee_usd: float = 0.0        # 开仓 taker + 平仓 maker（与 settle_exit 同口径反推）
    slippage_usd: float = 0.0   # 开/平两侧按配置滑点率估算（回测不建模盘口深度）
    funding_usd: float = 0.0    # ⚠️ 回测**不建模**资金费 —— 结构性 0，不是"没付费"
    rsi_15m: Optional[float] = None
    jerk_15m: Optional[float] = None
    regime: str = ""
    strategy_mode: str = ""


@dataclass
class BacktestSummary:
    symbol: str
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    profit_factor: float
    initial_equity: float
    final_equity: float
    total_return_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    avg_r_multiple: float
    gatekeeper_filtered_count: int
    equity_curve: List[Dict[str, Any]] = field(default_factory=list)
    recent_trades: List[Dict[str, Any]] = field(default_factory=list)
    #: 全量成交（规划文档 §12.4：影子规则与扩展绩效需要**每一笔**，
    #: `recent_trades` 只有最后 10 笔）。默认空列表保持旧构造点兼容。
    all_trades: List[Dict[str, Any]] = field(default_factory=list)


def fetch_okx_candles(inst_id: str, bar: str = "1H", limit: int = 100) -> List[Dict[str, Any]]:
    """Fetch live historical K-line candles via the zero-process 3-level
    failover service (www.okx.com → aws.okx.com → okx CLI) instead of a
    single hardcoded host."""
    try:
        from market_data_service import fetch_candles
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from market_data_service import fetch_candles
    raw = fetch_candles(inst_id, bar=bar, limit=limit, timeout=10.0)
    candles: List[Dict[str, Any]] = []
    try:
        for c in reversed(raw or []):
            ts_ms = int(c[0])
            dt_str = datetime.datetime.fromtimestamp(ts_ms / 1000.0, tz=datetime.timezone(datetime.timedelta(hours=8))).strftime("%m-%d %H:%M")
            candles.append({
                "symbol": inst_id,
                "timestamp": dt_str,
                "ts_ms": ts_ms,
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]) if len(c) > 5 else 0.0,
            })
    except (ValueError, IndexError) as exc:
        print(f"Failed to parse OKX public candles for {inst_id}: {exc}")
        return []
    return candles


class BacktestEngine:
    def __init__(
        self,
        initial_capital: float = 10000.0,
        risk_per_trade_pct: float = 0.02,
        maker_fee: float = 0.0002,
        taker_fee: float = 0.0005,
        slippage: float = 0.0002,
        min_confidence_gate: float = 0.75,
        min_rr_gate: float = 2.0,
        strategy_mode: str = "trend_confirm_5m",
    ):
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.risk_per_trade_pct = risk_per_trade_pct
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.slippage = slippage
        self.min_confidence_gate = min_confidence_gate
        self.min_rr_gate = min_rr_gate
        #: 影子规则评估用的策略模式（规划文档 §12.4 / 阶段 C）：只用于**统计**
        #: "如果执行会拒哪些交易"，**不拦截**任何回测成交。
        self.strategy_mode = str(strategy_mode or "")

    def run(self, candle_series: List[Dict[str, Any]], signals: Optional[List[Dict[str, Any]]] = None) -> BacktestSummary:
        symbol = candle_series[0].get("symbol", "PORTFOLIO") if candle_series else "UNKNOWN"
        if len(candle_series) < 20:
            return BacktestSummary(
                symbol=symbol,
                total_trades=0,
                winning_trades=0,
                losing_trades=0,
                win_rate_pct=0.0,
                profit_factor=0.0,
                initial_equity=self.initial_capital,
                final_equity=self.initial_capital,
                total_return_pct=0.0,
                max_drawdown_pct=0.0,
                sharpe_ratio=0.0,
                sortino_ratio=0.0,
                calmar_ratio=0.0,
                avg_r_multiple=0.0,
                gatekeeper_filtered_count=0,
                equity_curve=[],
                recent_trades=[],
            )

        equity_curve_data: List[Dict[str, Any]] = [{"time": candle_series[0]["timestamp"], "equity": round(self.initial_capital, 2)}]
        returns_list: List[float] = []
        trades: List[TradeRecord] = []
        active_position: Optional[Dict[str, Any]] = None
        filtered_by_gatekeeper = 0

        # Build signals
        signal_map = {}
        if signals:
            for s in signals:
                signal_map[s.get("timestamp")] = s
        else:
            closes = [float(c["close"]) for c in candle_series]
            for idx in range(15, len(candle_series)):
                ts = candle_series[idx]["timestamp"]
                c = closes[idx]
                ma_short = sum(closes[idx - 5 : idx]) / 5
                ma_long = sum(closes[idx - 15 : idx]) / 15
                vol = (max(closes[idx - 5 : idx]) - min(closes[idx - 5 : idx])) / (c or 1)

                conf = 0.82 if abs(ma_short - ma_long) / c > 0.004 else 0.65
                rr = 2.2 if vol > 0.008 else 1.5

                if ma_short > ma_long and c > ma_short:
                    signal_map[ts] = {"action": "BUY", "confidence": conf, "rr": rr, "atr": max(c * 0.012, 0.0001)}
                elif ma_short < ma_long and c < ma_short:
                    signal_map[ts] = {"action": "SELL", "confidence": conf, "rr": rr, "atr": max(c * 0.012, 0.0001)}

        peak_equity = self.initial_capital
        max_drawdown = 0.0

        for candle in candle_series:
            ts = candle["timestamp"]
            o = float(candle["open"])
            h = float(candle["high"])
            l = float(candle["low"])
            c = float(candle["close"])

            # 1. Active Position Lifecycle Management
            if active_position is not None:
                pos = active_position
                _exit = evaluate_position_exit(
                    pos=pos, high=h, low=l, close=c, slippage=self.slippage)

                if _exit.should_exit:
                    _gross = ((_exit.exit_price - pos["entry_price"])
                              if pos["direction"] == "LONG"
                              else (pos["entry_price"] - _exit.exit_price)) * pos["size"]
                    pnl, pnl_pct, r_mult = settle_exit(
                        pos=pos, decision=_exit,
                        taker_fee=self.taker_fee, maker_fee=self.maker_fee)

                    self.capital += pnl
                    trades.append(
                        TradeRecord(
                            symbol=candle.get("symbol", symbol),
                            entry_time=pos["entry_time"],
                            exit_time=ts,
                            direction=pos["direction"],
                            entry_price=round(pos["entry_price"], 4),
                            exit_price=round(_exit.exit_price, 4),
                            size=round(pos["size"], 4),
                            pnl_usd=round(pnl, 2),
                            pnl_pct=round(pnl_pct * 100, 2),
                            exit_reason=_exit.exit_reason,
                            r_multiple=round(r_mult, 2),
                            # 成本反推（§12.4）：settle_exit 里 pnl = 毛盈亏 - 手续费，
                            # 故手续费 = 毛盈亏 - 净盈亏；滑点按配置率×两侧名义额估算。
                            fee_usd=round(_gross - pnl, 4),
                            slippage_usd=round((abs(pos["entry_price"]) + abs(_exit.exit_price))
                                               * pos["size"] * self.slippage, 4),
                            funding_usd=0.0,
                            rsi_15m=pos.get("rsi_15m"),
                            jerk_15m=pos.get("jerk_15m"),
                            regime=str(pos.get("regime") or ""),
                            strategy_mode=str(pos.get("strategy_mode") or self.strategy_mode or ""),
                        )
                    )
                    active_position = None

            # Track equity curve
            cur_equity = self.capital
            equity_curve_data.append({"time": ts, "equity": round(cur_equity, 2)})
            if len(equity_curve_data) > 1:
                ret = (equity_curve_data[-1]["equity"] - equity_curve_data[-2]["equity"]) / equity_curve_data[-2]["equity"]
                returns_list.append(ret)

            if cur_equity > peak_equity:
                peak_equity = cur_equity
            dd = (peak_equity - cur_equity) / peak_equity if peak_equity > 0 else 0.0
            if dd > max_drawdown:
                max_drawdown = dd

            # 2. Gatekeeper Filter and Signal Evaluation
            if ts in signal_map:
                sig = signal_map[ts]
                conf = sig.get("confidence", 0.0)
                rr = sig.get("rr", 0.0)

                # Gatekeeper Hard Interceptors
                if conf < self.min_confidence_gate or rr < self.min_rr_gate:
                    filtered_by_gatekeeper += 1
                    continue

                if active_position is None:
                    active_position = build_entry_candidate(
                        sig=sig, close=c, timestamp=ts, capital=self.capital,
                        risk_per_trade_pct=self.risk_per_trade_pct,
                        slippage=self.slippage, rr=rr)
                    # 影子规则所需的开仓时刻证据（§12.4）：只**挂到内部仓位 dict**，
                    # 不改 `build_entry_candidate` 的返回键集（那是对拍门钉住的契约）。
                    if active_position is not None:
                        active_position["rsi_15m"] = sig.get("rsi_15m", sig.get("rsi"))
                        active_position["jerk_15m"] = sig.get("jerk_15m", sig.get("jerk"))
                        active_position["regime"] = sig.get("regime") or sig.get("market_regime") or ""
                        active_position["strategy_mode"] = sig.get("strategy_mode") or self.strategy_mode

        _metrics = compute_performance_metrics(
            trades=trades, capital=self.capital,
            initial_capital=self.initial_capital,
            returns_list=returns_list, max_drawdown=max_drawdown)
        win_rate = _metrics["win_rate"]
        profit_factor = _metrics["profit_factor"]
        total_return = _metrics["total_return"]
        sharpe = _metrics["sharpe"]
        sortino = _metrics["sortino"]
        calmar = _metrics["calmar"]
        avg_r = _metrics["avg_r"]
        winning_count = _metrics["winning_trades"]
        losing_count = _metrics["losing_trades"]

        # Format trade logs (last 10)
        recent_trades_json = [asdict(t) for t in reversed(trades[-10:])]

        return BacktestSummary(
            symbol=symbol,
            total_trades=len(trades),
            winning_trades=winning_count,
            losing_trades=losing_count,
            win_rate_pct=round(win_rate, 1),
            profit_factor=round(profit_factor, 2),
            initial_equity=round(self.initial_capital, 2),
            final_equity=round(self.capital, 2),
            total_return_pct=round(total_return, 2),
            max_drawdown_pct=round(max_drawdown * 100, 2),
            sharpe_ratio=round(sharpe, 2),
            sortino_ratio=round(sortino, 2),
            calmar_ratio=round(calmar, 2),
            avg_r_multiple=round(avg_r, 2),
            gatekeeper_filtered_count=filtered_by_gatekeeper,
            equity_curve=equity_curve_data[:: max(1, len(equity_curve_data) // 20)],  # sampled for mini-chart
            recent_trades=recent_trades_json,
            all_trades=[asdict(t) for t in trades],
        )


def run_full_portfolio_backtest(bar: str = "1H", limit: int = 100, capital_per_asset: float = 10000.0) -> Dict[str, Any]:
    """
    Runs multi-asset backtesting across all TARGET_INSTRUMENTS.
    Aggregates into both individual asset summaries and a combined Portfolio performance.
    """
    symbols = DEFAULT_SYMBOLS
    asset_results = {}
    combined_trades = []
    #: 全量成交（`recent_trades` 只有每个标的最后 10 笔）：影子规则与扩展绩效要逐笔。
    combined_all_trades: List[Dict[str, Any]] = []
    total_initial = 0.0
    total_final = 0.0
    total_gatekeeper_filtered = 0
    evaluated_symbols: List[str] = []
    unverifiable: List[Dict[str, Any]] = []

    for sym in symbols:
        candles = fetch_okx_candles(sym, bar=bar, limit=limit)
        if not candles:
            # ⚠️ 规划文档 §12.4：「回测不能使用合成行情替代缺失真实数据。
            # 缺数据必须标记不可验证」——旧实现在此处用 sin() 造 100 根假 K 线，
            # 等于用假数据得出真结论。现改为如实标记并**排除出聚合**。
            unverifiable.append({"symbol": sym, "status": "UNVERIFIABLE",
                                 "reason": "NO_REAL_CANDLES"})
            asset_results[sym] = {"symbol": sym, "status": "UNVERIFIABLE",
                                  "reason": "NO_REAL_CANDLES", "total_trades": 0}
            continue
        evaluated_symbols.append(sym)
        engine = BacktestEngine(initial_capital=capital_per_asset)
        summary = engine.run(candles)
        asset_results[sym] = asdict(summary)
        total_initial += capital_per_asset
        total_final += summary.final_equity
        total_gatekeeper_filtered += summary.gatekeeper_filtered_count
        combined_trades.extend(summary.recent_trades)
        combined_all_trades.extend(getattr(summary, "all_trades", None) or summary.recent_trades)

    # Portfolio combined performance（仅用**真实行情**评测出的标的）
    portfolio_summary = aggregate_portfolio(
        asset_results={s: asset_results[s] for s in evaluated_symbols},
        symbols=evaluated_symbols or ["PORTFOLIO"],
        total_initial=total_initial or capital_per_asset,
        total_final=total_final or capital_per_asset,
        total_gatekeeper_filtered=total_gatekeeper_filtered,
        combined_trades=combined_trades)

    # §12.4：完整绩效面板（成本/滑点/R/分组/规则触发/反例/完整性）
    try:
        from scripts.backtest.evidence import compute_extended_metrics
        extended = compute_extended_metrics(combined_all_trades,
                                            initial_capital=total_initial or capital_per_asset)
    except Exception as exc:  # 统计不可得不得把报告整个打挂
        extended = {"error": str(exc)[:200]}

    # 影子硬规则（阶段 C / §12.4）：只**计算**会拒哪些交易，不改变任何回测成交。
    try:
        from scripts.backtest.evidence import shadow_rule_report
        shadow_report = shadow_rule_report(combined_all_trades)
    except Exception as exc:
        shadow_report = {"error": str(exc)[:200]}

    full_payload = {
        "updated_at": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S (北京时间)"),
        "bar": bar,
        "limit": limit,
        "portfolio": portfolio_summary,
        "by_symbol": asset_results,
        "active_symbols": evaluated_symbols,
        "unverifiable_symbols": unverifiable,
        "extended_metrics": extended,
        "shadow_rules": shadow_report,
        # §12.4 诚实披露：回测建了什么成本、没建什么（资金费结构性为 0）。
        "modeled_costs": {"fees": True, "slippage": True, "funding": False},
    }
    return full_payload


def main():
    parser = argparse.ArgumentParser(description="R20 Multi-Asset Quantitative Backtesting & Statistical Engine")
    parser.add_argument("--symbol", default="ALL", help="Symbol or 'ALL' for portfolio")
    parser.add_argument("--bar", default="1H", help="Candle bar: 15m, 1H, 4H")
    parser.add_argument("--limit", type=int, default=100, help="Candle count")
    parser.add_argument("--capital", type=float, default=10000.0, help="Initial capital per asset")
    parser.add_argument("--output", default="data/backtest_report.json", help="Path to output json")
    args = parser.parse_args()

    print(f"Executing quantitative backtest (mode={args.symbol}, bar={args.bar}, limit={args.limit}, capital={args.capital})...")
    report = run_full_portfolio_backtest(bar=args.bar, limit=args.limit, capital_per_asset=args.capital)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    p = report["portfolio"]
    print("\n==========================================================================")
    print("      R20 QUANTUM TRADER 6-ASSET PORTFOLIO BACKTEST ATTRIBUTION REPORT    ")
    print("==========================================================================")
    print(f" Portfolio Mode       : 6大主力标的对齐组合 (BTC, ETH, SOL, DOGE, SUI, ASTER)")
    print(f" Backtest Range       : OKX 官方实时最新 {args.limit} 根 {args.bar} K线序列")
    print(f" Total Return         : {p['total_return_pct']}% (总净值: ${p['final_equity']:,.2f})")
    print(f" Win Rate             : {p['win_rate_pct']}% ({p['winning_trades']}胜 / {p['losing_trades']}负, 共{p['total_trades']}单)")
    print(f" Sharpe / Sortino     : {p['sharpe_ratio']} / {p['sortino_ratio']}")
    print(f" Max Drawdown         : {p['max_drawdown_pct']}% | Calmar: {p['calmar_ratio']}")
    print(f" Gatekeeper Blocked   : {p['gatekeeper_filtered_count']} 次物理过滤 (Fail-Closed防割肉)")
    print("==========================================================================\n")


if __name__ == "__main__":
    main()
