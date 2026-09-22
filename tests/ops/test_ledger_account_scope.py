"""台账账号范围过滤（步2）契约测试：纯函数，零 IO。

## 口径（2026-09 用户确认）

「当前账号」= **当前连接的交易所账号** —— 即凭证在位、能取到数的 (venue, environment)
三元身份。据此：

1. **非破坏性**：过滤只发生在读取侧，永不改文件。
2. 行身份缺失或为 UNKNOWN_LEGACY_ACCOUNT → 默认隐藏，include_hidden=True 才放行。
3. 同所不同 account_id（换 key / 换环境）→ 隐藏。
4. **该所未连接（没有该所凭证）→ 它的行不是当前账号，隐藏**。
5. **唯独**整条账号轴解析不出来（映射为空）时不做场所过滤 —— 那是"问不到"，
   不是"没有账号"，绝不能让密钥库读失败把页面清空。
6. 行没有 venue 字段时按 okx 认定（与 db_manager 迁移「旧行缺 venue → 默认 okx」一致）。

事故背景：trading_ledger.json 是全局并集，旧行靠 id 合并永久续命，/history 于是
出现不属于当前连接账号的记录（见 scripts/ledger/merge.py 的合并语义）。
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
        self.assertFalse(in_scope(_row(account_id="okx:demo:999988887777"), CUR))

    def test_other_environment_is_out(self):
        self.assertFalse(in_scope(_row(account_id="okx:live:aaaa11112222"), CUR))

    def test_other_connected_venue_account_is_in_scope_for_its_own_venue(self):
        self.assertTrue(in_scope(
            _row(venue="binance", account_id="binance:demo:bbbb33334444"), CUR))

    def test_disconnected_venue_is_out_of_scope(self):
        # 口径：gate 没有凭证 = 未连接 = 不是当前账号 → 隐藏（非破坏性，开关可放出）
        gate = _row(venue="gate", account_id="gate:demo:cccc55556666")
        self.assertFalse(in_scope(gate, CUR))
        self.assertTrue(in_scope(gate, CUR, include_hidden=True))

    def test_missing_identity_hidden_by_default_shown_with_flag(self):
        legacy = _row(account_id=None)
        self.assertFalse(in_scope(legacy, CUR))
        self.assertTrue(in_scope(legacy, CUR, include_hidden=True))

    def test_legacy_sentinel_hidden_by_default_shown_with_flag(self):
        legacy = _row(account_id=UNKNOWN_LEGACY_ACCOUNT)
        self.assertFalse(in_scope(legacy, CUR))
        self.assertTrue(in_scope(legacy, CUR, include_hidden=True))

    def test_no_current_accounts_means_no_venue_scoping(self):
        # 账号轴解析不出来（{} / None）→ 真实账号行全放行；无身份行仍按开关
        self.assertTrue(in_scope(_row(account_id="okx:live:deadbeefcafe"), {}))
        self.assertTrue(in_scope(_row(account_id="binance:live:deadbeefcafe"), None))
        self.assertFalse(in_scope(_row(account_id=None), None))
        self.assertTrue(in_scope(_row(account_id=None), None, include_hidden=True))

    def test_include_hidden_releases_everything(self):
        for t in (_row(), _row(account_id="okx:live:aaaa11112222"),
                  _row(venue="gate", account_id="gate:demo:cccc55556666"),
                  _row(account_id=None)):
            with self.subTest(row=t):
                self.assertTrue(in_scope(t, CUR, include_hidden=True))


class FilterTest(unittest.TestCase):
    def setUp(self):
        self.rows = [
            _row(id="cur"),
            _row(id="other-acct", account_id="okx:demo:999988887777"),
            _row(id="other-env", account_id="okx:live:aaaa11112222"),
            _row(id="legacy", account_id=None),
            _row(id="gate", venue="gate", account_id="gate:demo:cccc55556666"),
            _row(id="bn", venue="binance", account_id="binance:demo:bbbb33334444"),
        ]

    def test_filter_keeps_only_current_accounts(self):
        ids = [r["id"] for r in filter_in_scope(self.rows, CUR)]
        self.assertEqual(ids, ["cur", "bn"])

    def test_include_hidden_releases_all_other_rows(self):
        ids = [r["id"] for r in filter_in_scope(self.rows, CUR, include_hidden=True)]
        self.assertEqual(ids, ["cur", "other-acct", "other-env", "legacy", "gate", "bn"])

    def test_filter_never_mutates_input(self):
        before = [dict(r) for r in self.rows]
        filter_in_scope(self.rows, CUR)
        self.assertEqual(self.rows, before)

    def test_scope_summary_counts_hidden_and_splits_reason(self):
        s = scope_summary(self.rows, CUR)
        self.assertEqual(s["total"], 6)
        self.assertEqual(s["shown"], 2)
        self.assertEqual(s["hidden"], 4)
        self.assertEqual(s["hidden_legacy"], 1)     # 无身份
        self.assertEqual(s["hidden_foreign"], 3)    # 别的账号/环境/未连接场所
        self.assertFalse(s["include_hidden"])

    def test_scope_summary_with_hidden_released(self):
        s = scope_summary(self.rows, CUR, include_hidden=True)
        self.assertEqual(s["shown"], 6)
        self.assertEqual(s["hidden"], 0)
        self.assertEqual(s["hidden_legacy"], 0)
        self.assertEqual(s["hidden_foreign"], 0)
        self.assertTrue(s["include_hidden"])


if __name__ == "__main__":
    unittest.main()
