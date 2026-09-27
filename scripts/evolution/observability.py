"""数理快照**可观测性**审计（结构优化阶段 4·B3 第四十二刀）。

原样搬自 `scripts/self_improvement_engine.py` 的 L128–196 聚簇（约 69 行）：

| 成员 | 作用 |
|---|---|
| `DYNAMICS_FIELDS` | **17** 个动力学链字段名 |
| `DYNAMICS_OBSERVED_MIN` | 视为"可观测"的最低非空字段数（88% 容差）= **15** |
| `_parse_bj` | 北京时间解析（去掉 tzinfo 后再 join） |
| `classify_snapshot_observability` | 逐单打标签 |
| `prune_snapshot` | 剔除 null 字段 |
| `audit_snapshot_observability` | 汇总统计 |
| `render_observability_brief` | 渲染成一行人类可读摘要 |

## 为什么值得单独成模块

这一簇是**宿主侧的确定性审计**，与自进化引擎的编排、LLM 调用、记忆合并
没有任何关系。抽出来后，"可观测性怎么判定"这件事才有一个唯一的落点。

## 背景（原注释保留）

事故链：`build_signal_snapshot` 旧版 schema 错配（09-09 已修复写入侧）导致历史
journal 全部为「对象存在但 22/17 动力学字段 null」的空壳；宿主把空壳原样喂给
模型，模型只能自数 null，既易漂移，也给「倒推伪造」留了口子。从此由宿主逐单
判定可观测性并把统计结论前置注入 Prompt；join 侧同时禁止用未来或过期快照
回填因果证据。

## ⚠️ `15` 这个数不是手写的，且原注释里的「22/17」是错的

`DYNAMICS_OBSERVED_MIN = max(1, int(len(DYNAMICS_FIELDS) * 0.85) + 1)`
—— 字段表实际 **17** 项：17 × 0.85 = 14.45 → `int` 截断为 14 → +1 = **15**。
它是**从字段表算出来的**，不是常数。改字段表会同步改门槛，
**勿**"顺手"换成字面量。

> 原文件里那段事故说明写的是「22/17 动力学字段 null」，把 22 写成了字段数。
> 实测字段表是 **17** 项（`DYNAMICS_OBSERVED_MIN` 因此是 15 而非 19）。
> 这是个**注释与代码不符**的小瑕疵，本刀**只修正注释、不动代码** ——
> 因为 22 从未参与任何计算。
"""

from __future__ import annotations

import datetime
import json
from typing import Dict, List, Optional

from r20_backend.time_utils import parse_beijing

__all__ = [
    "DYNAMICS_FIELDS",
    "DYNAMICS_OBSERVED_MIN",
    "STRATEGY_FIELDS",
    "SNAPSHOT_SOURCES",
    "TRUSTED_STRATEGY_SOURCES",
    "SNAPSHOT_EVIDENCE_POLICY_VERSION",
    "DUPLICATE_SIGNAL_MIN_OCCURRENCES",
    "classify_snapshot_observability",
    "classify_strategy_snapshot",
    "prune_snapshot",
    "audit_snapshot_observability",
    "audit_snapshot_sources",
    "detect_reused_snapshots",
    "render_observability_brief",
]

DYNAMICS_FIELDS = (
    "velocity", "acceleration", "jerk", "impulse", "curvature", "power",
    "power_regime", "regime", "dynamics_quality",
    "continuation_prob_pct", "breakdown_prob_pct", "var_95_pct", "cvar_95_pct",
    "prob_regime", "is_fat_tail", "energy_integral", "deviation_area_integral",
)
# 动力学链视为「可观测」的最低非空字段数（88% 容差：允许个别外部观测缺失）
DYNAMICS_OBSERVED_MIN = max(1, int(len(DYNAMICS_FIELDS) * 0.85) + 1)

#: 目标策略全链路归因所需的字段集合（规划文档 §4.3-1）。
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

#: 快照来源枚举（规划文档 §3.4）。
SNAPSHOT_SOURCES = (
    "direct_signal_journal",
    "matched_signal_journal",
    "legacy_signal_snapshot",
    "calculus_snapshot_fallback",
    "unavailable",
)

#: 只有这两个来源可以做完整策略因果归因。
TRUSTED_STRATEGY_SOURCES = ("direct_signal_journal", "matched_signal_journal")

