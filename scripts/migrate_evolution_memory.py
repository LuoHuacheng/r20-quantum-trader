#!/usr/bin/env python3
"""自进化记忆迁移工具（规划文档 §11.3）。

```bash
.venv/bin/python scripts/migrate_evolution_memory.py --check
.venv/bin/python scripts/migrate_evolution_memory.py --report data/evolution_memory_migration.json
.venv/bin/python scripts/migrate_evolution_memory.py --apply --expected-version <hash>
```

## 纪律

- **默认只读**：没有 `--apply` 不写任何数据；
- `--apply` 必须带读取时得到的 `expected-version`（防并发覆盖）；
- 不自动启用停用条目、不自动删除历史条目；
- 代码基准只进入「待确认迁移列表」，由人工确认后随同一次原子提交写盘；
- 提交前后各算一次 hash，并同步 Markdown 镜像、跑一致性校验。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import evolution_shield as shield  # noqa: E402

DEFAULT_REPORT = PROJECT_ROOT / "data" / "evolution_memory_migration.json"
MIGRATION_TAG = "LEGACY_MIGRATION"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _hash_lessons(lessons: List[Dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(lessons, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def _default_evidence_level(item: Dict[str, Any]) -> str:
    """按规划文档 §11.2 第 4 步给历史条目补默认证据等级。"""
    if item.get("is_baseline"):
        return "BASELINE_HARD_RULE"
    if item.get("enabled") and (item.get("sample_size") or 0) > 0:
        return "REVIEWED_HEURISTIC"
    return "OBSERVATION_ONLY"


def build_migration_plan(lessons: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """只读生成迁移计划/差异报告（绝不写盘）。"""
    if lessons is None:
        lessons = shield.load_structured_memory()
    manifest = shield.baseline_manifest()
    baseline_consistency = shield.check_baseline_consistency(lessons)

    texts: Dict[str, int] = {}
    for item in lessons:
        text = str(item.get("rule_text") or "").strip()
        if text:
            texts[text] = texts.get(text, 0) + 1
    duplicates = sorted(t for t, n in texts.items() if n > 1)

    migrated: List[Dict[str, Any]] = []
    changes: List[Dict[str, Any]] = []
    for item in lessons:
        row = dict(item)
        before = dict(row)
        if not row.get("evidence_level"):
            row["evidence_level"] = _default_evidence_level(row)
        if not row.get("status"):
            row["status"] = "ACTIVE" if row.get("enabled") else "DISABLED"
        if not row.get("source_ledger_revision"):
            row["source_ledger_revision"] = ""
            row["source_tag"] = MIGRATION_TAG
        row.setdefault("independent_sample_groups", 0)
        row.setdefault("counterexample_count", 0)
        row.setdefault("source_strategy_versions", [])
        row.setdefault("scope", {})
        row.setdefault("approval", {"required": False, "status": "NOT_REQUIRED",
                                    "approved_by": "", "approved_at": ""})
        migrated.append(row)
        diff = {k: v for k, v in row.items() if before.get(k) != v}
        if diff:
            changes.append({"id": row.get("id"), "fields": sorted(diff.keys()),
                            "rule_text": str(row.get("rule_text") or "")[:60]})

    merged, merge_report = shield.merge_code_baselines(migrated)
    after_consistency = shield.check_baseline_consistency(merged)
    return {
        "generated_at": _now(),
        "mode": "dry-run",
        "current_total": len(lessons),
        "current_enabled": len([i for i in lessons if i.get("enabled")]),
        "current_baseline_count": baseline_consistency["authority_baseline_count"],
        "code_baseline_count": baseline_consistency["code_baseline_count"],
        "missing_baselines": baseline_consistency["missing_ids"],
        "mismatched_baselines": baseline_consistency["mismatched_ids"],
        "unexpected_baselines": baseline_consistency["unexpected_ids"],
        "duplicate_entries": duplicates,
        "expired_entries": [i.get("id") for i in lessons if shield.is_lesson_expired(i)],
        "unverifiable_source": [i.get("id") for i in lessons
                                if not i.get("source_ledger_revision")],
        "pending_baseline_additions": merge_report["added_ids"],
        "pending_baseline_repairs": merge_report["repaired_ids"],
        "pending_baseline_demotions": merge_report["demoted_ids"],
        "entries_changed": changes,
        "before_hash": _hash_lessons(lessons),
        "after_hash": _hash_lessons(merged),
        "before_consistency": baseline_consistency,
        "after_consistency": after_consistency,
        "next_step": ("人工确认后：--apply --expected-version <当前 memory revision>"
                      "（本工具不自动写盘）"),
    }


def write_report(plan: Dict[str, Any], path: os.PathLike | str) -> str:
    """把只读报告写到指定路径（默认只读模式下唯一允许的写入）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return str(target)


