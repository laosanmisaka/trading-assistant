# -*- coding: utf-8 -*-
"""动态止损 ``trail_exit`` 单测

规则（2026-09-23 定稿）：
- 第 1~10 个交易日：``stop = max(成本×(1−5%), 中枢上沿)``（**取较高者**，更紧）
- 第 11 个交易日起：``stop = max(成本, 前一交易日收盘的 MA20)``（保本优先）
- 收益触及 +15% 减半仓；止损盘中触及即出（跳空低开按开盘价成交）
- 同根 bar 内两者都触 ⇒ **先止损**（bar 内部无法分辨先后，取悲观）
- 收益按现金流口径 ``Σ(卖价×权重)/成本 − 1``

5min bar 按 **48 根 = 1 个交易日** 构造（与 ``MIN5_BARS_PER_DAY`` 一致），
日期逐日递增，这样阶段切换与 ``dtd`` 的日期映射都是确定的。
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.eval_daily_5min import MIN5_BARS_PER_DAY, trail_exit   # noqa: E402

T0 = pd.Timestamp("2025-01-02")


def mk(n_days, close=100.0):
    """构造 ``n_days`` 个交易日的 5min bar（价格恒为 ``close``）+ 日线时间轴"""
    dts = [T0 + pd.Timedelta(days=d, minutes=5 * (b + 1))
           for d in range(n_days) for b in range(MIN5_BARS_PER_DAY)]
    n = len(dts)
    cl = [close] * n
    hi = [close * 1.001] * n
    lo = [close * 0.999] * n
    op = [close] * n
    dtd = [T0 + pd.Timedelta(days=d) for d in range(n_days)]
    return hi, lo, op, cl, dts, dtd


def test_stage1_cost_minus_5pct_gap_down():
    """阶段 1 无中枢：止损线 = 成本×0.95；跳空低开按开盘价成交"""
    hi, lo, op, cl, dts, _ = mk(3)
    lo[5], op[5] = 94.0, 94.0
    r = trail_exit(hi, lo, op, cl, dts, 0, 100.0, max_hold=2)
    assert r is not None
    assert r["reason"] == "止损"
    assert r["half"] is False
    assert abs(r["ret"] - (-6.0)) < 1e-9          # 94/100 − 1，不是 −5%


def test_stage1_stop_fills_at_stop_price_when_no_gap():
    """未跳空时按止损价成交（止损线上方开盘、盘中下破）"""
    hi, lo, op, cl, dts, _ = mk(3)
    lo[5] = 94.0                                   # op[5] 仍为 100
    r = trail_exit(hi, lo, op, cl, dts, 0, 100.0, max_hold=2)
    assert abs(r["ret"] - (-5.0)) < 1e-9


def test_stage1_uses_zg_when_higher():
    """中枢上沿高于成本×0.95 ⇒ 取较高者（更紧），离场价可以高于成本"""
    hi, lo, op, cl, dts, _ = mk(3)
    lo = [102.5] * len(lo)                         # 止损线 102 高于成本，价格不许先破
    lo[3], op[3] = 101.0, 101.0
    r = trail_exit(hi, lo, op, cl, dts, 0, 100.0, zg=102.0, max_hold=2)
    assert r["reason"] == "止损"
    assert abs(r["ret"] - 1.0) < 1e-9              # min(101, 102)/100 − 1


def test_stage1_ignores_zg_when_lower():
    """中枢上沿低于成本×0.95 ⇒ 仍用成本×0.95，不能被更松的线顶掉"""
    hi, lo, op, cl, dts, _ = mk(3)
    lo[5], op[5] = 95.5, 95.5                      # 未破 95
    r = trail_exit(hi, lo, op, cl, dts, 0, 100.0, zg=90.0, max_hold=2)
    assert r["reason"] == "到期"
    assert abs(r["ret"]) < 1e-9


def test_half_profit_then_stop_cashflow():
    """+15% 减半后止损 ⇒ 现金流口径（两段权重各 0.5）"""
    hi, lo, op, cl, dts, _ = mk(3)
    hi[10] = 116.0                                 # 触及 +15% ⇒ 0.5 仓 @115
    lo[20], op[20] = 95.0, 95.0                    # 剩余 0.5 仓 @95 止损
    r = trail_exit(hi, lo, op, cl, dts, 0, 100.0, max_hold=2)
    assert r["half"] is True
    assert r["reason"] == "止损"
    assert abs(r["ret"] - 5.0) < 1e-9              # (0.5×115 + 0.5×95)/100 − 1


def test_half_profit_only_once():
    """+15% 只减一次，后续更高不重复减仓"""
    hi, lo, op, cl, dts, _ = mk(3)
    hi[10], hi[30] = 116.0, 130.0
    r = trail_exit(hi, lo, op, cl, dts, 0, 100.0, max_hold=2, mode="halfonly")
    legs = [x for x in r["legs"] if x[2] == "止盈减半"]
    assert len(legs) == 1
    assert abs(legs[0][1] - 115.0) < 1e-9          # 按 15% 挂单价成交，不是 130


def test_stop_wins_within_same_bar():
    """同根 bar 内既破止损又触止盈 ⇒ 先止损（悲观），不发生减半"""
    hi, lo, op, cl, dts, _ = mk(3)
    hi[7], lo[7], op[7] = 120.0, 90.0, 90.0
    r = trail_exit(hi, lo, op, cl, dts, 0, 100.0, max_hold=2)
    assert r["half"] is False
    assert r["reason"] == "止损"
    assert abs(r["ret"] - (-10.0)) < 1e-9


def test_stage2_switches_to_ma_prev():
    """第 11 个交易日起止损线换成 max(成本, 前一交易日收盘的 MA20)"""
    hi, lo, op, cl, dts, dtd = mk(5)
    ma_prev = [None, 105.0, 105.0, 105.0, 105.0]
    lo = [105.5] * len(lo)                         # 阶段 2 止损线 105 高于成本
    lo[100], op[100] = 104.0, 104.0                # k=100 落在第 3 个交易日
    r = trail_exit(hi, lo, op, cl, dts, 0, 100.0, ma_prev=ma_prev, dtd=dtd,
                   trail_days=2, max_hold=4)
    assert r["reason"] == "止损"
    assert r["days"] > 2.0                         # 已经进入阶段 2
    assert abs(r["ret"] - 4.0) < 1e-9              # min(104, 105)/100 − 1


def test_stage1_ignores_ma():
    """阶段 1 内即使 MA20 更高也不用它（换线时点必须是第 11 个交易日）"""
    hi, lo, op, cl, dts, dtd = mk(3)
    ma_prev = [None, 200.0, 200.0]
    lo[5] = 99.0                                   # 若用 MA20=200 会立刻止损
    r = trail_exit(hi, lo, op, cl, dts, 0, 100.0, ma_prev=ma_prev, dtd=dtd,
                   trail_days=2, max_hold=2)
    assert r["reason"] == "到期"


def test_halfonly_has_no_stop():
    """halfonly：只减半、不设止损，深跌也不出场（对照口径）"""
    hi, lo, op, cl, dts, _ = mk(3)
    hi[10] = 116.0
    lo[20], op[20] = 50.0, 50.0
    cl[96] = 80.0                                  # 到期那根收盘
    r = trail_exit(hi, lo, op, cl, dts, 0, 100.0, max_hold=2, mode="halfonly")
    assert r["reason"] == "到期"
    assert abs(r["ret"] - (-2.5)) < 1e-9           # (0.5×115 + 0.5×80)/100 − 1


def test_window_too_short_returns_none():
    """持仓窗不满 max_hold 个交易日 ⇒ None（不拿截断窗口冒充到期收益）"""
    hi, lo, op, cl, dts, _ = mk(3)
    assert trail_exit(hi, lo, op, cl, dts, 0, 100.0, max_hold=60) is None


def test_no_entry_returns_none():
    hi, lo, op, cl, dts, _ = mk(3)
    assert trail_exit(hi, lo, op, cl, dts, None, 100.0, max_hold=2) is None
    assert trail_exit(hi, lo, op, cl, dts, 0, 0.0, max_hold=2) is None
