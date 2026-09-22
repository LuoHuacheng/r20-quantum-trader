"""各所「当前账号」身份解析（步1b）：写入侧与读取侧**唯一事实源**。

台账行在同步时落 ``account_id``；读取侧要判断「这一行属不属于当前账号」，
必须用**同一套**身份算法，否则写读对不上（那会比不修更糟）。

故两边都调本模块：

- ``scripts/sync_full_ledger.py``（写）
- ``r20_backend/dashboard_cache.py``（读，经 dashboard_payload/account_scope 过滤）

## 只出声明的所，绝不用猜

某所未配置凭证 → 映射里**没有**该所。口径（2026-09 用户确认）：**「当前账号」= 当前连接的
交易所账号**，故映射里没有的所 = 未连接 = 不是当前账号 → 它的行默认出范围（可开关放出）。

唯一例外在**读取侧**：当整张映射为空（密钥库读不出来 / OKX 环境解析失败）时不做场所过滤 ——
那是"问不到"，不是"没有账号"，绝不能让一次读失败把页面清空。

⚠️ 返回值内嵌凭证指纹，属敏感内部标识：禁入 API 响应与日志。
"""
from __future__ import annotations

from r20_backend.exchanges.identity import account_id

__all__ = ["current_venue_accounts"]

#: 三所在「模拟资金环境」下的凭证档名（与 sync_full_ledger 的取数口径逐字一致）
_DEMO_ENV = {"binance": "demo", "gate": "sandbox"}


def current_venue_accounts(env=None) -> dict:
    """venue -> account_id；只含**凭证确实存在**的所。任何异常都被吞成「未知」。

    env: scripts.okx_runtime 的环境对象（含 mode/simulated/api_key/configured）。
    传 None 时自行解析 OKX 当前环境；无法解析则该所不出现在结果里。
    """
    out: dict = {}

    if env is None:
        try:
            import scripts.okx_runtime as _rt
            env = _rt.current_environment()
        except Exception:
            env = None

    if env is not None and getattr(env, "configured", False):
        try:
            out["okx"] = account_id("okx", getattr(env, "mode", "demo"),
                                    getattr(env, "api_key", "") or "")
        except Exception:
            pass

    simulated = bool(getattr(env, "simulated", True)) if env is not None else True
    try:
        from r20_backend.exchanges import venue_credentials
    except Exception:
        venue_credentials = None
    if venue_credentials is not None:
        for venue in ("binance", "gate"):
            environment = _DEMO_ENV[venue] if simulated else "live"
            try:
                ak, _sk = venue_credentials(venue, environment)
            except Exception:
                continue
            if ak:
                out[venue] = account_id(venue, environment, ak)
    return out
