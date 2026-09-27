#!/usr/bin/env python3
"""
R20 Evolution Shield & Anti-Poisoning Cognitive Guardian (evolution_shield.py)
-------------------------------------------------------------------------------
Ensures AI Self-Evolution DOES NOT become a double-edged sword:
1. Anti-Single-Event Bias / Outlier Rejection:
   Single flash-crash or anomalous spikes cannot dictate long-term strategy.
2. Constitution Red-Lines (Non-negotiable Rules):
   - Prohibits "Never go Long" or "Never go Short" biases.
   - Prohibits widening stop losses to bag-hold losses.
   - Prohibits aggressive revenge betting or Martingale sizing.
3. Structured White-Box Lesson Schema:
   - id, category, rule_text, health_score, enabled, created_at, ttl_days, sample_size
4. Cognitive Decay & Health Score:
   - Lessons lose health score if they contradict recent positive performance.
   - Decayed lessons auto-archive, preventing cognitive poisoning.
"""

from __future__ import annotations

import datetime
import json
import math
import re
import copy
import fcntl
import hashlib
import os
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 稳定 reason code（规划文档 §8.3）。scripts/ 单独在 sys.path 上时需回退导入。
try:  # pragma: no cover - 取决于导入路径
    from scripts.evolution.reasons import MEMORY_BASELINE_MISMATCH
    from scripts.evolution.reasons import MEMORY_PUBLISH_REJECTED as _MEMORY_PUBLISH_REJECTED
    from scripts.evolution.reasons import SNAPSHOT_REUSED
    from scripts.evolution.reasons import SNAPSHOT_TIME_UNVERIFIED
    from scripts.evolution.reasons import (ASSET_MULTIPLIER_APPLIED, ASSET_MULTIPLIER_EXPIRED,
                                           ASSET_MULTIPLIER_INVALID, HARD_RULE_BLOCKED,
                                           RULE_PROPOSAL_OBSERVATION_ONLY,
                                           RULE_PROPOSAL_REQUIRES_APPROVAL)
except ImportError:  # pragma: no cover
    from evolution.reasons import MEMORY_BASELINE_MISMATCH
    from evolution.reasons import MEMORY_PUBLISH_REJECTED as _MEMORY_PUBLISH_REJECTED
    from evolution.reasons import SNAPSHOT_REUSED
    from evolution.reasons import SNAPSHOT_TIME_UNVERIFIED
    from evolution.reasons import (ASSET_MULTIPLIER_APPLIED, ASSET_MULTIPLIER_EXPIRED,
                                   ASSET_MULTIPLIER_INVALID, HARD_RULE_BLOCKED,
                                   RULE_PROPOSAL_OBSERVATION_ONLY,
                                   RULE_PROPOSAL_REQUIRES_APPROVAL)

__all__ = [
    "BASELINE_LESSONS", "EVIDENCE_LEVELS", "LESSON_STATUSES", "MAX_INJECTED_LESSONS",
    "MemoryBaselineError", "MemoryConflictError", "MemoryCorruptError",
    "MemoryVersionRequiredError", "add_safe_lesson", "admin_memory_view", "admin_mutate",
    "audit_proposed_lesson", "audit_structured_lesson", "baseline_disable_token",
    "baseline_manifest", "check_baseline_consistency", "injection_report",
    "is_lesson_expired", "load_structured_memory", "memory_inventory", "merge_code_baselines",
    "publish_review", "read_memory_snapshot", "render_lessons", "render_trading_memory",
    "render_trading_memory_layered",
    "rollback_to_baseline", "save_structured_memory", "select_injected_lessons",
    "sync_markdown_mirror", "toggle_lesson",
]

WORKSPACE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = WORKSPACE_DIR / "data"
STRUCTURED_MEMORY_FILE = DATA_DIR / "structured_trading_memory.json"
AI_MEMORY_MD_FILE = DATA_DIR / "AI_TRADING_MEMORY.md"

# 官方不可逾越的基准心法库 (Baseline Golden Lessons)
BASELINE_LESSONS = [
    {
        "id": "lesson_trend_pullback",
        "category": "TREND_FOLLOWING",
        "rule_text": "【顺势回踩低吸做多与反弹承压高抛做空】绝对禁止在 4H/1H 多头通道逆势摸顶开空，空单只在宏观空头反弹受阻时限价挂单；箱体震荡边界双向高抛低吸。",
        "health_score": 98.0,
        "enabled": True,
        "created_at": "2026-09-01 00:00:00",
        "ttl_days": 14,
        "sample_size": 42,
        "is_baseline": True,
        "scope": {"strategy_modes": ["trend_following"], "timeframes": ["4h", "1h"],
                  "directions": ["long", "short"]},
        "shield_status": "PASSED",
    },
    {
        "id": "lesson_wide_atr_stop",
        "category": "RISK_CONTROL",
        "rule_text": "【宽止损抗噪杜绝随意割肉】止损必须设在结构外 1.8x~2.2x 1H ATR 以外，给足波动呼吸空间，从物理上隔绝 15M/5M 杂波插针洗损。",
        "health_score": 95.0,
        "enabled": True,
        "created_at": "2026-09-01 00:00:00",
        "ttl_days": 14,
        "sample_size": 38,
        "is_baseline": True,
        "scope": {"timeframes": ["1h"], "directions": ["long", "short"]},
        "shield_status": "PASSED",
    },
    {
        "id": "lesson_breakeven_lock",
        "category": "WIN_RATE_LOCK",
        "rule_text": "【浮盈0.8R坚决保本锁死胜率】持仓浮盈达到 0.8R~1.0R 坚决执行保本移损 (UPDATE_SL)，将潜在亏损彻底消除为零风险平仓，锁死胜率下限，杜绝盈利变割肉。",
        "health_score": 99.0,
        "enabled": True,
        "created_at": "2026-09-01 00:00:00",
        "ttl_days": 14,
        "sample_size": 50,
        "is_baseline": True,
        "scope": {"timeframes": ["15m", "1h"], "directions": ["long", "short"]},
        "shield_status": "PASSED",
    },
    {
        "id": "lesson_anti_correlation_rush",
        "category": "PORTFOLIO_DIVERSIFICATION",
        "rule_text": "【严禁跨标的同向共振堆叠单边敞口】无论阻力高空还是顺势做多，严禁在多相关标的（BTC/ETH/SOL/DOGE）上同向无节制开仓，必须对总同向在手仓位施加硬性约束，防范系统性 Beta 踩踏。",
        "health_score": 92.0,
        "enabled": True,
        "created_at": "2026-09-04 12:00:00",
        "ttl_days": 7,
        "sample_size": 15,
        "is_baseline": True,
        "scope": {"directions": ["long", "short"], "venues": ["okx"]},
        "shield_status": "PASSED",
    },
]

