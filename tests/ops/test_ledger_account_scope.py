"""台账账号范围过滤（步2）契约测试：纯函数，零 IO。

语义铁律（本门钉死）：
1. **非破坏性**：过滤只发生在读取侧，永不改文件；无法确知某所当前账号时**保留**其行
   （宁可多显示，不可抹事实）。
2. 行身份缺失或为 UNKNOWN_LEGACY_ACCOUNT → 默认隐藏，include_legacy=True 才放行。
3. 同所不同 account_id（换 key / 换环境 / 别的账号）→ 隐藏。
4. 行没有 venue 字段时按 okx 认定（与 db_manager 迁移「旧行缺 venue → 默认 okx」一致）。

事故背景：trading_ledger.json 是全局并集，旧行靠 id 合并永久续命，/history 于是
出现不属于当前账号的记录（见 scripts/ledger/merge.py 的合并语义）。
"""
from __future__ import annotations

import unittest

from r20_backend.dashboard_payload.account_scope import (
    filter_in_scope,
    in_scope,
    row_account_id,
    row_venue,
    scope_summary,
)
from r20_backend.exchanges.identity import UNKNOWN_LEGACY_ACCOUNT

CUR = {
    "okx": "okx:demo:aaaa11112222",
    "binance": "binance:demo:bbbb33334444",
}


def _row(**kw):
    base = {"id": "x", "inst": "BTC", "venue": "okx",
            "account_id": "okx:demo:aaaa11112222"}
    base.update(kw)
    return base


class RowIdentityTest(unittest.TestCase):
    def test_row_venue_defaults_to_okx_when_absent(self):
        self.assertEqual(row_venue({"id": "1"}), "okx")
        self.assertEqual(row_venue({"venue": "binance"}), "binance")

    def test_row_account_id_empty_when_absent(self):
        self.assertEqual(row_account_id({"id": "1"}), "")
        self.assertEqual(row_account_id({"id": "1", "account_id": None}), "")


class InScopeTest(unittest.TestCase):
    def test_current_account_row_is_in_scope(self):
        self.assertTrue(in_scope(_row(), CUR))

    def test_other_account_same_venue_is_out(self):
        # 换过 key / 换过环境 → 旧行不是「当前账号」
        self.assertFalse(in_scope(_row(account_id="okx:demo:999988887777"), CUR))

    def test_other_environment_is_out(self):
        self.assertFalse(in_scope(_row(account_id="okx:live:aaaa11112222"), CUR))

    def test_other_venue_account_is_in_scope_for_its_own_venue(self):
        self.assertTrue(in_scope(
            _row(venue="binance", account_id="binance:demo:bbbb33334444"), CUR))

    def test_venue_without_known_current_account_is_kept(self):
        # 无法确知 gate 当前账号 → 绝不隐藏（非破坏性）
        self.assertTrue(in_scope(
            _row(venue="gate", account_id="gate:demo:cccc55556666"), CUR))

    def test_missing_identity_hidden_by_default_shown_with_legacy_flag(self):
        legacy = _row(account_id=None)
        self.assertFalse(in_scope(legacy, CUR))
        self.assertTrue(in_scope(legacy, CUR, include_legacy=True))

    def test_legacy_sentinel_hidden_by_default_shown_with_flag(self):
        legacy = _row(account_id=UNKNOWN_LEGACY_ACCOUNT)
        self.assertFalse(in_scope(legacy, CUR))
        self.assertTrue(in_scope(legacy, CUR, include_legacy=True))

    def test_no_current_accounts_means_no_scoping(self):
        # 无账号轴信息 → 真实账号行全放行（不识别的环境绝不靠猜过滤）；
        # 「无身份」行仍按 legacy 开关处理（见下一条用例）
        self.assertTrue(in_scope(_row(account_id="okx:live:deadbeefcafe"), {}))
        self.assertTrue(in_scope(_row(account_id="binance:live:deadbeefcafe"), None))

    def test_legacy_still_hidden_by_default_even_without_scope(self):
        # 无账号轴时：真实账号行全放行，但「无身份」行仍按显示开关处理
        self.assertTrue(in_scope(_row(account_id="okx:live:deadbeefcafe"), None))
        self.assertFalse(in_scope(_row(account_id=None), None))
        self.assertTrue(in_scope(_row(account_id=None), None, include_legacy=True))


class FilterTest(unittest.TestCase):
    def setUp(self):
        self.rows = [
            _row(id="cur"),
            _row(id="other-acct", account_id="okx:demo:999988887777"),
            _row(id="other-env", account_id="okx:live:aaaa11112222"),
            _row(id="legacy", account_id=None),
            _row(id="gate", venue="gate", account_id="gate:demo:cccc55556666"),
        ]

    def test_filter_keeps_current_and_unresolved_venue(self):
        ids = [r["id"] for r in filter_in_scope(self.rows, CUR)]
        self.assertEqual(ids, ["cur", "gate"])

    def test_include_legacy_adds_back_identity_less_rows(self):
        ids = [r["id"] for r in filter_in_scope(self.rows, CUR, include_legacy=True)]
        self.assertEqual(ids, ["cur", "legacy", "gate"])

    def test_filter_never_mutates_input(self):
        before = [dict(r) for r in self.rows]
        filter_in_scope(self.rows, CUR)
        self.assertEqual(self.rows, before)

    def test_scope_summary_counts_hidden(self):
        s = scope_summary(self.rows, CUR)
        self.assertEqual(s["total"], 5)
        self.assertEqual(s["shown"], 2)
        self.assertEqual(s["hidden"], 3)
        self.assertEqual(s["hidden_legacy"], 1)
        self.assertFalse(s["include_legacy"])

    def test_scope_summary_with_legacy(self):
        s = scope_summary(self.rows, CUR, include_legacy=True)
        self.assertEqual(s["shown"], 3)
        self.assertEqual(s["hidden"], 2)
        self.assertEqual(s["hidden_legacy"], 0)
        self.assertTrue(s["include_legacy"])


if __name__ == "__main__":
    unittest.main()