#: 「快照是否声称携带策略链路」的判据：`regime` 与旧 DYNAMICS_FIELDS 重叠，
#: 单凭它不足以说明这是策略快照 —— 否则一份旧动力学快照会被误判成
#: STRATEGY_PARTIAL，丢掉 DYNAMICS_OBSERVED 这个正确标签（§3.5）。
_STRATEGY_EXCLUSIVE_FIELDS = tuple(f for f in STRATEGY_FIELDS if f not in DYNAMICS_FIELDS)

#: 证据策略版本（写进策略快照/报告，schema 变更时递增）。
SNAPSHOT_EVIDENCE_POLICY_VERSION = "2"

#: 同一标的、不同时间出现同一信号签名的次数达到该值 ⇒ 标记重复证据。
DUPLICATE_SIGNAL_MIN_OCCURRENCES = 3

#: 时间合法性容差。⚠️ `STRATEGY_MAX_AGE_SECONDS` 必须与门面 join 侧的 6 小时过期
#: 窗口同口径（该常量留在门面，属于 join 而不是可观测性判定），
#: `tests/llm/test_evolution_observability.py` 有一条专门断言两者相等，防止口径漂移。
STRATEGY_MAX_FUTURE_SKEW_SECONDS = 300
STRATEGY_MAX_AGE_SECONDS = 6 * 3600
STRATEGY_MAX_POST_FILL_LAG_SECONDS = 1200


#: 时间字段无法验证时禁止升级为「完整因果证据」的来源。
UNVERIFIED_SOURCES = ("calculus_snapshot_fallback", "unavailable", "")


def _parse_bj(ts) -> Optional[datetime.datetime]:
    dt = parse_beijing(ts)
    # Existing join callers use naive Beijing values; normalize BEFORE removing tz.
    return dt.replace(tzinfo=None) if dt else None


def _as_datetime(value) -> Optional[datetime.datetime]:
    """把 ISO / 北京时间字符串统一解析成**带时区**的 UTC 时刻（失败返回 None）。"""
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        parsed = parse_beijing(value)
        return parsed if parsed else None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def classify_snapshot_observability(snap) -> str:
    """逐单分类：DYNAMICS_OBSERVED / PARTIAL / PRICE_ONLY / NONE。

    只按 DYNAMICS_FIELDS 的真实非空计数；price/atr/adx/funding 等属于普通观测，
    不算动力学链。全 null 空壳不再是「有快照」，杜绝表面可观测、实际不可归因。
    """
    if not isinstance(snap, dict) or not snap:
        return "NONE"
    n = sum(1 for k in DYNAMICS_FIELDS if snap.get(k) is not None)
    if n == 0:
        return "PRICE_ONLY"
    if n >= DYNAMICS_OBSERVED_MIN:
        return "DYNAMICS_OBSERVED"
    return "PARTIAL"


def _strategy_field(snap: dict, name: str):
    """策略字段取值：`initial_stop` / `initial_risk_px` 允许住在 `risk` 子对象里。

    ⚠️ 空字符串不算「有值」—— 宿主回填的 `strategy_version=""` 绝不能被当成
    「策略版本存在」的证据（那正是 §3.5「字段存在≠字段可信」的反面教材）。
    """
    risk = snap.get("risk") if isinstance(snap.get("risk"), dict) else None
    for candidate in (snap.get(name), (risk or {}).get(name)):
        if candidate is None:
            continue
        if isinstance(candidate, str) and not candidate.strip():
            continue
        return candidate
    return None


def _strategy_time_status(snap: dict) -> Dict[str, object]:
    """时间可验证性：`(合法?, 原因, 时间差秒)`（规划文档 §2.2 / §3.5）。

    以下任一情况均判不合法：时间缺失/不可解析、未来时间超出容差、
    与开仓时间相差超窗口、`captured_at` 早于 `signal_time`。
    """
    captured = _as_datetime(snap.get("captured_at"))
    signal = _as_datetime(snap.get("signal_time"))
    open_time = _as_datetime(snap.get("open_time"))
    if captured is None or signal is None:
        return {"ok": False, "reason": "时间缺失或不可解析", "delta_seconds": None}
    if captured < signal:
        return {"ok": False, "reason": "captured_at 早于 signal_time", "delta_seconds": None}
    now = datetime.datetime.now(datetime.timezone.utc)
    if (captured - now).total_seconds() > STRATEGY_MAX_FUTURE_SKEW_SECONDS:
        return {"ok": False, "reason": "快照时间为未来", "delta_seconds": None}
    if (now - captured).total_seconds() > STRATEGY_MAX_AGE_SECONDS:
        return {"ok": False, "reason": "快照已过期", "delta_seconds": None}
    delta = None
    if open_time is not None:
        delta = (captured - open_time).total_seconds()
        if delta < -STRATEGY_MAX_AGE_SECONDS or delta > STRATEGY_MAX_POST_FILL_LAG_SECONDS:
            return {"ok": False, "reason": "快照与开仓时间差超出窗口",
                    "delta_seconds": delta}
    return {"ok": True, "reason": "", "delta_seconds": delta}