# 宪法红线规则（任何大模型总结出的心法如果触碰以下词汇或逻辑，直接物理阻断）：
#: (正则, 拒绝原因, 禁止式豁免)。第三列为 True 时，"严禁放宽止损"这类**禁止式**表述
#: 属安全知识而非风险扩张；方向偏见两条不豁免——"永远不做多"本身就是毒（毁掉多空对称）。
POISON_PATTERNS = [
    (r"(永远不|绝对不|严禁|彻底禁止).*(做多|开多|买入)", "EXTREME_DIRECTIONAL_BIAS (极端做多偏见阻断)", False),
    (r"(永远不|绝对不|严禁|彻底禁止).*(做空|开空|卖出)", "EXTREME_DIRECTIONAL_BIAS (极端做空偏见阻断)", False),
    (r"(扩大|放宽|取消|不设|移除|关闭|删除).*(止损|SL)", "RISK_EXPANSION_VIOLATION (违规抗单放大止损)", True),
    (r"(加倍|翻倍|加仓|重仓|梭哈).*(亏损|抗单|摊平)", "MARTINGALE_POISONING (马丁格尔赌徒加仓倾向)", True),
    (r"(忽视|不看|废弃|无视|忽略|跳过|绕过|关闭|禁用|停用|移除|删除|突破).*(4H|宏观|ATR|风控|拦截器|熔断|门禁|刹车)",
     "GOVERNANCE_OVERRIDE (企图推翻硬风控拦截器)", True),
    # 审计 P1-8(2026-09-13)：子审计实跑证明旧规则对下面三类**全部 PASS**——
    #   「把单笔杠杆从 5x 提升至 20x」「把单标的累计保证金上限提高到 50%」
    #   「关闭风控拦截器以绕过 4H 方向否决」（第三类旧词表只认 忽视/不看/废弃）。
    # 这三条一旦写进记忆就会被注入主脑提示词，等于让模型自己拆掉硬风控。
    (r"(提高|提升|放大|增加|调高|拉高|加大|上调|放宽|解除).*(杠杆|leverage)",
     "RISK_EXPANSION_VIOLATION (抬升杠杆上限)", True),
    (r"(杠杆|leverage)[^。；;\n]{0,24}(提高|提升|放大|增加|调高|拉高|加大|上调|放宽|解除)",
     "RISK_EXPANSION_VIOLATION (抬升杠杆上限)", True),
    (r"(提高|提升|放大|增加|调高|拉高|加大|上调|放宽|解除|取消).*(保证金|仓位上限|持仓上限|风险预算|敞口|单笔上限|风险额度)",
     "RISK_BUDGET_EXPANSION (放宽保证金/持仓/预算上限)", True),
    (r"(保证金|仓位|持仓|风险预算|敞口|单笔)[^。；;\n]{0,24}(提高|提升|放大|增加|调高|拉高|加大|上调|放宽|解除)",
     "RISK_BUDGET_EXPANSION (放宽保证金/持仓/预算上限)", True),
    (r"(降低|下调|放宽|取消|豁免).*(置信度|门槛|阈值|标准|审查|复核|风控参数)",
     "RISK_THRESHOLD_LOWERING (下调风控阈值/审查标准)", True),
    (r"(满仓|全仓|梭哈|all[ -]?in)", "OVER_CONCENTRATION (单次满仓/全仓倾向)", True),
]

#: 变更类动词 × 风险名词的共现兜底（同句内出现即拒）：覆盖词表未枚举的改写变体。
_RISK_CHANGE_VERBS = r"(提高|提升|放大|增加|调高|拉高|加大|上调|放宽|解除|取消|关闭|禁用|停用|移除|删除|跳过|绕过|突破|降低|下调|豁免)"
_RISK_NOUNS = r"(杠杆|leverage|保证金|仓位|持仓上限|风险预算|敞口|止损|风控|熔断|拦截器|置信度|门槛|阈值|硬约束)"
#: 同句含这些"禁止类"词 = 在**禁止**风险扩张（合法），豁免共现兜底。
_RISK_PROHIBITIONS = r"(严禁|禁止|不得|绝不|杜绝|防止|防范|严禁将|不允许|拒绝)"


def _strip_prohibition_clauses(sentence: str) -> str:
    """剔除"禁止类"子句，保留其余文本（"严禁逆势加仓，但可放宽止损" → "但可放宽止损"）。"""
    clauses = [c for c in re.split(r"[，,]", sentence or "") if c.strip()]
    kept = [c for c in clauses if not re.search(_RISK_PROHIBITIONS, c)]
    return "，".join(kept)


def audit_proposed_lesson(rule_text: str, sample_size: int = 1) -> Tuple[bool, str]:
    """
    Applies the Constitution Linter to verify whether a proposed lesson is safe.
    Returns (is_passed, reason).
    """
    if not rule_text or len(rule_text.strip()) < 10:
        return False, "心法文本过短，缺乏明确可复用的交易情境依据"

    # 1. 宪法红线：逐句判定（跨句不误伤），禁止式子句按需剥离后再匹配。
    for sentence in re.split(r"[。；;\n]", rule_text):
        if not sentence.strip():
            continue
        exempt_text = _strip_prohibition_clauses(sentence)
        for pattern, reason, prohibitive_exempt in POISON_PATTERNS:
            target = exempt_text if prohibitive_exempt else sentence
            if target and re.search(pattern, target, re.IGNORECASE):
                return False, f"触发宪法红线拦截: {reason}"

    # 1b. 共现兜底：同一句里既有"变更类动词"又有"风险名词" → 一律拒（禁止式子句先剥离）。
    for sentence in re.split(r"[。；;\n]", rule_text):
        remainder = _strip_prohibition_clauses(sentence)
        if not remainder:
            continue
        if re.search(_RISK_CHANGE_VERBS, remainder) and re.search(_RISK_NOUNS, remainder, re.IGNORECASE):
            return False, "触发宪法红线拦截: RISK_PARAMETER_TAMPERING (疑似修改杠杆/保证金/风控阈值/拦截器)"

    # 2. Outlier / Single-Event Rejection Gate
    if sample_size < 2:
        return False, "样本量不足 (单笔偶发事件或极端插针噪点，拒绝写入长期心法)"

    return True, "PASSED"


class MemoryCorruptError(ValueError):
    """Invalid authority is never replaced implicitly."""


