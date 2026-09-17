"""技术指标测试 — MACD / 分型 / 金叉 / 中枢 / 缩量"""

import numpy as np
import pytest
from core.technical import (
    calc_ma, calc_ema, calc_macd,
    _merge_contains, detect_top_fractal, detect_bottom_fractal,
    get_latest_top_fractal, get_latest_bottom_fractal,
    detect_golden_cross, detect_macd_golden_cross,
    detect_death_cross,
    calc_center_range, check_pullback_to_center,
    is_volume_contraction, is_volume_expansion,
    kline_to_arrays, find_stop_loss_price,
)


class TestMA:
    def test_sma_basic(self):
        closes = np.array([1, 2, 3, 4, 5], dtype=float)
        ma = calc_ma(closes, 3)
        assert np.isnan(ma[0])
        assert np.isnan(ma[1])
        assert ma[2] == 2.0   # (1+2+3)/3
        assert ma[3] == 3.0   # (2+3+4)/3
        assert ma[4] == 4.0   # (3+4+5)/3

    def test_ema_basic(self):
        closes = np.array([10, 11, 12], dtype=float)
        ema = calc_ema(closes, 3)
        assert ema[0] == 10.0
        # alpha = 2/(3+1) = 0.5
        # ema[1] = 0.5*11 + 0.5*10 = 10.5
        assert abs(ema[1] - 10.5) < 0.01

    def test_ma_too_short(self):
        ma = calc_ma(np.array([1.0]), 5)
        assert np.all(np.isnan(ma))


class TestMACD:
    def test_calc_macd_shape(self):
        """MACD 返回三个同长数组"""
        closes = np.random.randn(50) + 10
        dif, dea, bar = calc_macd(closes)
        assert len(dif) == 50
        assert len(dea) == 50
        assert len(bar) == 50

    def test_macd_golden_cross(self):
        """构造已知的MACD金叉场景 — V形反转产生金叉"""
        # 前40根：持续下跌 (EMA12 < EMA26, DIF为负)
        # 后30根：持续上涨 (DIF上穿DEA产生金叉)
        closes = np.array(
            [15.0 - i * 0.15 for i in range(40)] +    # 15 → 9.15
            [9.15 + i * 0.2 for i in range(30)]         # 9.15 → 14.95
        )
        has_gc, idx = detect_macd_golden_cross(closes, lookback=30)
        # V形反弹段应该有MACD金叉
        assert has_gc
        assert idx >= 40  # 金叉应发生在反弹期间

    def test_macd_no_golden_cross_in_downtrend(self):
        """持续下跌中无金叉"""
        closes = np.array([20 - i * 0.3 for i in range(50)])
        has_gc, idx = detect_macd_golden_cross(closes, lookback=10)
        assert not has_gc


