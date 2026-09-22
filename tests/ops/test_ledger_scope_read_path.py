"""读取侧账号范围行为门（步2）：``load_ledger_scoped`` 的端到端（文件→返回值）。

口径（2026-09 用户确认）：「当前账号」= **当前连接的交易所账号**（凭证在位 + 环境轴）。

本门钉四件事：
1. **默认隐藏**：不属于当前连接账号的行（别的账号/别的环境/未连接场所/无身份）默认不出现在 trades；
2. **开关能放出**：include_hidden=True 时原样放出（数据没被删，只是没显示）；
3. **顺序不可颠倒**：必须先按账号收窄、再截 LEDGER_TRADES_MAX —— 否则非当前账号的行会
   先吃掉 60 条上限，当前账号的行反而被挤掉；
4. **轴解析失败不清空页面**：current_accounts 为空 → 不做场所过滤。

封闭三律：ledger 文件在临时目录、autosync 显式关闭（绝不打网络、绝不动生产 data/）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from r20_backend.dashboard_payload.ledger_view import (  # noqa: E402
    LEDGER_TRADES_MAX,
    load_ledger_scoped,
)

CUR = {
    "okx": "okx:demo:aaaa11112222",
    "binance": "binance:demo:bbbb33334444",
}
RESET = "1970-01-01 00:00:00"


def _row(rid, *, venue="okx", account_id="okx:demo:aaaa11112222",
         close="2026-09-20 10:00:00"):
    row = {"id": rid, "inst": "BTC", "venue": venue, "status": "closed",
           "close_time": close, "open_time": close}
    if account_id is not None:
        row["account_id"] = account_id
    return row


class LedgerScopeReadPathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="r-scope-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        self.ledger = os.path.join(self.tmp, "trading_ledger.json")

    def _write(self, rows):
        with open(self.ledger, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)

    def _load(self, include_hidden=False, current_accounts=CUR):
        return load_ledger_scoped(
            self.ledger, self.tmp, False, RESET,
            current_accounts=current_accounts, include_hidden=include_hidden)

    def test_foreign_rows_hidden_by_default(self):
        self._write([
            _row("cur"),
            _row("other-acct", account_id="okx:demo:999988887777"),
            _row("other-env", account_id="okx:live:aaaa11112222"),
            _row("legacy", account_id=None),
            _row("gate", venue="gate", account_id="gate:demo:cccc55556666"),
            _row("bn", venue="binance", account_id="binance:demo:bbbb33334444"),
        ])
        valid, table, scope, hidden_rows = self._load()
        self.assertEqual([r["id"] for r in valid], ["cur", "bn"])
        self.assertEqual([r["id"] for r in table], ["cur", "bn"])
        self.assertEqual(scope["total"], 6)
        self.assertEqual(scope["shown"], 2)
        self.assertEqual(scope["hidden"], 4)
        self.assertEqual(scope["hidden_legacy"], 1)
        self.assertEqual(scope["hidden_foreign"], 3)
        # 开关放出的候选集 = 全部被挡下的行（不只是无身份的）
        self.assertEqual(sorted(r["id"] for r in hidden_rows),
                         ["gate", "legacy", "other-acct", "other-env"])

    def test_toggle_releases_hidden_rows_without_losing_them(self):
        self._write([
            _row("cur"),
            _row("legacy", account_id=None),
            _row("other-acct", account_id="okx:demo:999988887777"),
        ])
        valid, _table, scope, hidden_rows = self._load(include_hidden=True)
        self.assertEqual(sorted(r["id"] for r in valid),
                         ["cur", "legacy", "other-acct"])
        self.assertEqual(hidden_rows, [])
        self.assertTrue(scope["include_hidden"])

    def test_disconnected_venue_rows_are_hidden(self):
        self._write([_row("gate", venue="gate", account_id="gate:demo:ffff00001111")])
        valid, _t, scope, hidden_rows = self._load(current_accounts={"okx": CUR["okx"]})
        self.assertEqual(valid, [])
        self.assertEqual([r["id"] for r in hidden_rows], ["gate"])
        self.assertEqual(scope["hidden_foreign"], 1)

    def test_scope_narrows_before_the_60_cap(self):
        rows = [_row(f"cur{i}") for i in range(61)]
        rows += [_row(f"old{i}", account_id="okx:live:aaaa11112222") for i in range(61)]
        self._write(rows)
        valid, table, scope, _h = self._load()
        self.assertEqual(len(valid), 61)
        self.assertEqual(len(table), LEDGER_TRADES_MAX)
        self.assertTrue(all(str(r["id"]).startswith("cur") for r in table),
                        "非当前账号的行绝不允许占掉台账表格的名额")
        self.assertEqual(scope["hidden"], 61)

    def test_no_account_axis_keeps_real_rows_except_identity_less(self):
        # 账号轴解析失败（空映射）→ 不做场所过滤；无身份行仍按开关处理，不静默清空页面
        self._write([_row("cur"), _row("legacy", account_id=None)])
        valid, _t, scope, hidden_rows = self._load(current_accounts={})
        self.assertEqual([r["id"] for r in valid], ["cur"])
        self.assertFalse(scope["scoped"])
        self.assertEqual([r["id"] for r in hidden_rows], ["legacy"])


if __name__ == "__main__":
    unittest.main()
