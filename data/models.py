"""数据模型定义"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from enum import Enum


class GroupType(str, Enum):
    HOLDING = "holding"
    CLEARED = "cleared"
    TRACKING = "tracking"
    CUSTOM = "custom"


class TradeType(str, Enum):
    BUY = "buy"
    SELL = "sell"


class AlertType(str, Enum):
    """提醒类型

    ⚠️ 2026-09-20：`BUY_POINT = "buy_point"` 已删除 —— 买点自 2026-09-18 起
    不做提醒（只在 K 线图上标注），该枚举值失去唯一用途。DB 里若残留
    `alert_type='buy_point'` 的旧行，不会被任何代码读到。
    """
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    ALL = "all"


@dataclass
class Group:
    """分组"""
    id: int = 0
    name: str = ""
    type: str = GroupType.CUSTOM.value
    sort_order: int = 0


@dataclass
class Stock:
    """分组中的股票"""
    id: int = 0
    code: str = ""
    name: str = ""
    group_id: int = 0
    added_date: str = ""


@dataclass
class Trade:
    """交易记录"""
    id: int = 0
    stock_code: str = ""
    trade_type: str = TradeType.BUY.value
    price: float = 0.0
    quantity: int = 0
    fee: float = 0.0
    trade_date: str = ""
    notes: str = ""


@dataclass
class AlertDisabled:
    """已关闭提醒的股票"""
    stock_code: str = ""
    alert_type: str = AlertType.ALL.value
    disabled_at: str = ""


@dataclass
class DisciplineRule:
    """交易纪律"""
    id: int = 0
    stock_code: str = ""
    rule_text: str = ""


@dataclass
class RealtimeQuote:
    """实时行情"""
    code: str = ""
    name: str = ""
    price: float = 0.0        # 最新价
    change_pct: float = 0.0   # 涨跌幅 %
    change_amt: float = 0.0   # 涨跌额
    volume: int = 0           # 成交量(手)
    turnover: float = 0.0     # 成交额
    high: float = 0.0         # 今日最高
    low: float = 0.0          # 今日最低
    open: float = 0.0         # 今日开盘
    pre_close: float = 0.0    # 昨日收盘
    timestamp: str = ""       # 数据时间


@dataclass
class KLineData:
    """K线数据"""
    code: str = ""
    date: str = ""
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 0
    period: str = "daily"  # daily/weekly/monthly/30min


@dataclass
class AlertState:
    """股票提醒状态"""
    stock_code: str = ""
    stop_loss_price: float = 0.0       # 当前止损价
    take_profit_price: float = 0.0     # 当前止盈价
    stop_loss_triggered: bool = False  # 止损是否触发
    take_profit_triggered: bool = False  # 止盈是否触发
    top_fractal_detected: bool = False # 30min顶分型是否出现
    alert_disabled: bool = False       # 提醒是否被手动关闭
    # 手动覆写标记
    sl_manual: bool = False            # 止损价是否为手动设置
    tp_manual: bool = False            # 止盈价是否为手动设置
    sl_manual_value: float = 0.0       # 手动设置的止损价
    tp_manual_value: float = 0.0       # 手动设置的止盈价
