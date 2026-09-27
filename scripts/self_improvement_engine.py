#!/usr/bin/env python3
"""
R20 AI LLM-Native Self-Improvement & Strategy Evolution Engine v6.8.1 (self_improvement_engine.py)
Focuses purely on Crypto Alpha generation & dynamic quantitative risk adaptation.
Eliminates rigid cooldown bans in favor of dynamic volatility-adjusted thresholds,
asymmetric Kelly bet-sizing, and LLM cognitive post-mortem lessons.
"""

import os
import sys
import json
import time
import datetime
import urllib.request
import tempfile
import fcntl
import hashlib
from typing import Dict, Any, List, Optional, Tuple

from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = Path(PROJECT_ROOT)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from r20_backend.config import settings as standalone_settings
except ImportError:
    standalone_settings = None

WORKSPACE_DIR = PROJECT_ROOT
DATA_DIR = os.path.join(WORKSPACE_DIR, "data")
LOGS_DIR = os.path.join(WORKSPACE_DIR, "logs")

LEDGER_JSON_FILE = os.path.join(DATA_DIR, "trading_ledger.json")
REPORT_JSON_FILE = os.path.join(DATA_DIR, "self_improvement_report.json")
AI_DECISIONS_FILE = os.path.join(DATA_DIR, "ai_brain_decisions.json")
AI_MEMORY_FILE = os.path.join(DATA_DIR, "ai_trading_memory.json")
AI_MEMORY_MD_FILE = os.path.join(DATA_DIR, "AI_TRADING_MEMORY.md")
EVOLUTION_LAST_PROMPT_FILE = os.path.join(DATA_DIR, "self_improvement_last_prompt.txt")
LOG_FILE = os.path.join(LOGS_DIR, "self_improvement.log")
EVOLUTION_LOCK_FILE = os.path.join(DATA_DIR, ".self_improvement.lock")

from r20_backend.time_utils import parse_beijing
from r20_backend.version import __version__
from instrument_pool import load_instruments
from prompt_library import active_profile, apply_module_layout
from r20_gateway.telemetry import ModelCallTelemetry

# 结构优化阶段 4·B3 第四十二刀：数理快照可观测性聚簇外提到 scripts/evolution/observability.py。
# 这里**再导出**（不是搬空）——外部 `from scripts.self_improvement_engine import
# EVOLUTION_SYSTEM_PROMPT` 式的引用与既有测试都按门面名解析，故门面必须继续提供。
# ⚠️ 注意：`SNAPSHOT_MAX_STALE_SECONDS` / `SIDE_ALIASES` **留在本文件** ——
# 它们属于 join 侧（`_match_snapshot`），不属于可观测性判定。
from scripts.evolution.memory_review import apply_memory_review, evaluate_rule_proposals
from scripts.evolution.review_context import (
    build_host_constitution,
    build_host_constitution_v2,
    independent_sample_groups,
    normalize_asset_multiplier_proposals,
    normalize_asset_multipliers,
    parse_review_contract,
    parse_review_json,
    summarize_closed_trades,
    summarize_evidence_stats,
)
from scripts.evolution.report import build_evolution_report, build_evolution_report_v2
from scripts.evolution.observability import (  # noqa: E402,F401
    DYNAMICS_FIELDS,
    DYNAMICS_OBSERVED_MIN,
    _parse_bj,
    audit_snapshot_observability,
    audit_snapshot_sources,
    classify_snapshot_observability,
    classify_strategy_snapshot,
    detect_reused_snapshots,
    prune_snapshot,
    render_observability_brief,
    strategy_evidence_status,
)
from llm_credentials import get_cpa_client_config as _get_cpa_client_config  # noqa: E402
try:  # 稳定 reason code（规划文档 §8.3）
    from scripts.evolution.reasons import (ASSET_MULTIPLIER_APPLIED, ASSET_MULTIPLIER_EXPIRED,
                                           ASSET_MULTIPLIER_INVALID, MEMORY_BASELINE_MISMATCH,
                                           MEMORY_PUBLISH_REJECTED, RULE_PROPOSAL_OBSERVATION_ONLY,
                                           RULE_PROPOSAL_REQUIRES_APPROVAL, SNAPSHOT_REUSED,
                                           SNAPSHOT_TIME_UNVERIFIED)
except ImportError:  # pragma: no cover
    from evolution.reasons import (ASSET_MULTIPLIER_APPLIED, ASSET_MULTIPLIER_EXPIRED,
                                   ASSET_MULTIPLIER_INVALID, MEMORY_BASELINE_MISMATCH,
                                   MEMORY_PUBLISH_REJECTED, RULE_PROPOSAL_OBSERVATION_ONLY,
                                   RULE_PROPOSAL_REQUIRES_APPROVAL, SNAPSHOT_REUSED,
                                   SNAPSHOT_TIME_UNVERIFIED)

from r20_backend.math_utils import clamp as _clamp
TARGET_INSTRUMENTS = [item["name"] for item in load_instruments()]

#: 资产乘数有效期（规划文档 §5.6：7 天 TTL 后自动回到 1.0）。
ASSET_MULTIPLIER_TTL_DAYS = 7
#: 证据策略版本（进策略快照/报告/资产乘数写盘）。
EVIDENCE_POLICY_VERSION = "2"

def atomic_write_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".evolution-", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def clamp(value, lower, upper, default):
    """把 value 夹到 [lower, upper]；不可比较时返回 default。

    结构优化阶段 4·B3 第五十一刀：本函数与 ``scripts/trader/signals.py`` 的同名函数原为逐字重复，
    已收敛到 `r20_backend.math_utils.clamp`。

    ⚠️ 名字保留在本模块：调用点按全局名查找，且 `patch.object(模块, "clamp")`
    是既有接缝（别名赋值会让它失效）。
    """
    return _clamp(value, lower, upper, default)


def single_evolution_cycle(func):
    def wrapped(*args, **kwargs):
        lock_handle = open(EVOLUTION_LOCK_FILE, "a+", encoding="utf-8")
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_handle.close()
            log_msg("Self-evolution skipped: another cycle is still running")
            return None
        try:
            return func(*args, **kwargs)
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()
    return wrapped


def _log_file() -> str:
    """调用时解析（审计卫生）：测试未 patch LOG_FILE 时（如 evolution_fallback_model
    的异常路径）不再污染生产 logs/self_improvement.log。R20_SELF_IMPROVEMENT_LOG
    覆盖 + tests/__init__.py 统一隔离；生产默认不变。"""
    return os.environ.get("R20_SELF_IMPROVEMENT_LOG") or LOG_FILE


def log_msg(msg: str):
    tz_bj = datetime.timezone(datetime.timedelta(hours=8))
    timestamp = datetime.datetime.now(tz_bj).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    try:
        target = _log_file()
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def get_cpa_client_config() -> Tuple[str, str]:
    """薄壳：调用时解析门面全局，使测试的 patch / 直接赋值生效。

    实现已迁往 r20_backend.llm.credentials（结构优化阶段 4·B3 第四十六刀）。
    ⚠️ `standalone_settings` 必须**在这里**读取后传入 —— 门面全局会被测试
    patch / 原地 reload，子模块 import 期绑定会读到陈旧副本。
    """
    return _get_cpa_client_config(standalone_settings)

# =============================================================================
# 数理快照可观测性（宿主确定性审计，2026-09-10）
# 事故链：build_signal_snapshot 旧版 schema 错配（09-09 已修复写入侧）导致历史
# journal 全部为「对象存在但 22/17 动力学字段 null」的空壳；宿主把空壳原样喂给
# 模型，模型只能自数 null，既易漂移，也给「倒推伪造」留了口子。从此由宿主逐单
# 判定可观测性并把统计结论前置注入 Prompt；join 侧同时禁止用未来或过期快照
# 回填因果证据。
# =============================================================================
SNAPSHOT_MAX_STALE_SECONDS = 6 * 3600
#: 限价单撮合成交后、在下一个 15 分钟巡检周期首次被 trackers 建档捕获的最大时差（20分钟）
SNAPSHOT_MAX_POST_FILL_LAG_SECONDS = 1200
SIDE_ALIASES = {"多": "long", "空": "short", "long": "long", "short": "short"}