def strategy_evidence_status(snap) -> Dict[str, object]:
    """策略证据的**可信性**判定（字段存在 ≠ 字段可信）。

    返回 `{fields_present, complete, source_trusted, time_verified, version_present,
    reasons}`；`classify_strategy_snapshot` 与报告都从这里取数。
    """
    if not isinstance(snap, dict) or not snap:
        return {"fields_present": 0, "missing_fields": list(STRATEGY_FIELDS), "complete": False,
                "source_trusted": False, "time_verified": False, "version_present": False,
                "reasons": ["无快照"]}
    present = [f for f in STRATEGY_FIELDS if _strategy_field(snap, f) is not None]
    missing = [f for f in STRATEGY_FIELDS if _strategy_field(snap, f) is None]
    source = str(snap.get("snapshot_source") or "")
    source_trusted = source in TRUSTED_STRATEGY_SOURCES
    version_present = bool(str(snap.get("strategy_version") or "").strip())
    time_status = _strategy_time_status(snap)
    reasons: List[str] = []
    if missing:
        reasons.append("策略字段缺失: " + ", ".join(missing))
    if not source_trusted:
        reasons.append(f"快照来源不可信: {source or '未标注'}")
    if not version_present:
        reasons.append("策略版本缺失")
    if not time_status["ok"]:
        reasons.append(str(time_status["reason"]))
    complete = bool(present and not missing and source_trusted and version_present
                    and time_status["ok"])
    return {"fields_present": len(present), "missing_fields": missing, "complete": complete,
            "source_trusted": source_trusted, "time_verified": bool(time_status["ok"]),
            "time_delta_seconds": time_status["delta_seconds"],
            "version_present": version_present, "reasons": reasons, "source": source}


def classify_strategy_snapshot(snap) -> str:
    """策略专属可观测性分类（规划文档 §3.5）。

    顺序：STRATEGY_OBSERVED > STRATEGY_PARTIAL > DYNAMICS_* > PRICE_ONLY > NONE。
    只有「字段全、时间可验证、来源可信、版本存在」四项同时成立才算 STRATEGY_OBSERVED；
    否则**不得**因为字段看起来完整就升级（§2.2）。

    没有任何策略字段的快照回落到旧动力学分类（旧标签语义不变）。
    """
    if not isinstance(snap, dict) or not snap:
        return "NONE"
    strategy_declared = any(_strategy_field(snap, f) is not None
                            for f in _STRATEGY_EXCLUSIVE_FIELDS)
    if strategy_declared:
        status = strategy_evidence_status(snap)
        return "STRATEGY_OBSERVED" if status["complete"] else "STRATEGY_PARTIAL"
    return classify_snapshot_observability(snap)


def prune_snapshot(snap):
    """剔除值为 null 的字段；可观测性判定由 snapshot_observability 标签承载，
    不再让模型在 22 个 null 里自行数证据。"""
    if not isinstance(snap, dict):
        return None
    pruned = {k: v for k, v in snap.items() if v is not None}
    return pruned or None


def audit_snapshot_observability(closed_trades) -> Dict[str, int]:
    """汇总统计：旧动力学标签 + 策略专属标签（规划文档 §4.3-3）。

    旧大写键（DYNAMICS_OBSERVED / PARTIAL / PRICE_ONLY / NONE）语义不变，
    新增 `strategy_observed` / `strategy_partial` / `strategy_observable` 与
    小写别名（报告结构 §4.2 使用小写键）。
    """
    total = len(closed_trades)
    counts = {"DYNAMICS_OBSERVED": 0, "PARTIAL": 0, "PRICE_ONLY": 0, "NONE": 0,
              "STRATEGY_OBSERVED": 0, "STRATEGY_PARTIAL": 0}
    for t in closed_trades:
        tag = str(t.get("snapshot_observability") or "NONE")
        if tag == "DYNAMICS_PARTIAL":
            tag = "PARTIAL"
        counts[tag] = counts.get(tag, 0) + 1
    counts["total"] = total
    counts["math_observable"] = counts["DYNAMICS_OBSERVED"] + counts["PARTIAL"]
    counts["strategy_observed"] = counts["STRATEGY_OBSERVED"]
    counts["strategy_partial"] = counts["STRATEGY_PARTIAL"]
    counts["strategy_observable"] = counts["STRATEGY_OBSERVED"] + counts["STRATEGY_PARTIAL"]
    # 报告结构（§4.2）用小写键；保留旧大写键以兼容既有前端与测试。
    counts["dynamics_observed"] = counts["DYNAMICS_OBSERVED"]
    counts["dynamics_partial"] = counts["PARTIAL"]
    counts["price_only"] = counts["PRICE_ONLY"]
    counts["none"] = counts["NONE"]
    return counts


