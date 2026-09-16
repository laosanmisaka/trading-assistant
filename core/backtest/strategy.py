"""回测策略接口 + 当前买点策略（点内时间计算，无未来函数）"""
from abc import ABC, abstractmethod
from enum import Enum
from dataclasses import dataclass

import numpy as np
import pandas as pd

from data.models import KLineData
from core.technical import (
    kline_to_arrays, get_latest_bottom_fractal, detect_macd_golden_cross,
)
from config import GOLDEN_CROSS_LOOKBACK_DAYS, TAKE_PROFIT_LIMITUP_RATIO
from utils.logger import get_logger

logger = get_logger(__name__)


class Action(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass
class Signal:
    """单条交易信号。date 为成交日，price 为成交价。"""
    date: str
    action: Action
    price: float = 0.0
    reason: str = ""


def _resample_weekly(daily_prefix: list[KLineData]):
    """日线前缀 → 周线 OHLC 数组 (highs, lows, closes, opens)

    与 data.market_data.fetch_kline 一致的聚合口径（resample 'W'）。
    传入的是前缀切片，保证点内时间正确。
    """
    if len(daily_prefix) < 3:
        return (np.array([]), np.array([]), np.array([]), np.array([]))

    df = pd.DataFrame({
        "date": pd.to_datetime([k.date for k in daily_prefix]),
        "open": [k.open for k in daily_prefix],
        "high": [k.high for k in daily_prefix],
        "low": [k.low for k in daily_prefix],
        "close": [k.close for k in daily_prefix],
    }).set_index("date")

    agg = df.resample("W").agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
    }).dropna()

    return (
        agg["high"].to_numpy(), agg["low"].to_numpy(),
        agg["close"].to_numpy(), agg["open"].to_numpy(),
    )


class Strategy(ABC):
    """策略抽象基类 — 换策略只需继承并实现 generate_signals，引擎不动。"""

    name = "base"

    @abstractmethod
    def generate_signals(self, daily: list[KLineData]) -> list[Signal]:
        """输入升序日线，输出逐日信号。第 i 日信号必须只用 daily[0..i] 计算。"""


class BuyPointStrategy(Strategy):
    """当前买点策略（v1 简化版）

    入场：周线底分型 **且** 日线 MACD 金叉（原「三选二」缺 60min 第三条件，暂按两条件同时成立触发）。
    出场：买入后日线低点只上移的止损 / 成本 × take_profit_ratio 的止盈。
    """

    name = "buypoint"

    def __init__(
        self,
        take_profit_ratio: float = TAKE_PROFIT_LIMITUP_RATIO,
        golden_cross_lookback: int = GOLDEN_CROSS_LOOKBACK_DAYS,
    ):
        self.take_profit_ratio = take_profit_ratio
        self.golden_cross_lookback = golden_cross_lookback

    def generate_signals(self, daily: list[KLineData]) -> list[Signal]:
        n = len(daily)
        if n < 36:
            return []

        arr = kline_to_arrays(daily)
        opens, highs, lows, closes, volumes = (
            arr["opens"], arr["highs"], arr["lows"], arr["closes"], arr["volumes"],
        )

        signals: list[Signal] = []
        holding = False
        entry_price = 0.0
        stop = 0.0
        target = 0.0

        i = 0
        while i < n:
            if not holding:
                # 入场：T 日收盘确认，T+1 日开盘成交
                if i >= 35 and self._entry_triggered(daily, i, closes, volumes):
                    if i + 1 < n:
                        fill_date = daily[i + 1].date
                        fill_price = round(float(opens[i + 1]), 2)
                        signals.append(Signal(
                            date=fill_date, action=Action.BUY, price=fill_price,
                            reason=f"周底分型+日MACD金叉(确认于{daily[i].date})",
                        ))
                        holding = True
                        entry_price = fill_price
                        stop = float(lows[i + 1])
                        target = round(entry_price * self.take_profit_ratio, 2)
                        i += 2  # 跳过成交日，天然满足 T+1
                        continue
                    break  # 最后一天确认，无次日可成交
                i += 1
            else:
                # 出场：先查日内止损（保守），再查止盈，否则止损只上移
                if lows[i] <= stop:
                    exit_price = stop if opens[i] >= stop else opens[i]
                    signals.append(Signal(
                        date=daily[i].date, action=Action.SELL,
                        price=round(float(exit_price), 2), reason=f"止损({stop:.2f})",
                    ))
                    holding = False
                elif highs[i] >= target:
                    exit_price = target if opens[i] <= target else opens[i]
                    signals.append(Signal(
                        date=daily[i].date, action=Action.SELL,
                        price=round(float(exit_price), 2), reason=f"止盈({target:.2f})",
                    ))
                    holding = False
                else:
                    stop = max(stop, lows[i])
                i += 1

        return signals

    def _entry_triggered(self, daily, i, closes, volumes) -> bool:
        """入场条件：周线底分型 且 日线 MACD 金叉（仅用 daily[0..i]）"""
        # 条件一：周线底分型（复用 get_latest_bottom_fractal）
        w_highs, w_lows, w_closes, _ = _resample_weekly(daily[:i + 1])
        cond1 = False
        if len(w_highs) >= 3:
            has_bottom, idx = get_latest_bottom_fractal(w_highs, w_lows)
            if has_bottom and idx >= len(w_highs) - 4:
                if idx + 2 < len(w_closes):
                    cond1 = w_closes[idx + 2] > w_lows[idx + 1]
                else:
                    cond1 = True

        # 条件二：日线 MACD 金叉 + 量能确认
        has_gc, gc_idx = detect_macd_golden_cross(
            closes[:i + 1], fast=12, slow=26, signal=9,
            lookback=self.golden_cross_lookback,
        )
        cond2 = False
        if has_gc and gc_idx >= 0:
            if gc_idx >= 6:
                prev_avg = np.mean(volumes[gc_idx - 5:gc_idx])
                cond2 = volumes[gc_idx] >= prev_avg
            else:
                cond2 = True

        return cond1 and cond2
