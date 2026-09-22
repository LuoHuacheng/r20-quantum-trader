"""台账账号身份（步1）契约测试：account_id 纯函数 + 遗留哨兵。

事故背景：trading_ledger.json 是**全局并集**，行身份只有 venue/environment，
没有账号维度；换 key / 换环境后旧行永久留存，/history 于是出现「不属于当前账号」
的记录（见 tests/ops/test_ledger_history_union_whitelist 的并集语义）。

本门只钉**纯函数**：
- account_id 三元（venue/environment/凭证指纹）串，形如 `venue:env:<fp12>`；
- 未配置凭证走 ANON 哨兵指纹，**绝不冒充已认证账户**；
- 遗留哨兵 UNKNOWN_LEGACY_ACCOUNT 显式标「无账号维度」的历史行。

封闭三律：零 IO、零网络、零生产文件（本模块全纯函数）。
"""
from __future__ import annotations

import hashlib
import unittest

from r20_backend.exchanges.identity import (
    ANON_CREDENTIAL,
    UNKNOWN_LEGACY_ACCOUNT,
    account_id,
    credential_fingerprint,
)


def _fp(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


class AccountIdTest(unittest.TestCase):
    def test_shape_is_venue_env_fingerprint(self):
        self.assertEqual(account_id("binance", "demo", "KEY123"),
                         f"binance:demo:{_fp('KEY123')}")

    def test_matches_credential_fingerprint_scheme(self):
        # 与 AccountKey 三元指纹同源，避免两套指纹并存
        self.assertTrue(account_id("gate", "live", "K").endswith(credential_fingerprint("K")))

    def test_missing_key_uses_anon_sentinel(self):
        for blank in ("", None, "   "):
            self.assertEqual(account_id("okx", "demo", blank),
                             f"okx:demo:{_fp(ANON_CREDENTIAL)}")

    def test_environment_change_changes_identity(self):
        # 同一把 key，demo 与 live 必须是两个账号身份（否则环境串台）
        self.assertNotEqual(account_id("okx", "demo", "K"),
                            account_id("okx", "live", "K"))

    def test_venue_change_changes_identity(self):
        self.assertNotEqual(account_id("okx", "live", "K"),
                            account_id("binance", "live", "K"))

    def test_rotate_key_changes_identity(self):
        # 凭证代际隔离：轮换 key 后身份必须变化
        self.assertNotEqual(account_id("okx", "live", "OLD"),
                            account_id("okx", "live", "NEW"))

    def test_legacy_sentinel_is_distinct_from_any_real_account(self):
        self.assertEqual(UNKNOWN_LEGACY_ACCOUNT, "unknown_legacy")
        self.assertNotIn(":", UNKNOWN_LEGACY_ACCOUNT)
        self.assertNotEqual(UNKNOWN_LEGACY_ACCOUNT, account_id("okx", "live", "K"))


if __name__ == "__main__":
    unittest.main()
