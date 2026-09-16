"""回测引擎包 — 可插拔策略 + 哑执行器"""
from core.backtest.strategy import Strategy, Action, Signal, BuyPointStrategy
from core.backtest.engine import BacktestEngine, Trade, BacktestReport

__all__ = [
    "Strategy", "Action", "Signal", "BuyPointStrategy",
    "BacktestEngine", "Trade", "BacktestReport",
]
