"""自进化记忆迁移工具回归（规划文档 §11 / §12.1）。

判据：默认只读；`--apply` 必须带 `expected-version`；stale version 不写盘；
记忆损坏 fail-closed；迁移补 schema 字段、补回代码基准、不自动启用/删除历史条目。
"""
from __future__ import annotations

import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import scripts.evolution_shield as shield  # noqa: E402
import scripts.migrate_evolution_memory as mig  # noqa: E402


def _lesson(text="历史心法条目内容需要足够长", **over):
    item = {"id": "lesson_" + (over.pop("id_suffix", None) or "a" * 8),
            "category": "TACTICAL", "rule_text": text, "enabled": True,
            "health_score": 90.0, "created_at": "2026-09-20T00:00:00+00:00",
            "ttl_days": 30, "sample_size": 3, "is_baseline": False,
            "shield_status": "PASSED"}
    item.update(over)
    return item


class _Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.memory = self.root / "structured_trading_memory.json"
        self.mirror = self.root / "AI_TRADING_MEMORY.md"
        for name, value in (("STRUCTURED_MEMORY_FILE", self.memory),
                            ("AI_MEMORY_MD_FILE", self.mirror),
                            ("DATA_DIR", self.root)):
            import unittest.mock as mock
            p = mock.patch.object(shield, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _write(self, lessons, revision="rev-1"):
        self.memory.write_text(json.dumps({"schema_version": 1, "revision": revision,
                                           "lessons": lessons}), encoding="utf-8")

    def _version(self):
        return shield.read_memory_snapshot()["version"]


class PlanTests(_Sandbox):
    def test_plan_reports_the_required_fields(self):
        self._write([_lesson(), _lesson(text="已停用的历史条目内容足够长", enabled=False,
                                        id_suffix="b" * 8)])
        plan = mig.build_migration_plan()
        for key in ("current_total", "current_enabled", "current_baseline_count",
                    "code_baseline_count", "missing_baselines", "mismatched_baselines",
                    "duplicate_entries", "expired_entries", "unverifiable_source",
                    "before_hash", "after_hash", "pending_baseline_additions"):
            self.assertIn(key, plan)
        self.assertEqual(plan["current_total"], 2)
        self.assertEqual(plan["current_enabled"], 1)
        self.assertEqual(plan["code_baseline_count"], len(shield.BASELINE_LESSONS))
        self.assertEqual(sorted(plan["pending_baseline_additions"]),
                         sorted(shield.baseline_manifest()))

    def test_duplicates_are_reported(self):
        text = "重复心法条目内容需要足够长"
        self._write([_lesson(text=text), _lesson(text=text, id_suffix="b" * 8)])
        plan = mig.build_migration_plan()
        self.assertEqual(plan["duplicate_entries"], [text])

    def test_plan_is_read_only(self):
        self._write([_lesson()])
        before = self.memory.read_bytes()
        plan = mig.build_migration_plan()
        self.assertEqual(self.memory.read_bytes(), before)
        self.assertEqual(plan["mode"], "dry-run")

    def test_plan_assigns_evidence_levels(self):
        self._write([_lesson(), _lesson(text="未启用历史条目内容足够长", enabled=False,
                                        id_suffix="c" * 8)])
        plan = mig.build_migration_plan()
        migrated = {c["id"]: c["fields"] for c in plan["entries_changed"]}
        self.assertTrue(migrated, "迁移计划必须列出被补字段的条目")
        self.assertTrue(all("evidence_level" in f for f in migrated.values()))

    def test_report_writer_only_writes_the_report_path(self):
        self._write([_lesson()])
        memory_before = self.memory.read_bytes()
        path = mig.write_report(mig.build_migration_plan(), self.root / "report.json")
        self.assertTrue(Path(path).is_file())
        self.assertEqual(self.memory.read_bytes(), memory_before)


class ApplyTests(_Sandbox):
    def test_apply_requires_expected_version(self):
        self._write([_lesson()])
        with self.assertRaises(shield.MemoryVersionRequiredError):
            mig.apply_migration(expected_version="")

    def test_apply_with_stale_version_does_not_write(self):
        self._write([_lesson()])
        before = self.memory.read_bytes()
        with self.assertRaises(shield.MemoryConflictError):
            mig.apply_migration(expected_version="stale")
        self.assertEqual(self.memory.read_bytes(), before)

    def test_apply_adds_code_baselines_and_keeps_history(self):
        self._write([_lesson(), _lesson(text="已停用历史条目内容足够长", enabled=False,
                                        id_suffix="b" * 8)])
        result = mig.apply_migration(expected_version=self._version())
        rows = shield.load_structured_memory()
        texts = [r["rule_text"] for r in rows]
        self.assertIn("历史心法条目内容需要足够长", texts)
        self.assertIn("已停用历史条目内容足够长", texts)
        self.assertTrue(shield.check_baseline_consistency(rows)["healthy"])
        self.assertEqual(result["mode"], "apply")
        self.assertTrue(result["markdown_mirror_synced"])

    def test_apply_does_not_enable_disabled_entries(self):
        self._write([_lesson(text="已停用的历史条目内容足够长", enabled=False)])
        mig.apply_migration(expected_version=self._version())
        row = next(r for r in shield.load_structured_memory()
                   if r["rule_text"] == "已停用的历史条目内容足够长")
        self.assertFalse(row["enabled"])
        self.assertEqual(row["status"], "DISABLED")

    def test_apply_does_not_delete_history(self):
        self._write([_lesson(id_suffix="d" * 8), _lesson(id_suffix="e" * 8,
                                                         text="第二条历史心法内容足够长")])
        mig.apply_migration(expected_version=self._version())
        ids = {r["id"] for r in shield.load_structured_memory()}
        self.assertIn("lesson_" + "d" * 8, ids)
        self.assertIn("lesson_" + "e" * 8, ids)

    def test_corrupt_memory_is_refused(self):
        self.memory.write_text("{ broken", encoding="utf-8")
        before = self.memory.read_bytes()
        with self.assertRaises(shield.MemoryCorruptError):
            mig.apply_migration(expected_version="x")
        self.assertEqual(self.memory.read_bytes(), before)

    def test_migrated_entries_carry_the_legacy_migration_tag(self):
        self._write([_lesson()])
        mig.apply_migration(expected_version=self._version())
        row = next(r for r in shield.load_structured_memory() if not r.get("is_baseline"))
        self.assertEqual(row["source_tag"], mig.MIGRATION_TAG)
        self.assertEqual(row["source_ledger_revision"], "")
        self.assertIn("approval", row)


class CliTests(_Sandbox):
    def test_check_mode_is_read_only_and_returns_zero(self):
        self._write([_lesson()])
        before = self.memory.read_bytes()
        self.assertEqual(mig.main(["--check"]), 0)
        self.assertEqual(self.memory.read_bytes(), before)

    def test_default_invocation_is_read_only(self):
        self._write([_lesson()])
        before = self.memory.read_bytes()
        self.assertEqual(mig.main([]), 0)
        self.assertEqual(self.memory.read_bytes(), before)

    def test_corrupt_memory_exits_with_code_two(self):
        self.memory.write_text("{ broken", encoding="utf-8")
        self.assertEqual(mig.main(["--check"]), 2)

    def test_stale_apply_exits_with_code_three(self):
        self._write([_lesson()])
        self.assertEqual(mig.main(["--apply", "--expected-version", "stale"]), 3)


if __name__ == "__main__":
    unittest.main()
