"""台账账号范围过滤（步2）：把「当前账号是谁」的判定收进一个纯模块。

## 口径（2026-09 用户确认）

「当前账号」= **当前连接的交易所账号**：凭证在位、能取到数的
``(venue, environment, credential_fingerprint)`` 三元身份（见 exchanges/accounts.py）。

据此，以下四类在**默认视图**里出范围：

| 出范围的原因 | 例子 |
|---|---|
| 同所不同账号 | 换过 API Key（指纹代际变了） |
| 同所不同环境 | 同一把 key 的 demo 行跑到 live 视图里 |
| **该所未连接** | 没有该所凭证了（旧所、已卸载的所） |
| 无账号归属 | 迁移前写入的旧行（回填 UNKNOWN_LEGACY_ACCOUNT） |

**唯独**整条账号轴解析不出来（映射为空）时不做场所过滤 —— 那是"问不到"，不是"没有账号"，
绝不能让密钥库读失败把页面清空。

## 为什么在**读取侧**过滤，而不是在合并侧删

``scripts/ledger/merge.py`` 的合并是「事实存储」：旧行靠 id 续命，换账号后旧行仍在。
若在合并侧删除非当前账号的行，用户**换回**旧账号时历史已经没了 —— 那是毁数据。

故本模块只做**视图范围**：文件里留全部事实，页面只出当前连接账号的行；
``include_hidden=True``（``/api/all?include_hidden=1``）把被挡下的行原样放出。

⚠️ account_id 内嵌凭证指纹：本模块只用于**服务端比较**，其值禁入 API 响应
（载荷只出粗粒度 venue/account_mode 与计数）。
"""
from __future__ import annotations

from r20_backend.exchanges.identity import UNKNOWN_LEGACY_ACCOUNT

__all__ = [
    "UNKNOWN_LEGACY_ACCOUNT",
    "row_venue",
    "row_account_id",
    "has_identity",
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


def has_identity(trade: dict) -> bool:
    """该行是否有真实账号身份（legacy 哨兵不算）。"""
    aid = row_account_id(trade)
    return bool(aid) and aid != UNKNOWN_LEGACY_ACCOUNT


def in_scope(trade: dict, current_accounts, include_hidden: bool = False) -> bool:
    """该行是否属于「当前连接的交易所账号」视图。

    current_accounts: venue -> account_id，**只含当前连接（凭证在位）的场所**。
    空/None = 账号轴问不到 → 不做场所过滤，只按「有无身份」判定。
    include_hidden: 用户显式要求放出全部被挡下的行（数据从未删除）。
    """
    if include_hidden:
        return True
    aid_ok = has_identity(trade)
    if not current_accounts:
        # 问不到账号轴：有身份就放行（不靠猜），无身份仍按默认隐藏
        return aid_ok
    if not aid_ok:
        return False
    want = (current_accounts or {}).get(row_venue(trade))
    if not want:
        return False   # 该所未连接 → 不是当前账号
    return row_account_id(trade) == want


def filter_in_scope(trades, current_accounts, include_hidden: bool = False) -> list:
    """按当前连接账号过滤，保持原顺序，绝不改写入参。"""
    return [t for t in (trades or [])
            if in_scope(t, current_accounts, include_hidden)]


def scope_summary(trades, current_accounts, include_hidden: bool = False) -> dict:
    """给前端的**粗粒度**披露（不含任何 account_id / 指纹）。

    hidden 按原因拆两栏，让前端能说清"隐藏的是什么"，而不是含糊的"已隐藏 N 条"：
    - hidden_legacy：无账号归属（迁移前旧行）
    - hidden_foreign：有身份但不属于当前连接账号（换过 key/环境/场所未连接）
    """
    rows = list(trades or [])
    hidden_rows = [t for t in rows
                   if not in_scope(t, current_accounts, include_hidden)]
    hidden_legacy = sum(1 for t in hidden_rows if not has_identity(t))
    return {
        "total": len(rows),
        "shown": len(rows) - len(hidden_rows),
        "hidden": len(hidden_rows),
        "hidden_legacy": hidden_legacy,
        "hidden_foreign": len(hidden_rows) - hidden_legacy,
        "include_hidden": bool(include_hidden),
        "scoped": bool(current_accounts),
    }