def audit_snapshot_sources(closed_trades) -> Dict[str, int]:
    """来源统计（规划文档 §4.3-4）：不只数字段，还要数「证据从哪来」。"""
    counts: Dict[str, int] = {name: 0 for name in SNAPSHOT_SOURCES}
    counts["unknown"] = 0
    for t in closed_trades:
        source = str((t or {}).get("snapshot_source") or "unavailable")
        if source not in counts:
            counts["unknown"] = counts.get("unknown", 0) + 1
            continue
        counts[source] += 1
    counts["total"] = len(closed_trades)
    counts["trusted"] = sum(counts[s] for s in TRUSTED_STRATEGY_SOURCES)
    counts["untrusted"] = counts["total"] - counts["trusted"]
    counts["time_unverified"] = len(
        [t for t in closed_trades if not (t or {}).get("snapshot_time_verified", False)])
    return counts


#: 重复签名只看**信号指标**，刻意排除价格/盈亏/时间/版本字段。
_SIGNATURE_FIELDS = tuple(DYNAMICS_FIELDS) + tuple(
    f for f in STRATEGY_FIELDS if f not in ("captured_at", "signal_time", "snapshot_source"))


def _signal_signature(snap) -> Optional[str]:
    if not isinstance(snap, dict):
        return None
    payload = {k: snap.get(k) for k in _SIGNATURE_FIELDS
               if snap.get(k) is not None and k not in ("strategy_version",)}
    if not payload:
        return None
    return json.dumps(payload, sort_keys=True, default=str)


def detect_reused_snapshots(closed_trades,
                            min_occurrences: int = DUPLICATE_SIGNAL_MIN_OCCURRENCES) -> List[Dict]:
    """重复快照检测（规划文档 §4.3-6）。

    「同一标的、**不同时间**、完全相同的信号指标」达到阈值 ⇒ 标记
    `DUPLICATED_SIGNAL_EVIDENCE`。时间必须不同 —— 同一时刻拆成多笔订单
    属于 §4.5 的「同信号拆单」（独立样本组问题），不是复用证据。
    """
    groups: Dict[tuple, Dict] = {}
    for idx, t in enumerate(closed_trades or []):
        trade = t if isinstance(t, dict) else {}
        snap = trade.get("entry_snapshot")
        signature = _signal_signature(snap)
        if not signature:
            continue
        inst = str(trade.get("inst") or "")
        open_time = str(trade.get("open_time") or trade.get("time") or "")
        key = (inst, signature)
        entry = groups.setdefault(key, {"inst": inst, "signature": signature,
                                        "trade_indexes": [], "times": set()})
        entry["trade_indexes"].append(idx)
        if open_time:
            entry["times"].add(open_time)
    flagged = []
    for entry in groups.values():
        if len(entry["times"]) >= 2 and len(entry["trade_indexes"]) >= min_occurrences:
            flagged.append({
                "inst": entry["inst"],
                "count": len(entry["trade_indexes"]),
                "distinct_times": len(entry["times"]),
                "trade_indexes": entry["trade_indexes"],
                "reason_code": "DUPLICATED_SIGNAL_EVIDENCE",
            })
    return sorted(flagged, key=lambda x: (-x["count"], x["inst"]))


def render_observability_brief(audit) -> str:
    return (
        f"已平仓 {audit['total']} 笔 | 开仓时刻数理快照：完全可观测 {audit['DYNAMICS_OBSERVED']} / "
        f"部分可观测 {audit['PARTIAL']} / 仅价格与普通观测 {audit['PRICE_ONLY']} / 无快照 {audit['NONE']}"
    )
