"""回测引擎与策略测试"""
import datetime as dt

import pytest

from data.models import KLineData
from core.backtest.strategy import (
    Strategy, Action, Signal, BuyPointStrategy,
    WeeklyAggregator, _resample_weekly,
)
from core.backtest.engine import BacktestEngine


def make_daily(closes, opens=None, highs=None, lows=None, volumes=None):
    """按收盘价序列构造日线，其余价格缺省由 close 派生"""
    n = len(closes)
    opens = opens if opens is not None else list(closes)
    highs = highs if highs is not None else [max(c, o) for c, o in zip(closes, opens)]
    lows = lows if lows is not None else [min(c, o) for c, o in zip(closes, opens)]
    volumes = volumes if volumes is not None else [10000] * n
    base = dt.date(2020, 1, 1)
    return [
        KLineData(
            code="000001",
            date=(base + dt.timedelta(days=i)).strftime("%Y-%m-%d"),
            open=opens[i], high=highs[i], low=lows[i],
            close=closes[i], volume=volumes[i], period="daily",
        )
        for i in range(n)
    ]


def make_decline_rise_data(n_decline=120, n_rise=60, spread=0.5):
    """先长跌、再长涨，构造 MACD 金叉 + 周线底分型重叠的场景"""
    closes = []
    for i in range(n_decline):
        closes.append(20.0 - i * (10.0 / n_decline))
    for i in range(n_rise):
        closes.append(10.0 + i * (15.0 / n_rise))
    highs = [c + spread for c in closes]
    lows = [c - spread for c in closes]
    return make_daily(closes, highs=highs, lows=lows)


class DummyStrategy(Strategy):
    """固定信号策略，用于隔离测试引擎的执行逻辑"""
    name = "dummy"

    def __init__(self, signals):
        self._signals = signals

    def generate_signals(self, daily):
        return self._signals


# ============================================================
# 引擎测试
# ============================================================

def test_engine_buy_sell_profit():
    daily = make_daily([10.0] * 10)
    sigs = [
        Signal(date=daily[1].date, action=Action.BUY, price=10.0),
        Signal(date=daily[3].date, action=Action.SELL, price=11.0),
    ]
    engine = BacktestEngine(initial_capital=10000.0)
    report = engine.run_on_data(DummyStrategy(sigs), "000001", daily)

    assert report.total_trades == 1
    assert report.win_trades == 1
    assert report.win_rate == 1.0
    # 数量: int(10000 / (10*100*1.00025)) * 100 = 900
    assert report.trades[0].quantity == 900
    # 利润 = 900 - 买入佣金5 - 卖出佣金5 - 印花税9.9 = 880.1
    assert report.trades[0].profit == pytest.approx(880.1, rel=1e-6)
    assert report.total_return == pytest.approx(880.1 / 10000.0, rel=1e-6)


def test_engine_no_trades_when_no_signals():
    daily = make_daily([10.0] * 10)
    report = BacktestEngine().run_on_data(DummyStrategy([]), "000001", daily)
    assert report.total_trades == 0
    assert report.total_return == 0.0


def test_engine_profit_factor_and_drawdown():
    daily = make_daily([10.0] * 20)
    sigs = [
        Signal(date=daily[1].date, action=Action.BUY, price=10.0),
        Signal(date=daily[3].date, action=Action.SELL, price=11.0),
        Signal(date=daily[5].date, action=Action.BUY, price=10.0),
        Signal(date=daily[7].date, action=Action.SELL, price=9.0),
    ]
    report = BacktestEngine(initial_capital=10000.0).run_on_data(
        DummyStrategy(sigs), "000001", daily,
    )
    assert report.total_trades == 2
    assert report.win_trades == 1
    assert report.loss_trades == 1
    assert report.win_rate == 0.5
    assert 0 < report.profit_factor < float("inf")
    assert report.max_drawdown >= 0.0


def test_engine_profit_factor_inf_when_no_losses():
    daily = make_daily([10.0] * 10)
    sigs = [
        Signal(date=daily[1].date, action=Action.BUY, price=10.0),
        Signal(date=daily[3].date, action=Action.SELL, price=11.0),
    ]
    report = BacktestEngine(initial_capital=10000.0).run_on_data(
        DummyStrategy(sigs), "000001", daily,
    )
    assert report.profit_factor == float("inf")


# ============================================================
# 策略测试
# ============================================================

def test_buypoint_strategy_flat_data_no_signal():
    daily = make_daily([10.0] * 200)
    assert BuyPointStrategy().generate_signals(daily) == []


def test_buypoint_strategy_no_lookahead():
    """点内时间正确性：给同一前缀加未来数据，历史信号不应改变。"""
    daily = make_decline_rise_data(150, 60)  # 该参数确实产生信号
    strat = BuyPointStrategy()
    full = strat.generate_signals(daily)
    assert [s for s in full if s.action == Action.BUY]  # 保证非空跑
    for k in (100, 150, 180, 210):
        prefix = strat.generate_signals(daily[:k])
        cutoff = daily[k - 1].date
        full_prefix = [s for s in full if s.date <= cutoff]
        assert [(s.date, s.action.value, s.price) for s in prefix] == \
               [(s.date, s.action.value, s.price) for s in full_prefix]


