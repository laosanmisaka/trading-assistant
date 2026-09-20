"""回测引擎包 — 可插拔策略 + 哑执行器

⚠️ 2026-09-20：内置的伪缠论策略 `BuyPointStrategy` 已删除（连同命令行入口
`__main__.py`）。本包现在只提供**通用执行器**：自己实现 `Strategy` 接口即可。
"""
from core.backtest.strategy import Strategy, Action, Signal
from core.backtest.engine import BacktestEngine, Trade, BacktestReport

__all__ = [
    "Strategy", "Action", "Signal",
    "BacktestEngine", "Trade", "BacktestReport",
]
