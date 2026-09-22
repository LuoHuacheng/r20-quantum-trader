r"""order_lifecycle 抽取对拍门（结构优化阶段 4·B3 第八十四刀）。

`clean_stale_open_orders`(132) / `reconcile_pending_orders`(83) 从
`scripts/ai_factor_trader.py` **纯搬家**到 `scripts/trader/order_lifecycle.py`。

本域两函数各带**嵌套闭包**（`_intent_covers` / `_cancel_orphan`）——本门钉：
① 闭包随整体搬迁、函数体（含嵌套 def）AST **零例外全等**；
② 壳同名注入形状；
③ **`_BROKEN_VENUES` 引用语义**（既有测试只断言 ok/日志，未断言集合被改到
   —— 若注入的是副本，凭证坏所的"本轮摘除执行资格"会静默失效）；
④ fail-closed 与 reason 文案经门面常量注入后仍生效；
⑤ 判据自检（±双向）。
"""
from __future__ import annotations

import ast
import io
import subprocess
import sys
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parent))   # 同级黄金快照工具
from _verbatim_golden import load as _golden_load, record_mode as _golden_recording, save as _golden_save  # noqa: E402

GOLDEN_NAME = "order_lifecycle"
FNS = ("clean_stale_open_orders", "reconcile_pending_orders")


def _get_func(tree: ast.Module, name: str) -> ast.FunctionDef:
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"{name} 不在顶层")


def _body_dump(fn: ast.FunctionDef) -> str:
    return ast.dump(ast.Module(body=fn.body, type_ignores=[]), include_attributes=False)