def evolution_fallback_model() -> Optional[str]:
    """复盘专属回退模型：主模型网关故障时，按后台模型池顺序选下一个候选。

    同网关优先（2026-09-10）：模型池可能横跨多域名（tokenrhythm/cpa 混布），
    死域上的席位（如 cpa 的 gemini）回退过去也是 400/504，故先选与激活模型
    同 base_url 的健康池成员，其次才考虑异域名候选。

    刻意只作用于自进化复盘调用——交易主脑的选模与回退链是风险行为，
    调整需用户批准（fallback_model_ids 属全局配置，本函数绝不改写）。
    """
    try:
        from r20_backend.llm_manager import init_llm_config
        cfg = init_llm_config() or {}
        active = str(cfg.get("active_model_id") or "").strip()
        models = [m for m in (cfg.get("models") or []) if isinstance(m, dict)]
        active_base = ""
        for m in models:
            if str(m.get("id") or "").strip() == active:
                active_base = str(m.get("base_url") or "").strip()
                break
        same_gw, other_gw = [], []
        for m in models:
            mid = str(m.get("id") or "").strip()
            if not mid or mid == active:
                continue
            (same_gw if str(m.get("base_url") or "").strip() == active_base else other_gw).append(mid)
        for mid in same_gw + other_gw:
            return mid
    except Exception as exc:
        log_msg(f"复盘回退模型解析失败: {exc}")
    return None


def load_signal_journal():
    """读取开仓时刻的数理快照日志，按标的分组，供平仓台账 join 真实因果证据。"""
    journal_file = os.path.join(DATA_DIR, "signal_journal.json")
    by_inst = {}
    if not os.path.exists(journal_file):
        return by_inst
    try:
        with open(journal_file, "r", encoding="utf-8") as f:
            for rec in json.load(f):
                inst = str(rec.get("name") or rec.get("inst") or "")
                if inst:
                    by_inst.setdefault(inst, []).append(rec)
    except Exception as e:
        log_msg(f"读取 signal_journal 异常: {e}")
    return by_inst


def _match_snapshot(journal_by_inst, inst, open_time, side=None):
    """按方向与开仓时间就近匹配开仓时刻快照。

    因果铁律与巡检容差（2026-09-18）：
    1. 方向必须一致——台账方向可解析且 journal 记录带方向时，不一致者跳过，
       防止把空头开仓快照当多头成因；
    2. 允许 15 分钟巡检时差——限价单挂单撮合成交后，持仓在下一个 15 分钟巡检周期
       首次被 trackers 建档并写入快照（entryTime 略晚于 open_time 几分钟至 15 分钟），
       允许 [open_dt - 6h, open_dt + 20min] 的合理首巡检窗口，杜绝误杀真实开仓快照；
    3. 禁止远期未来快照——开仓 20 分钟之后的快照绝非开仓因果现场，一律返回 None；
    4. 禁止过期证据——快照早于开仓超过 SNAPSHOT_MAX_STALE_SECONDS 即非本次开仓
       的因果现场，弃用。

    实现已收敛到 `_match_snapshot_record`（同一套铁律），本函数只回传 snapshot 本体
    以保持历史契约。
    """
    record, _delta = _match_snapshot_record(journal_by_inst, inst, open_time, side)
    return (record or {}).get("snapshot")


def _match_snapshot_record(journal_by_inst, inst, open_time, side=None):
    """与 `_match_snapshot` 同一套 join 铁律，但**连记录一起返回**。

    记录里的 `entryTime` / `side` 是宿主可验证的时间证据，分类器需要它们；
    旧 `_match_snapshot` 只回传 snapshot 本体（既有测试按其契约断言）。
    返回 `(record, delta_seconds)`；无匹配时 `({}, None)`。
    """
    candidates = journal_by_inst.get(inst) or []
    open_dt = _parse_bj(open_time)
    if not candidates or open_dt is None:
        return {}, None
    wanted_side = SIDE_ALIASES.get(str(side or "").strip())
    best_diff, best_rec = None, None
    for rec in candidates:
        rec_side = SIDE_ALIASES.get(str(rec.get("side") or "").strip())
        if wanted_side and rec_side and rec_side != wanted_side:
            continue
        rec_dt = _parse_bj(rec.get("entryTime"))
        if rec_dt is None:
            continue
        delta_sec = (rec_dt - open_dt).total_seconds()
        if delta_sec < -SNAPSHOT_MAX_STALE_SECONDS or delta_sec > SNAPSHOT_MAX_POST_FILL_LAG_SECONDS:
            continue
        abs_diff = abs(delta_sec)
        if best_diff is None or abs_diff < best_diff:
            best_diff, best_rec = abs_diff, rec
    if best_rec is None:
        return {}, None
    return best_rec, (best_diff if best_diff is None else
                      (_parse_bj(best_rec.get("entryTime")) - open_dt).total_seconds())


def _untimed_calculus_fallback(inst, t):
    """全局 `calculus_snapshot.json` 最后兵：**无交易时间戳，不能当因果证据**（§2.2）。

    旧实现拿它建一个字段完整的 snapshot 并当成 DYNAMICS_OBSERVED/STRATEGY_*，
    等于用「当前指标」冒充「开仓时刻指标」。现在只作为普通观察返回，
    由 `_resolve_trade_evidence` 强制降到 PRICE_ONLY / NONE。
    """
    calc_file = os.path.join(DATA_DIR, "calculus_snapshot.json")
    if not os.path.exists(calc_file):
        return None
    try:
        with open(calc_file, "r", encoding="utf-8") as f_calc:
            calc_data = json.load(f_calc)
        for item in calc_data.get("instruments", []):
            if item.get("name") == inst or item.get("instId") in (inst, f"{inst}-USDT-SWAP"):
                from scripts.trader.signal_snapshot import build_signal_snapshot
                f_mock = {
                    "name": inst,
                    "instId": f"{inst}-USDT-SWAP",
                    "price": float(t.get("open_px") or t.get("close_px") or 0.0),
                    "atr": 0.0,
                    "calculus": item.get("calculus", {}),
                }
                return build_signal_snapshot(f_mock, data_dir=DATA_DIR)
    except Exception:
        return None
    return None


def _resolve_trade_evidence(t, inst, raw_side, journal_by_inst):
    """逐单解析证据：来源、时间证明、策略版本、可观测性等级（规划文档 §3.4/§3.5）。

    返回 `{view, stored, source, time_verified, delta, reason_codes, observability,
    strategy_complete}`：`view` 是**宿主补全上下文后的判定视图**（只用于分类/审计），
    `stored` 是 journal/台账里**实际记录的快照本体**（不把宿主注入当成新证据）。

    规则：

    - 台账自带 `signal_snapshot`（建仓时写入）⇒ `direct_signal_journal`，可信；
    - 否则按方向/时间就近 join journal ⇒ `matched_signal_journal`，可信，
      并记录时间差；
    - 否则全局 calculus fallback ⇒ `calculus_snapshot_fallback`，**无时间证明**，
      只能 PRICE_ONLY / NONE；
    - 都没有 ⇒ `unavailable`。
    """
    reason_codes: List[str] = []
    matched = None
    direct = t.get("signal_snapshot")
    if direct:
        source, snap, delta = "direct_signal_journal", direct, None
    else:
        record, delta = _match_snapshot_record(journal_by_inst, inst, t.get("open_time"), raw_side)
        snap = (record or {}).get("snapshot")
        if snap:
            source = "matched_signal_journal"
            matched = record
        else:
            snap = _untimed_calculus_fallback(inst, t)
            source = "calculus_snapshot_fallback" if snap else "unavailable"
            delta = None

    result = {"view": None, "stored": None, "source": source, "time_verified": False,
              "delta": delta, "reason_codes": reason_codes, "observability": "NONE",
              "strategy_complete": False}
    if not isinstance(snap, dict) or not snap:
        return result
    result["stored"] = dict(snap)

    # 宿主已知的上下文补进**视图**（不改写 journal 本体）：方向/时间/关联 ID
    view = dict(snap)
    view.setdefault("instId", f"{inst}-USDT-SWAP")
    if raw_side and not view.get("side"):
        view["side"] = SIDE_ALIASES.get(str(raw_side).strip(), None)
    if matched and not view.get("signal_time"):
        view["signal_time"] = matched.get("entryTime")
    if t.get("open_time") and not view.get("open_time"):
        view["open_time"] = t.get("open_time")
    if not view.get("venue"):
        view["venue"] = str(t.get("venue") or "okx").strip().lower() or "okx"
    if not view.get("strategy_version"):
        view["strategy_version"] = str(t.get("strategy_rule_version") or "")

    tag = classify_strategy_snapshot(view)
    status = strategy_evidence_status(view)
    time_verified = bool(status["time_verified"])
    if source == "calculus_snapshot_fallback":
        # §2.2：无时间证明的 fallback 不得因字段完整而升级。
        tag = "PRICE_ONLY" if prune_snapshot(view) else "NONE"
        time_verified = False
        reason_codes.append(SNAPSHOT_TIME_UNVERIFIED)
    result.update({"view": view, "time_verified": time_verified, "observability": tag,
                   "strategy_complete": bool(status["complete"])})
    return result


