"""回测引擎测试

⚠️ 2026-09-20：原内置的伪缠论策略 `BuyPointStrategy`（及其 `WeeklyAggregator`
增量周线优化）已整体删除，对应的策略用例、周线等价性用例、性能基准用例一并移除。
本文件现在只覆盖**通用执行器** `BacktestEngine` 的执行/记账/绩效指标逻辑。

删掉策略后 `Strategy` 变成纯接口，测试用 `DummyStrategy` 喂固定信号 ——
这也正是本文件原本的写法（引擎测试从来就与具体策略解耦）。
"""
import datetime as dt

import pytest

from data.models import KLineData
from core.backtest.strategy import Strategy, Action, Signal
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
# 部分成交（分级减仓 / 回补）
# ============================================================

def test_engine_partial_sell_and_buyback():
    """卖半仓 → 再买回半仓 → 清仓：两笔同属一轮，且能精确回到满仓

    资金 10000 / 价 10 ⇒ 满仓 900 股。整手四舍五入下 900 的一半是 500 股：
    卖 500 剩 400，回补 500 又是 900 —— 「卖一半」与「买回一半」用同一个数，
    所以不会越买越少。
    """
    daily = make_daily([10.0] * 10)
    sigs = [
        Signal(date=daily[1].date, action=Action.BUY, price=10.0, weight=1.0),
        Signal(date=daily[3].date, action=Action.SELL, price=11.0, weight=0.5),
        Signal(date=daily[5].date, action=Action.BUY, price=10.0, weight=0.5),
        Signal(date=daily[7].date, action=Action.SELL, price=12.0, weight=1.0),
    ]
    rep = BacktestEngine(initial_capital=10000.0).run_on_data(
        DummyStrategy(sigs), "000001", daily,
    )
    assert [t.quantity for t in rep.trades] == [500, 900]
    assert [t.episode for t in rep.trades] == [1, 1], "同一轮持仓，不能算成两轮"
    assert rep.total_trades == 2
    assert rep.win_rate == 1.0
    # 12263.70 - 10000 = 2263.70
    assert rep.total_return == pytest.approx(2263.7 / 10000.0, rel=1e-6)


def test_engine_partial_buy_cannot_exceed_full():
    """已满仓时再发 BUY 不应加仓（room = 0）"""
    daily = make_daily([10.0] * 10)
    sigs = [
        Signal(date=daily[1].date, action=Action.BUY, price=10.0, weight=1.0),
        Signal(date=daily[2].date, action=Action.BUY, price=10.0, weight=0.5),
        Signal(date=daily[3].date, action=Action.SELL, price=10.0, weight=1.0),
    ]
    rep = BacktestEngine(initial_capital=10000.0).run_on_data(
        DummyStrategy(sigs), "000001", daily,
    )
    assert [t.quantity for t in rep.trades] == [900]


def test_engine_full_exit_leaves_no_dust():
    """清仓必须一笔卖光，不留碎股"""
    daily = make_daily([10.0] * 10)
    sigs = [
        Signal(date=daily[1].date, action=Action.BUY, price=10.0),
        Signal(date=daily[3].date, action=Action.SELL, price=11.0, weight=0.5),
        Signal(date=daily[5].date, action=Action.SELL, price=12.0, weight=1.0),
    ]
    rep = BacktestEngine(initial_capital=10000.0).run_on_data(
        DummyStrategy(sigs), "000001", daily,
    )
    # 第一笔卖半仓 500，第二笔把剩下的 400 全卖掉
    assert [t.quantity for t in rep.trades] == [500, 400]
    assert rep.open_position is False


def test_engine_rejects_nonpositive_price():
    """价格为 0 的信号必须被跳过，不能除零"""
    daily = make_daily([10.0] * 6)
    sigs = [
        Signal(date=daily[1].date, action=Action.BUY, price=0.0),
        Signal(date=daily[2].date, action=Action.BUY, price=10.0),
        Signal(date=daily[3].date, action=Action.SELL, price=11.0),
    ]
    rep = BacktestEngine(initial_capital=10000.0).run_on_data(
        DummyStrategy(sigs), "000001", daily,
    )
    assert rep.total_trades == 1


# ============================================================
# 已取缔的伪缠论策略：确保它不会悄悄回来
# ============================================================

def test_fake_chan_strategy_is_gone():
    """`BuyPointStrategy` / `WeeklyAggregator` / `_resample_weekly` 必须不存在

    这三者是伪缠论回测链路（周线底分型 + 日线 MACD 金叉 + 自算缩量回踩）。
    老三 2026-09-20 要求"伪缠论全都取缔"，这里用断言防止有人在后续重构中
    把它们（或等价物）从旧提交里恢复回来。
    """
    import core.backtest as pkg
    import core.backtest.strategy as strat

    for name in ("BuyPointStrategy", "WeeklyAggregator", "_resample_weekly"):
        assert not hasattr(strat, name), f"{name} 不该再出现（伪缠论已取缔）"
        assert not hasattr(pkg, name), f"{name} 不该再从 core.backtest 导出"


def test_fake_chan_entry_hooks_are_gone():
    """伪缠论依赖的技术函数也必须删掉；止盈在用的顶分型链路必须保留"""
    import core.technical as tech

    for name in ("calc_center_range", "check_pullback_to_center",
                 "is_volume_contraction", "is_volume_expansion",
                 "get_latest_bottom_fractal", "detect_golden_cross",
                 "detect_macd_golden_cross", "detect_death_cross"):
        assert not hasattr(tech, name), f"core.technical.{name} 不该再出现"

    # 活跃链路：alert_engine 用 get_latest_top_fractal 判止盈
    assert hasattr(tech, "get_latest_top_fractal")
    assert hasattr(tech, "detect_top_fractal")
    assert hasattr(tech, "kline_to_arrays")
    assert hasattr(tech, "find_stop_loss_price")