class OrderLifecycleVerbatimTest(unittest.TestCase):
    def test_moved_bodies_match_recorded_baseline(self):
        """含嵌套闭包的函数体必须与**录制基线**逐字（零归一规则）。

        基线原本是 `git show 9dccec0:scripts/ai_factor_trader.py`（搬运前文件）。但函数
        搬走后被后续提交有意改过（aa6d4e0 等给对账链路加了多所形态兜底），one-shot
        基线不可能再成立 —— 改为录制式快照（见 tests/extraction/_verbatim_golden.py）。
        """
        new = ast.parse((ROOT / "scripts/trader/order_lifecycle.py").read_text(encoding="utf-8"))
        current = {fn: {"args": [a.arg for a in _get_func(new, fn).args.args],
                        "body": _body_dump(_get_func(new, fn))} for fn in FNS}
        if _golden_recording():
            _golden_save(GOLDEN_NAME, current)
            self.skipTest(f"已重录 {GOLDEN_NAME} 黄金快照")
        gold = _golden_load(GOLDEN_NAME)
        for fn in FNS:
            with self.subTest(fn=fn):
                n = _get_func(new, fn)
                self.assertIn(fn, gold, f"{GOLDEN_NAME} 快照缺 {fn}（请重录）")
                self.assertEqual(gold[fn]["args"], current[fn]["args"],
                                 f"{fn} 形参表变了——抽取注入形状被改动")
                self.assertEqual(gold[fn]["body"], current[fn]["body"],
                                 f"{fn} 与录制基线**不再是同一实现**")
                # 嵌套闭包必须**还在**（整体随迁，不是被外提）
                nested = {x.name for x in ast.walk(n) if isinstance(x, ast.FunctionDef)}
                self.assertTrue(nested - {fn}, f"{fn} 的嵌套闭包没随迁")

    def test_shells_are_def_with_lazy_same_name_injection(self):
        tree = ast.parse((ROOT / "scripts/ai_factor_trader.py").read_text(encoding="utf-8"))
        want = {
            "clean_stale_open_orders": ("load_open_intents", "OPEN_INTENT_TTL_MS",
                                        "_BROKEN_VENUES", "current_environment",
                                        "load_instruments", "okx_rest", "venue_registry"),
            "reconcile_pending_orders": ("_order_pos_side", "load_open_intents",
                                         "load_trackers", "OPEN_INTENT_TTL_MS",
                                         "RECONCILE_REASON_SIDE_MISMATCH",
                                         "RECONCILE_REASON_INTENT_STALE",
                                         "RECONCILE_REASON_ORPHAN", "okx_rest"),
        }
        facade = set(dir(__import__("scripts.ai_factor_trader", fromlist=["x"])))
        for fn, names in want.items():
            with self.subTest(fn=fn):
                dumped = ast.unparse(_get_func(tree, fn))
                self.assertIn("_order_lifecycle_", dumped, "壳没转调子包")
                for g in names:
                    self.assertIn(f"{g}={g}", dumped, f"壳缺同名注入 {g}")
                    self.assertIn(g, facade, f"{g} 不是门面全局 ⇒ 壳传参必 NameError")

    def test_broken_venues_reference_semantics_survive_injection(self):
        """**本刀最易静默失效的一条**：凭证坏所必须被记进**调用方那个**集合。

        既有 batch6 用例只断言"跳过而非拦轮"，没断言集合被改到 ——
        注入若传成副本，`venue_execution_ready` 就永远看不到坏所，
        本轮路由会继续往外所派单（真金白银的口子）。
        """
        import scripts.ai_factor_trader as aft
        gate_err = type("GateAPIError", (Exception,), {})("Gate INVALID_KEY: Invalid key provided")
        gate = type("G", (), {
            "list_open_orders": lambda self, b: (_ for _ in ()).throw(gate_err)})()
        okx = types.SimpleNamespace(pending_orders=lambda: [], cancel_order=lambda *a: None)
        broken = set()   # ← 我们自己持有引用
        with patch.object(aft, "_BROKEN_VENUES", broken), \
             patch.object(aft, "okx_rest", okx), \
             patch.object(aft, "current_environment",
                          lambda: types.SimpleNamespace(mode="demo")), \
             patch.object(aft.venue_registry, "execution_open", lambda v, e: v == "gate"), \
             patch.object(aft.venue_registry, "get_adapter",
                          lambda v, environment=None: gate), \
             patch.object(aft, "load_instruments", lambda: [{"instId": "BTC-USDT-SWAP"}]), \
             redirect_stdout(io.StringIO()):
            ok, msg = aft.clean_stale_open_orders()
        self.assertTrue(ok, f"凭证坏所应跳过而非拦轮: {msg}")
        self.assertIn("gate", broken,
                      "注入的是集合副本 ⇒ 坏所没被登记，路由仍会派单到该所")

    def test_reconcile_reacts_to_facade_patches_and_fails_closed(self):
        """孤儿单撤销 + reason 文案（门面常量注入）+ 撤销失败 fail-closed。"""
        import scripts.ai_factor_trader as aft
        calls = []
        okx = types.SimpleNamespace(
            pending_orders=lambda: [{"instId": "BTC-USDT-SWAP", "ordId": "o1",
                                     "side": "buy", "state": "live"}],
            cancel_order=lambda inst, oid: calls.append((inst, oid)))
        with patch.object(aft, "okx_rest", okx), \
             patch.object(aft, "load_trackers", lambda: {}), \
             patch.object(aft, "load_open_intents", lambda: []), \
             patch.object(aft, "RECONCILE_REASON_ORPHAN", "测试原因-孤儿"), \
             redirect_stdout(io.StringIO()) as buf:
            ok, kept = aft.reconcile_pending_orders()
        self.assertTrue(ok)
        self.assertEqual(calls, [("BTC-USDT-SWAP", "o1")], "孤儿单没被撤销")
        self.assertEqual(kept, set())
        self.assertIn("测试原因-孤儿", buf.getvalue(),
                      "reason 文案没走门面注入（patch 面断了）")

        boom = types.SimpleNamespace(
            pending_orders=lambda: [{"instId": "BTC-USDT-SWAP", "ordId": "o1",
                                     "side": "buy", "state": "live"}],
            cancel_order=lambda *a: (_ for _ in ()).throw(RuntimeError("net down")))
        with patch.object(aft, "okx_rest", boom), \
             patch.object(aft, "load_trackers", lambda: {}), \
             patch.object(aft, "load_open_intents", lambda: []), \
             redirect_stdout(io.StringIO()):
            ok2, _ = aft.reconcile_pending_orders()
        self.assertFalse(ok2, "撤销失败必须 fail-closed")

    def test_judgment_actually_notices_a_change(self):
        base = "def f():\n    def g():\n        return 1\n    return g()\n"
        tampered = "def f():\n    def g():\n        return 2\n    return g()\n"
        o = _body_dump(_get_func(ast.parse(base), "f"))
        self.assertNotEqual(o, _body_dump(_get_func(ast.parse(tampered), "f")),
                            "自检：嵌套闭包内的改动看不见")
        self.assertEqual(o, _body_dump(_get_func(ast.parse(base), "f")),
                         "自检：同文误报")


if __name__ == "__main__":
    unittest.main()