class TestFractal:
    def test_top_fractal_detection(self):
        """明确顶分型场景"""
        highs = np.array([10, 12, 11, 15, 13, 10, 9], dtype=float)
        lows  = np.array([8, 10, 9, 13, 11, 8, 7], dtype=float)
        # idx=3 (high=15) 是顶分型: high3 > high2/4, low3 > low2/4
        tops = detect_top_fractal(highs, lows)
        assert len(tops) >= 1

    def test_bottom_fractal_detection(self):
        """明确底分型场景"""
        highs = np.array([12, 10, 11, 8, 9, 13, 12], dtype=float)
        lows  = np.array([10, 8, 9, 5, 7, 11, 10], dtype=float)
        # idx=3 (low=5) 是底分型: low3 < low2/4, high3 < high2/4
        bottoms = detect_bottom_fractal(highs, lows)
        assert len(bottoms) >= 1

    def test_no_fractal_too_short(self):
        """少于3根K线无法形成分型"""
        tops = detect_top_fractal(np.array([10, 12]), np.array([8, 10]))
        assert len(tops) == 0

    def test_get_latest_top_fractal_returns_high(self):
        """get_latest_top_fractal 返回顶分型最高价"""
        highs = np.array([10, 12, 11, 15, 13, 10, 9], dtype=float)
        lows  = np.array([8, 10, 9, 13, 11, 8, 7], dtype=float)
        has_top, idx, top_high = get_latest_top_fractal(highs, lows)
        if has_top:
            assert top_high == highs[idx], f"top_high={top_high} != highs[{idx}]={highs[idx]}"

    def test_top_fractal_index_after_containment_merge(self):
        """发生多根嵌套包含合并时，顶分型索引必须指向极值实际所在的原始K线"""
        # 前4根逐级嵌套包含 → 合并为1根；第5根(索引4, high=15)是顶分型高点所在K线，
        # 随后又被索引5、6两根包含合并。合并序列:
        #   [(7,5), (15,11), (10,7), (6,4)]  → 顶分型在合并索引1
        # 旧的 mi*2 近似会错误地得到索引2 (highs[2]=8 != 15)
        highs = np.array([10, 9, 8, 7, 15, 14, 13, 10, 6], dtype=float)
        lows  = np.array([5, 6, 5.5, 5.2, 9, 10, 11, 7, 4], dtype=float)
        tops = detect_top_fractal(highs, lows)
        assert tops == [4]
        assert highs[tops[0]] == 15.0  # 分型高点所在原始K线
        # get_latest_top_fractal 的索引与价格必须一致
        has_top, idx, top_high = get_latest_top_fractal(highs, lows)
        assert has_top
        assert idx == 4
        assert top_high == 15.0

    def test_bottom_fractal_index_after_containment_merge(self):
        """发生多根嵌套包含合并时，底分型索引必须指向极值实际所在的原始K线"""
        # 前4根逐级嵌套包含 → 合并为1根；索引4(low=1)是底分型低点所在K线，
        # 随后与索引5发生包含合并。合并序列:
        #   [(7,5), (2.5,1), (2.8,1.2), (5,3)]  → 底分型在合并索引1
        # 旧的 mi*2 近似会错误地得到索引2 (lows[2]=5.5 != 1)
        highs = np.array([10, 9, 8, 7, 3, 2.5, 2.8, 5], dtype=float)
        lows  = np.array([5, 6, 5.5, 5.8, 1, 1.5, 1.2, 3], dtype=float)
        bottoms = detect_bottom_fractal(highs, lows)
        assert bottoms == [4]
        assert lows[bottoms[0]] == 1.0  # 分型低点所在原始K线
        has_bottom, idx = get_latest_bottom_fractal(highs, lows)
        assert has_bottom
        assert idx == 4


class TestGoldenCross:
    def test_sma_golden_cross(self):
        """快速上穿慢速产生金叉"""
        closes = np.array([10]*20 + list(range(10, 20)), dtype=float)
        # 快线(5)会上穿慢线(10)
        has_gc, _ = detect_golden_cross(closes, fast_period=5, slow_period=10, lookback=10)
        assert has_gc

    def test_no_cross_in_flat(self):
        """盘整中无金叉"""
        closes = np.full(30, 10.0)
        has_gc, _ = detect_golden_cross(closes, lookback=10)
        assert not has_gc


class TestCenterRange:
    def test_center_calculation(self):
        highs = np.array([10 + i for i in range(20)], dtype=float)
        lows  = np.array([8 + i for i in range(20)], dtype=float)
        ch, cl = calc_center_range(highs, lows, lookback=10)
        assert ch > cl

    def test_pullback_to_center(self):
        """价格在中枢区间内"""
        assert check_pullback_to_center(15.0, 16.0, 14.0)


class TestVolume:
    def test_volume_contraction(self):
        """前5根大成交量，当前缩量"""
        vols = np.array([1000, 1200, 1100, 1300, 1150, 600], dtype=float)
        # 前5均量 = 1150; 600 < 1150 * 0.7 = 805
        assert is_volume_contraction(vols)

    def test_no_contraction(self):
        vols = np.array([1000, 1200, 1100, 1300, 1150, 2000], dtype=float)
        assert not is_volume_contraction(vols)


class TestStopLoss:
    def test_stop_loss_only_up(self):
        """止损价只上移不下移"""
        daily_lows = np.array([10.0, 9.5, 10.2, 9.8, 10.5], dtype=float)
        sl = find_stop_loss_price(daily_lows, prev_stop=10.0)
        assert sl >= 10.0  # 不低于初始止损

    def test_initial_stop_loss(self):
        sl = find_stop_loss_price(np.array([10.0, 10.5, 11.0]), prev_stop=0.0)
        assert sl == 11.0  # 最新最低价