def load_closed_trades(start_time_override: str | None = None):
    account_init_file = os.path.join(DATA_DIR, "account_initial_state.json")
    reset_time_str = "1970-01-01 00:00:00"
    evo_start_str = os.getenv("R20_EVOLUTION_START_TIME", "").strip()
    if os.path.exists(account_init_file):
        try:
            with open(account_init_file, "r", encoding="utf-8") as f:
                acc_init = json.load(f)
                reset_time_str = str(acc_init.get("reset_time") or "1970-01-01 00:00:00")
                if not evo_start_str:
                    evo_start_str = str(acc_init.get("evolution_start_time") or "").strip()
        except Exception:
            pass

    # 确定自进化复盘起始时间（过滤更早的人工历史交易，杜绝远古历史单污染自进化）：
    # 显式入参 > 环境变量 R20_EVOLUTION_START_TIME > account_initial_state.json evolution_start_time > reset_time > 默认 2026-09-01 00:00:00
    effective_start = (
        start_time_override
        or evo_start_str
        or (reset_time_str if reset_time_str > "2026-01-01 00:00:00" else "2026-09-01 00:00:00")
    ).strip()
    if len(effective_start) == 10:
        effective_start = f"{effective_start} 00:00:00"

    journal_by_inst = load_signal_journal()
    closed_trades = []
    if os.path.exists(LEDGER_JSON_FILE):
        try:
            with open(LEDGER_JSON_FILE, "r", encoding="utf-8") as f:
                t_list = json.load(f)
                for t in t_list:
                    if t.get("status") == "holding":
                        continue
                    
                    c_time = str(t.get("close_time") or t.get("time") or "")
                    o_time = str(t.get("open_time") or "")
                    # 时间过滤器：若平仓或开仓早于自进化起始时间，则不纳入复盘
                    check_time = c_time or o_time
                    if check_time and check_time < effective_start:
                        continue

                    inst = str(t.get("inst") or t.get("name") or "OTHER")
                    if inst not in TARGET_INSTRUMENTS:
                        continue
                    pnl = float(t.get("pnl", 0.0) or 0.0)
                    gross = float(t.get("gross_pnl", pnl) or pnl)
                    fee = abs(float(t.get("fee", 0.0) or 0.0))
                    strat = str(t.get("strategy") or "⚡ 趋势")
                    reason = str(t.get("exit_reason") or t.get("remark") or "")

                    # join 铁律：方向一致、非未来、非过期；宿主逐单标注可观测性
                    # （规划文档 Task 2：来源 + 时间证明 + 策略版本 + 证据等级）
                    raw_side = str(t.get("side") or t.get("direction") or "")
                    evidence = _resolve_trade_evidence(t, inst, raw_side, journal_by_inst)
                    snap = evidence["stored"]
                    closed_trades.append({
                        "inst": inst,
                        # 逐单交易所归属（2026-09-23）：此前不带 venue，复盘提示词里既无
                        # 分布统计、逐单 JSON 也看不出是哪个所——多所台账形同单所。
                        "venue": str(t.get("venue") or "okx").strip().lower() or "okx",
                        "side": raw_side,
                        "time": c_time,
                        "open_time": t.get("open_time", ""),
                        "strategy": strat,
                        "margin": t.get("margin", "--"),
                        "gross_pnl": round(gross, 2),
                        "fee": round(fee, 2),
                        "funding": round(float(t.get("funding") or 0.0), 4),
                        "slippage_estimate": t.get("slippage_estimate"),
                        "protection_failed": bool(t.get("protection_failed", False)),
                        "net_pnl": round(pnl, 2),
                        "exit_reason": reason,
                        "snapshot_observability": evidence["observability"],
                        "snapshot_source": evidence["source"],
                        "snapshot_time_verified": bool(evidence["time_verified"]),
                        "snapshot_time_delta_seconds": evidence["delta"],
                        "snapshot_reason_codes": list(evidence["reason_codes"]),
                        "snapshot_strategy_complete": bool(evidence["strategy_complete"]),
                        "strategy_version": str(t.get("strategy_rule_version")
                                              or (snap or {}).get("strategy_version") or ""),
                        "strategy_mode": str(t.get("strategy_mode")
                                             or (snap or {}).get("strategy_mode") or ""),
                        "policy_hash": str(t.get("policy_hash") or (snap or {}).get("policy_hash") or ""),
                        "signal_id": str(t.get("signal_id") or t.get("intent_id") or ""),
                        # 影子规则报告（§12.4 阶段 C）要按 15M 指标复算门禁；
                        # 指标只从**开仓快照**取，取不到就是 None（下游 fail-closed 如实报缺）。
                        "rsi_15m": (snap or {}).get("rsi_15m", (snap or {}).get("rsi")),
                        "jerk_15m": (snap or {}).get("jerk_15m", (snap or {}).get("jerk")),
                        "regime": (snap or {}).get("regime"),
                        "entry_snapshot": prune_snapshot(snap),
                    })
        except Exception as e:
            log_msg(f"读取交易台账异常: {e}")

    return closed_trades

