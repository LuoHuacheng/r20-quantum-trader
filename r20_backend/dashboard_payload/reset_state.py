"""重置基准（reset_time / initial_capital）读取（结构优化阶段 2·B2 第九刀）。

原样搬自 update_cache_cycle 的「# 3. Read Reset Initial State」段（11 行）。
data_dir 由门面注入（测试会把 DATA_DIR 指向沙箱）。
"""
from __future__ import annotations

import os

from r20_backend.dashboard_payload.readers import read_json

__all__ = ["read_reset_initial_state"]


def read_reset_initial_state(data_dir, account_id=None):
    """读取 reset_time / initial_capital。

    步4·账号分区：传 account_id 时优先取 ``accounts[account_id]``（未列出的键回落扁平）。
    不传 / 分区缺失 → 逐字等价旧行为（单份全局基线）。
    """
    # 3. Read Reset Initial State
    account_init_file = os.path.join(data_dir, "account_initial_state.json")
    reset_time_str = "1970-01-01 00:00:00"
    initial_capital_val = float(os.getenv("INITIAL_CAPITAL", "10000.0"))
    acc_init = read_json(account_init_file, {})
    if account_id and isinstance(acc_init, dict):
        _section = (acc_init.get("accounts") or {}).get(str(account_id))
        if isinstance(_section, dict):
            acc_init = {**acc_init, **_section}
    try:
        reset_time_str = acc_init.get("reset_time", "1970-01-01 00:00:00")
        initial_capital_val = float(acc_init.get("initial_capital", 10000.0) or 10000.0)
    except Exception:
        pass

    return reset_time_str, initial_capital_val
