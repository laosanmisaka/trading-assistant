"""做T引擎 — 管理仓位/资金/交易执行

====================================================================
[未接线 / 已知缺陷] 本模块在 ui/ 层零引用，不对应任何在用功能。

2026-09-17 决策：做 T 暂不实现，本模块保持现状，不修不接。

以下缺陷已实证，接线前必须先重写记账逻辑
（详见 docs/KNOWN_ISSUES.md KI-001 / KI-002）：

  1. 资金/仓位双向不守恒。BUY_FIRST 路径每轮净减 10000 元，
     而每日上限 max_trades = n_lots，连做 N 轮后资金归零，
     open() 静默返回 None，当日剩余做 T 机会全部失效——
     不报错、不记日志。
  2. force_close() 只是转调 check_close() 且不传 signal，
     价格落在开仓价 ±0.5% 区间时两个平仓条件都不满足，
     返回 None，状态机停在 WAIT_SELL 自锁。

禁止在未修复以上两项的前提下把本模块接入 UI 或真实交易。
====================================================================

做T规则:
  - 底仓 = N * 10000 元 (以股数表示)
  - 可用资金 = N * 10000 元
  - 每日最多做T N 次
  - 每次固定 10000 元
  - 两种模式:
    1. 先买后卖: 用可用资金买入 → 当天高点卖出底仓中对应股数
    2. 先卖后回补: 卖出底仓 → 当天低点买回

状态机:
  IDLE → WAIT_SELL (先买后卖, 已买入等待卖出)
  IDLE → WAIT_BUY  (先卖后回补, 已卖出等待回补)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from core.trading.model import TTSignal, TTDirection


class TTState(str, Enum):
    IDLE = "idle"            # 空闲, 可开新仓
    WAIT_SELL = "wait_sell"  # 已买入, 等待卖出
    WAIT_BUY = "wait_buy"    # 已卖出, 等待回补


@dataclass
class TTTrade:
    """单笔做T交易记录"""
    code: str = ""
    open_time: str = ""        # 开仓时间
    close_time: str = ""       # 平仓时间
    direction: str = ""        # buy_first / sell_first
    open_price: float = 0.0    # 开仓价
    close_price: float = 0.0   # 平仓价
    quantity: int = 0          # 股数
    amount: float = 0.0        # 金额
    profit: float = 0.0        # 盈亏
    profit_pct: float = 0.0    # 盈亏百分比
    reason: str = ""           # 做T原因


@dataclass
class TTStatus:
    """做T状态"""
    code: str = ""
    base_position: int = 0       # 底仓股数
    base_cost: float = 0.0       # 底仓均价
    available_funds: float = 0.0 # 可用资金
    total_profit: float = 0.0    # 累计做T收益
    trades_today: int = 0        # 今日已完成做T笔数
    max_trades: int = 5          # 每日最大做T次数
    trade_amount: float = 10000  # 每次做T金额
    state: TTState = TTState.IDLE
    pending_trade: TTTrade | None = None  # 等待平仓的交易
    trade_history: list[TTTrade] = field(default_factory=list)


class TTrader:
    """做T交易引擎

    用法:
        trader = TTrader(n_lots=5)
        signal = model.predict(features)
        if signal.win_rate > 0.55:
            trader.open(code, price, signal)
        # ... 价格变化 ...
        trader.check_close(code, current_price)
    """

    def __init__(self, n_lots: int = 5):
        self.n_lots = n_lots
        self._status: dict[str, TTStatus] = {}  # code → TTStatus

    @property
    def base_capital(self) -> float:
        """底仓资金 = N * 10000"""
        return self.n_lots * 10000.0

    @property
    def trade_amount(self) -> float:
        """单次做T金额"""
        return 10000.0

    def init_stock(self, code: str, base_cost: float):
        """初始化某只股票的做T状态"""
        base_position = int(self.base_capital / base_cost) if base_cost > 0 else 0
        self._status[code] = TTStatus(
            code=code,
            base_position=base_position,
            base_cost=base_cost,
            available_funds=self.base_capital,
            max_trades=self.n_lots,
            trade_amount=self.trade_amount,
        )
        return self._status[code]

    def get_status(self, code: str) -> TTStatus | None:
        """获取做T状态，首次访问自动初始化"""
        return self._status.get(code)

    def get_or_init(self, code: str, base_cost: float) -> TTStatus:
        """获取或初始化"""
        if code not in self._status:
            return self.init_stock(code, base_cost)
        return self._status[code]

    def can_open(self, code: str) -> bool:
        """检查是否可以开新仓"""
        s = self._status.get(code)
        if s is None:
            return True
        if s.state != TTState.IDLE:
            return False
        if s.trades_today >= s.max_trades:
            return False
        return True

    def open(self, code: str, price: float, signal: TTSignal) -> TTTrade | None:
        """
        根据信号开仓做T
        返回 TTTrade (pending状态) 或 None (无法开仓)
        """
        s = self._status.get(code)
        if s is None:
            return None

        if not self.can_open(code):
            return None

        if price <= 0:
            return None
        quantity = int(self.trade_amount / price)
        if quantity <= 0:
            return None

        trade = TTTrade(
            code=code,
            open_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            direction=signal.direction.value,
            open_price=price,
            quantity=quantity,
            amount=self.trade_amount,
            reason=signal.reason,
        )

        if signal.direction == TTDirection.BUY_FIRST:
            # 先买: 用可用资金买入
            if s.available_funds < self.trade_amount:
                return None
            s.available_funds -= self.trade_amount
            s.state = TTState.WAIT_SELL
        elif signal.direction == TTDirection.SELL_FIRST:
            # 先卖: 从底仓卖出
            if quantity > s.base_position:
                return None
            s.base_position -= quantity
            s.state = TTState.WAIT_BUY

        s.pending_trade = trade
        return trade

    def check_close(self, code: str, current_price: float,
                    signal: TTSignal | None = None) -> TTTrade | None:
        """
        检查是否应该平仓
        - 到达目标价 / 止损价 / 信号反转 → 平仓
        返回已完成的 TTTrade 或 None
        """
        s = self._status.get(code)
        if s is None or s.state == TTState.IDLE or s.pending_trade is None:
            return None

        trade = s.pending_trade
        should_close = False

        # 信号触发平仓
        if signal is not None:
            if trade.direction == TTDirection.BUY_FIRST.value:
                # 先买后卖: 新信号说 SELL_FIRST 或 HOLD 但 win_rate 降低 → 平仓
                if signal.direction in (TTDirection.SELL_FIRST, TTDirection.HOLD):
                    if signal.win_rate < 0.45:
                        should_close = True
            elif trade.direction == TTDirection.SELL_FIRST.value:
                if signal.direction in (TTDirection.BUY_FIRST, TTDirection.HOLD):
                    if signal.win_rate < 0.45:
                        should_close = True

        # 盈利达标 → 平仓
        if trade.direction == TTDirection.BUY_FIRST.value:
            if current_price >= trade.open_price * 1.005:  # 0.5%盈利
                should_close = True
        else:
            if current_price <= trade.open_price * 0.995:
                should_close = True

        # 止损
        if current_price <= trade.open_price * 0.99:
            should_close = True
        elif current_price >= trade.open_price * 1.01:
            should_close = True

        if not should_close:
            return None

        # 执行平仓
        trade.close_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        trade.close_price = current_price

        if trade.direction == TTDirection.BUY_FIRST.value:
            # 买入后卖出: 盈利 = (卖出价-买入价) * 数量
            trade.profit = (current_price - trade.open_price) * trade.quantity
            # 恢复仓位
            s.base_position += trade.quantity
        else:
            # 卖出后回补: 盈利 = (卖出价-回补价) * 数量
            trade.profit = (trade.open_price - current_price) * trade.quantity
            # 恢复资金
            s.available_funds += trade.amount

        trade.profit_pct = round((current_price / trade.open_price - 1) * 100, 3)
        if trade.direction == TTDirection.SELL_FIRST.value:
            trade.profit_pct = -trade.profit_pct

        s.total_profit += trade.profit
        s.trades_today += 1
        s.state = TTState.IDLE
        s.trade_history.append(trade)
        s.pending_trade = None

        return trade

    def force_close(self, code: str, current_price: float) -> TTTrade | None:
        """强制平仓 (收盘前)"""
        return self.check_close(code, current_price)

    def reset_daily(self, code: str):
        """重置每日计数器 (新交易日)"""
        s = self._status.get(code)
        if s:
            s.trades_today = 0
            s.state = TTState.IDLE
            s.pending_trade = None

    def get_stats(self, code: str) -> dict:
        """获取做T统计"""
        s = self._status.get(code)
        if s is None:
            return {}

        trades = s.trade_history
        wins = [t for t in trades if t.profit > 0]
        losses = [t for t in trades if t.profit <= 0]

        return {
            "code": code,
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(trades) if trades else 0.0,
            "total_profit": s.total_profit,
            "avg_profit": sum(t.profit for t in trades) / len(trades) if trades else 0.0,
            "max_profit": max((t.profit for t in trades), default=0.0),
            "max_loss": min((t.profit for t in trades), default=0.0),
            "profit_per_trade": s.total_profit / len(trades) if trades else 0.0,
            "trades_today": s.trades_today,
            "base_position": s.base_position,
            "available_funds": s.available_funds,
        }
