"""`frontend/src/views/admin/evolutionMemoryLogic.ts` 回归（规划文档 §9.1/§9.2/§9.3）。

纯逻辑用 node `--experimental-strip-types` 直接跑：计数口径与告警分派算错了
页面照样渲染，只能这样钉住。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "frontend" / "src" / "views" / "admin" / "evolutionMemoryLogic.ts"
PAGE = ROOT / "frontend" / "src" / "views" / "admin" / "EvolutionPage.vue"
DASH = ROOT / "frontend" / "src" / "views" / "dashboard" / "EvolutionView.vue"
STUDIO_MODULE = ROOT / "frontend" / "src" / "views" / "admin" / "promptStudioLogic.ts"
NODE = shutil.which("node")

_PROBE = r"""
const healthy = {total: 12, enabled: 9, active: 10, injected: 8, limit: 8,
  baseline_count: 4, reviewed_heuristics: 5, observations: 3, expired: 2,
  ledger_revision: 'abcdef1234567890', memory_revision: '0123456789abcdef',
  not_injected: [{id: 'x', reason: 'CAPACITY_LIMIT'}],
  baseline_consistency: {healthy: true, missing_ids: [], mismatched_ids: [], unexpected_ids: []}};
const broken = {...healthy, baseline_consistency: {healthy: false, missing_ids: ['lesson_a'], mismatched_ids: []}};
const out = {
  rows: m.summarizeMemoryInventory({inventory: healthy, version: 'v'}),
  rowsMissing: m.summarizeMemoryInventory({}),
  rowsBroken: m.summarizeMemoryInventory({inventory: broken}),
  alertsClean: m.memoryAlerts({inventory: healthy}, {asset_multiplier_status: 'NEUTRAL'}),
  alertsBroken: m.memoryAlerts({inventory: broken},
    {snapshot_source_audit: {total: 10, untrusted: 8}, reused_snapshot_groups: [1, 2],
     asset_multiplier_status: 'EXPIRED', llm_failed: true, pending_proposals: [{}],
     ledger_revision: 'aaa', memory_revision: 'bbb'}),
  alertsLegacy: m.memoryAlerts({legacy_read_only: true}),
  layers: m.summarizeReviewLayers({facts: [{text: 'f', evidence_ids: ['trade:1'],
    independent_sample_groups: 4, sample_size: 12}],
    hypotheses: [{text: 'h', counterexample_count: 3, scope: {timeframes: ['15m']}}],
    rule_proposals: [{text: 'p', requested_level: 'REVIEWED_HEURISTIC', requires_approval: true}]}),
  layersLegacy: m.summarizeReviewLayers({fact_texts: ['f2'], heuristics: [{text: 'p2'}]}),
  evidence: m.summarizeReviewEvidence({ledger_revision: 'L', memory_revision: 'M',
    policy_hash: 'P', baseline_hash: 'B', review_input_hash: 'I', review_output_hash: 'O',
    snapshot_source_audit: {trusted: 3, time_unverified: 1},
    evidence_stats: {independent_sample_groups: 2, strategy_versions: {'legacy@1': 4}},
    asset_multiplier_status: 'NEUTRAL'}),
};
process.stdout.write(JSON.stringify(out));
"""


_STUDIO_PROBE = r"""
const out = {};
out.modes = m.EXECUTION_MODE_RULE_SETS;
out.defaultPolicy = m.DEFAULT_EXECUTION_POLICY;
out.missing = m.normalizeExecutionPolicy(undefined);
out.unknownMode = m.normalizeExecutionPolicy({mode: 'yolo', revision: 4});
out.valid = m.normalizeExecutionPolicy({mode: 'trend_confirm_5m', revision: 3});
out.badRevision = m.normalizeExecutionPolicy({mode: 'legacy', revision: 0});
out.patch = m.executionPolicyPatch('mean_reversion');
process.stdout.write(JSON.stringify(out));
"""


def _run_node_probe(module: Path, probe: str):
    """在 node 里 import 一个 TS 纯逻辑模块并跑探针（返回 `(data, error)`）。"""
    if not NODE:
        return None, "找不到 node"
    script = ("import('" + module.as_uri() + "').then(m => {" + probe + "}).catch(e => {"
              "process.stderr.write('IMPORT_ERR ' + e.message); process.exit(4); });")
    try:
        proc = subprocess.run([NODE, "--experimental-strip-types", "--input-type=module",
                               "-e", script],
                              capture_output=True, text=True, timeout=90, cwd=str(ROOT))
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"node 执行失败: {exc}"
    if proc.returncode != 0:
        return None, f"node 退出码 {proc.returncode}: {proc.stderr[:400]}"
    return json.loads(proc.stdout), ""


def _run_probe():
    if not NODE:
        return None, "找不到 node"
    script = ("import('" + MODULE.as_uri() + "').then(m => {" + _PROBE + "}).catch(e => {"
        "process.stderr.write('IMPORT_ERR ' + e.message); process.exit(4); });")
    try:
        proc = subprocess.run([NODE, "--experimental-strip-types", "--input-type=module",
                               "-e", script],
                              capture_output=True, text=True, timeout=90, cwd=str(ROOT))
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"node 执行失败: {exc}"
    if proc.returncode != 0:
        return None, f"node 退出码 {proc.returncode}: {proc.stderr[:400]}"
    return json.loads(proc.stdout), ""


class EvolutionMemoryLogicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out, cls.error = _run_probe()

    def setUp(self):
        if self.out is None:
            self.skipTest(self.error or "node 不可用")

    def _row(self, rows, key):
        return next(r for r in rows if r["key"] == key)

    def test_inventory_rows_show_every_required_counter(self):
        rows = {r["key"]: r for r in self.out["rows"]}
        for key in ("total", "enabled", "injected", "not_injected", "baseline",
                    "baseline_consistency", "reviewed_heuristics", "observations",
                    "expired", "ledger_revision", "memory_revision"):
            self.assertIn(key, rows)
        self.assertEqual(rows["injected"]["value"], "8 / 8")
        self.assertEqual(rows["baseline_consistency"]["value"], "HEALTHY")
        self.assertEqual(rows["baseline_consistency"]["tone"], "ok")
        self.assertEqual(rows["not_injected"]["value"], "1")
        self.assertEqual(rows["ledger_revision"]["value"], "abcdef123456")

    def test_missing_inventory_is_unknown_not_zero(self):
        rows = {r["key"]: r for r in self.out["rowsMissing"]}
        self.assertEqual(rows["total"]["value"], "--")
        self.assertEqual(rows["baseline_consistency"]["value"], "--")
        self.assertEqual(rows["baseline_consistency"]["tone"], "muted")

    def test_broken_baseline_is_critical(self):
        rows = {r["key"]: r for r in self.out["rowsBroken"]}
        self.assertEqual(rows["baseline_consistency"]["value"], "CRITICAL")
        self.assertEqual(rows["baseline_consistency"]["tone"], "bad")

    def test_clean_payload_has_no_alerts(self):
        self.assertEqual(self.out["alertsClean"], [])

    def test_alerts_cover_the_documented_cases(self):
        codes = {a["code"] for a in self.out["alertsBroken"]}
        for code in ("MEMORY_BASELINE_MISMATCH", "SNAPSHOT_UNOBSERVABLE_RATIO",
                     "SNAPSHOT_REUSED", "ASSET_MULTIPLIER_INVALID",
                     "LLM_REVIEW_FAILED", "RULE_PROPOSAL_REQUIRES_APPROVAL"):
            self.assertIn(code, codes)
        severity = {a["code"]: a["severity"] for a in self.out["alertsBroken"]}
        self.assertEqual(severity["MEMORY_BASELINE_MISMATCH"], "CRITICAL")

    def test_legacy_memory_raises_a_critical_alert(self):
        self.assertEqual(self.out["alertsLegacy"][0]["code"], "MEMORY_AUTHORITY_MISSING")
        self.assertEqual(self.out["alertsLegacy"][0]["severity"], "CRITICAL")

    def test_review_layers_carry_evidence_metadata(self):
        layers = {layer["key"]: layer for layer in self.out["layers"]}
        self.assertEqual(layers["facts"]["rows"][0]["sample_size"], 12)
        self.assertEqual(layers["facts"]["rows"][0]["independent_sample_groups"], 4)
        self.assertEqual(layers["hypotheses"]["rows"][0]["counterexample_count"], 3)
        self.assertTrue(layers["rule_proposals"]["rows"][0]["requires_approval"])

    def test_review_layers_fall_back_to_legacy_fields(self):
        layers = {layer["key"]: layer for layer in self.out["layersLegacy"]}
        self.assertEqual(layers["facts"]["rows"][0]["text"], "f2")
        self.assertEqual(layers["rule_proposals"]["rows"][0]["text"], "p2")

    def test_review_evidence_exposes_hashes(self):
        evidence = {row["key"]: row["value"] for row in self.out["evidence"]}
        self.assertEqual(evidence["ledger_revision"], "L")
        self.assertEqual(evidence["policy_hash"], "P")
        self.assertEqual(evidence["trusted_snapshots"], "3")
        self.assertEqual(evidence["independent_sample_groups"], "2")


class WiringTests(unittest.TestCase):
    def test_memory_page_renders_the_inventory(self):
        src = PAGE.read_text(encoding="utf-8")
        self.assertIn("evolutionMemoryLogic", src)
        self.assertIn("summarizeMemoryInventory", src)
        self.assertIn("memoryAlerts", src)

    def test_dashboard_renders_the_three_layers(self):
        src = DASH.read_text(encoding="utf-8")
        self.assertIn("evolutionMemoryLogic", src)
        self.assertIn("summarizeReviewLayers", src)
        self.assertIn("summarizeReviewEvidence", src)


if __name__ == "__main__":
    unittest.main()


class ExecutionPolicyUiTests(unittest.TestCase):
    """规划文档 §7.1：profile 执行策略的前端纯逻辑 + 与 Python 表**跨语言对拍**。"""

    @classmethod
    def setUpClass(cls):
        cls.out, cls.error = _run_node_probe(STUDIO_MODULE, _STUDIO_PROBE)

    def setUp(self):
        if self.out is None:
            self.skipTest(self.error or "node 不可用")

    def test_mode_table_matches_the_python_registry(self):
        from scripts import strategy_rules as sr
        self.assertEqual(sorted(self.out["modes"]), sorted(sr.MODE_SETUP_FAMILY))
        for mode, rule_set in self.out["modes"].items():
            self.assertEqual(rule_set, sr.RULE_SET_BY_MODE[mode], f"{mode} 的规则集与后端漂移")
            self.assertIn(rule_set, sr.RULESETS)

    def test_missing_policy_means_legacy(self):
        self.assertEqual(self.out["missing"], self.out["defaultPolicy"])
        self.assertEqual(self.out["missing"]["mode"], "legacy")

    def test_unknown_mode_never_invents_a_value(self):
        self.assertEqual(self.out["unknownMode"], self.out["defaultPolicy"])

    def test_valid_policy_derives_the_rule_set(self):
        self.assertEqual(self.out["valid"], {"mode": "trend_confirm_5m", "revision": 3,
                                             "rule_set": "trend_following@1"})

    def test_invalid_revision_falls_back_to_one(self):
        self.assertEqual(self.out["badRevision"]["revision"], 1)

    def test_patch_only_carries_mode_derived_fields(self):
        self.assertEqual(self.out["patch"], {"mode": "mean_reversion", "revision": 1,
                                            "rule_set": "trend_following@1"})

    def test_page_wires_the_policy_into_the_save_payload(self):
        page = (ROOT / "frontend" / "src" / "views" / "admin" / "PromptStudioPage.vue")
        src = page.read_text(encoding="utf-8")
        self.assertIn("executionPolicyPatch(", src)
        self.assertIn("execution_policy:", src)
        self.assertIn("EXECUTION_MODE_RULE_SETS", src)


if __name__ == "__main__":
    unittest.main()
