# -*- coding: utf-8 -*-
"""「六脉神剑」策略测试 —— 通达信函数等价、指标口径、成交延迟、无未来函数

2026-09-20 新增，随 `core/six_pulse.py` 与 `scripts/eval_six_pulse.py` 一起。
全部离线：合成 K 线，不触网。
"""
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from core.backtest.engine import BacktestEngine
from core.backtest.strategy import Action
from core.six_pulse import (
    PULSE_NAMES,
    SixPulseStrategy,
    compute_indicators,
    ema,
    sma_tdx,
    to_frame,
)
from data.models import KLineData


# ======================================================================
# 合成数据
# ======================================================================

def synth(n: int = 300, seed: int = 7, base: float = 20.0) -> list[KLineData]:
    """合成一段"跌—涨—盘"的日线，用于产生共振信号（固定种子，可复现）"""
    rng = np.random.default_rng(seed)
    seg = n // 3
    drift = np.concatenate([
        np.full(seg, -0.002), np.full(seg, 0.003), np.full(n - 2 * seg, 0.001),
    ])
    close = base * np.exp(np.cumsum(rng.normal(drift, 0.02)))
    d0 = date(2020, 1, 1)
    out: list[KLineData] = []
    for i in range(n):
        c = float(close[i])
        o = float(close[i - 1]) if i else c
        hi = max(o, c) * (1 + abs(rng.normal(0, 0.004)))
        lo = min(o, c) * (1 - abs(rng.normal(0, 0.004)))
        out.append(KLineData(
            code="TEST", date=(d0 + timedelta(days=i)).isoformat(),
            open=o, high=hi, low=lo, close=c, volume=1_000_000,
        ))
    return out


@pytest.fixture(scope="module")
def daily() -> list[KLineData]:
    return synth(300)


@pytest.fixture(scope="module")
def ind(daily) -> pd.DataFrame:
    return compute_indicators(to_frame(daily))


# ======================================================================
# 通达信函数等价
# ======================================================================

class TestTdxFunctions:
    def test_sma_recursion(self):
        """SMA(X,N,M): Y=(M*X+(N-M)*Y')/N —— 手算前三项"""
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        got = sma_tdx(s, 3, 1)
        assert got.iloc[0] == pytest.approx(1.0)
        assert got.iloc[1] == pytest.approx((2 + 2 * 1.0) / 3)
        assert got.iloc[2] == pytest.approx((3 + 2 * got.iloc[1]) / 3)

    def test_ema_recursion(self):
        """EMA(X,N): Y=(2X+(N-1)Y')/(N+1)，N=3 ⇒ alpha=0.5"""
        s = pd.Series([1.0, 2.0, 3.0])
        got = ema(s, 3)
        assert got.iloc[0] == pytest.approx(1.0)
        assert got.iloc[1] == pytest.approx(1.5)
        assert got.iloc[2] == pytest.approx(2.25)

    def test_sma_is_not_simple_ma(self):
        """SMA 是加权平均，不能与 rolling().mean() 混用"""
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        assert sma_tdx(s, 3, 1).iloc[-1] != pytest.approx(s.rolling(3).mean().iloc[-1])


# ======================================================================
# 指标
# ======================================================================

class TestIndicators:
    def test_columns_present(self, ind):
        """六项 + 派生列 + OHLC 一起返回（下游诊断要用 close/open）"""
        for name in list(PULSE_NAMES) + ["all6", "buy_signal", "sell_signal"]:
            assert name in ind.columns
        for col in ("date", "open", "high", "low", "close"):
            assert col in ind.columns

    def test_flags_are_bool(self, ind):
        for name in list(PULSE_NAMES) + ["all6", "buy_signal", "sell_signal"]:
            assert ind[name].dtype == bool

    def test_buy_signal_is_first_day_of_resonance(self, ind):
        """买点 = 全真且前一日不全真（原文 `REF(...)=0` 的语义）"""
        all6 = ind["B1"] & ind["B2"] & ind["B3"] & ind["B4"] & ind["B5"] & ind["B6"]
        assert all6.equals(ind["all6"])
        expected = all6 & ~all6.shift(1).fillna(False).astype(bool)
        assert ind["buy_signal"].equals(expected)
        # 共振段内不会天天出买点
        assert int(ind["buy_signal"].sum()) < int(ind["all6"].sum())

    def test_sell_signal_is_below_ma(self, ind, daily):
        df = to_frame(daily)
        assert ind["sell_signal"].equals(df["close"] < df["close"].rolling(10).mean())

    def test_ma_exit_param(self, daily):
        df = to_frame(daily)
        ind5 = compute_indicators(df, ma_exit=5)
        assert ind5["sell_signal"].equals(df["close"] < df["close"].rolling(5).mean())
        ind20 = compute_indicators(df, ma_exit=20)
        assert ind20["sell_signal"].equals(df["close"] < df["close"].rolling(20).mean())

    def test_b_indicators_direction(self, daily):
        """B4（LWR）带负号 ⇒ 与其它项同向，不是反向指标"""
        df = to_frame(daily)
        ind = compute_indicators(df)
        # 单调上涨序列上，B5（C>BBI）应几乎恒真；B4 不应几乎恒假
        up = to_frame(synth(120, seed=3))
        ind_up = compute_indicators(up)
        assert ind_up["B4"].mean() > 0.3, "LWR 若为反向，上涨段 B4 会几乎恒假"
        assert ind["B4"].dtype == bool

    def test_flat_series_does_not_crash(self):
        """一字板（high==low）造成除零 —— 必须 fillna 成 False 而不是抛异常"""
        n = 80
        d0 = date(2020, 1, 1)
        flat = [
            KLineData(code="FLAT", date=(d0 + timedelta(days=i)).isoformat(),
                      open=20.0, high=20.0, low=20.0, close=20.0, volume=1000)
            for i in range(n)
        ]
        ind = compute_indicators(to_frame(flat))
        assert not ind["B2"].any() and not ind["B6"].any()
        assert not ind["all6"].any()


