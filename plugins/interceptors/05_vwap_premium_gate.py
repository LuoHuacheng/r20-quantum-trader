"""
R20 物理拦截插件规范
====================
id: 05_vwap_premium_gate
name: VWAP 溢价区追高门禁
version: 1.0.0
author: R20 Official
description: 禁止在明显偏离 VWAP 的溢价区追多（或在折价区追空）。入场折价再深也不救「买在溢价区」——溢价区的正常回归到 VWAP 就吃掉了大半止损距离。
tags: 入场质量, 追高防护, VWAP, 官方预设
"""

# ── 阈值（占 VWAP 的百分比）─────────────────────────────────────────────
#
# 取值依据分两层，第二层才是定 1.0 而不是 1.5 的真正原因：
#
# ① 观测分布（2026-09-23 之前 26 笔做多入场，取自各轮提示词里的真实 VWAP 乖离）：
#       min -2.87% | p25 +0.18% | 中位 +1.19% | p75 +1.99% | max +4.15%
#    已知亏损多单 +1.99% / +1.98% / +1.41% / +1.26%；两笔止盈成功 +0.23% / +0.26%。
#    注意：结果样本只有 6 笔，**不足以区分 1.0 与 1.5**，别拿它当调参依据。
#
# ② 机制耦合（真正定调的一条）：溢价区的均值回归就是逆风行程。
#    入场地板（order_intent.MIN_ENTRY_PULLBACK_RATIO = 1.2%）把成交推到现价下方 1.2%，
#    只要放行条件满足 `(1 - 0.012) × (1 + 阈值) < 1`，**每一笔成交就必在 VWAP 之下**。
#    阈值 1.0 ⇒ 0.988 × 1.01 = 0.99788 < 1（成立）；
#    阈值 1.5 ⇒ 0.988 × 1.015 = 1.00282 > 1（不成立，成交可仍在 VWAP 之上）。
#    该不变量由 tests/core/test_interceptor_core_safety.py::VwapPremiumGateTests
#    的正向用例钉住 —— 两个常量分居两个模块，改其中一个会立即报红。
PREMIUM_LIMIT_PCT = 1.0


def check_risk(package: dict, decision: dict, context: dict) -> tuple[bool, str]:
    """
    VWAP 溢价区门禁：
    - 做多：现价高于 VWAP 超阈值（买在溢价区）→ 拦截
    - 做空：现价低于 VWAP 超阈值（卖在折价区，同一失效模式的镜像）→ 拦截

    为什么是 VWAP 乖离而不是 15M 速度/加速度：
    同一批入场的实测分布是「15M v>0 且 a<0」（冲顶减速，26 笔里 19 笔），
    且**盈亏两组共用同一形态**（止盈成功的 BTC 与止损的 XRP 同形），
    按 v/a 拦截要么零命中（v<0 且 a<0 在 26 笔里 0 命中），要么拦掉全部 73%。
    乖离度才是区分「回踩到折扣」与「追在半山腰」的那一维。
    """
    action = str(decision.get("action", "WAIT")).upper()
    if action == "WAIT":
        return True, ""

    bias = package.get("vwap_bias")
    if not isinstance(bias, (int, float)) or isinstance(bias, bool):
        # fail-closed：读不到乖离就不给开仓。注意 0.0 是**合法读数**（现价正落在 VWAP 上，
        # 那是最理想的入场位之一），不是缺省值，所以只判类型不判真假。
        return False, f"入场门禁：VWAP 乖离数据不可用 (vwap_bias={bias!r})，安全降级为 WAIT"

    bias = float(bias)
    if action == "BUY_LONG" and bias >= PREMIUM_LIMIT_PCT:
        return False, (f"做多但现价已高于 VWAP {bias:.2f}%（溢价区追高，"
                       f"门禁上限 {PREMIUM_LIMIT_PCT:g}%）：等回踩到 VWAP 附近再挂单")
    if action == "SELL_SHORT" and bias <= -PREMIUM_LIMIT_PCT:
        return False, (f"做空但现价已低于 VWAP {bias:.2f}%（折价区追空，"
                       f"门禁下限 -{PREMIUM_LIMIT_PCT:g}%）：等反弹到 VWAP 附近再挂单")
    return True, ""