def test_buypoint_strategy_sell_after_buy():
    """买入后卖出必须晚于买入（T+1 由策略保证）。"""
    daily = make_decline_rise_data(150, 60)
    signals = BuyPointStrategy().generate_signals(daily)
    buys = [s for s in signals if s.action == Action.BUY]
    sells = [s for s in signals if s.action == Action.SELL]
    assert len(buys) >= 1
    assert len(sells) >= 1
    first_buy = buys[0].date
    assert all(s.date > first_buy for s in sells)
    # 信号结构合法性
    for s in signals:
        assert s.date
        assert s.action in (Action.BUY, Action.SELL)
        if s.action == Action.BUY:
            assert s.price > 0


# ============================================================
# 周线增量聚合 — 与 pandas 前缀重采样的等价性
# ============================================================

class TestWeeklyAggregatorEquivalence:
    """增量周线必须与 _resample_weekly(daily[:i+1]) 逐日完全一致

    generate_signals 复用同一个 WeeklyAggregator 以避免 O(n^2) 重采样，
    前提是两者语义完全等价。回测是策略验证工具，结果失真比跑得慢危险得多，
    所以这层等价性必须在测试里钉死。
    """

    @staticmethod
    def random_daily(n, seed):
        import random
        from datetime import date, timedelta

        rng = random.Random(seed)
        out, d, price = [], date(2024, 1, 1), 10.0
        while len(out) < n:
            d += timedelta(days=1)
            if d.weekday() >= 5:
                continue
            price *= (1 + rng.uniform(-0.03, 0.03))
            o = price * (1 + rng.uniform(-0.01, 0.01))
            h = max(o, price) * (1 + rng.uniform(0, 0.02))
            l = min(o, price) * (1 - rng.uniform(0, 0.02))
            out.append(KLineData(code="000001", date=d.isoformat(), open=o,
                                 high=h, low=l, close=price, volume=1000,
                                 period="daily"))
        return out

    def test_equivalent_to_prefix_resample(self):
        """对每个前缀位置，增量结果必须等于前缀重采样结果"""
        import numpy as np

        names = ("highs", "lows", "closes", "opens")
        for seed in range(3):
            daily = self.random_daily(300, seed)
            agg = WeeklyAggregator()
            for i in range(len(daily)):
                agg.advance(daily, i)
                inc = agg.arrays()
                ref = _resample_weekly(daily[:i + 1])
                for name, a, b in zip(names, inc, ref):
                    assert a.shape == b.shape, (
                        f"seed={seed} i={i} {name} 长度不一致: {a.shape} vs {b.shape}")
                    assert np.allclose(a, b, equal_nan=True), (
                        f"seed={seed} i={i} {name} 数值不一致")

    def test_advance_handles_skipped_bars(self):
        """主循环买入后会 i += 2 跳步，advance 必须支持跳跃推进"""
        import numpy as np

        daily = self.random_daily(120, 5)

        jumped = WeeklyAggregator()
        jumped.advance(daily, 80)          # 一次性跳到第 80 根

        stepwise = WeeklyAggregator()
        for k in range(81):                # 逐根推进
            stepwise.advance(daily, k)

        assert jumped._upto == 80
        for a, b in zip(jumped.arrays(), stepwise.arrays()):
            assert np.allclose(a, b, equal_nan=True), "跳跃推进与逐根推进结果不一致"

    def test_week_boundary_is_sunday(self):
        """周边界取所在周的周日（对齐 pandas resample('W') 的 W-SUN / closed=right）"""
        # 2026-09-17 为周四 → 本周周日是 09-20
        assert WeeklyAggregator.week_key("2026-09-17") == "2026-09-20"
        # 周日当天属于本周
        assert WeeklyAggregator.week_key("2026-09-20") == "2026-09-20"
        # 次周一属于下一周 → 周日是 09-27
        assert WeeklyAggregator.week_key("2026-09-21") == "2026-09-27"


class TestBacktestPerformance:
    """性能基准 — 防复杂度退化

    优化前为 O(n^2)（逐日对前缀做 pandas resample），实测 1000 天 3.02s；
    改为增量周线后约 0.44s。这里用宽松上限守住量级，避免有人无意间去掉
    增量复用或 _entry_triggered 的持仓短路。
    """

    def test_1000_days_under_budget(self):
        import time

        daily = TestWeeklyAggregatorEquivalence.random_daily(1000, 42)
        strat = BuyPointStrategy()

        t0 = time.perf_counter()
        strat.generate_signals(daily)
        elapsed = time.perf_counter() - t0

        # 优化后实测约 0.44s；上限 2.0s 仍能在退回 O(n^2)（3.0s+）时报警
        assert elapsed < 2.0, (
            f"1000 天回测耗时 {elapsed:.2f}s，疑似复杂度退化回 O(n^2)")