class MemoryVersionRequiredError(ValueError):
    """Administrative writes require the version from GET."""


class MemoryConflictError(ValueError):
    """The snapshot used to prepare an update is stale."""


class MemoryBaselineError(ValueError):
    """Code baselines are immutable through ordinary interfaces."""


#: 证据等级（规划文档 §3.1）。只允许这四种：模型永远不能自称基线。
EVIDENCE_LEVELS = (
    "BASELINE_HARD_RULE",
    "REVIEWED_HEURISTIC",
    "PROPOSED_HEURISTIC",
    "OBSERVATION_ONLY",
)

#: 条目状态（规划文档 §3.1）。
LESSON_STATUSES = ("ACTIVE", "DISABLED", "RETIRED", "REJECTED")

#: 「已验证结论」绝不能出现的绝对化表述（规划文档 §4.6 第一条）。
ABSOLUTE_CLAIM_PATTERNS = (
    r"(锁死|锁定的?)胜率",
    r"彻底(避免|隔绝|消除|杜绝)",
    r"(保证|证明)正期望",
    r"(稳赚|必胜|零风险|绝对盈利)",
)

#: 「把提案写成当前硬规则」的红线（规划文档 §4.6 第五条）。
CURRENT_RULE_CLAIM_PATTERNS = (
    r"当前(硬)?规则[^。；;\n]{0,8}(生效|执行|强制|落地)",
    r"现已(生效|强制执行)",
    r"本条(已|已经)写入(硬规则|策略配置)",
)


def _baseline_text_hash(text: Any) -> str:
    """基准文本的稳定 SHA-256 截断值（规划文档 §4.1「文本 hash 使用 SHA-256 截断值」）。"""
    return hashlib.sha256(str(text or "").strip().encode("utf-8")).hexdigest()[:16]


def baseline_manifest() -> Dict[str, Dict[str, Any]]:
    """把代码里的 `BASELINE_LESSONS` 转成稳定的 ID → 文本/hash 清单，按 ID 排序。

    这是**唯一**可以生产 `is_baseline=True` 的来源：模型的提案、后台的普通写入
    都不允许自己创造一个基线（规划文档 §4.6 末条）。
    """
    manifest: Dict[str, Dict[str, Any]] = {}
    for item in BASELINE_LESSONS:
        lesson_id = str(item["id"])
        manifest[lesson_id] = {
            "id": lesson_id,
            "category": str(item.get("category") or ""),
            "rule_text": str(item["rule_text"]),
            "text_hash": _baseline_text_hash(item["rule_text"]),
            "scope": copy.deepcopy(item.get("scope") or {}),
        }
    return dict(sorted(manifest.items()))


