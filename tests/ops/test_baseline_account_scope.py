"""账号基线分区（步4）契约测试：reset_time / initial_capital 按账号身份读取与写入。

事故背景：``account_initial_state.json`` 是**单份全局基线**。换账号（换 key/换环境）后
旧账号的 reset_time 继续生效，KPI 与台账窗口于是横跨两个账号 —— 与台账并集同一个病根。

本门钉死的语义：
1. **向后兼容**：文件只有扁平字段（现状）时，任何 account_id 都回落到扁平值；
   不传 account_id 更是逐字等价旧行为。
2. **分区优先**：``accounts[account_id]`` 存在时覆盖扁平字段（未覆盖的键仍回落扁平）。
3. **写入隔离**：带 account_id 写只动该分区，**绝不动扁平字段**（旧账号基线不被污染）。
4. **未知账号**：分区里没有 → 回落扁平，绝不编造。

封闭三律：基线文件与 DATA_DIR 全部 patch 到临时目录，零生产文件、零网络。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from r20_backend import account_baseline as ab  # noqa: E402
from r20_backend.dashboard_payload import reset_state  # noqa: E402

AID = "okx:demo:aaaa11112222"
OTHER = "okx:live:bbbb33334444"


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="r-baseline-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        self.file = Path(self.tmp) / "account_initial_state.json"
        self._p = patch.object(ab, "BASELINE_FILE", self.file)
        self._p.start()
        self.addCleanup(self._p.stop)
        os.environ.pop("INITIAL_CAPITAL", None)

    def _write(self, payload):
        self.file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _read(self):
        return json.loads(self.file.read_text(encoding="utf-8"))


class LoadBaselineScopeTest(_Base):
    def test_flat_only_file_falls_back_for_any_account(self):
        self._write({"reset_time": "2026-09-01 00:00:00", "initial_capital": 5000.0})
        got = ab.load_account_baseline(AID)
        self.assertEqual(got["reset_time"], "2026-09-01 00:00:00")
        self.assertEqual(got["initial_capital"], 5000.0)

    def test_scoped_section_overrides_flat(self):
        self._write({
            "reset_time": "2026-09-01 00:00:00", "initial_capital": 5000.0,
            "accounts": {AID: {"reset_time": "2026-09-20 00:00:00", "initial_capital": 1234.0}},
        })
        got = ab.load_account_baseline(AID)
        self.assertEqual(got["reset_time"], "2026-09-20 00:00:00")
        self.assertEqual(got["initial_capital"], 1234.0)
        # 未传 account_id → 扁平值原样（旧行为）
        flat = ab.load_account_baseline()
        self.assertEqual(flat["reset_time"], "2026-09-01 00:00:00")
        self.assertEqual(flat["initial_capital"], 5000.0)

    def test_unknown_account_falls_back_to_flat(self):
        self._write({
            "reset_time": "2026-09-01 00:00:00",
            "accounts": {OTHER: {"reset_time": "2026-09-21 00:00:00"}},
        })
        got = ab.load_account_baseline(AID)
        self.assertEqual(got["reset_time"], "2026-09-01 00:00:00")


class WriteBaselineScopeTest(_Base):
    def test_scoped_write_does_not_touch_flat_fields(self):
        self._write({"reset_time": "2026-09-01 00:00:00", "initial_capital": 5000.0})
        ab.update_initial_capital(2000.0, account_id=AID)
        raw = self._read()
        self.assertEqual(raw["initial_capital"], 5000.0, "扁平字段绝不能被分区写污染")
        self.assertEqual(raw["accounts"][AID]["initial_capital"], 2000.0)
        self.assertEqual(ab.load_account_baseline(AID)["initial_capital"], 2000.0)
        self.assertEqual(ab.load_account_baseline()["initial_capital"], 5000.0)

    def test_flat_write_still_works(self):
        self._write({"reset_time": "2026-09-01 00:00:00", "initial_capital": 5000.0})
        ab.update_initial_capital(3000.0)
        self.assertEqual(self._read()["initial_capital"], 3000.0)


class ResetStateScopeTest(_Base):
    def _read_reset(self, account_id=None):
        return reset_state.read_reset_initial_state(self.tmp, account_id=account_id)

    def test_flat_only_is_unchanged(self):
        self._write({"reset_time": "2026-09-01 00:00:00", "initial_capital": 7000.0})
        self.assertEqual(self._read_reset(), ("2026-09-01 00:00:00", 7000.0))
        self.assertEqual(self._read_reset(AID), ("2026-09-01 00:00:00", 7000.0))

    def test_scoped_reset_time_wins(self):
        self._write({
            "reset_time": "2026-09-01 00:00:00", "initial_capital": 7000.0,
            "accounts": {AID: {"reset_time": "2026-09-25 12:00:00"}},
        })
        self.assertEqual(self._read_reset(AID), ("2026-09-25 12:00:00", 7000.0))
        self.assertEqual(self._read_reset(), ("2026-09-01 00:00:00", 7000.0))
        self.assertEqual(self._read_reset(OTHER), ("2026-09-01 00:00:00", 7000.0))

    def test_scoped_capital_zero_falls_back_to_default(self):
        # 与扁平语义一致：0 / 非法值 → 回落 10000（`or` 语义），不是取 0
        self._write({"reset_time": "2026-09-01 00:00:00",
                     "accounts": {AID: {"initial_capital": 0}}})
        self.assertEqual(self._read_reset(AID)[1], 10000.0)


if __name__ == "__main__":
    unittest.main()
