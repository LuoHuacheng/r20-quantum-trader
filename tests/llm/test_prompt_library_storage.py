"""提示词方案库存储层契约 —— A / B / C 三案的落地钉子（见 docs/PROMPT_LIBRARY_STORAGE.md）。

## 三案的契约各是什么

- **A①（切换只改指针）**：`activate_profile` 之后，`active_profile_id` 变了，**其它方案的
  任何字段逐字节未变**，文件其余字节也未变。旧实现走「读整库 → 全库规范化 → 写回」，
  实测切换一次改掉 3 个字段（stable 扁平文本 298 → 6490），git diff 看起来像策略被换了。
- **A②（未改动的方案不改写）**：任何一次保存都只规范化**真正被改的那个**方案 ——
  扁平文本是派生缓存（`resolve_profile` 读取时无条件用 `compile_modules` 覆盖），
  代码基座一变，磁盘上的旧缓存就会被判成「已改动」进而被整库重算（写放大）。
- **B（一方案一文件）**：`index.json` + `<pid>.json`（方案本体 + 该方案历史）。
  切换只动 index；改一个方案只动那一个文件；删方案连带清掉它的文件与孤儿历史；
  旧单文件仍可读（迁移在其上叠加，不删不改）。
- **C（退出版本库）**：运行时可变状态的方案库不再被 git 跟踪（静态规则 + 不跟踪断言）。

## 与「零行为影响」的关系

存储层的形状变化**不得**改变任何渲染结果：`resolve_profile` / `active_profile` /
布局指纹在各布局下必须一致 —— 这条比 diff 外观更重要，故单独钉住。
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]

import scripts.prompt_library as pl  # noqa: E402

STALE = "STALE-FLAT-CACHE"


def _modules(pipeline: str) -> list[dict]:
    return [
        {"id": "custom-x1", "title": f"{pipeline}-自定义风格", "content": "只做顺势，证据不足则等待。",
         "enabled": True, "locked": False, "source": "custom"},
        {"id": "base-x2", "title": f"{pipeline}-任务", "content": "{{market_matrix}}",
         "enabled": True, "locked": False, "source": "base"},
    ]


def _raw_profile(pid: str, name: str) -> dict:
    """**未经规范化**的方案原文：四类扁平文本故意留成陈旧缓存（复现 §3.2 的漂移）。"""
    return {
        "id": pid, "name": name, "description": "", "editable": True, "enabled": True,
        "created_at": "2026-01-01 00:00:00", "updated_at": "2026-01-01 00:00:00",
        "editor_mode": "modules",
        "simple_policy": {"strategy": "", "review_focus": "", "participation": "balanced",
                          "evidence": "strict", "risk_budget": "middle"},
        "pipelines": {key: _modules(key) for key in pl.TEMPLATE_KEYS},
        **{key: STALE for key in pl.TEMPLATE_KEYS},
    }


def _raw_library() -> dict:
    return {"version": 2, "active_profile_id": "stable",
            "profiles": {"stable": _raw_profile("stable", "稳健"),
                         "alt": _raw_profile("alt", "备选")},
            "revisions": []}


def _digests() -> dict:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(pl.library_dir().glob("*.json"))}


def _changed_lines(before: str, after: str) -> list[str]:
    diff = difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
    return [line for line in diff if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]


class _Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.library = self.root / "prompt_library.json"
        p = patch.object(pl, "LIBRARY_FILE", self.library)
        p.start()
        self.addCleanup(p.stop)

    def _seed_legacy(self, payload: dict | None = None) -> str:
        text = json.dumps(payload if payload is not None else _raw_library(), ensure_ascii=False, indent=2) + "\n"
        self.library.write_text(text, encoding="utf-8")
        return text

    def _stored_profile(self, pid: str) -> dict:
        """目录存储里某方案的原文（迁移后的真源；旧单文件此时已冻结）。"""
        return json.loads((pl.library_dir() / f"{pid}.json").read_text(encoding="utf-8"))["profile"]


class ActivationPointerOnlyTests(_Sandbox):
    """A①：切换方案只改指针，不碰任何方案内容。"""

    def test_only_the_pointer_line_changes(self):
        before = self._seed_legacy()
        pl.activate_profile("alt")
        after = self.library.read_text(encoding="utf-8")
        changed = _changed_lines(before, after)
        self.assertEqual(len(changed), 2, f"切换应只产生「改指针」一行 diff，实测：{changed}")
        self.assertTrue(all("active_profile_id" in line for line in changed), changed)
        self.assertIn("alt", after)

    def test_every_other_profile_field_is_byte_identical(self):
        before = json.loads(self._seed_legacy())
        pl.activate_profile("alt")
        after = json.loads(self.library.read_text(encoding="utf-8"))
        self.assertEqual(after["active_profile_id"], "alt")
        self.assertEqual(after["profiles"], before["profiles"], "切换方案改写了无关方案的内容")
        for key in pl.TEMPLATE_KEYS:
            self.assertEqual(after["profiles"]["stable"][key], STALE, "陈旧扁平缓存被重算写回")

    def test_the_activation_is_visible_through_the_api(self):
        self._seed_legacy()
        pl.activate_profile("alt")
        self.assertEqual(pl.load_library()["active_profile_id"], "alt")
        self.assertEqual(pl.active_profile()["id"], "alt")
        self.assertEqual([p["id"] for p in pl.all_profiles()], ["stable", "wide_oscillation", "alt"])

    def test_an_unknown_or_disabled_profile_is_still_refused(self):
        self._seed_legacy()
        with self.assertRaises(ValueError):
            pl.activate_profile("nope")
        library = pl.load_library()
        library["profiles"]["alt"]["enabled"] = False
        pl.save_library(library)
        with self.assertRaises(ValueError) as ctx:
            pl.activate_profile("alt")
        self.assertIn("已停用", str(ctx.exception))

    def test_a_v1_library_falls_back_to_the_full_write_path(self):
        """v1 单方案文件没有 `profiles` 键，轻量写不可用 ⇒ 必须走整库迁移写路径。"""
        legacy = json.dumps({"version": 1, "active_style": "custom",
                             "custom": {"trading_system": "OLD"}}, ensure_ascii=False)
        self.library.write_text(legacy, encoding="utf-8")
        pl.activate_profile("stable")
        self.assertEqual(self.library.read_text(encoding="utf-8"), legacy, "整库迁移不应改写旧 v1 文件")
        migrated = pl.load_library()
        self.assertEqual(migrated["version"], 2)
        self.assertEqual(migrated["active_profile_id"], "stable")
        self.assertEqual(migrated["profiles"]["custom-default"]["trading_system"], "OLD")

    def test_zero_behaviour_change_for_every_rendered_template(self):
        """切换前后，每个方案的渲染结果逐字节一致（「零行为影响」不是推理，是被测住的）。"""
        self._seed_legacy()
        before = {pid: pl.resolve_profile(prof)
                  for pid, prof in pl.load_library()["profiles"].items()}
        pl.activate_profile("alt")
        after = {pid: pl.resolve_profile(prof)
                 for pid, prof in pl.load_library()["profiles"].items()}
        self.assertEqual(before, after)
        for pid, resolved in after.items():
            for key in pl.TEMPLATE_KEYS:
                self.assertNotEqual(resolved[key], STALE, f"{pid}.{key} 未按管线重算")


class UnrelatedSaveKeepsStoredProfileTests(_Sandbox):
    """A②：只规范化真正被改的方案。"""

    def test_creating_a_profile_does_not_refresh_other_flat_caches(self):
        self._seed_legacy()
        created = pl.create_profile("新方案", source_id="stable")
        legacy = json.loads(self.library.read_text(encoding="utf-8"))
        self.assertEqual(legacy["profiles"]["stable"][pl.TEMPLATE_KEYS[0]], STALE)
        self.assertEqual(self._stored_profile("stable")[pl.TEMPLATE_KEYS[0]], STALE,
                         "未改动的方案被按当前代码基座重算写回（写放大）")
        self.assertTrue((pl.library_dir() / f"{created['id']}.json").is_file())

    def test_updating_one_profile_leaves_the_others_untouched(self):
        self._seed_legacy()
        pl.save_library(pl.load_library())
        stored = self._stored_profile("stable")
        pl.update_profile("alt", {"name": "改名了"})
        self.assertEqual(self._stored_profile("stable"), stored, "更新 A 方案改写了 B 方案")
        self.assertEqual(self._stored_profile("alt")["name"], "改名了")

    def test_a_real_change_is_still_normalised(self):
        """A② 不能把「真改动」也沿用磁盘旧缓存 —— 改了管线就必须重算扁平文本。"""
        self._seed_legacy()
        pl.save_library(pl.load_library())
        modules = copy.deepcopy(pl.load_library()["profiles"]["alt"]["pipelines"]["trading_system"])
        modules[0]["content"] = "改成完全不同的内容"
        updated = pl.update_profile("alt", {"pipelines": {"trading_system": modules}})
        self.assertIn("改成完全不同的内容", updated["trading_system"])


class StoreLayoutTests(_Sandbox):
    """B：`index.json` + 一方案一文件。"""

    def test_a_save_splits_the_library_into_one_file_per_profile(self):
        self._seed_legacy()
        pl.save_library(pl.load_library())
        self.assertTrue(pl.store_index_file().is_file())
        self.assertEqual(sorted(_digests()), ["alt.json", "index.json", "stable.json"])
        self.assertEqual(json.loads(pl.store_index_file().read_text(encoding="utf-8"))["version"],
                         pl.STORE_VERSION)

    def test_the_legacy_single_file_is_left_alone(self):
        """迁移不删不改旧单文件：混版本进程仍读得到旧库，人工也能回滚。"""
        before = self._seed_legacy()
        pid = pl.create_profile("新方案")["id"]
        self.assertTrue(self.library.is_file())
        self.assertEqual(self.library.read_text(encoding="utf-8"), before,
                         "目录存储落地后仍在改写旧单文件")
        self.assertTrue((pl.library_dir() / f"{pid}.json").is_file())

    def test_switching_touches_only_the_index(self):
        self._seed_legacy()
        pl.save_library(pl.load_library())
        before = _digests()
        pl.activate_profile("alt")
        after = _digests()
        self.assertEqual([name for name in after if after[name] != before.get(name)], ["index.json"])

    def test_editing_one_profile_touches_only_that_file(self):
        self._seed_legacy()
        pl.save_library(pl.load_library())
        before = _digests()
        pl.update_profile("alt", {"name": "只改这一个"})
        after = _digests()
        self.assertEqual([name for name in after if after[name] != before.get(name)], ["alt.json"])

    def test_the_store_reads_back_identically_to_the_legacy_file(self):
        """同一份方案库，两种落盘布局必须读出**完全相同**的内存整库。"""
        payload = pl.load_library()
        payload["profiles"]["alt"] = pl._clean_profile(_raw_profile("alt", "备选"), "alt")
        with tempfile.TemporaryDirectory() as other:
            legacy_file = Path(other) / "prompt_library.json"
            legacy_file.write_text(json.dumps({k: v for k, v in payload.items()
                                               if k in ("version", "active_profile_id", "profiles", "revisions")},
                                              ensure_ascii=False), encoding="utf-8")
            with patch.object(pl, "LIBRARY_FILE", legacy_file):
                from_legacy = pl.load_library()
        pl.save_library(payload)
        self.assertEqual(pl.load_library(), from_legacy)

    def test_migration_drops_only_the_orphan_history(self):
        """线上那份旧库里真有一条孤儿历史（`create custom-1768944f2c`，方案已删，见文档 §4）。

        迁移的唯一内容差异就是它：方案本体、active 指针、其余历史必须逐一字不变。
        """
        payload = _raw_library()
        payload["revisions"] = [
            {"id": "rev-keep", "profile_id": "stable", "action": "create", "note": "",
             "created_at": "2026-01-01 00:00:01", "snapshot": payload["profiles"]["stable"]},
            {"id": "rev-orphan", "profile_id": "ghost", "action": "create", "note": "",
             "created_at": "2026-01-01 00:00:02", "snapshot": {}},
        ]
        self._seed_legacy(payload)
        legacy_read = pl.load_library()
        pl.save_library(legacy_read)                      # 迁移写盘
        store_read = pl.load_library()
        self.assertEqual([item["id"] for item in store_read["revisions"]], ["rev-keep"])
        self.assertEqual({k: v for k, v in store_read.items() if k != "revisions"},
                         {k: v for k, v in legacy_read.items() if k != "revisions"},
                         "迁移除孤儿历史外还改了别的东西")

    def test_history_round_trips_and_stays_newest_first(self):
        self._seed_legacy()
        pl.save_library(pl.load_library())
        pid = pl.create_profile("带历史", source_id="stable")["id"]
        pl.update_profile(pid, {"description": "第二次"}, note="第二次")
        actions = [item["action"] for item in pl.profile_history(pid)]
        self.assertEqual(actions, ["update", "create"])
        self.assertEqual(pl.profile_history(pid)[0]["snapshot"]["description"], "第二次")

    def test_deleting_a_profile_removes_its_file_and_orphan_history(self):
        self._seed_legacy()
        library = pl.load_library()
        library["revisions"].append({"id": "rev-orphan", "profile_id": "ghost", "action": "create",
                                     "note": "", "created_at": pl._now(), "snapshot": {}})
        pl.save_library(library)
        pid = pl.create_profile("待删")["id"]
        pl.delete_profile(pid)
        self.assertFalse((pl.library_dir() / f"{pid}.json").exists(), "已删方案的文件仍在（下次 load 会复活）")
        reloaded = pl.load_library()
        self.assertNotIn(pid, reloaded["profiles"])
        self.assertEqual([r for r in reloaded["revisions"] if r["profile_id"] in (pid, "ghost")], [],
                         "孤儿历史仍在库中")

    def test_a_corrupt_profile_file_does_not_break_the_library(self):
        self._seed_legacy()
        pl.save_library(pl.load_library())
        (pl.library_dir() / "alt.json").write_text("{ 不是 JSON", encoding="utf-8")
        library = pl.load_library()
        self.assertIn("stable", library["profiles"])
        self.assertNotIn("alt", library["profiles"])

    def test_a_missing_index_falls_back_to_the_legacy_file(self):
        """索引写失败（崩溃窗口）时，旧单文件仍在 ⇒ 整库仍可读，不会退化成默认。"""
        self._seed_legacy()
        pl.save_library(pl.load_library())
        pl.store_index_file().unlink()
        self.assertIn("alt", pl.load_library()["profiles"])

    def test_a_path_unsafe_profile_id_is_refused(self):
        self._seed_legacy()
        library = pl.load_library()
        library["profiles"]["../escape"] = {**library["profiles"]["alt"], "id": "../escape"}
        with self.assertRaises(ValueError) as ctx:
            pl.save_library(library)
        self.assertIn("非法字符", str(ctx.exception))


class PolicyRoundTripTests(_Sandbox):
    """§7 的连带改造项：采集/恢复、指纹必须与布局无关。"""

    def test_capture_then_restore_keeps_the_library(self):
        """策略归档「采集 → 恢复」的闭环：快照里存的是 load_library 的整库，回写走 save_library。"""
        self._seed_legacy()
        pid = pl.create_profile("被归档的方案", source_id="stable")["id"]
        captured = pl.load_library()
        pl.delete_profile(pid) if pid != captured["active_profile_id"] else None
        self.assertNotIn(pid, pl.load_library()["profiles"])
        pl.save_library(captured)                                   # = policy/restore.py 的动作
        restored = pl.load_library()
        self.assertEqual(restored, captured)

    def test_an_old_archive_payload_without_profiles_is_accepted(self):
        self._seed_legacy()
        pl.save_library({"version": 2, "active_profile_id": "stable",
                         "profiles": {"alt": _raw_profile("alt", "备选")}, "revisions": []})
        self.assertIn("alt", pl.load_library()["profiles"])

    def test_layout_hash_does_not_drift_with_the_storage_layout(self):
        from r20_backend.policy.fingerprints import compute_layout_hash
        self._seed_legacy()
        before = compute_layout_hash(pl.active_profile())
        pl.save_library(pl.load_library())
        self.assertEqual(compute_layout_hash(pl.active_profile()), before,
                         "存储布局变化让布局指纹漂移 ⇒ 历史策略快照全部失配")


class RuntimeStateIsNotTrackedTests(unittest.TestCase):
    """C：运行时可变状态退出 git（静态规则 + 跟踪状态）。"""

    def test_gitignore_covers_the_store_and_the_legacy_file(self):
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        rules = {line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")}
        self.assertIn("data/prompt_profiles/", rules, "目录存储未忽略 ⇒ 每次方案变更都脏工作区")
        self.assertIn("data/prompt_library.json", rules, "旧单文件未按运行时状态忽略")
        self.assertNotIn("!data/prompt_library.json", rules, "例外跟踪规则必须撤掉（C 的判据）")

    def test_the_library_is_not_tracked_by_git(self):
        from tests.config_sandbox import skip_if_offline_suite
        skip_if_offline_suite(self, "判据需要 git 子进程，离线护栏按约定拦子进程")
        done = subprocess.run(["git", "ls-files", "data/prompt_library.json"],
                              cwd=str(ROOT), capture_output=True, text=True, timeout=10)
        self.assertEqual(done.stdout.strip(), "", "方案库仍被 git 跟踪（C 未落地）")


if __name__ == "__main__":
    unittest.main()
