"""止损止盈引擎 — 实时计算止损/止盈线，检测触发条件，支持手动覆写"""

from datetime import datetime, timedelta
from typing import Optional, Tuple

import numpy as np

from data.models import AlertState, RealtimeQuote, KLineData
from data.market_data_manager import get_data_manager
from data.database import (
    get_position_summary, get_first_buy_date, is_alert_disabled,
    get_manual_alert, set_manual_alert, clear_manual_alert,
)
from core.technical import (
    kline_to_arrays, find_stop_loss_price, get_latest_top_fractal,
)
from config import TAKE_PROFIT_LIMITUP_RATIO
from utils.logger import get_logger

logger = get_logger(__name__)


class AlertEngine:
    """止损止盈计算引擎（支持手动覆写 + 冲突检测）"""

    def __init__(self):
        self._states: dict[str, AlertState] = {}

    def get_state(self, code: str) -> AlertState:
        """获取某股票的提醒状态（首次加载时从数据库恢复手动设置）"""
        if code not in self._states:
            state = AlertState(stock_code=code)
            # 从数据库恢复手动设置
            manual = get_manual_alert(code)
            if manual["sl_active"] and manual["sl_price"] > 0:
                state.sl_manual = True
                state.sl_manual_value = manual["sl_price"]
                state.stop_loss_price = manual["sl_price"]
                logger.debug(f"{code} 加载手动止损={manual['sl_price']:.2f}")
            if manual["tp_active"] and manual["tp_price"] > 0:
                state.tp_manual = True
                state.tp_manual_value = manual["tp_price"]
                state.take_profit_price = manual["tp_price"]
                logger.debug(f"{code} 加载手动止盈={manual['tp_price']:.2f}")
            self._states[code] = state
        return self._states[code]

    def set_manual_sl(self, code: str, price: float) -> None:
        """手动设置止损价"""
        state = self.get_state(code)
        state.sl_manual = True
        state.sl_manual_value = price
        state.stop_loss_price = price
        set_manual_alert(code, sl_active=True, sl_price=price)
        logger.info(f"{code} 手动止损设置为 {price:.2f}")

    def set_manual_tp(self, code: str, price: float) -> None:
        """手动设置止盈价"""
        state = self.get_state(code)
        state.tp_manual = True
        state.tp_manual_value = price
        state.take_profit_price = price
        set_manual_alert(code, tp_active=True, tp_price=price)
        logger.info(f"{code} 手动止盈设置为 {price:.2f}")

    def clear_manual(self, code: str, field: str = "all") -> None:
        """清除手动设置，恢复自动模式
        field: 'sl' | 'tp' | 'all'
        """
        state = self.get_state(code)
        if field in ("sl", "all"):
            state.sl_manual = False
            state.sl_manual_value = 0.0
            clear_manual_alert(code, "sl")
            logger.info(f"{code} 止损恢复自动模式")
        if field in ("tp", "all"):
            state.tp_manual = False
            state.tp_manual_value = 0.0
            state.top_fractal_detected = False  # 重新计算止盈时重新检测顶分型
            clear_manual_alert(code, "tp")
            logger.info(f"{code} 止盈恢复自动模式")

    def calc_stop_loss(
        self,
        code: str,
        current_daily_low: float,
    ) -> Tuple[float, Optional[dict]]:
        """
        计算止损价（自动逻辑）
        规则:
        - 初始止损 = 买入当日最低价
        - 每日更新: max(昨日止损, 今日最低价)  → 只上移不下移
        - 如果存在手动设置，返回冲突信息而不自动更新

        返回: (当前止损价, 冲突信息或None)
              冲突信息 = {field: 'sl', auto_value: float, manual_value: float}
        """
        state = self.get_state(code)
        conflict = None

        # 如果手动设置生效中，仅返回当前值，不自动更新
        if state.sl_manual:
            return state.stop_loss_price, None

        # ---- 自动计算逻辑 ----
        new_sl = state.stop_loss_price

        # 如果尚未设置止损，尝试从日线数据获取
        if state.stop_loss_price <= 0:
            first_buy_date = get_first_buy_date(code)
            if first_buy_date:
                manager = get_data_manager()
                klines = manager.get_klines(code, "daily", days=120)
                if klines:
                    arr = kline_to_arrays(klines)
                    buy_idx = -1
                    for i, d in enumerate(arr["dates"]):
                        if str(d)[:10] >= first_buy_date[:10]:
                            buy_idx = i
                            break
                    if buy_idx >= 0:
                        initial_stop = float(arr["lows"][buy_idx])
                        for i in range(buy_idx, len(arr["lows"])):
                            initial_stop = max(initial_stop, float(arr["lows"][i]))
                        new_sl = round(initial_stop, 2)
                        logger.info(
                            f"{code} 初始止损={new_sl:.2f} "
                            f"(买入日{buy_idx}最低{arr['lows'][buy_idx]:.2f} → 买入后最低价最大值)"
                        )

        # 用今日最低价更新
        if new_sl <= 0:
            new_sl = current_daily_low
        else:
            new_sl = max(new_sl, current_daily_low)

        new_sl = round(new_sl, 2)

        # 检查是否与手动设置冲突
        # （如果用户先设手动再清除，state.sl_manual已经是False，直接应用）
        # 这里我们只检查一种边缘情况：数据库中有手动设置但内存中没有
        manual = get_manual_alert(code)
        if manual["sl_active"] and manual["sl_price"] > 0:
            if abs(new_sl - manual["sl_price"]) > 0.001:
                conflict = {
                    "field": "sl",
                    "auto_value": new_sl,
                    "manual_value": manual["sl_price"],
                }
                return state.stop_loss_price, conflict

        state.stop_loss_price = new_sl
        return new_sl, None

    def calc_take_profit(
        self,
        code: str,
        current_price: float,
    ) -> Tuple[float, Optional[dict]]:
        """
        计算止盈价（自动逻辑）
        规则:
        - 默认: 买入价 × 1.10 (涨停价)
        - 检测到30分钟级别缠论顶分型后: 切换为该顶分型的最高价，之后不再更新
        - 如果存在手动设置，返回冲突信息而不自动更新
        - 限流: 30min分型检测每分钟最多查一次 (避免高频调用重复拉API)

        返回: (当前止盈价, 冲突信息或None)
        """
        state = self.get_state(code)
        conflict = None

        # 如果手动设置生效中，仅返回当前值，不自动更新
        if state.tp_manual:
            return state.take_profit_price, None

        # ---- 自动计算逻辑 ----
        new_tp = state.take_profit_price

        # 首次设置: 用涨停价
        if state.take_profit_price <= 0:
            summary = get_position_summary(code)
            if summary["avg_cost"] > 0:
                new_tp = round(summary["avg_cost"] * TAKE_PROFIT_LIMITUP_RATIO, 2)
                logger.info(f"{code} 初始止盈(涨停)={new_tp:.2f}")

        # 如果已检测到顶分型，止盈线已锁定不再更新
        if state.top_fractal_detected:
            return state.take_profit_price, None

        # 限流: 30min分型检测每分钟最多查一次
        import time as _time
        now_ts = _time.time()
        if not hasattr(state, '_last_fractal_check'):
            state._last_fractal_check = 0.0
        if now_ts - state._last_fractal_check < 60:
            return state.take_profit_price, None
        state._last_fractal_check = now_ts

        # 检查是否有30分钟级别顶分型（从入库的 60min 数据读取）
        manager = get_data_manager()
        rows = manager.get_minute_klines_from_db(code, "60min")
        if rows:
            klines_60min = [KLineData(
                code=code, date=r["timestamp"][:10],
                open=r["open"], high=r["high"], low=r["low"],
                close=r["close"], volume=r["volume"], period="60min",
            ) for r in rows]
            arr = kline_to_arrays(klines_60min)
            has_top, idx, top_high = get_latest_top_fractal(arr["highs"], arr["lows"])
            if has_top:
                state.top_fractal_detected = True
                new_tp = round(top_high, 2)
                logger.info(
                    f"{code} 检测到30min顶分型(idx={idx})，"
                    f"止盈线锁定为顶分型最高价={top_high:.2f}"
                )

        # 检查是否与手动设置冲突
        manual = get_manual_alert(code)
        if manual["tp_active"] and manual["tp_price"] > 0:
            if abs(new_tp - manual["tp_price"]) > 0.001:
                conflict = {
                    "field": "tp",
                    "auto_value": new_tp,
                    "manual_value": manual["tp_price"],
                }
                return state.take_profit_price, conflict

        state.take_profit_price = new_tp
        return new_tp, None

    def check_alerts(
        self,
        code: str,
        quote: RealtimeQuote,
    ) -> dict:
        """
        检查是否触发止损/止盈
        返回: {triggered: bool, type: str, trigger_price: float}
        """
        if is_alert_disabled(code):
            return {"triggered": False, "type": "", "trigger_price": 0.0}

        state = self.get_state(code)

        # 止损检查: 价格 <= 止损线
        if state.stop_loss_price > 0 and quote.price <= state.stop_loss_price:
            state.stop_loss_triggered = True
            logger.warning(
                f"⚠ {code} 止损触发! 现价{quote.price:.2f} ≤ 止损{state.stop_loss_price:.2f}"
            )
            return {
                "triggered": True,
                "type": "stop_loss",
                "trigger_price": state.stop_loss_price,
                "message": f"止损触发! 现价{quote.price:.2f} ≤ 止损{state.stop_loss_price:.2f}",
            }

        # 止盈检查
        if state.take_profit_price > 0:
            if state.top_fractal_detected:
                if quote.price <= state.take_profit_price:
                    state.take_profit_triggered = True
                    logger.warning(
                        f"⚠ {code} 止盈触发(顶分型)! "
                        f"现价{quote.price:.2f} ≤ 止盈{state.take_profit_price:.2f}"
                    )
                    return {
                        "triggered": True,
                        "type": "take_profit",
                        "trigger_price": state.take_profit_price,
                        "message": f"止盈触发! 现价{quote.price:.2f} ≤ 止盈{state.take_profit_price:.2f}",
                    }
            else:
                if quote.price >= state.take_profit_price:
                    state.take_profit_triggered = True
                    logger.warning(
                        f"⚠ {code} 止盈触发(涨停)! "
                        f"现价{quote.price:.2f} ≥ 止盈{state.take_profit_price:.2f}"
                    )
                    return {
                        "triggered": True,
                        "type": "take_profit",
                        "trigger_price": state.take_profit_price,
                        "message": f"止盈(涨停)触发! 现价{quote.price:.2f} ≥ 止盈{state.take_profit_price:.2f}",
                    }

        return {"triggered": False, "type": "", "trigger_price": 0.0}

    def update_daily_stop_loss(self, code: str) -> Tuple[float, Optional[dict]]:
        """
        每日更新止损线 (收盘后调用)
        从日线数据重新计算止损线
        返回: (新的止损价, 冲突信息或None)
        """
        # 检查手动覆写
        state = self.get_state(code)
        if state.sl_manual:
            return state.stop_loss_price, None

        manager = get_data_manager()
        klines = manager.get_klines(code, "daily", days=120)
        if not klines:
            return 0.0, None

        arr = kline_to_arrays(klines)
        old_stop = state.stop_loss_price
        new_stop = find_stop_loss_price(arr["lows"], state.stop_loss_price)
        new_stop = round(new_stop, 2)

        # 检查冲突
        manual = get_manual_alert(code)
        if manual["sl_active"] and manual["sl_price"] > 0:
            if abs(new_stop - manual["sl_price"]) > 0.001:
                conflict = {
                    "field": "sl",
                    "auto_value": new_stop,
                    "manual_value": manual["sl_price"],
                }
                return old_stop, conflict

        state.stop_loss_price = new_stop
        if new_stop != old_stop:
            logger.info(f"{code} 每日止损更新: {old_stop:.2f} → {new_stop:.2f}")
        return new_stop, None