EVOLUTION_SYSTEM_PROMPT = """你是 R20 Quantum Trader 的首席投资官，负责基于真实已平仓交易证据进行认知复盘。模型只输出严格 JSON；宿主程序负责北京时间戳与 Markdown 渲染。

【证据纪律】
1. 先报告**事实**（台账字段可直接读出），再报告**假设**（解释），最后才报告**规则提案**；三层不得混写。
2. 只允许根据输入台账中真实可见的字段归因；不得把盈亏结果倒推成未提供的微积分、定积分、概率、新闻或聪明钱事实。
3. 宿主已逐单标注 snapshot_observability / snapshot_source / snapshot_time_verified 并前置注入确定性审计（非模型推断）：仅 STRATEGY_OBSERVED / DYNAMICS_OBSERVED 可做对应链路的因果归因；STRATEGY_PARTIAL / PARTIAL 只允许引用 entry_snapshot 中实际非空的字段；PRICE_ONLY / NONE、以及未通过时间验证的 calculus_snapshot_fallback，一律按「数理快照不可观测」处理，严禁对 v/a/j/I、energy_integral、deviation_area_integral、延续/击穿概率、VaR/CVaR 作任何因果陈述或假设性归因，不得编造。
4. 失败与反例必须与正例一起报告：只报盈利样本的结论视为证据不足（NO_CHANGE / OBSERVATION_ONLY）。
5. 单一信号拆成多笔订单只能算**一个独立样本组**；同一标的同一时段的多笔同向单也不得当成多个独立样本。
6. 每条结论必须标明适用范围：策略模式、周期、方向、交易所与样本窗口（scope 字段）。
7. 不得使用「证明正期望」「完全由」「彻底隔绝」「锁死胜率」等绝对表述，除非它描述的是**代码行为事实**（例如「当前策略版本冻结了 0.8×ATR 保本移损」）。
8. 不得把软启发式写成硬规则，也不得把建议写成「当前硬规则已生效」。
9. 不得直接生成或修改 baseline（is_baseline 只能来自代码 manifest），不得修改杠杆、保证金、熔断、止损与持仓上限。
10. 证据不足时输出 NO_CHANGE 或把提案降级为 OBSERVATION_ONLY。

【三层输出契约】
- `facts`：可直接从台账读出的事实（带 evidence_ids / evidence_level=OBSERVED / sample_size / independent_sample_groups）；
- `hypotheses`：待验证解释（带 evidence_ids / counterexample_count / scope）；
- `rule_proposals`：希望引入的规则提案（带 rule_id / text / scope / requested_level / requires_approval / evidence_ids / counterexample_count / counterexamples_checked）。
  提案**不会**直接生效：宿主会按「≥2 个独立样本组 + ≥2 个独立时间窗口 + 反例检查 + 不得与硬规则冲突 + 必须声明 scope」逐条闸门，不合格者降级为待验证观察，触碰硬规则或声称基线失效者进入人工审批队列。

【记忆更新规则】
- ADD：多个独立样本支持新的可复用经验。
- REVISE：新证据明确限定旧经验的适用条件。
- INVALIDATE：充分反例证明旧经验失效（仍不得物理删除基线）。
- NO_CHANGE：证据不足、无新增交易或结论无法区分策略问题与随机性。
- `ai_long_term_memory` 仍需给出生效后的完整清单（含全部现有基准心法），宿主会把省略的基准原样补回并留痕。
- 输出 0~4 条结论即可；没有高质量新证据时宁可空数组，不得凑数。

必须输出严格 JSON 对象，不得输出 Markdown、代码围栏或额外解释。建议结构：
{
  "change_status": "NO_CHANGE" | "ADD" | "REVISE" | "INVALIDATE",
  "facts": [{"text": "...", "evidence_ids": ["trade:0"], "evidence_level": "OBSERVED", "sample_size": 12, "independent_sample_groups": 4}],
  "hypotheses": [{"text": "...", "evidence_ids": [], "counterexample_count": 3, "scope": {"strategy_modes": ["trend_following"], "timeframes": ["15m"]}}],
  "rule_proposals": [{"rule_id": "anti_extreme_chase", "text": "...", "scope": {"strategy_modes": ["trend_following"], "directions": ["long"]}, "requested_level": "REVIEWED_HEURISTIC", "requires_approval": false, "evidence_ids": [], "counterexample_count": 0, "counterexamples_checked": true}],
  "diagnosis_insights": ["0~4 条诊断（兼容字段，可与 facts 同源）"],
  "evolution_actions": ["0~4 条建议动作（兼容字段）"],
  "asset_multipliers": {"BTC": 1.0},
  "ai_long_term_memory": ["生效后的完整心法清单"],
  "memory_overwrites_reason": "说明证据支持何种变更；NO_CHANGE 时明确为何不覆盖旧记忆"
}
"""

def resolve_memory_update(change_status: str, proposed_memory: Any, existing_memory: List[str]) -> Tuple[str, List[str], bool]:
    """Normalize LLM memory change and preserve existing lessons when evidence is insufficient."""
    status = str(change_status or "NO_CHANGE").upper()
    if status not in {"NO_CHANGE", "ADD", "REVISE", "INVALIDATE"}:
        status = "NO_CHANGE"
    proposed = proposed_memory if isinstance(proposed_memory, list) else []
    # 心法条目同受模型 schema 漂移影响，入库前统一压平为字符串
    proposed = [s for s in (_coerce_display_str(x) for x in proposed) if s]
    preserve = status == "NO_CHANGE" or not proposed
    return status, list(existing_memory if preserve else proposed), preserve


