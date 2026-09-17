"""买点扫描引擎 — 三选二策略 (周线底分型 + 日线MACD金叉 + 30分钟缩量回踩中枢)
使用QThread异步扫描，不阻塞UI"""

from datetime import datetime
from typing import Callable

import numpy as np

from PyQt5.QtCore import QThread, pyqtSignal

from data.models import BuyPointState, KLineData
from data.market_data_manager import get_data_manager
from core.technical import (
    kline_to_arrays,
    get_latest_bottom_fractal,
    detect_macd_golden_cross,
    calc_center_range, check_pullback_to_center, is_volume_contraction,
)
from config import (
    GOLDEN_CROSS_LOOKBACK_DAYS,
    VOLUME_CONTRACTION_RATIO,
    CENTER_LOOKBACK_WEEKS,
)
import traceback

from utils.logger import get_logger

logger = get_logger(__name__)


class BuyPointScanner:
    """买点扫描器 — 三条件满足其二触发 (同步版本，供直接调用)"""

    def __init__(self):
        self._states: dict[str, BuyPointState] = {}

    def get_state(self, code: str) -> BuyPointState:
        if code not in self._states:
            self._states[code] = BuyPointState(stock_code=code)
        return self._states[code]

    def scan(self, code: str, callback: Callable = None) -> BuyPointState:
        """
        扫描某股票的买点
        返回 BuyPointState，同时通过 callback 异步通知
        """
        state = self.get_state(code)
        state.last_checked = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # ---- 条件1: 周线底分型 ----
        weekly_bottom = self._check_weekly_bottom_fractal(code)
        state.weekly_bottom_fractal = weekly_bottom

        # ---- 条件2: 日线MACD金叉 ----
        daily_macd_gc = self._check_daily_macd_golden_cross(code)
        state.daily_golden_cross = daily_macd_gc

        # ---- 条件3: 缩量回踩中枢 (使用短周期K线) ----
        shallow_pullback = self._check_shallow_pullback(code)
        state.shallow_pullback_center = shallow_pullback

        # ---- 综合判定: 三选二 ----
        signal_count = sum([weekly_bottom, daily_macd_gc, shallow_pullback])
        state.buy_point_triggered = signal_count >= 2

        # 生成信号详情
        parts = []
        if weekly_bottom:
            parts.append("周底分型")
        if daily_macd_gc:
            parts.append("日MACD金叉")
        if shallow_pullback:
            parts.append("回踩中枢缩量")
        state.signal_details = "+".join(parts) if parts else "无"

        self._states[code] = state

        logger.info(
            f"买点扫描 {code}: 周底分型={weekly_bottom} "
            f"日MACD金叉={daily_macd_gc} 缩量回踩={shallow_pullback} "
            f"→ {'触发!' if state.buy_point_triggered else '未触发'} "
            f"(信号: {state.signal_details})"
        )

        # 回调通知
        if callback:
            result = {
                "code": code,
                "triggered": state.buy_point_triggered,
                "weekly_bottom_fractal": weekly_bottom,
                "daily_golden_cross": daily_macd_gc,
                "shallow_pullback_center": shallow_pullback,
                "signal_count": signal_count,
                "signal_details": state.signal_details,
            }
            callback(code, result)

        return state

    def _check_weekly_bottom_fractal(self, code: str) -> bool:
        """检查周线底分型 (数据源: Manager)"""
        manager = get_data_manager()
        weekly_klines = manager.get_klines(code, "weekly", days=365)
        if not weekly_klines:
            return False

        w_arr = kline_to_arrays(weekly_klines)
        has_bottom, idx = get_latest_bottom_fractal(w_arr["highs"], w_arr["lows"])

        if has_bottom and idx >= len(weekly_klines) - 4:
            # 确认: 底分型第三K线(右侧K线)收盘 > 底分型最低价
            #
            # idx 的语义见 core/technical.py:detect_bottom_fractal ——
            # 它是「分型最低价实际所在的原始K线」，即三根中的中间那根。
            # 故「第三K线」= idx + 1，「底分型最低价」= lows[idx]。
            #
            # 注意(2026-09-17)：在底分型定义下 lows[idx+1] > lows[idx]，
            # 且收盘价恒 >= 当日最低价，因此本条件恒成立 ——
            # 该函数实际等价于「最近 4 根周线内存在底分型」。
            # 若需要更严格的二买确认规则，见 docs/KNOWN_ISSUES.md KI-004。
            if idx + 1 < len(w_arr["closes"]):
                bottom_low = w_arr["lows"][idx]
                confirm_close = w_arr["closes"][idx + 1]
                return confirm_close > bottom_low
            return True
        return False

    def _check_daily_macd_golden_cross(self, code: str) -> bool:
        """检查日线MACD金叉 (数据源: Manager)"""
        manager = get_data_manager()
        daily_klines = manager.get_klines(code, "daily", days=120)
        if not daily_klines:
            return False

        d_arr = kline_to_arrays(daily_klines)
        has_gc, gc_idx = detect_macd_golden_cross(
            d_arr["closes"],
            fast=12, slow=26, signal=9,
            lookback=GOLDEN_CROSS_LOOKBACK_DAYS,
        )

        if has_gc and gc_idx >= 0:
            # 确认: 金叉当日成交量放大
            if gc_idx >= 6:
                prev_avg = np.mean(d_arr["volumes"][gc_idx - 5:gc_idx])
                return d_arr["volumes"][gc_idx] >= prev_avg
            return True
        return False

    def _check_shallow_pullback(self, code: str) -> bool:
        """检查缩量回踩中枢 — 使用已入库的 60min K线"""
        manager = get_data_manager()
        rows = manager.get_minute_klines_from_db(code, "60min")
        if not rows:
            return False

        klines = [KLineData(
            code=code, date=r["timestamp"][:10],
            open=r["open"], high=r["high"], low=r["low"],
            close=r["close"], volume=r["volume"], period="60min",
        ) for r in rows]

        m_arr = kline_to_arrays(klines)
        if len(m_arr["closes"]) == 0:
            return False

        # 计算中枢
        center_high, center_low = calc_center_range(
            m_arr["highs"], m_arr["lows"],
            lookback=CENTER_LOOKBACK_WEEKS * 5,
        )

        # 当前价格是否回踩中枢
        current_close = m_arr["closes"][-1]
        in_pullback = check_pullback_to_center(current_close, center_high, center_low)

        # 是否缩量
        vol_contract = is_volume_contraction(
            m_arr["volumes"], period=5, ratio=VOLUME_CONTRACTION_RATIO
        )

        return in_pullback and vol_contract


