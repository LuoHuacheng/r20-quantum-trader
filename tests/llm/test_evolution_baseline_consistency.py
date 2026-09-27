"""代码基准 manifest ↔ 权威结构化记忆的一致性门（规划文档 §3.2 / §12.1）。

覆盖场景（文档 Task 1「新增场景」逐条）：

- 权威文件缺少 baseline 时发布被拒绝或补回；
- 删除 baseline 被拒绝；
- baseline 文本被修改时报告不一致；
- 记忆文件损坏时不被自动覆盖；
- 8 条注入上限与后台显示一致。

沙箱纪律：`STRUCTURED_MEMORY_FILE` / `AI_MEMORY_MD_FILE` 指向临时目录，
绝不在真实 `data/` 上 flock/原子替换。
"""
from __future__ import annotations

import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import evolution_shield as es


def _lesson(text="这是一个足够长的可复用交易情境心法", **over):
    item = {"id": "lesson_" + (over.pop("id_suffix", None) or "a" * 8),
            "category": "TACTICAL", "rule_text": text, "enabled": True,
            "health_score": 90.0,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "ttl_days": 7, "sample_size": 3, "is_baseline": False,
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
                            ("DATA_DIR", self.root),
                            ("AI_MEMORY_MD_FILE", self.mirror)):
            p = patch.object(es, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _write(self, lessons):
        self.memory.write_text(json.dumps({"schema_version": 1, "revision": "rev-1",
                                           "lessons": lessons}), encoding="utf-8")

    def _version(self):
        return es.read_memory_snapshot()["version"]


class BaselineManifestTests(_Sandbox):
    def test_manifest_is_id_sorted_and_hashes_are_sha256_prefixes(self):
        manifest = es.baseline_manifest()
        self.assertEqual(list(manifest), sorted(manifest))
        self.assertEqual(len(manifest), len(es.BASELINE_LESSONS))
        for lesson_id, entry in manifest.items():
            self.assertEqual(entry["id"], lesson_id)
            self.assertEqual(len(entry["text_hash"]), 16)
            self.assertEqual(entry["text_hash"],
                             __import__("hashlib").sha256(
                                 entry["rule_text"].strip().encode()).hexdigest()[:16])

    def test_check_is_unhealthy_when_authority_lacks_baselines(self):
        report = es.check_baseline_consistency([])
        self.assertFalse(report["healthy"])
        self.assertEqual(sorted(report["missing_ids"]), sorted(es.baseline_manifest()))
        self.assertEqual(report["code_baseline_count"], len(es.BASELINE_LESSONS))
        self.assertEqual(report["authority_baseline_count"], 0)

    def test_healthy_when_merged(self):
        merged, merge_report = es.merge_code_baselines([])
        self.assertEqual(sorted(merge_report["added_ids"]), sorted(es.baseline_manifest()))
        self.assertTrue(es.check_baseline_consistency(merged)["healthy"])

    def test_merge_is_idempotent(self):
        once, _ = es.merge_code_baselines([])
        twice, report = es.merge_code_baselines(once)
        self.assertEqual(once, twice)
        self.assertEqual(report["added_ids"], [])
        self.assertEqual(report["repaired_ids"], [])

    def test_tampered_baseline_text_is_reported_and_repaired(self):
        merged, _ = es.merge_code_baselines([])
        merged[0]["rule_text"] = "被篡改的基准文本内容需要足够长"
        report = es.check_baseline_consistency(merged)
        self.assertFalse(report["healthy"])
        self.assertEqual(report["mismatched_ids"], [merged[0]["id"]])
        fixed, merge_report = es.merge_code_baselines(merged)
        self.assertEqual(merge_report["repaired_ids"], [merged[0]["id"]])
        self.assertTrue(es.check_baseline_consistency(fixed)["healthy"])

    def test_unknown_baseline_is_demoted_not_deleted(self):
        rows = [_lesson(id_suffix="9" * 8, is_baseline=True, rule_text="模型自造的基准条目内容需要足够长")]
        merged, report = es.merge_code_baselines(rows)
        self.assertEqual(report["demoted_ids"], [rows[0]["id"]])
        demoted = next(i for i in merged if i["id"] == rows[0]["id"])
        self.assertFalse(demoted["is_baseline"])
        self.assertIn("baseline_demoted_reason", demoted)


class BaselinePublishTests(_Sandbox):
    def test_publish_repairs_a_baseline_less_authority(self):
        self._write([_lesson(text="既有心法条目内容需要足够长")])
        self.assertFalse(es.check_baseline_consistency(es.load_structured_memory())["healthy"])
        self.assertTrue(es.publish_review(["既有心法条目内容需要足够长"],
                                          expected_version=self._version(),
                                          sample_size=3, change_status="ADD"))
        self.assertTrue(es.check_baseline_consistency(es.load_structured_memory())["healthy"])

    def test_publish_rejects_when_consistency_cannot_be_reached(self):
        self._write([_lesson(text="既有心法条目内容需要足够长")])
        broken = {"code_baseline_count": 4, "authority_baseline_count": 0,
                  "missing_ids": ["x"], "mismatched_ids": [], "unexpected_ids": [],
                  "healthy": False}
        with patch.object(es, "check_baseline_consistency", return_value=broken):
            with self.assertRaises(es.MemoryBaselineError) as ctx:
                es.publish_review(["既有心法条目内容需要足够长"], expected_version=self._version(),
                                  sample_size=3, change_status="ADD")
        self.assertIn(es.MEMORY_BASELINE_MISMATCH, str(ctx.exception))
        self.assertEqual([i["rule_text"] for i in es.load_structured_memory()],
                         ["既有心法条目内容需要足够长"], "拒绝发布不得写盘")

    def test_delete_baseline_is_refused_by_id(self):
        self._write(es.merge_code_baselines([])[0])
        baseline_id = es.BASELINE_LESSONS[0]["id"]
        with self.assertRaises(es.MemoryBaselineError) as ctx:
            es.admin_mutate("delete", lesson_id=baseline_id, expected_version=self._version())
        self.assertIn("BASELINE_REMOVAL_FORBIDDEN", str(ctx.exception))
        self.assertEqual(len(es.load_structured_memory()), len(es.BASELINE_LESSONS))

    def test_delete_baseline_is_refused_by_index(self):
        self._write(es.merge_code_baselines([])[0])
        with self.assertRaises(es.MemoryBaselineError):
            es.admin_mutate("delete", index=0, expected_version=self._version())

    def test_replace_cannot_drop_a_baseline(self):
        self._write(es.merge_code_baselines([_lesson(text="既有心法条目内容需要足够长")])[0])
        with self.assertRaises(es.MemoryBaselineError):
            es.admin_mutate("replace", texts=["既有心法条目内容需要足够长"],
                            expected_version=self._version())

    def test_save_structured_memory_cannot_disable_a_baseline(self):
        rows = es.merge_code_baselines([])[0]
        self._write(rows)
        rows[0]["enabled"] = False
        with self.assertRaises(es.MemoryBaselineError) as ctx:
            es.save_structured_memory(rows, expected_version=self._version())
        self.assertIn("BASELINE_DISABLE_REQUIRES_APPROVAL", str(ctx.exception))

    def test_toggle_baseline_requires_the_confirm_token(self):
        rows = es.merge_code_baselines([])[0]
        self._write(rows)
        lesson_id = rows[0]["id"]
        with self.assertRaises(es.MemoryBaselineError):
            es.toggle_lesson(lesson_id, expected_version=self._version())
        with self.assertRaises(es.MemoryBaselineError):
            es.toggle_lesson(lesson_id, expected_version=self._version(), confirm_token="wrong")
        out = es.toggle_lesson(lesson_id, expected_version=self._version(),
                               confirm_token=es.baseline_disable_token(lesson_id))
        self.assertFalse(out["enabled"])
        self.assertEqual(out["status"], "DISABLED")
        # 显式人工停用不等于「不一致」：单列披露，仍允许发布。
        report = es.check_baseline_consistency(es.load_structured_memory())
        self.assertTrue(report["healthy"])
        self.assertEqual(report["disabled_ids"], [lesson_id])

    def test_rollback_uses_the_shared_merge_and_lands_on_code_baselines(self):
        self._write([_lesson(text="既有心法条目内容需要足够长")])
        lessons = es.rollback_to_baseline(expected_version=self._version())
        self.assertEqual([i["id"] for i in lessons],
                         [i["id"] for i in es.BASELINE_LESSONS])

    def test_corrupt_memory_is_never_overwritten_by_a_repair(self):
        for text in ("{ broken", "{}", "null"):
            with self.subTest(text=text):
                self.memory.write_text(text, encoding="utf-8")
                before = self.memory.read_bytes()
                for action in (lambda: es.publish_review(["应当保持耐心等待更好的入场时机"],
                                                         expected_version="x", sample_size=3,
                                                         change_status="ADD"),
                               lambda: es.rollback_to_baseline(expected_version="x"),
                               lambda: es.save_structured_memory(es.merge_code_baselines([])[0],
                                                                 expected_version="x")):
                    with self.assertRaises(es.MemoryCorruptError):
                        action()
                self.assertEqual(self.memory.read_bytes(), before)


class InjectionInventoryTests(_Sandbox):
    def test_injection_cap_matches_backend_inventory(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        rows = [{"id": f"lesson_{n:08d}", "category": "TACTICAL",
                 "rule_text": f"可复用心法条目{n}内容足够长",
                 "enabled": True, "is_baseline": False, "health_score": 90.0,
                 "created_at": (now - datetime.timedelta(hours=n)).isoformat(),
                 "ttl_days": 7, "sample_size": 3, "shield_status": "PASSED"} for n in range(12)]
        report = es.injection_report(rows)
        self.assertEqual(report["active"], 12)
        self.assertEqual(report["injected"], es.MAX_INJECTED_LESSONS)
        self.assertEqual(report["limit"], es.MAX_INJECTED_LESSONS)
        self.assertEqual(len(report["not_injected"]), 12 - es.MAX_INJECTED_LESSONS)
        self.assertEqual(len(es.render_lessons(rows).splitlines()), es.MAX_INJECTED_LESSONS)
        inventory = es.memory_inventory(rows)
        self.assertEqual(inventory["injected"], es.MAX_INJECTED_LESSONS)
        self.assertEqual(inventory["active"], 12)
        self.assertEqual(len(inventory["not_injected"]), 12 - es.MAX_INJECTED_LESSONS)
        self.assertTrue(all(i["reason"] == "CAPACITY_LIMIT" for i in inventory["not_injected"]))

    def test_inventory_counts_levels_and_consistency(self):
        rows = es.merge_code_baselines([_lesson(text="已审核启发式内容需要足够长")])[0]
        inventory = es.memory_inventory(rows)
        self.assertEqual(inventory["baseline_count"], len(es.BASELINE_LESSONS))
        self.assertEqual(inventory["reviewed_heuristics"], 1)
        self.assertEqual(inventory["total"], len(rows))
        self.assertTrue(inventory["baseline_consistency"]["healthy"])

    def test_expired_lessons_are_not_injected(self):
        old = _lesson(text="过期心法条目内容需要足够长",
                      created_at="2020-01-01T00:00:00+00:00", ttl_days=1)
        report = es.injection_report([old])
        self.assertEqual(report["active"], 0)
        self.assertEqual(report["expired"], 1)
        self.assertEqual(es.render_lessons([old]), "")


class StructuredLessonAuditTests(unittest.TestCase):
    def test_reviewed_heuristic_needs_scope_and_independence(self):
        ok, reason = es.audit_structured_lesson(
            rule_text="趋势追随多单在 15M RSI 极值时不追价，等待回踩",
            evidence_level="REVIEWED_HEURISTIC", sample_size=3)
        self.assertFalse(ok)
        self.assertIn("scope", reason)
        ok, reason = es.audit_structured_lesson(
            rule_text="趋势追随多单在 15M RSI 极值时不追价，等待回踩",
            evidence_level="REVIEWED_HEURISTIC", sample_size=3,
            independent_sample_groups=1, scope={"strategy_modes": ["trend_following"]})
        self.assertFalse(ok)
        self.assertIn("独立样本组", reason)
        ok, _ = es.audit_structured_lesson(
            rule_text="趋势追随多单在 15M RSI 极值时不追价，等待回踩",
            evidence_level="REVIEWED_HEURISTIC", sample_size=3, independent_sample_groups=2,
            scope={"strategy_modes": ["trend_following"]})
        self.assertTrue(ok)

    def test_absolute_claims_are_refused_as_verified_conclusions(self):
        ok, reason = es.audit_structured_lesson(
            rule_text="浮盈达到阈值就保本移损可以锁死胜率下限",
            evidence_level="REVIEWED_HEURISTIC", sample_size=8, independent_sample_groups=4,
            scope={"timeframes": ["15m"]})
        self.assertFalse(ok)
        self.assertIn("绝对化表述", reason)

    def test_models_cannot_claim_a_rule_is_already_hard_law(self):
        ok, reason = es.audit_structured_lesson(
            rule_text="当前硬规则已生效：RSI 大于 75 时禁止追多，立即执行",
            evidence_level="REVIEWED_HEURISTIC", sample_size=8, independent_sample_groups=4,
            scope={"strategy_modes": ["trend_following"]})
        self.assertFalse(ok)

    def test_models_cannot_mint_a_baseline(self):
        ok, reason = es.audit_structured_lesson(
            rule_text="模型自造的基准心法条目内容需要足够长",
            evidence_level="BASELINE_HARD_RULE", is_baseline=True, sample_size=99)
        self.assertFalse(ok)
        self.assertIn("manifest", reason)
        # 代码 manifest 里的真基线在同样条件下通过
        ok, _ = es.audit_structured_lesson(
            rule_text=es.BASELINE_LESSONS[0]["rule_text"],
            evidence_level="BASELINE_HARD_RULE", is_baseline=True, sample_size=99)
        self.assertTrue(ok)

    def test_unknown_evidence_level_is_refused(self):
        ok, reason = es.audit_structured_lesson(rule_text="趋势跟随策略在极值区不追价等待回踩",
                                                evidence_level="WHATEVER", sample_size=3)
        self.assertFalse(ok)
        self.assertIn("evidence_level", reason)


if __name__ == "__main__":
    unittest.main()