def merge_memory_with_constitution(change_status: str, proposed_texts: List[str],
                                   existing_lessons: List[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    """基准心法宪法级保护（2026-09-10，落实「NO_CHANGE 全量保留」纪律的推广形态）。

    - ADD 为纯追加：现有全部条目保留 + 新增条目去重后置；
    - REVISE / INVALIDATE：模型可整理非基准战术层，但任何被省略的基准心法
      （is_baseline）由宿主原样补回——大模型复盘无权物理删除宪法级记忆，
      证伪基准必须走 diagnosis_insights → 人工/管理端复核通道；
    - 返回 (最终清单, 被强制补回的基准心法)。
    """
    def _text(lesson):
        return _coerce_display_str(lesson.get("rule_text") or "")

    enabled = [l for l in (existing_lessons or []) if isinstance(l, dict) and l.get("enabled")]
    existing_texts = [t for t in (_text(l) for l in enabled) if t]
    baseline_texts = [t for t in (_text(l) for l in enabled if l.get("is_baseline")) if t]

    final: List[str] = []
    for p in proposed_texts or []:
        t = _coerce_display_str(p)
        if t and t not in final:
            final.append(t)
    if change_status == "ADD":
        final = existing_texts + [t for t in final if t not in existing_texts]
    readded = [t for t in baseline_texts if t not in final]
    return final + readded, readded


def ledger_revision_of(closed_trades: List[Dict[str, Any]]) -> str:
    """台账 revision（与报告/提示词/资产乘数共用同一口径）。"""
    return hashlib.sha256(
        json.dumps(closed_trades, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def compose_evolution_prompts(closed_trades: List[Dict[str, Any]], existing_memory_md: str = "", timestamp_str: str = "",
                              baseline_consistency: Optional[Dict[str, Any]] = None,
                              rule_versions: Optional[Dict[str, Any]] = None) -> Tuple[str, str, str, Dict[str, int]]:
    """组装自进化 System/User 提示词，并前置注入宿主确定性数理快照可观测性审计。

    返回 (system, user, now_bj_str, snapshot_audit)。审计由宿主统计而非模型自数
    null，从结构上杜绝「表面有快照、实际全空值」诱发的倒推伪造。
    """
    tz_bj = datetime.timezone(datetime.timedelta(hours=8))
    now_bj_str = timestamp_str or datetime.datetime.now(tz_bj).strftime("%Y-%m-%d %H:%M:%S (北京时间)")

    (total, wins, losses, win_rate, total_net, total_fees, snapshot_audit, observability_brief) = summarize_closed_trades(
        audit_snapshot_observability=audit_snapshot_observability,
        closed_trades=closed_trades,
        render_observability_brief=render_observability_brief    )

    v_counts: Dict[str, int] = {}
    for t in closed_trades:
        v = str(t.get("venue") or "okx").upper()
        v_counts[v] = v_counts.get(v, 0) + 1
    venue_summary = ", ".join(f"{v}: {c}笔" for v, c in sorted(v_counts.items())) if v_counts else "无"

    # 宿主确定性证据统计（§4.2-3 / §12.2）：来源分布、独立样本组、策略版本、成本。
    ledger_revision = ledger_revision_of(closed_trades)
    try:
        evidence_stats = summarize_evidence_stats(
            closed_trades=closed_trades,
            audit_snapshot_sources=audit_snapshot_sources,
            detect_reused_snapshots=detect_reused_snapshots,
            baseline_consistency=baseline_consistency or {})
    except Exception as exc:  # 统计不可得不得阻断复盘（prompt 会明说缺失）
        log_msg(f"证据统计不可用: {exc}")
        evidence_stats = {"error": str(exc)[:160]}
    source_line = "；".join(f"{k}={v}" for k, v in (evidence_stats.get("snapshot_sources") or {}).items())
    version_line = "；".join(f"{k}={v}" for k, v in (evidence_stats.get("strategy_versions") or {}).items())
    cost_line = (f"毛盈亏 {evidence_stats.get('gross_pnl', '--')} / 净盈亏 {evidence_stats.get('net_pnl', '--')} / "
                 f"手续费 {evidence_stats.get('fees', '--')} / 资金费 {evidence_stats.get('funding', '--')} / "
                 f"滑点 {evidence_stats.get('slippage', '--')} / 保护失败 {evidence_stats.get('protection_failures', '--')} 次")
    version_block = rule_versions or {}
    evidence_block = (
        "【宿主证据统计（确定性，非模型推断）】:\n"
        f"- 台账 revision: {ledger_revision}\n"
        f"- 快照来源分布: {source_line or '无'}\n"
        f"- 独立样本组: {evidence_stats.get('independent_sample_groups', '--')} 组"
        f"（同一信号拆单只算一组）| 重复信号证据组: {len(evidence_stats.get('duplicated_signal_groups') or [])}\n"
        f"- 策略版本分布: {version_line or '无'}\n"
        f"- 成本与保护: {cost_line}\n"
        f"- 当前执行规则: {version_block.get('rule_set') or 'legacy@1'} / "
        f"rule_set_hash {version_block.get('rule_set_hash') or '--'} / baseline hash {version_block.get('baseline_hash') or '--'}\n"
    )

    memory_context = f"""======================= 【当前系统已有的历史长期记忆库】 =======================
{existing_memory_md.strip()}
""" if existing_memory_md.strip() else "当前长期记忆库为空 (系统初始冷启动状态)"

    prompt = f"""======================= 【当前认知复盘基准时间】 =======================
【复盘基准时间】: {now_bj_str}

{memory_context}

======================= 【R20 加密量化实盘战绩与历史交易台账】 =======================
{evidence_block}
【统计汇总】:
- 总平仓笔数: {total} 笔 (胜 {len(wins)} / 负 {len(losses)} | 胜率: {win_rate}%)
- 累计净盈亏: {total_net:+.2f} USDT | 累计手续费消耗: {total_fees:.2f} USDT
- 当前聚焦标的池: {TARGET_INSTRUMENTS}

【逐笔历史交易明细 (按时间排序)】:
{json.dumps(closed_trades, indent=2, ensure_ascii=False)}

【复盘与长期记忆进化任务】:
请按「事实 → 假设 → 提案」三层复盘，严格基于可观测台账证据。以宿主注入的「数理快照可观测性审计」为准：对 PRICE_ONLY / NONE 或 snapshot_time_verified=false 的交易不得输出任何数理因果，只能标注“数理快照不可观测”。单一信号拆成多笔订单只算一个独立样本组；反例必须与正例一起报告；每条结论必须带 scope。证据不足时使用 NO_CHANGE 或 OBSERVATION_ONLY，不得强行生成新规律。输出标准 JSON：
{{
  "change_status": "NO_CHANGE" | "ADD" | "REVISE" | "INVALIDATE",
  "facts": [
    {{"text": "可从台账直接读出的事实", "evidence_ids": ["trade:0"], "evidence_level": "OBSERVED", "sample_size": 0, "independent_sample_groups": 0}}
  ],
  "hypotheses": [
    {{"text": "待验证解释", "evidence_ids": [], "counterexample_count": 0, "scope": {{"strategy_modes": [], "timeframes": []}}}}
  ],
  "rule_proposals": [
    {{"rule_id": "...", "text": "建议规则文本", "scope": {{"strategy_modes": [], "directions": []}}, "requested_level": "REVIEWED_HEURISTIC", "requires_approval": false, "evidence_ids": [], "counterexample_count": 0, "counterexamples_checked": true}}
  ],
  "diagnosis_insights": [
    "0~4 条有台账字段支持的诊断（兼容字段）"
  ],
  "evolution_actions": [
    "0~4 条可执行改进；证据不足时只提出数据采集或观察建议"
  ],
  "ai_long_term_memory": [
    "生效后的完整心法清单：必须原样包含现有全部基准心法（宿主会把省略的基准补回并留痕），新增条目须有多个独立样本支持；不得覆盖任何硬风控"
  ],
  "asset_multipliers": {{"BTC": 1.0}},
  "memory_overwrites_reason": "说明证据支持何种变更；NO_CHANGE 时明确为何不覆盖旧记忆"
}}
"""

    profile = active_profile()
    runtime_context = {
        "decision_timestamp": now_bj_str, "timestamp": now_bj_str,
        "timestamp_beijing": now_bj_str,
        "trading_memory": existing_memory_md.strip(),
        "existing_memory_markdown": existing_memory_md.strip() or "当前长期记忆库为空 (系统初始冷启动状态)",
        "total": total, "wins": len(wins), "losses": len(losses), "win_rate": win_rate,
        "total_net": f"{total_net:+.2f}", "total_fees": f"{total_fees:.2f}",
        "target_instruments": ", ".join(TARGET_INSTRUMENTS),
        "closed_trades_json": json.dumps(closed_trades, indent=2, ensure_ascii=False),
        "active_instruments": ",".join(TARGET_INSTRUMENTS),
        "snapshot_observability_summary": observability_brief,
        "dynamics_observable_trades": snapshot_audit["math_observable"],
        "unobservable_trades": snapshot_audit["PRICE_ONLY"] + snapshot_audit["NONE"],
        "profile_name": profile.get("name", ""), "timezone": "Asia/Shanghai",
        "strategy_version": os.getenv("R20_VERSION", f"v{__version__}"),
    }
    effective_evolution_system = apply_module_layout(EVOLUTION_SYSTEM_PROMPT, profile, "evolution_system", f"{profile.get('name', '稳健')}自进化系统提示词模板", context=runtime_context)
    effective_evolution_user = apply_module_layout(prompt, profile, "evolution_user", f"{profile.get('name', '稳健')}自进化用户提示词模板", context=runtime_context)
    # 宿主宪章：代码层硬约束，在风格档案 layout 之后强制追加——profile 只能调整
    # 措辞风格，永远无法删改证据纪律与基准心法保护（Code is Law，2026-09-10）。
    host_constitution = build_host_constitution(
        observability_brief=observability_brief    )
    # 交易所分布属**宿主确定性证据**，与宪章同源，故在 layout 之后追加：
    # prompt_library 的 evolution_user 档案会整段替换内置模板（实测该档案里没有
    # 「跨交易所分布」这句 → 2026-09-23 复盘提示词 grep 计数 0），只有宿主追加
    # 才能保证它不被档案措辞改写或静默丢掉。
    host_constitution = host_constitution + (
        f"5. 交易所分布（宿主确定性统计，非模型推断）：{venue_summary}。\n"
    )
    # 规划文档 §4.4-2 / §6.1：v1 四条硬约束之后续写「无时间证明不得作因果证据、
    # 拆单不算独立样本、资产乘数不是硬风控、复盘不能直接改交易参数、baseline 异常
    # 只出报告」五条。续篇单独成函数，v1 逐字不动（AST 对拍门）。
    host_constitution = host_constitution + build_host_constitution_v2(
        observability_brief=observability_brief, evidence_stats=evidence_stats,
        baseline_consistency=baseline_consistency, rule_versions=rule_versions)
    effective_evolution_system = effective_evolution_system.rstrip() + host_constitution
    effective_evolution_user = effective_evolution_user.rstrip() + host_constitution
    return effective_evolution_system, effective_evolution_user, now_bj_str, snapshot_audit


def call_llm_evolution_review(closed_trades: List[Dict[str, Any]], existing_memory_md: str = "", timestamp_str: str = "",
                              model_override: Optional[str] = None,
                              baseline_consistency: Optional[Dict[str, Any]] = None,
                              rule_versions: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base_url, api_key = get_cpa_client_config()
    if not api_key:
        log_msg("[AI Evolution] Error: CPA API Key not found, using fallback heuristics.")
        return {}

    effective_evolution_system, effective_evolution_user, now_bj_str, _audit = compose_evolution_prompts(
        closed_trades, existing_memory_md=existing_memory_md, timestamp_str=timestamp_str,
        baseline_consistency=baseline_consistency, rule_versions=rule_versions)
    try:
        snapshot = f"【SYSTEM PROMPT】:\n{effective_evolution_system.strip()}\n\n{'='*70}\n【USER PROMPT ({now_bj_str})】：\n{effective_evolution_user.strip()}"
        fd, temp_path = tempfile.mkstemp(prefix=".evolution-prompt-", suffix=".tmp", dir=DATA_DIR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(snapshot)
        os.replace(temp_path, EVOLUTION_LAST_PROMPT_FILE)
    except OSError:
        pass

    model_name = os.environ.get("LLM_MODEL") or ""
    effort = os.environ.get("LLM_REASONING_EFFORT") or "high"
    api_format = "openai_chat"
    try:
        from r20_backend.llm_manager import get_active_llm_runtime, execute_llm_request
        active_llm = get_active_llm_runtime()
        model_name = os.environ.get("LLM_MODEL") or active_llm.get("model") or model_name
        effort = os.environ.get("LLM_REASONING_EFFORT") or active_llm.get("reasoning_effort") or effort
        api_format = active_llm.get("api_format", "openai_chat")
        base_url = active_llm.get("base_url") or base_url
        api_key = active_llm.get("api_key") or api_key
        thinking_timeout = max(90.0, float(active_llm.get("thinking_timeout") or os.environ.get("LLM_THINKING_TIMEOUT", 120.0)))
    except Exception:
        execute_llm_request = None
        thinking_timeout = max(90.0, float(os.environ.get("LLM_THINKING_TIMEOUT", os.environ.get("LLM_TIMEOUT_SECONDS", 120.0))))
    if model_override:
        # 复盘专属回退模型：优先于 env 与主脑激活位（见 evolution_fallback_model）
        model_name = str(model_override)

    telemetry = ModelCallTelemetry(
        "self_improvement", model_name, str(effort), effective_evolution_system, effective_evolution_user
    )
    try:
        t0 = time.time()
        log_msg(f"🚀 正在调用 {model_name} ({api_format} / 思考上限 {thinking_timeout:.0f}s) 进行 AI 大脑深度认知复盘与策略参数优化...")
        raw_res = None
        content = ""
        if execute_llm_request:
            content, _, usage_dict, _ = execute_llm_request(
                messages=[
                    {"role": "system", "content": effective_evolution_system},
                    {"role": "user", "content": effective_evolution_user}
                ],
                model=model_name,
                base_url=base_url,
                api_key=api_key,
                api_format=api_format,
                reasoning_effort=effort,
                temperature=0.2,
                response_format={"type": "json_object"},
                timeout=thinking_timeout,
            )
            raw_res = {"usage": usage_dict} if isinstance(usage_dict, dict) else {}
        else:
            payload = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": effective_evolution_system},
                    {"role": "user", "content": effective_evolution_user}
                ],
                "temperature": 0.2,
                "response_format": {"type": "json_object"}
            }
            if effort not in ("none", "auto"):
                payload["reasoning_effort"] = effort
            req = urllib.request.Request(
                f"{base_url}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=thinking_timeout) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                content = res["choices"][0]["message"]["content"].strip()
                raw_res = res

        content = (content or "").strip()
        content, review_json = parse_review_json(content=content)
        telemetry.finish("success", raw_res, output_chars=len(content))
        log_msg(f"✅ AI 大脑认知复盘完成 (耗时 {round(time.time() - t0, 2)}s)")
        return review_json
    except Exception as e:
        telemetry.finish("failed", error=e)
        log_msg(f"Error in LLM evolution review: {e}")
        # Surface the upstream failure in the dashboard report instead of silently
        # degrading to an unexplained NO_CHANGE (which looks like a stale cache).
        return {"__llm_error__": f"{type(e).__name__}: {e}"}


_TEXTISH_KEYS = (
    "observation", "detail", "text", "action", "content", "analysis",
    "finding", "summary", "description", "reason", "evidence",
)


def _coerce_display_str(item) -> str:
    """把复盘数组项归一为展示字符串。

    - 字符串原样；若内容是自序列化的 JSON（模型常见漂移）则解包递归处理；
    - 对象：优先【dimension/title/category】+ 已知正文字段；action_type 类对象
      用其作标题；无已知键时按 key:value 拼接，绝不落回 str(dict)。
    """
    if isinstance(item, str):
        s = item.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                parsed = json.loads(s)
            except Exception:
                return s
            if isinstance(parsed, (dict, list)):
                return _coerce_display_str(parsed)
        return s
    if isinstance(item, dict):
        title = str(item.get("dimension") or item.get("title") or item.get("category")
                    or item.get("action_type") or "").strip()
        body = ""
        for k in _TEXTISH_KEYS:
            v = item.get(k)
            if isinstance(v, (str, int, float)) and str(v).strip():
                body = str(v).strip()
                break
        if not body:
            parts = [f"{k}:{v}" for k, v in item.items()
                     if not isinstance(v, (dict, list)) and str(v).strip() and k != "dimension"]
            body = "；".join(parts)
        if title and body and not body.startswith(f"【{title}】"):
            return f"【{title}】{body}"
        return body or title
    if isinstance(item, list):
        return "；".join(filter(None, (_coerce_display_str(x) for x in item)))
    return str(item).strip() if item is not None else ""


@single_evolution_cycle
def run_self_evolution(force: bool = False):
    tz_bj = datetime.timezone(datetime.timedelta(hours=8))
    now_bj = datetime.datetime.now(tz_bj)
    timestamp_str = now_bj.strftime("%Y-%m-%d %H:%M:%S")
    log_msg(f"🧬 启动 R20 AI 大脑自进化认知复盘与实战心法提炼 (v{__version__} Crypto Focus)...")

    closed_trades = load_closed_trades()
    total_trades = len(closed_trades)
    ledger_revision = hashlib.sha256(
        json.dumps(closed_trades, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    if not force and os.path.exists(REPORT_JSON_FILE):
        try:
            with open(REPORT_JSON_FILE, "r", encoding="utf-8") as f:
                previous_report = json.load(f)
            if previous_report.get("ledger_revision") == ledger_revision:
                log_msg("No new closed-trade evidence; keeping the current adaptive configuration")
                return previous_report
        except Exception:
            pass

    # 1. Base Stats
    win_trades = [t for t in closed_trades if t["net_pnl"] > 0]
    loss_trades = [t for t in closed_trades if t["net_pnl"] <= 0]
    win_count = len(win_trades)
    win_rate = round(win_count / total_trades * 100, 1) if total_trades > 0 else 0.0
    total_win_amt = sum(t["net_pnl"] for t in win_trades)
    total_loss_amt = abs(sum(t["net_pnl"] for t in loss_trades))
    total_fees_amt = sum(t["fee"] for t in closed_trades)
    profit_factor = round(total_win_amt / total_loss_amt, 2) if total_loss_amt > 0 else (99.0 if total_win_amt > 0 else 0.0)

    from scripts import evolution_shield as memory_service
    memory_snapshot, existing_memory_md, existing_core_lessons = memory_service.read_trading_context(
        AI_MEMORY_MD_FILE, AI_MEMORY_FILE)

    # 执行规则版本（代码事实）：进提示词宪章与报告，供模型知道“当前硬规则是什么”。
    try:
        from scripts.strategy_rules import active_execution_policy, rule_set_hash, strategy_rule_version
        _policy = active_execution_policy()
        rule_versions = {"rule_set": _policy["rule_set"],
                         "rule_set_hash": rule_set_hash(_policy["rule_set"]),
                         "strategy_rule_version": strategy_rule_version(_policy["rule_set"]),
                         "mode": _policy["mode"],
                         "baseline_hash": memory_service.baseline_manifest_hash()}
    except Exception as exc:
        log_msg(f"执行规则版本解析失败（按 unknown 注入）: {exc}")
        rule_versions = {"rule_set": "unknown", "baseline_hash": memory_service.baseline_manifest_hash()}

    # 基准一致性（§3.2）：发布前检查。
    #
    # 两种口径分开：`baseline_consistency` 是**权威现状**（面板/报告要看见 CRITICAL），
    # `baseline_publish_ok` 是**发布路径修复后**的结果 —— `publish_review` 会先把
    # 缺失/偏离的代码基准合并回来（§4.1-3），所以「权威缺基线」不会把首次自举锁死，
    # 真正的硬阻断留给修复后仍不一致的状态。
    baseline_consistency = memory_service.check_baseline_consistency(
        memory_snapshot.get("lessons") or [])
    _repaired_lessons, _merge_report = memory_service.merge_code_baselines(
        memory_snapshot.get("lessons") or [])
    baseline_after_merge = memory_service.check_baseline_consistency(_repaired_lessons)
    if not baseline_consistency["healthy"]:
        log_msg(f"🛠️ {MEMORY_BASELINE_MISMATCH}: 权威基准与代码 manifest 不一致 "
                f"missing={baseline_consistency['missing_ids']} "
                f"mismatched={baseline_consistency['mismatched_ids']} "
                f"unexpected={baseline_consistency['unexpected_ids']}；"
                f"发布路径将补回（repair_planned={bool(_merge_report['added_ids'] or _merge_report['repaired_ids'] or _merge_report['demoted_ids'])}）")
    if not baseline_after_merge["healthy"]:
        log_msg(f"🚨 {MEMORY_BASELINE_MISMATCH}: 合并代码基准后仍不一致，本周期停止发布，"
                "交易继续使用代码硬规则")
    evidence_stats = summarize_evidence_stats(
        closed_trades=closed_trades,
        audit_snapshot_sources=audit_snapshot_sources,
        detect_reused_snapshots=detect_reused_snapshots,
        baseline_consistency=baseline_consistency)

    # 宿主确定性数理快照可观测性审计（写进报告，结论不依赖模型自数 null）
    snapshot_audit = audit_snapshot_observability(closed_trades)
    constitution_readded: List[str] = []
    log_msg("🔬 数理快照可观测性审计: " + render_observability_brief(snapshot_audit))

    # 2. Call LLM for Cognitive Review & Memory Overwriting
    # 复盘预算守卫：调度器对子进程有 600s 硬超时，504 内部重试可达 ~560s。
    # 主调用超过 EVOLUTION_FALLBACK_BUDGET_SECONDS 后不再回退（回退大概率被腰斩，
    # 徒耗一池模型调用；本周期照常落 NO_CHANGE + 错误透传）。
    EVOLUTION_FALLBACK_BUDGET_SECONDS = 400.0
    cycle_t0 = time.time()
    llm_review = call_llm_evolution_review(closed_trades, existing_memory_md=existing_memory_md, timestamp_str=timestamp_str,
                                          baseline_consistency=baseline_consistency,
                                          rule_versions=rule_versions)
    if not isinstance(llm_review, dict):
        llm_review = {}
    # 复盘专属单次回退（2026-09-10）：qwen3.8-flash 网关 504 曾连续吞掉 09-09 与
    # 验证轮复盘；换池内下一个模型重试一次，交易主脑选模不受影响。
    if llm_review.get("__llm_error__"):
        fallback_model = evolution_fallback_model()
        elapsed = time.time() - cycle_t0
        if fallback_model and elapsed > EVOLUTION_FALLBACK_BUDGET_SECONDS:
            log_msg(f"⏳ 复盘主模型已耗时 {elapsed:.0f}s 超预算 {EVOLUTION_FALLBACK_BUDGET_SECONDS:.0f}s，"
                    f"放弃回退避免调度器 600s 腰斩（NO_CHANGE + 错误透传）")
        elif fallback_model:
            log_msg(f"⚠️ 复盘主模型失败（{str(llm_review['__llm_error__'])[:120]}），回退 {fallback_model} 重试一次")
            fb_review = call_llm_evolution_review(
                closed_trades, existing_memory_md=existing_memory_md,
                timestamp_str=timestamp_str, model_override=fallback_model)
            if isinstance(fb_review, dict) and fb_review and not fb_review.get("__llm_error__"):
                llm_review = fb_review
                log_msg(f"✅ 回退模型 {fallback_model} 复盘完成（仅本周期；不改全局激活位）")

    # ── 证据纪律与权限分层（规划文档 §3.2 / §4.2 / §4.5 / §6）───────────────
    llm_failed = bool(llm_review.get("__llm_error__"))
    review_contract = parse_review_contract(llm_review=llm_review)
    baseline_hash = memory_service.baseline_manifest_hash()
    if not baseline_consistency["healthy"]:
        log_msg(f"🚨 {MEMORY_BASELINE_MISMATCH}: 基准心法与代码 manifest 不一致 "
                f"missing={baseline_consistency['missing_ids']} "
                f"mismatched={baseline_consistency['mismatched_ids']} "
                f"unexpected={baseline_consistency['unexpected_ids']}；"
                "本周期停止发布，交易继续使用代码硬规则")
    if llm_failed:
        log_msg(f"🧯 LLM 复盘失败（{str(llm_review.get('__llm_error__'))[:120]}）："
                "只写失败报告，不生成任何新规则")
    publish_allowed = bool(baseline_after_merge["healthy"]) and not llm_failed
    policy_hash = str(memory_snapshot.get("version") or "")  # 兼容：真实 policy hash 来自策略快照
    try:
        from policy_snapshot import generate_policy_snapshot
        policy_hash = str(generate_policy_snapshot().get("policy_hash") or policy_hash)
    except Exception:
        pass

    change_status = review_contract["change_status"] if publish_allowed else "NO_CHANGE"
    insights, actions_taken = [], []
    facts = review_contract.get("facts") or []
    hypotheses = review_contract.get("hypotheses") or []
    for row in facts:
        text = _coerce_display_str(row)
        if text:
            insights.append(text)
    for row in hypotheses:
        text = _coerce_display_str(row)
        if text:
            actions_taken.append(text)
    # 兼容旧契约：模型仍可能只输出 diagnosis_insights / evolution_actions。
    legacy_insights = llm_review.get("diagnosis_insights", [])
    legacy_actions = llm_review.get("evolution_actions", [])
    if not isinstance(legacy_insights, list):
        legacy_insights = []
    if not isinstance(legacy_actions, list):
        legacy_actions = []
    # 模型 schema 漂移归一：部分模型把数组项输出为对象（{dimension, analysis} /
    # {action_type, action}）或自序列化 JSON 字符串；不归一则前端渲染成
    # [object Object] / 原始 JSON（2026-09-09 用户截图）。统一压平成展示字符串。
    insights += [s for s in (_coerce_display_str(x) for x in legacy_insights) if s]
    actions_taken += [s for s in (_coerce_display_str(x) for x in legacy_actions) if s]

    # 规则提案闸门（规划文档 §4.5）：模型只能提交提案，晋级为 L2 需要过独立性/反例/scope。
    sample_group_count, _sample_groups = independent_sample_groups(closed_trades)
    proposal_result = evaluate_rule_proposals(
        rule_proposals=review_contract.get("rule_proposals") or [],
        closed_trades=closed_trades,
        audit_structured_lesson=memory_service.audit_structured_lesson,
        independent_sample_groups=sample_group_count)
    if not publish_allowed:
        # 基线不健康 / LLM 失败：提案降级为观察，绝不标记为已生效（§3.2）。
        proposal_result = dict(proposal_result)
        proposal_result["observation_only"] = (list(proposal_result["observation_only"])
                                               + list(proposal_result["accepted"]))
        proposal_result["accepted"] = []
    for code in proposal_result["reason_codes"]:
        log_msg(f"🧾 {code}")
    if not publish_allowed:
        log_msg(f"🧾 {MEMORY_PUBLISH_REJECTED}: 本周期不发布记忆"
                f"（baseline_healthy={baseline_after_merge['healthy']} llm_failed={llm_failed}）")
    if evidence_stats.get("duplicated_signal_groups"):
        log_msg(f"🧾 {SNAPSHOT_REUSED}: {len(evidence_stats['duplicated_signal_groups'])} "
                "组重复信号快照（只作观察，不得当独立样本）")

    # 保留历史调用点（对拍门按名字解析参数）；生效值改用严格契约（§4.4-3 / §5.6）。
    legacy_asset_mults = normalize_asset_multipliers(
        TARGET_INSTRUMENTS=TARGET_INSTRUMENTS,
        clamp=clamp,
        llm_review=llm_review    )
    asset_multiplier_result = normalize_asset_multiplier_proposals(
        llm_review=llm_review, target_instruments=TARGET_INSTRUMENTS)
    asset_mults = dict(asset_multiplier_result["multipliers"])
    if not publish_allowed:
        asset_multiplier_result["status"] = "UNAVAILABLE"
        asset_mults = {p: 1.0 for p in TARGET_INSTRUMENTS}
    if asset_mults != legacy_asset_mults:
        log_msg(f"🛡️ 资产乘数严格契约：{len(asset_multiplier_result['rejected'])} 项被拒绝"
                "（超出 0.5~1.5 或非标的池资产）")

    proposal_texts = ([p.get("text") for p in proposal_result["accepted"]]
                      + [p.get("text") for p in proposal_result["observation_only"]])
    llm_memory_rows = llm_review.get("ai_long_term_memory", [])
    if not isinstance(llm_memory_rows, list):
        llm_memory_rows = []
    change_status, long_term_memory, preserve_existing_memory = resolve_memory_update(
        change_status, list(llm_memory_rows) + [t for t in proposal_texts if t],
        existing_core_lessons
    )

    retired_lessons: List[str] = []
    (constitution_readded, preserve_existing_memory, retired_lessons) = apply_memory_review(
        change_status=change_status,
        constitution_readded=constitution_readded,
        log_msg=log_msg,
        long_term_memory=long_term_memory,
        memory_service=memory_service,
        memory_snapshot=memory_snapshot,
        merge_memory_with_constitution=merge_memory_with_constitution,
        preserve_existing_memory=preserve_existing_memory,
        retired_lessons=retired_lessons,
        total_trades=total_trades    )

    # 提案元数据（evidence_level/scope/独立性/反例/审批）在发布后按文本回填；
    # 发布段被 `apply_memory_review` 的 AST 对拍门钉住，不能在那里加参数。
    if not preserve_existing_memory and proposal_result.get("metadata"):
        try:
            memory_service.annotate_lesson_metadata(
                proposal_result["metadata"],
                expected_version=memory_service.read_memory_snapshot()["version"])
        except Exception as exc:
            log_msg(f"提案元数据回填失败（保留既定权威）: {exc}")

    # 待审批队列（§4.5-6 / §9.3）：触碰硬规则或声称基线失效的提案只进人工队列，不自动生效。
    pending_proposals: List[Dict[str, Any]] = []
    if proposal_result["requires_approval"]:
        pending_proposals = [dict(p, created_at=timestamp_str,
                                  status="PENDING_APPROVAL",
                                  ledger_revision=ledger_revision)
                             for p in proposal_result["requires_approval"]]
        try:
            atomic_write_json(os.path.join(DATA_DIR, "rule_proposals_pending.json"),
                              {"generated_at": timestamp_str,
                               "source_ledger_revision": ledger_revision,
                               "ttl_days": ASSET_MULTIPLIER_TTL_DAYS,
                               "proposals": pending_proposals})
            log_msg(f"🧾 {RULE_PROPOSAL_REQUIRES_APPROVAL}: "
                    f"{len(pending_proposals)} 条提案等待人工审批（不自动生效）")
        except Exception as exc:
            log_msg(f"待审批提案写入失败: {exc}")

    # Keep the legacy markdown mirror in lock-step with the authority so the
    # public dashboard can never freeze on a hand-edited snapshot.
    mirror_synced: Optional[bool] = None
    try:
        mirror_synced = bool(memory_service.sync_markdown_mirror())
        if mirror_synced:
            log_msg("🪞 AI_TRADING_MEMORY.md 已同步至结构化心法权威库")
        else:
            log_msg("🪞 Markdown 镜像未生成（结构化权威保留不变）")
    except Exception as exc:
        mirror_synced = False
        log_msg(f"Markdown mirror sync skipped: {exc}")

    # Persist asset multipliers to data/asset_multipliers.json so brain trader can consume.
    # 写盘必须带来源/有效期/证据 revision/审批状态（规划文档 §4.2-5 / §5.6）。
    try:
        expires_at = (now_bj + datetime.timedelta(days=ASSET_MULTIPLIER_TTL_DAYS)).isoformat()
        mults_payload = {
            "timestamp": timestamp_str,
            "multipliers": asset_mults,
            "updated_by": "self_improvement_engine",
            "source": "self_improvement_review",
            "status": asset_multiplier_result["status"],
            "ttl_days": ASSET_MULTIPLIER_TTL_DAYS,
            "expires_at": expires_at,
            "source_ledger_revision": ledger_revision,
            "evidence_policy_version": EVIDENCE_POLICY_VERSION,
            "allowed_range": asset_multiplier_result["allowed_range"],
            "rejected": asset_multiplier_result["rejected"],
            "approval": {"required": asset_multiplier_result["status"] != "REVIEWED",
                         "status": ("NOT_REQUIRED" if asset_multiplier_result["status"] == "REVIEWED"
                                    else "PENDING_REVIEW"),
                         "approved_by": "", "approved_at": ""},
        }
        atomic_write_json(os.path.join(DATA_DIR, "asset_multipliers.json"), mults_payload)
    except Exception as exc:
        log_msg(f"Failed to persist asset multipliers: {exc}")

    # Reflect concurrent toggle/rollback even when the model returns NO_CHANGE.
    _, _, long_term_memory = memory_service.read_trading_context(AI_MEMORY_MD_FILE, AI_MEMORY_FILE)

    # 4. Save Dashboard Report
    report_payload = build_evolution_report(
        actions_taken=actions_taken,
        change_status=change_status,
        constitution_readded=constitution_readded,
        insights=insights,
        ledger_revision=ledger_revision,
        llm_review=llm_review,
        long_term_memory=long_term_memory,
        preserve_existing_memory=preserve_existing_memory,
        profit_factor=profit_factor,
        retired_lessons=retired_lessons,
        snapshot_audit=snapshot_audit,
        timestamp_str=timestamp_str,
        total_trades=total_trades,
        win_rate=win_rate    )

    # 报告 v2（规划文档 §4.2）：v1 的 18 键不动，只增不改。
    try:
        _memory_revision = memory_service.read_memory_snapshot()["version"]
    except Exception:
        _memory_revision = "missing"
    try:
        _input_hash = hashlib.sha256(Path(EVOLUTION_LAST_PROMPT_FILE).read_bytes()).hexdigest()
    except Exception:
        _input_hash = ""
    _output_hash = hashlib.sha256(
        json.dumps(llm_review, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
    report_payload = build_evolution_report_v2(
        report_payload=report_payload,
        ledger_revision=ledger_revision,
        memory_revision=_memory_revision,
        policy_hash=policy_hash,
        baseline_hash=baseline_hash,
        baseline_consistency=baseline_consistency,
        evidence_stats=evidence_stats,
        review_contract=review_contract,
        proposal_result=proposal_result,
        asset_multiplier_status=asset_multiplier_result["status"],
        review_input_hash=_input_hash,
        review_output_hash=_output_hash,
        pending_proposals=pending_proposals,
        llm_failed=llm_failed,
        mirror_synced=mirror_synced    )

    atomic_write_json(REPORT_JSON_FILE, report_payload)

    log_msg(f"🧬 自进化认知复盘完成 | 状态={change_status} | 当前保留 {len(long_term_memory)} 条启发式长期记忆")
    try:
        from qq_notifier import notify_evolution_report
        top_lesson = long_term_memory[0] if long_term_memory else "保持风控原则"
        notify_evolution_report(win_rate, total_trades, change_status, top_lesson)
    except Exception as e:
        log_msg(f"自进化通知发送失败: {e}")
    return report_payload

if __name__ == "__main__":
    force_run = "--force" in sys.argv or "-f" in sys.argv
    res = run_self_evolution(force=force_run)
    print(json.dumps(res, indent=2, ensure_ascii=False))