def apply_migration(*, expected_version: str, plan: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """人工确认后原子提交迁移（§11.2 第 9~12 步）。"""
    if not expected_version:
        raise shield.MemoryVersionRequiredError("--apply 必须带 --expected-version")
    if plan is None:
        plan = build_migration_plan()
    lessons = shield.load_structured_memory()
    merged, merge_report = shield.merge_code_baselines(_migrate_rows(lessons))
    result = shield.commit_migrated_memory(merged, expected_version=expected_version)
    mirror_ok = False
    try:
        mirror_ok = bool(shield.sync_markdown_mirror())
    except Exception:
        mirror_ok = False
    return {
        "applied_at": _now(),
        "mode": "apply",
        "before_hash": plan.get("before_hash"),
        "after_hash": _hash_lessons(result["lessons"]),
        "memory_revision": result["version"],
        "merged_baselines": merge_report,
        "consistency": result["consistency"],
        "markdown_mirror_synced": mirror_ok,
        "lessons": len(result["lessons"]),
    }


def _migrate_rows(lessons: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """与 `build_migration_plan` 完全同一套字段补齐（两份实现共用一份逻辑）。"""
    out = []
    for item in lessons:
        row = dict(item)
        if not row.get("evidence_level"):
            row["evidence_level"] = _default_evidence_level(row)
        if not row.get("status"):
            row["status"] = "ACTIVE" if row.get("enabled") else "DISABLED"
        if not row.get("source_ledger_revision"):
            row["source_ledger_revision"] = ""
            row["source_tag"] = MIGRATION_TAG
        row.setdefault("independent_sample_groups", 0)
        row.setdefault("counterexample_count", 0)
        row.setdefault("source_strategy_versions", [])
        row.setdefault("scope", {})
        row.setdefault("approval", {"required": False, "status": "NOT_REQUIRED",
                                    "approved_by": "", "approved_at": ""})
        out.append(row)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="自进化结构化记忆迁移（默认只读）")
    parser.add_argument("--check", action="store_true", help="打印差异报告（不写盘）")
    parser.add_argument("--report", nargs="?", const=str(DEFAULT_REPORT), default=None,
                        help="把差异报告写到指定 JSON")
    parser.add_argument("--apply", action="store_true", help="执行迁移（必须带 expected-version）")
    parser.add_argument("--expected-version", default="", help="读取时得到的 memory revision")
    args = parser.parse_args(argv)

    try:
        plan = build_migration_plan()
    except shield.MemoryCorruptError as exc:
        print(f"权威记忆损坏，拒绝迁移（fail-closed）: {exc}", file=sys.stderr)
        return 2

    if args.report:
        path = write_report(plan, args.report)
        print(f"差异报告已写入 {path}")
    if args.apply:
        try:
            result = apply_migration(expected_version=args.expected_version, plan=plan)
        except shield.MemoryConflictError as exc:
            print(f"并发更新或 expected-version 不匹配，未写入: {exc}", file=sys.stderr)
            return 3
        except (shield.MemoryBaselineError, shield.MemoryCorruptError) as exc:
            print(f"迁移被拒绝: {exc}", file=sys.stderr)
            return 4
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.check or not (args.report or args.apply):
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
