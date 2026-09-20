"""技术指标计算 — 均线/分型/MACD（自研）

⚠️ 这里的「分型」是**自研 K 线包含处理**，与 czsc 的笔/分型不是同一套
（实测两者逐点等价，但本模块只做单序列分型，不做笔/中枢）。
缠论结构一律走 `core/chan.py`。

**已取缔的伪缠论链路（两轮）**
- 2026-09-18 删 `calc_center_range` / `check_pullback_to_center` /
  `is_volume_contraction` / `is_volume_expansion` —— 伪缠论买点扫描
  （`core/buy_point_scanner.py`）专用。旧「中枢」取高 75 分位 / 低 25 分位，
  不是缠论中枢。
- 2026-09-20 删 `get_latest_bottom_fractal` / `detect_golden_cross` /
  `detect_macd_golden_cross` / `detect_death_cross` —— 随伪缠论回测策略
  `core/backtest/strategy.py::BuyPointStrategy` 一并失活。生产代码里
  `get_latest_top_fractal` 仍被 `alert_engine` 用于止盈判定（**活跃，勿删**）。

保留 `detect_bottom_fractal`：它是 `detect_top_fractal` 的对称构件，与
`_merge_contains` 共用实现。当前生产代码无调用方（原调用方是已删的伪缠论策略），
保留是因为它不是伪缠论、且成对删除会让分型模块残缺。**老三确认不要可一并删。**
"""

from typing import Tuple
import numpy as np
from data.models import KLineData


def calc_ma(closes: np.ndarray, period: int) -> np.ndarray:
    """计算移动平均线 (SMA)"""
    if len(closes) < period:
        return np.full_like(closes, np.nan, dtype=float)
    ma = np.full_like(closes, np.nan, dtype=float)
    cumsum = np.cumsum(np.insert(closes, 0, 0))
    ma[period - 1:] = (cumsum[period:] - cumsum[:-period]) / period
    return ma


def calc_ema(closes: np.ndarray, period: int) -> np.ndarray:
    """计算指数移动平均线 (EMA)"""
    if len(closes) < 2:
        return np.array(closes, dtype=float)
    ema = np.full_like(closes, np.nan, dtype=float)
    ema[0] = closes[0]
    alpha = 2.0 / (period + 1)
    for i in range(1, len(closes)):
        ema[i] = alpha * closes[i] + (1 - alpha) * ema[i - 1]
    return ema