def baseline_manifest_hash() -> str:
    """代码基准 manifest 的整体 hash（进策略快照/报告，用于比对不同版本）。"""
    return hashlib.sha256(
        json.dumps(baseline_manifest(), sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]


def baseline_disable_token(lesson_id: str) -> str:
    """停用基准心法所需的**人工确认 token**（规划文档 §4.1-5）。

    不是密钥，而是「必须显式、刻意地做这件事」的凭证：后台要把这个值回填给
    `toggle_lesson(..., confirm_token=...)` 才允许把基线从提示词里摘掉。
    """
    digest = hashlib.sha256(f"R20-DISABLE-BASELINE:{lesson_id}".encode("utf-8")).hexdigest()
    return f"DISABLE-BASELINE-{digest[:16].upper()}"


def check_baseline_consistency(lessons: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """确定性基准一致性报告（规划文档 §3.2）。

    `healthy=False` 时：禁止自进化发布、禁止把提案标为已生效、交易继续使用代码硬规则。
    """
    manifest = baseline_manifest()
    authority: Dict[str, Dict[str, Any]] = {}
    for item in lessons or []:
        if isinstance(item, dict) and item.get("is_baseline"):
            lid = item.get("id")
            if isinstance(lid, str) and lid:
                authority[lid] = item

    missing_ids: List[str] = []
    mismatched_ids: List[str] = []
    disabled_ids: List[str] = []
    for lesson_id, entry in manifest.items():
        item = authority.get(lesson_id)
        if item is None:
            missing_ids.append(lesson_id)
            continue
        if _baseline_text_hash(item.get("rule_text")) != entry["text_hash"]:
            mismatched_ids.append(lesson_id)
        elif not item.get("enabled"):
            # 显式人工停用是合法状态（需 confirm token），单列披露但不冒充「不一致」。
            disabled_ids.append(lesson_id)
    unexpected_ids = sorted(lid for lid in authority if lid not in manifest)

    return {
        "code_baseline_count": len(manifest),
        "authority_baseline_count": len(authority),
        "missing_ids": missing_ids,
        "mismatched_ids": mismatched_ids,
        "unexpected_ids": unexpected_ids,
        "disabled_ids": disabled_ids,
        "healthy": not missing_ids and not mismatched_ids and not unexpected_ids,
        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def _baseline_record(entry: Dict[str, Any], now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """由代码 manifest 生成一条完整基线条目（新字段齐全，旧字段兼容）。

    ⚠️ 本函数必须**确定性**（同输入同输出，不使用 `now`）：`merge_code_baselines`
    的幂等性就靠它 —— 否则每次 `publish_review` 都会生成“内容等价但时间戳不同”
    的基线条目，候选永远 != 权威快照，发布不存在的变更。
    """
    source = next((i for i in BASELINE_LESSONS if i.get("id") == entry["id"]), {})
    stamp = str(source.get("created_at") or "2026-09-01 00:00:00")
    return {
        "id": entry["id"],
        "category": entry.get("category") or "BASELINE",
        "rule_text": entry["rule_text"],
        "enabled": True,
        "is_baseline": True,
        "status": "ACTIVE",
        "evidence_level": "BASELINE_HARD_RULE",
        "health_score": float(source.get("health_score", 100.0)),
        "created_at": stamp,
        "updated_at": stamp,
        "ttl_days": float(source.get("ttl_days", 3650)),
        "sample_size": int(source.get("sample_size", 0) or 0),
        "scope": copy.deepcopy(entry.get("scope") or {}),
        "shield_status": "PASSED",
        "baseline_source": "CODE_MANIFEST",
        "baseline_text_hash": entry["text_hash"],
    }


def merge_code_baselines(lessons: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """把代码基准合并进记忆清单（规划文档 §4.1-2/7 的同一个函数）。

    - 缺失的基线：按 manifest **补回**；
    - 文本偏离的基线：以代码文本为准**纠正**（代码是权威）；
    - 被停用的基线：保持停用（人工 token 的显式决定），只记录在 `disabled_ids`；
    - 不在 manifest 里的 `is_baseline=True`：**降级**为非基线（模型不得自造基线），
      留在原处并留痕，绝不静默删除。

    返回 `(合并后的清单, 变更报告)`；报告里的 `consistency` 是合并后的复查结果。
    """
    merged = [copy.deepcopy(item) for item in (lessons or []) if isinstance(item, dict)]
    manifest = baseline_manifest()
    by_id = {item.get("id"): item for item in merged if isinstance(item.get("id"), str)}

    added_ids: List[str] = []
    repaired_ids: List[str] = []
    demoted_ids: List[str] = []
    # 注意：**合并顺序按 BASELINE_LESSONS 的声明顺序**（回滚后清单与历史一致，
    # `baseline_manifest()` 自身仍按 ID 排序 —— 那是给一致性报告用的稳定清单）。
    ordered = [manifest[str(entry["id"])] for entry in BASELINE_LESSONS]
    for entry in ordered:
        lesson_id = entry["id"]
        item = by_id.get(lesson_id)
        if item is None:
            merged.append(_baseline_record(entry))
            added_ids.append(lesson_id)
            continue
        if _baseline_text_hash(item.get("rule_text")) != entry["text_hash"]:
            item["rule_text"] = entry["rule_text"]
            item["baseline_text_hash"] = entry["text_hash"]
            repaired_ids.append(lesson_id)
        item["is_baseline"] = True
        item.setdefault("evidence_level", "BASELINE_HARD_RULE")
        item.setdefault("status", "ACTIVE" if item.get("enabled") else "DISABLED")

    for item in merged:
        if item.get("is_baseline") and item.get("id") not in manifest:
            item["is_baseline"] = False
            item["baseline_demoted_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            item["baseline_demoted_reason"] = "不在代码 baseline manifest 中：模型/后台不得自造基线"
            demoted_ids.append(str(item.get("id")))

    report = {
        "added_ids": added_ids,
        "repaired_ids": repaired_ids,
        "demoted_ids": demoted_ids,
        "consistency": check_baseline_consistency(merged),
    }
    return merged, report


def select_injected_lessons(lessons: List[Dict[str, Any]], now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """统一的过期/启用/注入筛选（规划文档 §4.1-7）。

    `render_lessons`、`injection_report`、后台面板都从这一个函数取数，
    杜绝「页面显示生效中、提示词里其实没注入」的口径漂移。
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    rows = [item for item in (lessons or []) if isinstance(item, dict)]
    disabled = [i for i in rows if not i.get("enabled")]
    expired = [i for i in rows if i.get("enabled") and is_lesson_expired(i, now)]
    active = [i for i in rows if i.get("enabled") and not is_lesson_expired(i, now)]
    sorted_active = sorted(
        active, key=lambda x: (1 if x.get("is_baseline") else 0, x.get("created_at", "")), reverse=True)
    injected = sorted_active[:MAX_INJECTED_LESSONS]
    injected_ids = {id(i) for i in injected}

    def _brief(item):
        return {
            "id": item.get("id"),
            "category": item.get("category"),
            "evidence_level": item.get("evidence_level") or (
                "BASELINE_HARD_RULE" if item.get("is_baseline") else "REVIEWED_HEURISTIC"),
            "rule_text": str(item.get("rule_text") or "")[:60],
        }

    return {
        "active": sorted_active,
        "injected": injected,
        "expired": expired,
        "disabled": disabled,
        "limit": MAX_INJECTED_LESSONS,
        "not_injected": [dict(_brief(i), reason="CAPACITY_LIMIT")
                         for i in sorted_active if id(i) not in injected_ids],
    }


def audit_structured_lesson(*, rule_text: str, evidence_level: Optional[str] = None,
                            scope: Optional[Dict[str, Any]] = None,
                            sample_size: int = 1,
                            independent_sample_groups: Optional[int] = None,
                            counterexample_count: Optional[int] = None,
                            is_baseline: bool = False) -> Tuple[bool, str]:
    """结构化提案审查（规划文档 §4.6 的结构化部分；文本红线继续复用 audit_proposed_lesson）。

    与 `audit_proposed_lesson` 分开的原因：那个函数是**文本红线**，被策略回滚复核
    等旧调用方按位置参数使用；这里额外要求 scope / 证据等级 / 独立性，
    基线文本（代码 manifest 产物）必须豁免绝对化措辞。
    """
    if not isinstance(rule_text, str) or len(rule_text.strip()) < 10:
        return False, "结构化审查拒绝: 心法文本过短"

    level = str(evidence_level or "").strip()
    if level and level not in EVIDENCE_LEVELS:
        return False, f"结构化审查拒绝: 未知 evidence_level={level}"

    # `audit_proposed_lesson` 的「样本量不足」是**晋升门槛**（单笔异常不得写成长期心法）。
    # 待验证观察恰恰是「不晋升、只记录」的那一层，故不得被该门槛误杀（§4.5-4）。
    gate_sample = max(2, int(sample_size or 0)) if level == "OBSERVATION_ONLY" else sample_size
    passed, reason = audit_proposed_lesson(
        rule_text, sample_size=max(2, int(sample_size or 0)) if is_baseline else gate_sample)
    if not passed:
        return passed, reason

    # 禁止模型直接生成 is_baseline=True（只有代码 manifest 可以产出基线）。
    if is_baseline:
        manifest = baseline_manifest()
        if rule_text.strip() not in {e["rule_text"] for e in manifest.values()}:
            return False, "结构化审查拒绝: is_baseline 只能来自代码 baseline manifest"
        return True, "PASSED"

    if level in {"REVIEWED_HEURISTIC", "PROPOSED_HEURISTIC"}:
        for pattern in CURRENT_RULE_CLAIM_PATTERNS:
            if re.search(pattern, rule_text):
                return False, "结构化审查拒绝: 提案不得把建议写成当前硬规则"

    if level == "REVIEWED_HEURISTIC":
        for pattern in ABSOLUTE_CLAIM_PATTERNS:
            if re.search(pattern, rule_text):
                return False, "结构化审查拒绝: 绝对化表述不得作为已验证结论"
        if sample_size < 2:
            return False, "结构化审查拒绝: 已审核启发式至少需要 2 个样本"
        if independent_sample_groups is not None and independent_sample_groups < 2:
            return False, "结构化审查拒绝: 已审核启发式至少需要 2 个独立样本组"
        if not _scope_is_declared(scope):
            return False, "结构化审查拒绝: 进入提示词的规则必须声明适用 scope"

    return True, "PASSED"


def _scope_is_declared(scope: Any) -> bool:
    """scope 必须至少有一个非空的适用维度（策略/周期/方向/交易所）。"""
    if not isinstance(scope, dict):
        return False
    for key in ("strategy_modes", "timeframes", "directions", "venues", "setup_kinds"):
        value = scope.get(key)
        if isinstance(value, (list, tuple, set)) and any(str(v).strip() for v in value):
            return True
        if isinstance(value, str) and value.strip():
            return True
    return False


def _validate(lessons):
    if not isinstance(lessons, list):
        raise MemoryCorruptError("Expected a lesson list")
    ids = set()
    for item in lessons:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                or not item["id"] or item["id"] in ids
                or not isinstance(item.get("rule_text"), str) or not item["rule_text"].strip()
                or type(item.get("enabled")) is not bool):
            raise MemoryCorruptError("Invalid lesson schema or duplicate id")
        for key in ("health_score", "ttl_days", "sample_size"):
            if key in item and (type(item[key]) not in (int, float) or not math.isfinite(item[key]) or item[key] < 0):
                raise MemoryCorruptError(f"Invalid numeric field: {key}")
        for key in ("category", "created_at", "shield_status"):
            if key in item and not isinstance(item[key], str):
                raise MemoryCorruptError(f"Invalid text field: {key}")
        level = item.get("evidence_level")
        if level is not None and level not in EVIDENCE_LEVELS:
            raise MemoryCorruptError(f"Invalid evidence_level: {level}")
        status = item.get("status")
        if status is not None and status not in LESSON_STATUSES:
            raise MemoryCorruptError(f"Invalid status: {status}")
        if "scope" in item and item["scope"] is not None and not isinstance(item["scope"], dict):
            raise MemoryCorruptError("Invalid scope: expected an object")
        if "approval" in item and item["approval"] is not None and not isinstance(item["approval"], dict):
            raise MemoryCorruptError("Invalid approval: expected an object")
        for key in ("source_strategy_versions", "independent_sample_groups"):
            if key == "source_strategy_versions" and key in item:
                value = item[key]
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    raise MemoryCorruptError("Invalid source_strategy_versions")
        ids.add(item["id"])
    return lessons


def read_memory_snapshot():
    """Pure read: missing != empty; legacy lists remain readable without migration."""
    try:
        raw = STRUCTURED_MEMORY_FILE.read_bytes()
    except FileNotFoundError:
        return {"exists": False, "version": "missing", "lessons": []}
    try:
        payload = json.loads(raw)
        if isinstance(payload, list):
            lessons = payload
        else:
            if (not isinstance(payload, dict) or payload.get("schema_version") != 1
                    or not isinstance(payload.get("revision"), str)):
                raise MemoryCorruptError("Invalid memory envelope")
            lessons = payload["lessons"]
        _validate(lessons)
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise MemoryCorruptError("Structured memory is damaged; retained unchanged") from exc
    return {"exists": True, "version": hashlib.sha256(raw).hexdigest(), "lessons": lessons}


def load_structured_memory() -> List[Dict[str, Any]]:
    return read_memory_snapshot()["lessons"]


def is_lesson_expired(item: Dict[str, Any], now: Optional[datetime.datetime] = None) -> bool:
    """Checks if a non-baseline lesson has exceeded its natural half-life (TTL days)."""
    if item.get("is_baseline"):
        return False
    created_at_str = item.get("created_at")
    ttl_days = float(item.get("ttl_days") or 7)
    if not created_at_str:
        return False
    try:
        if created_at_str.endswith("Z"):
            created_at_str = created_at_str[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(created_at_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        cur = now or datetime.datetime.now(datetime.timezone.utc)
        return (cur - dt).total_seconds() > (ttl_days * 86400)
    except Exception:
        return False


def render_lessons(lessons):
    selection = select_injected_lessons(lessons)
    return "\n".join(f"- {item['rule_text']}" for item in selection["injected"])


#: 提示词注入上限（基线优先，其余按时间倒序）——**超出的 active 心法不会进主脑**。
#: 审计 P1-8：旧实现把这个截断藏在读侧，页面仍把超出的算作"生效中"，文案还写
#: "实时透明注入主脑 Prompt" → 管理员以为 12 条都在生效，实际只有 8 条。
MAX_INJECTED_LESSONS = 8


def injection_report(lessons) -> Dict[str, Any]:
    """披露注入实况：active(未过期)/injected 的条数与未注入清单（供面板与审计）。

    与 `render_lessons` 共用同一个筛选函数（规划文档 §4.1-7），页面数字与
    提示词实况不再各算一遍。
    """
    selection = select_injected_lessons(lessons)
    return {
        "active": len(selection["active"]),
        "injected": len(selection["injected"]),
        "limit": selection["limit"],
        "expired": len(selection["expired"]),
        "disabled": len(selection["disabled"]),
        "not_injected": selection["not_injected"],
    }


def memory_inventory(lessons) -> Dict[str, Any]:
    """后台记忆页所需的完整口径（规划文档 §9.1）：计数、基线一致性、注入实况。"""
    rows = [i for i in (lessons or []) if isinstance(i, dict)]
    selection = select_injected_lessons(rows)
    levels: Dict[str, int] = {}
    for item in rows:
        level = str(item.get("evidence_level") or (
            "BASELINE_HARD_RULE" if item.get("is_baseline") else "REVIEWED_HEURISTIC"))
        levels[level] = levels.get(level, 0) + 1
    consistency = check_baseline_consistency(rows)
    # ⚠️ `checked_at` 是这个内存视图里**唯一**每次都变的值，而 `GET /admin/memory`
    # 必须是纯读且可重复（tests/test_memory_routes_isolated.py 钉死两次 GET 相等）。
    # 带时间戳的完整报告由 `check_baseline_consistency()` 与自进化报告持有。
    consistency.pop("checked_at", None)
    return {
        "total": len(rows),
        "enabled": len([i for i in rows if i.get("enabled")]),
        "active": len(selection["active"]),
        "injected": len(selection["injected"]),
        "limit": selection["limit"],
        "expired": len(selection["expired"]),
        "baseline_count": len([i for i in rows if i.get("is_baseline")]),
        "reviewed_heuristics": levels.get("REVIEWED_HEURISTIC", 0),
        "observations": levels.get("OBSERVATION_ONLY", 0),
        "evidence_levels": levels,
        "baseline_consistency": consistency,
        "not_injected": selection["not_injected"],
    }


def latest_ledger_revision() -> str:
    """最近一次复盘使用的台账 revision（面板显示用；读不到就如实返回空串）。"""
    try:
        payload = json.loads((DATA_DIR / "self_improvement_report.json").read_text(encoding="utf-8"))
        return str(payload.get("ledger_revision") or "")
    except Exception:
        return ""


def read_trading_context(legacy_md=None, legacy_json=None):
    snapshot = read_memory_snapshot()
    if snapshot["exists"]:
        texts = [i["rule_text"] for i in snapshot["lessons"] if i["enabled"]]
        return snapshot, render_lessons(snapshot["lessons"]), texts
    # Compatibility is read-only and only used if the authority does not exist.
    md_path = Path(legacy_md) if legacy_md is not None else AI_MEMORY_MD_FILE
    text = md_path.read_text(encoding="utf-8") if md_path.is_file() else ""
    texts = []
    if legacy_json is not None and Path(legacy_json).is_file():
        payload = json.loads(Path(legacy_json).read_text(encoding="utf-8"))
        texts = payload.get("core_lessons", [])
        if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
            raise MemoryCorruptError("Invalid legacy lessons")
    return snapshot, text or "\n".join(f"- {t}" for t in texts), texts


def render_trading_memory_layered(legacy_md=None, legacy_json=None) -> str:
    """交易主脑用的**分层**记忆渲染（规划文档 §6.3 / §6.4）。

    - 结构化权威存在且有条目 ⇒ 【已审核启发式】/【待验证观察】两块；
      未审核提案、退役/过期条目、基线一律**不出现**（基线上硬规则区块）；
    - 权威为空 ⇒ 回落到历史 markdown（行为不变）；
    - 权威损坏 ⇒ 抛出 `MemoryCorruptError`（fail-closed，绝不静默降级）。
    """
    try:
        from scripts.evolution.review_context import (current_strategy_mode, render_lesson_block,
                                                      split_lessons_by_level)
    except ImportError:  # pragma: no cover - scripts/ 单独在 sys.path 上时
        from evolution.review_context import (current_strategy_mode, render_lesson_block,
                                              split_lessons_by_level)
    snapshot = read_memory_snapshot()
    if snapshot["exists"] and snapshot["lessons"]:
        # §6.4：只渲染当前策略模式适用的条目（未声明 scope 的通用条目不受影响）。
        return render_lesson_block(split_lessons_by_level(snapshot["lessons"]),
                                   strategy_mode=current_strategy_mode())
    return render_trading_memory(legacy_md, legacy_json)


def render_trading_memory(legacy_md=None, legacy_json=None):
    text = read_trading_context(legacy_md, legacy_json)[1]
    if not text.strip():
        return ""
    return "======================= 【R20 启发式实战认知与长期记忆】 =======================\n" + text


def sync_markdown_mirror() -> bool:
    """Refresh the derived AI_TRADING_MEMORY.md mirror from the structured authority.

    The markdown file is a read-only compatibility artifact for legacy readers
    (notably the public dashboard). It must never drift behind the authority, or
    the homepage freezes on a stale snapshot while the engine keeps revising.
    The structured store remains the single authority; this mirror is rewritten
    after every review cycle.
    """
    snapshot = read_memory_snapshot()
    if not snapshot["exists"]:
        return False
    body = render_trading_memory()
    if not body.strip():
        return False
    lessons = snapshot["lessons"]
    stamps = [i.get("created_at") for i in lessons if i.get("created_at")]
    newest = max(stamps) if stamps else ""
    if newest:
        try:
            local = datetime.datetime.fromisoformat(newest).astimezone(
                datetime.timezone(datetime.timedelta(hours=8))
            ).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            local = str(newest)[:19]
    else:
        local = "--"
    doc = (
        "# R20 AI 交易实战长期心法 (Heuristic Long-Term Memory)\n\n"
        f"> 状态：由自进化防污染认知中枢实时纳管 | 更新基准: {local} (UTC+8)\n"
        f"> 权威来源: structured_trading_memory.json | 修订 {str(snapshot.get('version'))[:8]} | 共 {len(lessons)} 条心法\n"
        "> 宪法安全护栏：已通过极端离群值过滤 (Outlier Rejection) 与防偏见白盒审查。\n\n"
        + body
        + "\n"
    )
    tmp = AI_MEMORY_MD_FILE.with_suffix(f".md.tmp.{os.getpid()}")
    try:
        AI_MEMORY_MD_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(doc, encoding="utf-8")
        os.replace(tmp, AI_MEMORY_MD_FILE)
    finally:
        if tmp.exists():
            tmp.unlink()
    return True


@contextmanager
def _memory_lock():
    STRUCTURED_MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(str(STRUCTURED_MEMORY_FILE) + ".lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _commit(lessons):
    _validate(lessons)
    payload = {"schema_version": 1, "revision": uuid.uuid4().hex, "lessons": lessons}
    fd, name = tempfile.mkstemp(prefix=".memory-", suffix=".tmp", dir=STRUCTURED_MEMORY_FILE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, STRUCTURED_MEMORY_FILE)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    # Markdown is rendered on demand, never a second commit or an authority.


def _check_version(snapshot, expected_version):
    if expected_version is None or expected_version == "":
        raise MemoryVersionRequiredError("缺少 expected_version，请重新加载心法后再操作")
    if snapshot["version"] != expected_version:
        raise MemoryConflictError("心法版本已变化，请重新加载后再操作；未覆盖当前数据")


def _new_lesson(text, sample_size, category="TACTICAL", evidence_level="REVIEWED_HEURISTIC",
                metadata=None):
    """新发布条目必须写全新 schema 字段（规划文档 §3.1：旧条目缺字段按旧读，新发布写全）。"""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    meta = dict(metadata or {})
    item = {"id": "lesson_" + uuid.uuid4().hex, "category": category,
            "rule_text": text.strip(), "enabled": True, "is_baseline": False,
            "status": "ACTIVE", "evidence_level": evidence_level,
            "health_score": 90.0, "created_at": now, "updated_at": now,
            "ttl_days": meta.get("ttl_days", 7), "sample_size": sample_size,
            "independent_sample_groups": int(meta.get("independent_sample_groups", 0) or 0),
            "counterexample_count": int(meta.get("counterexample_count", 0) or 0),
            "source_ledger_revision": str(meta.get("source_ledger_revision") or ""),
            "source_strategy_versions": list(meta.get("source_strategy_versions") or []),
            "scope": copy.deepcopy(meta.get("scope") or {}),
            "shield_status": "PASSED"}
    approval = meta.get("approval")
    item["approval"] = (copy.deepcopy(approval) if isinstance(approval, dict)
                        else {"required": False, "status": "NOT_REQUIRED",
                              "approved_by": "", "approved_at": ""})
    return item


def _review_candidates(texts, old, sample_size, strict, change_status=None, metadata=None):
    """候选条目筛选与审查。

    `metadata`（可选）：`{rule_text: {evidence_level, scope, independent_sample_groups,
    counterexample_count, source_ledger_revision, ttl_days, approval}}` ——
    只作用于**新建**条目，让已审核启发式/待验证观察带上结构化审计字段。
    """
    meta_by_text = metadata if isinstance(metadata, dict) else {}
    if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
        raise ValueError("Expected text list")
    by_text = {i["rule_text"].strip(): i for i in old}
    result = []
    seen = set()
    for text in texts:
        text = text.strip()
        if text in seen:
            continue
        seen.add(text)
        # Unchanged entries retain identity, audit metadata and disabled state.
        if text in by_text:
            result.append(copy.deepcopy(by_text[text]))
            continue
        passed, reason = audit_proposed_lesson(text, sample_size=sample_size)
        if not passed:
            if strict:
                raise ValueError(reason)
            continue
        meta = meta_by_text.get(text) if isinstance(meta_by_text.get(text), dict) else None
        level = (meta or {}).get("evidence_level", "REVIEWED_HEURISTIC")
        result.append(_new_lesson(text, sample_size, metadata=meta, evidence_level=level))
    # Preserve disabled tombstones even when omitted by a model or legacy editor.
    result.extend(copy.deepcopy(i) for i in old if not i["enabled"] and i["rule_text"].strip() not in seen)
    # 审计 P1-8c：REVISE/INVALIDATE 下，模型没复述的**已学（非基准）**心法旧实现直接消失，
    # 报告只统计被宪法补回的基准条目 → 静默丢知识。现在改为保留为**停用存档**
    # （enabled=False + 退役留痕），既不注入提示词、也不蒸发，面板可见并可人工恢复。
    if str(change_status or "").upper() in {"REVISE", "INVALIDATE"}:
        for item in old:
            if not item.get("enabled") or item.get("is_baseline"):
                continue
            if item["rule_text"].strip() in seen:
                continue
            tomb = copy.deepcopy(item)
            tomb["enabled"] = False
            tomb["retired_at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            tomb["retired_reason"] = f"{str(change_status).upper()} 未复述，宿主保留为停用存档（可人工复核恢复）"
            result.append(tomb)
    return result


def publish_review(texts, *, expected_version, sample_size, change_status, metadata=None):
    """发布复盘心法：**发布前先合并代码基准，发布后再次校验**（规划文档 §4.1-3）。

    这样即使权威文件已经丢失过基线，候选清单也会被代码 manifest 修复；
    而模型遗漏/改写基线的提案在提交前就会被纠正。校验不通过则**拒绝发布**，
    权威文件保持原样（fail-closed）。
    """
    if change_status == "NO_CHANGE":
        return False
    if change_status not in {"ADD", "REVISE", "INVALIDATE"}:
        raise ValueError("Invalid change status")
    with _memory_lock():
        snapshot = read_memory_snapshot()
        _check_version(snapshot, expected_version)
        merged_old, _ = merge_code_baselines(snapshot["lessons"])
        candidates = _review_candidates(texts, merged_old, sample_size, True, change_status,
                                        metadata=metadata)
        candidates, _ = merge_code_baselines(candidates)
        consistency = check_baseline_consistency(candidates)
        if not consistency["healthy"]:
            raise MemoryBaselineError(
                f"{MEMORY_BASELINE_MISMATCH}: 基准心法与代码 manifest 不一致，拒绝发布 "
                f"missing={consistency['missing_ids']} mismatched={consistency['mismatched_ids']} "
                f"unexpected={consistency['unexpected_ids']}")
        if not candidates or candidates == snapshot["lessons"]:
            return False
        # Rejected-only proposals must not remove existing active entries.
        if not any(i["rule_text"].strip() in {t.strip() for t in texts} for i in candidates):
            return False
        _commit(candidates)
        # 发布后再次校验（规划文档 §4.1-3）：不健康就当作发布失败，绝不静默带着不一致跑。
        post = check_baseline_consistency(read_memory_snapshot()["lessons"])
        if not post["healthy"]:
            raise MemoryBaselineError(
                f"{MEMORY_BASELINE_MISMATCH}: 发布后校验不一致 "
                f"missing={post['missing_ids']} mismatched={post['mismatched_ids']}")
        return True


def _assert_baselines_intact(old_lessons, new_lessons):
    """普通写入接口不得删除/停用/改写 baseline 文本（规划文档 §4.1-4）。"""
    by_id = {i.get("id"): i for i in (new_lessons or []) if isinstance(i, dict)}
    for item in old_lessons or []:
        if not (isinstance(item, dict) and item.get("is_baseline")):
            continue
        target = by_id.get(item.get("id"))
        if target is None:
            raise MemoryBaselineError(
                "BASELINE_REMOVAL_FORBIDDEN: 基准心法不能通过普通接口删除")
        if _baseline_text_hash(target.get("rule_text")) != _baseline_text_hash(item.get("rule_text")):
            raise MemoryBaselineError(
                "BASELINE_TEXT_IMMUTABLE: 基准心法文本不能通过普通接口修改")
        if not target.get("enabled"):
            raise MemoryBaselineError(
                "BASELINE_DISABLE_REQUIRES_APPROVAL: 停用基准心法需要人工确认 token")


def annotate_lesson_metadata(metadata, *, expected_version):
    """给**已发布**条目补齐结构化审计字段（规划文档 §3.1 新字段落地）。

    为什么不直接在 `publish_review` 里带上 metadata：发布段由 `apply_memory_review`
    的 AST 对拍门钉住（它调 `publish_review(texts, expected_version, sample_size,
    change_status)`），故采用「先发布、再按文本补元数据」的两步写入。

    安全边界：只允许改白名单字段，`rule_text` / `enabled` / `is_baseline` 一律不动；
    不存在或属于基线的条目直接跳过。
    """
    allowed = {"evidence_level", "scope", "independent_sample_groups", "counterexample_count",
               "source_ledger_revision", "source_strategy_versions", "ttl_days", "approval",
               "status"}
    if not isinstance(metadata, dict) or not metadata:
        return False
    with _memory_lock():
        snapshot = read_memory_snapshot()
        _check_version(snapshot, expected_version)
        lessons = snapshot["lessons"]
        by_text = {}
        for item in lessons:
            by_text.setdefault(str(item.get("rule_text") or "").strip(), item)
        changed = False
        for text, meta in metadata.items():
            item = by_text.get(str(text).strip())
            if item is None or item.get("is_baseline") or not isinstance(meta, dict):
                continue
            for key, value in meta.items():
                if key in allowed:
                    item[key] = copy.deepcopy(value)
                    changed = True
        if changed:
            _commit(lessons)
        return changed


def save_structured_memory(lessons, *, expected_version):
    """Compatibility publisher; changed/new records must pass the same audit."""
    _validate(lessons)
    with _memory_lock():
        snapshot = read_memory_snapshot()
        _check_version(snapshot, expected_version)
        _assert_baselines_intact(snapshot["lessons"], lessons)
        disabled = {i["rule_text"].strip() for i in snapshot["lessons"] if not i["enabled"]}
        for item in lessons:
            if item["enabled"] and item["rule_text"].strip() in disabled:
                raise ValueError("Disabled text requires explicit toggle, not republication")
            if item not in snapshot["lessons"]:
                passed, reason = audit_proposed_lesson(item["rule_text"], item.get("sample_size", 1))
                if not passed:
                    raise ValueError(reason)
        _commit(lessons)


def toggle_lesson(lesson_id, *, expected_version=None, confirm_token=None):
    with _memory_lock():
        snapshot = read_memory_snapshot()
        _check_version(snapshot, expected_version)
        lessons = snapshot["lessons"]
        for item in lessons:
            if item["id"] == lesson_id:
                if item.get("is_baseline") and item.get("enabled"):
                    # 停用基线 = 把代码硬规则从提示词里摘掉，必须显式人工确认（§4.1-5）。
                    if str(confirm_token or "") != baseline_disable_token(lesson_id):
                        raise MemoryBaselineError(
                            "BASELINE_DISABLE_REQUIRES_APPROVAL: 停用基准心法需要 confirm_token")
                item["enabled"] = not item["enabled"]
                item["status"] = "ACTIVE" if item["enabled"] else "DISABLED"
                item["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                _commit(lessons)
                return item
    return None


def rollback_to_baseline(*, expected_version=None):
    with _memory_lock():
        snapshot = read_memory_snapshot()  # Corruption is never overwritten.
        _check_version(snapshot, expected_version)
        lessons, _ = merge_code_baselines([])
        _commit(lessons)
        return lessons


def commit_migrated_memory(lessons, *, expected_version):
    """迁移专用提交（规划文档 §11.2 第 9 步：人工确认后原子提交）。

    与 `save_structured_memory` 的区别：**不重新审查历史条目**（迁移是 schema 变更，
    不是新规则发布；历史条目本来就可能不满足新审查口径）。但硬约束一个不少：

    - 持锁 + `expected_version` CAS（防并发覆盖）；
    - 完整性校验（`_validate`）；
    - 强制合并代码基准并校验一致性（迁移不得让基准脱节）；
    - 原子替换提交。
    """
    _validate(lessons)
    with _memory_lock():
        snapshot = read_memory_snapshot()
        _check_version(snapshot, expected_version)
        merged, merge_report = merge_code_baselines(lessons)
        consistency = check_baseline_consistency(merged)
        if not consistency["healthy"]:
            raise MemoryBaselineError(
                f"{MEMORY_BASELINE_MISMATCH}: 迁移后基准不一致，拒绝提交 "
                f"missing={consistency['missing_ids']} mismatched={consistency['mismatched_ids']}")
        _commit(merged)
        return {"lessons": merged, "merge_report": merge_report,
                "consistency": consistency,
                "version": read_memory_snapshot()["version"]}


def admin_memory_view():
    snapshot, raw, _ = read_trading_context()
    items = [i["rule_text"] for i in snapshot["lessons"] if i["enabled"]]
    if not snapshot["exists"]:
        items = [line.strip()[2:].strip() for line in raw.splitlines() if line.strip().startswith("- ")]
    inventory = memory_inventory(snapshot["lessons"])
    inventory["ledger_revision"] = latest_ledger_revision()
    inventory["memory_revision"] = snapshot["version"]
    return {"items": items, "count": len(items), "raw": raw,
            "structured_lessons": snapshot["lessons"], "version": snapshot["version"],
            "legacy_read_only": not snapshot["exists"], "inventory": inventory}


def admin_mutate(operation, *, texts=None, index=None, lesson_id=None, expected_version=None,
                 confirm_token=None):
    with _memory_lock():
        snapshot = read_memory_snapshot()
        _check_version(snapshot, expected_version)
        if not snapshot["exists"]:
            raise MemoryConflictError("Legacy memory is read-only; explicit initialization required")
        old = snapshot["lessons"]
        active = [i["rule_text"] for i in old if i["enabled"]]
        removed = None
        if operation == "delete":
            target = None
            if lesson_id is not None:
                target = next((i for i in old if i["id"] == lesson_id), None)
                if target is None:
                    raise IndexError("Memory id not found")
                removed = target["rule_text"]
                lessons = [i for i in old if i["id"] != lesson_id]
            else:
                if index is None or index < 0 or index >= len(active):
                    raise IndexError("Memory index not found")
                removed = active.pop(index)
                target = next((i for i in old if i["rule_text"] == removed), None)
                lessons = [i for i in old if i["rule_text"] != removed]
            # 删除 baseline 返回明确错误（规划文档 §4.1-5）。
            if isinstance(target, dict) and target.get("is_baseline"):
                raise MemoryBaselineError(
                    "BASELINE_REMOVAL_FORBIDDEN: 基准心法禁止删除，只能通过 confirm_token 停用")
        elif operation in {"add", "replace"}:
            candidates = list(texts or [])
            if operation == "add":
                candidates += active
            lessons = _review_candidates(candidates, old, 3, True)
            # 普通 replace 不得绕过基准校验（规划文档 §4.1-5）。
            _assert_baselines_intact(old, lessons)
        else:
            raise ValueError("Unknown memory operation")
        if lessons != old:
            _commit(lessons)
        result = {"saved": True, "items": [i["rule_text"] for i in lessons if i["enabled"]]}
        if removed is not None:
            result["removed"] = removed
        return result


def add_safe_lesson(rule_text, category="TACTICAL", sample_size=3):
    passed, reason = audit_proposed_lesson(rule_text, sample_size)
    if not passed:
        return False, reason, None
    with _memory_lock():
        lessons = load_structured_memory()
        for item in lessons:
            if item["rule_text"].strip() == rule_text.strip():
                return True, "条目已存在（保留启停状态）", item
        item = _new_lesson(rule_text, sample_size, category)
        lessons.append(item)
        _commit(lessons)
        return True, "心法审查通过并成功收录", item