# ======================================================================
# 策略
# ======================================================================

class TestStrategy:
    def test_signals_alternate(self, daily):
        """买卖交替、首条必为买入（状态机保证不会 T+0）"""
        sig = SixPulseStrategy().generate_signals(daily)
        assert sig, "合成数据应产生信号"
        acts = [s.action for s in sig]
        assert acts[0] == Action.BUY
        for a, b in zip(acts, acts[1:]):
            assert a != b

    def test_fill_price_is_next_open(self, daily, ind):
        """成交价 = 信号**次日**开盘价；买点日期是共振触发日的次日"""
        sig = SixPulseStrategy().generate_signals(daily)
        pos = {k.date: i for i, k in enumerate(daily)}
        for s in sig:
            i = pos[s.date]
            assert i > 0, "信号日不能是首根（无前一日可确认）"
            assert s.price == pytest.approx(daily[i].open)

        dates = [k.date for k in daily]
        trig = {dates[i + 1] for i in range(len(daily) - 1) if ind["buy_signal"].iloc[i]}
        buys = {s.date for s in sig if s.action == Action.BUY}
        assert buys <= trig, "买入信号必须落在共振日次日（不被持仓过滤掉的部分）"

    def test_no_lookahead(self, daily):
        """截断数据不改变历史信号 —— 无未来函数"""
        full = SixPulseStrategy().generate_signals(daily)
        cut = daily[:250]
        part = SixPulseStrategy().generate_signals(cut)
        cutoff = daily[248].date
        a = [(s.date, s.action, round(s.price, 6)) for s in full if s.date <= cutoff]
        b = [(s.date, s.action, round(s.price, 6)) for s in part if s.date <= cutoff]
        assert a == b and a, "比较区间内应有信号"

    def test_insufficient_data(self):
        assert SixPulseStrategy().generate_signals([]) == []
        assert SixPulseStrategy().generate_signals(synth(30)) == []
        assert SixPulseStrategy(warmup=60).generate_signals(synth(61)) == []

    def test_warmup_gates_first_signal(self, daily):
        """预热期内不出信号"""
        sig = SixPulseStrategy(warmup=100).generate_signals(daily)
        pos = {k.date: i for i, k in enumerate(daily)}
        assert min(pos[s.date] for s in sig) > 100

    def test_hold_days_exit(self, daily):
        """hold_days 出口：卖出日 = 买入日 + N（持有 N 个交易日）"""
        sig = SixPulseStrategy(hold_days=10).generate_signals(daily)
        pos = {k.date: i for i, k in enumerate(daily)}
        pairs = list(zip(sig[0::2], sig[1::2]))
        assert pairs
        for b, s in pairs:
            assert b.action == Action.BUY and s.action == Action.SELL
            assert pos[s.date] - pos[b.date] == 10
            assert "持有 10 日" in s.reason

    def test_ma_exit_reason(self, daily):
        sig = SixPulseStrategy(ma_exit=20).generate_signals(daily)
        sells = [s for s in sig if s.action == Action.SELL]
        assert sells and all(s.reason == "破 MA20" for s in sells)


# ======================================================================
# 与回测引擎集成
# ======================================================================

class TestEngineIntegration:
    def test_engine_runs(self, daily):
        strat = SixPulseStrategy()
        rep = BacktestEngine(initial_capital=1_000_000).run_on_data(strat, "TEST", daily)
        assert rep.strategy == "six_pulse"
        assert rep.total_trades == len(rep.trades) > 0
        assert rep.win_trades + rep.loss_trades == rep.total_trades
        assert -1.0 < rep.total_return < 10.0
        assert 0.0 <= rep.max_drawdown <= 1.0

    def test_expensive_stock_needs_enough_capital(self):
        """高价股一手买不起时引擎静默跳过 ⇒ 评估必须用足够大的初始资金

        这不是 bug，是"整手买入"的真实约束（茅台一手 ≈ 15 万）。
        `scripts/eval_six_pulse.py` 因此默认 100 万而非 10 万，
        否则会得到"0 笔"并被误读成"无信号"。
        """
        n = 300
        d0 = date(2020, 1, 1)
        rng = np.random.default_rng(11)
        close = 1500 * np.exp(np.cumsum(rng.normal(0.001, 0.02, n)))
        expensive = [
            KLineData(code="EXP", date=(d0 + timedelta(days=i)).isoformat(),
                      open=float(close[i]), high=float(close[i]) * 1.01,
                      low=float(close[i]) * 0.99, close=float(close[i]), volume=10_000)
            for i in range(n)
        ]
        strat = SixPulseStrategy()
        poor = BacktestEngine(initial_capital=100_000).run_on_data(strat, "EXP", expensive)
        rich = BacktestEngine(initial_capital=1_000_000).run_on_data(strat, "EXP", expensive)
        assert poor.total_trades == 0, "10 万买不起一手高价股"
        assert rich.total_trades > 0, "100 万应能成交"