def calc_macd(
    closes: np.ndarray,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    计算MACD指标
    返回: (DIF, DEA, MACD柱)
    """
    if len(closes) < slow:
        empty = np.full_like(closes, np.nan, dtype=float)
        return empty, empty, empty

    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)
    dif = ema_fast - ema_slow
    dea = calc_ema(dif, signal)
    macd_bar = 2 * (dif - dea)

    return dif, dea, macd_bar


# ============================================================
# 分型检测 (Fractal Detection) — 缠论标准分型
# ============================================================

def _merge_contains(highs: np.ndarray, lows: np.ndarray) -> Tuple[list, list, list]:
    """
    K线包含处理（向上/向下）
    将存在包含关系的K线合并为单根K线
    返回: (merged_highs, merged_lows, index_map)
    index_map[mi] = 合并后第 mi 根K线吞掉的原始K线索引列表（升序），
    用于把合并序列中的分型索引精确还原回原始序列。
    """
    n = len(highs)
    if n < 2:
        return list(highs), list(lows), [[i] for i in range(n)]

    m_highs = [highs[0]]
    m_lows = [lows[0]]
    index_map = [[0]]
    direction = 0  # 0=unknown, 1=up, -1=down

    for i in range(1, n):
        prev_h, prev_l = m_highs[-1], m_lows[-1]
        cur_h, cur_l = highs[i], lows[i]

        # 判断包含关系
        if (cur_h <= prev_h and cur_l >= prev_l) or (cur_h >= prev_h and cur_l <= prev_l):
            if direction == 1 or (direction == 0 and cur_h > prev_h):
                # 向上处理: 取高高, 高低
                m_highs[-1] = max(prev_h, cur_h)
                m_lows[-1] = max(prev_l, cur_l)
                direction = 1
            else:
                # 向下处理: 取低高, 低低
                m_highs[-1] = min(prev_h, cur_h)
                m_lows[-1] = min(prev_l, cur_l)
                direction = -1
            index_map[-1].append(i)
        else:
            # 无包含关系
            m_highs.append(cur_h)
            m_lows.append(cur_l)
            index_map.append([i])
            direction = 1 if cur_h > prev_h else -1

    return m_highs, m_lows, index_map


def _resolve_fractal_index(
    mi: int,
    index_map: list,
    values: np.ndarray,
    extreme: float,
) -> int:
    """
    将合并序列中的分型索引 mi 还原为原始序列索引。
    一根合并K线可能吞掉多根原始K线，分型极值（顶分型取高点、底分型取低点）
    实际只来自其中一根：在 index_map[mi] 中找到第一个取得该极值的原始索引。
    取"最早出现"是因为极值首次形成的位置才是分型极值K线；
    对下游新近度判断而言这也是更保守（更不容易误判为新近分型）的选择。
    """
    candidates = index_map[mi]
    for i in candidates:
        if values[i] == extreme:
            return i
    return candidates[-1]


def detect_top_fractal(highs: np.ndarray, lows: np.ndarray) -> list[int]:
    """
    检测顶分型
    先进行包含处理，然后在处理后序列中找顶分型
    顶分型定义: 中间K线最高价 > 左K线最高价 且 > 右K线最高价,
               且中间K线最低价 > 左K线最低价 且 > 右K线最低价
    返回: 原始K线序列中顶分型所在的索引列表
          （若中间K线由包含合并产生，取分型最高价实际所在的原始K线）
    """
    n = len(highs)
    if n < 3:
        return []

    m_highs, m_lows, index_map = _merge_contains(highs, lows)

    top_fractals = []
    for mi in range(1, len(m_highs) - 1):
        if (m_highs[mi] > m_highs[mi - 1] and m_highs[mi] > m_highs[mi + 1] and
                m_lows[mi] > m_lows[mi - 1] and m_lows[mi] > m_lows[mi + 1]):
            top_fractals.append(
                _resolve_fractal_index(mi, index_map, highs, m_highs[mi])
            )

    return top_fractals


def detect_bottom_fractal(highs: np.ndarray, lows: np.ndarray) -> list[int]:
    """
    检测底分型
    底分型定义: 中间K线最低价 < 左K线最低价 且 < 右K线最低价,
               且中间K线最高价 < 左K线最高价 且 < 右K线最高价
    返回: 原始K线序列中底分型所在的索引列表
          （若中间K线由包含合并产生，取分型最低价实际所在的原始K线）
    """
    n = len(highs)
    if n < 3:
        return []

    m_highs, m_lows, index_map = _merge_contains(highs, lows)

    bottom_fractals = []
    for mi in range(1, len(m_highs) - 1):
        if (m_lows[mi] < m_lows[mi - 1] and m_lows[mi] < m_lows[mi + 1] and
                m_highs[mi] < m_highs[mi - 1] and m_highs[mi] < m_highs[mi + 1]):
            bottom_fractals.append(
                _resolve_fractal_index(mi, index_map, lows, m_lows[mi])
            )

    return bottom_fractals


def get_latest_top_fractal(highs: np.ndarray, lows: np.ndarray) -> Tuple[bool, int, float]:
    """
    获取最近的顶分型
    返回: (是否存在, 索引, 顶分型最高价)
    """
    tops = detect_top_fractal(highs, lows)
    if tops:
        idx = tops[-1]
        return True, idx, float(highs[idx])
    return False, -1, 0.0


# ============================================================
# K线数据转换工具
# ============================================================

def kline_to_arrays(kline_list: list[KLineData]) -> dict:
    """将KLineData列表转为numpy数组"""
    if not kline_list:
        return {
            "dates": np.array([]), "opens": np.array([]),
            "highs": np.array([]), "lows": np.array([]),
            "closes": np.array([]), "volumes": np.array([]),
        }
    return {
        "dates": np.array([k.date for k in kline_list]),
        "opens": np.array([k.open for k in kline_list], dtype=float),
        "highs": np.array([k.high for k in kline_list], dtype=float),
        "lows": np.array([k.low for k in kline_list], dtype=float),
        "closes": np.array([k.close for k in kline_list], dtype=float),
        "volumes": np.array([k.volume for k in kline_list], dtype=float),
    }


def find_stop_loss_price(daily_lows: np.ndarray, prev_stop: float = 0.0) -> float:
    """
    计算止损价
    止损线 = max(昨日止损线, 今日最低价)
    首次设置时(prev_stop=0): 使用最近一日最低价
    """
    if len(daily_lows) == 0:
        return 0.0

    today_low = daily_lows[-1]
    if prev_stop <= 0:
        return float(today_low)
    return float(max(prev_stop, today_low))
