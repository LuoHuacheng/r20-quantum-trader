"""Atomic account performance baseline storage shared by admin and dashboard."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import ROOT

BASELINE_FILE = ROOT / "data" / "account_initial_state.json"
BJ_TZ = timezone(timedelta(hours=8))
DEFAULT_CAPITAL = 10_000.0
MIN_CAPITAL = 1.0
MAX_CAPITAL = 1_000_000_000.0


def _number(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def load_account_baseline(account_id: str | None = None) -> dict[str, Any]:
    """读取账号基线。传入 account_id 时优先取 ``accounts[account_id]`` 分区。

    步4·账号分区：文件此前是**单份全局基线**，换账号后旧账号的 reset_time 继续生效，
    KPI/台账窗口横跨两个账号（与台账并集同一个病根）。

    兼容铁律：扁平字段仍是默认值与旧行为；分区存在则**覆盖**同名键（未列出的键回落扁平）；
    分区里没有该账号 → 一律回落扁平，绝不编造。
    """
    data: dict[str, Any] = {}
    if BASELINE_FILE.exists():
        try:
            loaded = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
    if account_id:
        section = (data.get("accounts") or {}).get(str(account_id))
        if isinstance(section, dict):
            data = {**data, **section}
    env_default = _number(os.getenv("INITIAL_CAPITAL"), DEFAULT_CAPITAL)
    capital = _number(data.get("initial_capital"), env_default)
    env_evo_start = str(os.getenv("R20_EVOLUTION_START_TIME", "")).strip()
    evo_start = env_evo_start or str(data.get("evolution_start_time") or data.get("reset_time") or "2026-09-01 00:00:00")
    return {
        **data,
        "initial_capital": round(capital, 2),
        "reset_time": str(data.get("reset_time") or "1970-01-01 00:00:00"),
        "evolution_start_time": evo_start,
    }


def update_initial_capital(initial_capital: float, account_id: str | None = None) -> dict[str, Any]:
    capital = round(float(initial_capital), 2)
    if not MIN_CAPITAL <= capital <= MAX_CAPITAL:
        raise ValueError(f"初始本金必须在 {MIN_CAPITAL:.2f} 到 {MAX_CAPITAL:.2f} USDT 之间")
    from r20_backend.file_locks import file_lock

    with file_lock(BASELINE_FILE):
        return _write_baseline(capital, account_id=account_id)


def _write_baseline(capital: float, account_id: str | None = None) -> dict[str, Any]:
    """锁内完成 load → merge → 原子替换（审计 P3-6 家族收口）。

    步4：带 account_id 时只写 ``accounts[account_id]`` 分区，**扁平字段绝不动** ——
    否则改一个账号的本金会顺手污染另一个账号的基线。
    """
    previous = load_account_baseline()
    stamp = datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
    if account_id:
        accounts = dict(previous.get("accounts") or {})
        section = dict(accounts.get(str(account_id)) or {})
        section.update({"initial_capital": capital, "capital_updated_at": stamp})
        accounts[str(account_id)] = section
        updated = {**previous, "accounts": accounts}
    else:
        updated = {
            **previous,
            "initial_capital": capital,
            "capital_updated_at": stamp,
        }
    BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".account-baseline-", suffix=".json", dir=BASELINE_FILE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(updated, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, BASELINE_FILE)
        os.chmod(BASELINE_FILE, 0o600)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    return {
        "previous_initial_capital": previous["initial_capital"],
        **updated,
    }


def update_evolution_start_time(start_time: str) -> dict[str, Any]:
    """更新自进化复盘起始时间（过滤更早的人工历史交易）。"""
    st = str(start_time or "").strip()
    if not st:
        st = "2026-09-01 00:00:00"
    if len(st) == 10:
        st = f"{st} 00:00:00"
    from r20_backend.file_locks import file_lock

    with file_lock(BASELINE_FILE):
        previous = load_account_baseline()
        updated = {
            **previous,
            "evolution_start_time": st,
            "evolution_start_updated_at": datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        }
        BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".account-baseline-", suffix=".json", dir=BASELINE_FILE.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(updated, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, BASELINE_FILE)
            os.chmod(BASELINE_FILE, 0o600)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        return updated
