"""读取侧账号范围行为门（步2）：``load_ledger_scoped`` 的端到端（文件→返回值）。

事故背景：trading_ledger.json 是全局并集，换 key/换环境后旧行永久留存，
/history 于是出现不属于当前账号的成交（见 scripts/ledger/merge.py 的合并语义）。

本门钉三件事：
1. **默认隐藏**：无账号归属的旧行（回填 UNKNOWN_LEGACY_ACCOUNT）默认不出现在 trades；
2. **开关能放出**：include_legacy=True 时原样放出（数据没被删，只是没显示）；
3. **顺序不可颠倒**：必须先按账号收窄、再截 LEDGER_TRADES_MAX —— 否则非当前账号
   的行会先吃掉 60 条上限，当前账号的行反而被挤掉（这是最难从代码上一眼看出的坑）。

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

    def _load(self, include_legacy=False, current_accounts=CUR):
        return load_ledger_scoped(
            self.ledger, self.tmp, False, RESET,
            current_accounts=current_accounts, include_legacy=include_legacy)

    def test_foreign_rows_hidden_by_default(self):
        self._write([
            _row("cur"),
            _row("other-acct", account_id="okx:demo:999988887777"),
            _row("other-env", account_id="okx:live:aaaa11112222"),
            _row("legacy", account_id=None),
        ])
        valid, table, scope, legacy_rows = self._load()
        self.assertEqual([r["id"] for r in valid], ["cur"])
        self.assertEqual([r["id"] for r in table], ["cur"])
        self.assertEqual(scope["total"], 4)
        self.assertEqual(scope["shown"], 1)
        self.assertEqual(scope["hidden"], 3)
        self.assertEqual(scope["hidden_legacy"], 1)
        self.assertEqual([r["id"] for r in legacy_rows], ["legacy"])

    def test_toggle_releases_legacy_rows_without_losing_them(self):
        self._write([
            _row("cur"),
            _row("legacy", account_id=None),
            _row("other-acct", account_id="okx:demo:999988887777"),
        ])
        valid, _table, scope, legacy_rows = self._load(include_legacy=True)
        self.assertEqual(sorted(r["id"] for r in valid), ["cur", "legacy"])
        self.assertEqual(legacy_rows, [])
        self.assertTrue(scope["include_legacy"])

    def test_unresolved_venue_is_kept_not_hidden(self):
        # binance 不在 current_accounts（未配置/取数失败）→ 其行一律保留，绝不靠猜隐藏
        self._write([_row("bn", venue="binance", account_id="binance:demo:ffff00001111")])
        valid, _t, _s, _l = self._load(current_accounts={"okx": CUR["okx"]})
        self.assertEqual([r["id"] for r in valid], ["bn"])

    def test_scope_narrows_before_the_60_cap(self):
        # 61 条非当前账号 + 61 条当前账号：先收窄再截断 → 表格必须是 60 条当前账号行
        rows = [_row(f"cur{i}") for i in range(61)]
        rows += [_row(f"old{i}", account_id="okx:live:aaaa11112222") for i in range(61)]
        self._write(rows)
        valid, table, scope, _l = self._load()
        self.assertEqual(len(valid), 61)
        self.assertEqual(len(table), LEDGER_TRADES_MAX)
        self.assertTrue(all(str(r["id"]).startswith("cur") for r in table),
                        "非当前账号的行绝不允许占掉台账表格的名额")
        self.assertEqual(scope["hidden"], 61)

    def test_no_account_axis_keeps_everything_except_legacy(self):
        # 账号轴解析失败（空映射）→ 真实账号行全放行；无身份行仍按开关处理
        self._write([_row("cur"), _row("legacy", account_id=None)])
        valid, _t, scope, legacy_rows = self._load(current_accounts={})
        self.assertEqual([r["id"] for r in valid], ["cur"])
        self.assertFalse(scope["scoped"])
        self.assertEqual([r["id"] for r in legacy_rows], ["legacy"])


if __name__ == "__main__":
    unittest.main()