# ============================================================
# 异步买点扫描工作线程
# ============================================================

class BuyPointScanWorker(QThread):
    """买点扫描后台线程 — 批量串行扫描，不阻塞UI

    原实现为每只股票各起一个 QThread：无并发上限、无 deleteLater 回收，
    且靠主线程计数归零来复位扫描状态（任一 worker 异常退出即永久停摆）。
    改为单线程串行扫描：
    - 复用同一个 BuyPointScanner（其状态按 code 分键，可安全共享）
    - scan_done 每只股票发一次，UI 仍能逐只刷新
    - batch_finished 在 run() 末尾必然触发，复位不依赖计数
    """
    scan_done = pyqtSignal(str, dict)   # (code, result_dict)
    batch_finished = pyqtSignal(int)    # 本轮成功扫描的只数

    def __init__(self, codes: list[str], parent=None):
        super().__init__(parent)
        self.codes = list(codes)

    def run(self):
        scanner = BuyPointScanner()
        done = 0
        for code in self.codes:
            try:
                scanner.scan(code, callback=self._on_result)
                done += 1
            except Exception:
                logger.error(
                    f"买点扫描异常 ({code}):\n{traceback.format_exc()}")
                self.scan_done.emit(code, {
                    "code": code,
                    "triggered": False,
                    "error": traceback.format_exc(),
                })
        self.batch_finished.emit(done)

    def _on_result(self, code: str, result: dict):
        """扫描完成回调（在子线程中，通过信号发回主线程）"""
        self.scan_done.emit(code, result)
