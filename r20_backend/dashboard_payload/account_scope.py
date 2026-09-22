"""台账账号范围过滤（步2）：把「当前账号是谁」的判定收进一个纯模块。

## 为什么在**读取侧**过滤，而不是在合并侧删

``scripts/ledger/merge.py` 的合并是「事实存储」：旧行靠 id 续命，换账号后旧行仍在。
若在合并侧删除非当前账号的行，用户**换回**旧账号时历史已经没了 —— 那是毁数据。

故本模块只做**视图范围**：文件里留全部事实，页面只出当前账号。
无法确知某所当前账号（该所未配置/取数失败）时**保留**其行 —— 宁可多显示，不可抹事实。

## 身份从哪来

``r20_backend.exchanges.identity.account_id()` 的 ``venue:environment:<fp12>``。
行身份缺失（迁移前旧行）或为 ``UNKNOWN_LEGACY_ACCOUNT`` → 默认隐藏，
``include_legacy=True`` 才放行；绝不把无身份的行冒充成当前账号。

⚠️ account_id 内嵌凭证指纹：本模块只用于**服务端比较**，其值禁入 API 响应
（载荷只出粗粒度 venue/account_mode 与计数）。
"""
from __future__ import annotations

from r20_backend.exchanges.identity import UNKNOWN_LEGACY_ACCOUNT

__all__ = [
    "UNKNOWN_LEGACY_ACCOUNT",
    "row_venue",
    "row_account_id",
    "in_scope",
    "filter_in_scope",
    "scope_summary",
]

#: 旧行缺 venue 时的认定（与 db_manager 迁移「旧行缺 venue 列 → 默认 okx」一致）
_DEFAULT_VENUE = "okx"


def row_venue(trade: dict) -> str:
    """行归属场所；缺失按 okx 认定。"""
    return str((trade or {}).get("venue") or _DEFAULT_VENUE).strip().lower()


def row_account_id(trade: dict) -> str:
    """行账号身份；缺失/空返回空串（调用方按「无身份」处理）。"""
    value = (trade or {}).get("account_id")
    return "" if value is None else str(value).strip()


def in_scope(trade: dict, current_accounts, include_legacy: bool = False) -> bool:
    """该行是否属于「当前账号」视图。

    current_accounts: venue -> account_id，**只含确实取到当前账号的场所**。
    None/空 = 没有账号轴信息 → 不再按场所猜，全部放行（仅按 legacy 开关处理无身份行）。
    """
    aid = row_account_id(trade)
    if not aid or aid == UNKNOWN_LEGACY_ACCOUNT:
        return bool(include_legacy)
    if not current_accounts:
        return True
    want = (current_accounts or {}).get(row_venue(trade))
    if not want:
        return True   # 该所当前账号未知 → 不隐藏（非破坏性）
    return aid == want


def filter_in_scope(trades, current_accounts, include_legacy: bool = False) -> list:
    """按当前账号过滤，保持原顺序，绝不改写入参。"""
    return [t for t in (trades or []) if in_scope(t, current_accounts, include_legacy)]


def scope_summary(trades, current_accounts, include_legacy: bool = False) -> dict:
    """给前端的**粗粒度**披露（不含任何 account_id / 指纹）。"""
    rows = list(trades or [])
    hidden_rows = [t for t in rows if not in_scope(t, current_accounts, include_legacy)]
    hidden_legacy = sum(
        1 for t in hidden_rows
        if not row_account_id(t) or row_account_id(t) == UNKNOWN_LEGACY_ACCOUNT
    )
    return {
        "total": len(rows),
        "shown": len(rows) - len(hidden_rows),
        "hidden": len(hidden_rows),
        "hidden_legacy": hidden_legacy,
        "include_legacy": bool(include_legacy),
        "scoped": bool(current_accounts),
    }
