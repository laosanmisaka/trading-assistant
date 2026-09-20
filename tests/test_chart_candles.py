"""K 线绘制批量化的等价性与 artist 数量测试

背景：`ui/chart_widget.py::_draw_kline_manual` 原先逐根蜡烛调用
`ax1.plot()`（影线）+ `ax1.bar()`（实体），250 根 K 线约产生 500 个 artist；
缩放/重绘时 matplotlib 要逐个 artist 走一遍布局与变换，是滚轮卡顿的主因。

现改为 `LineCollection` + `PolyCollection` 各一次绘制。本文件钉住两件事：

  1. **等价性** —— 批量绘制出的影线线段与实体多边形，必须与逐根绘制
     逐个坐标完全相同（含「平开平收」时高度退化为整根高低区间的处理）。
  2. **数量解耦** —— candle 的 artist 数量必须是常数，不随 K 线根数增长。

另：`add_collection` 会把几何纳入 `ax.dataLim`，自动缩放行为因此与逐根
绘制一致 —— 这条也一并断言，否则图表会被裁切。
"""

import sys

import numpy as np
import pandas as pd
import pytest


@pytest.fixture(scope="module")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


# ============================================================
# 测试数据与工具
# ============================================================

def _df(n=250, seed=7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 10.0 + np.cumsum(rng.normal(0, 0.15, n))
    open_ = close + rng.normal(0, 0.08, n)
    high = np.maximum(open_, close) + rng.uniform(0, 0.2, n)
    low = np.minimum(open_, close) - rng.uniform(0, 0.2, n)
    return pd.DataFrame(
        {
            "Open": open_,
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": rng.uniform(1e5, 1e6, n),
        },
        index=pd.date_range("2025-01-01", periods=n, freq="D"),
    )


def _bar_width(n: int) -> float:
    """与实现一致的自适应蜡烛宽度"""
    return max(0.3, min(0.8, 200.0 / max(n, 1)))


def _legacy_geometry(df: pd.DataFrame, width: float):
    """逐根绘制的几何 —— 即重构前的实现，用作等价性基准

    原代码:
        ax1.plot([i, i], [row["Low"], row["High"]], ...)
        body_bottom = min(row["Open"], row["Close"])
        body_height = abs(row["Close"] - row["Open"])
        if body_height < 0.0001:
            body_height = max(row["High"] - row["Low"], 0.001)
        ax1.bar(i, body_height, width=width, bottom=body_bottom, ...)
    """
    half = width / 2.0
    wicks, bodies = [], []
    for i, (_idx, row) in enumerate(df.iterrows()):
        wicks.append([(i, float(row["Low"])), (i, float(row["High"]))])

        bottom = min(row["Open"], row["Close"])
        height = abs(row["Close"] - row["Open"])
        if height < 0.0001:
            height = max(row["High"] - row["Low"], 0.001)
        bodies.append([
            (i - half, float(bottom)), (i + half, float(bottom)),
            (i + half, float(bottom + height)), (i - half, float(bottom + height)),
        ])
    return wicks, bodies


def _path_points(path):
    """把 Path 的顶点取出为普通元组；去掉 closed=True 造成的重复末点"""
    pts = [(float(x), float(y)) for x, y in path.vertices]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts


def _draw(n=250, df=None):
    """渲染一次并返回 (ax_price, ax_volume)"""
    from ui.chart_widget import ChartTabWidget

    if df is None:
        df = _df(n=n)
    widget = ChartTabWidget("daily")
    widget.canvas.fig.clear()
    widget._draw_kline_manual(df, "test")
    axes = widget.canvas.fig.get_axes()
    assert len(axes) == 2, "应绘制价格与成交量两个子图"
    return axes[0], axes[1]


def _assert_matches_legacy(df: pd.DataFrame):
    """批量绘制结果必须与逐根绘制逐坐标一致"""
    ax_price, _ = _draw(df=df)
    width = _bar_width(len(df))
    exp_wicks, exp_bodies = _legacy_geometry(df, width)

    wick_col, body_col = ax_price.collections
    got_wicks = [[tuple(map(float, pt)) for pt in seg]
                 for seg in wick_col.get_segments()]
    assert got_wicks == exp_wicks, "影线线段与逐根绘制不一致"

    got_bodies = [_path_points(p) for p in body_col.get_paths()]
    assert got_bodies == exp_bodies, "实体多边形与逐根绘制不一致"


# ============================================================
# 1. 等价性
# ============================================================

class TestGeometryMatchesLegacyDrawing:

    @pytest.mark.parametrize("n", [3, 7, 60, 250])
    def test_wicks_and_bodies_match(self, qapp, n):
        _assert_matches_legacy(_df(n=n))

    def test_flat_candle_uses_full_high_low_range(self, qapp):
        """平开平收（Open == Close）时高度退化为整根高低区间

        这是原实现里唯一的非平凡分支，容易被重构漏掉 —— 漏掉后一字板
        会变成一条不可见的零高度线。
        """
        df = _df(n=12)
        df.iloc[4, df.columns.get_loc("Open")] = 10.0
        df.iloc[4, df.columns.get_loc("Close")] = 10.0
        df.iloc[4, df.columns.get_loc("High")] = 10.6
        df.iloc[4, df.columns.get_loc("Low")] = 9.4

        _assert_matches_legacy(df)

        # 直接确认该根实体高度确实是高低区间，而不是 0
        ax_price, _ = _draw(df=df)
        body = _path_points(ax_price.collections[1].get_paths()[4])
        ys = [pt[1] for pt in body]
        assert max(ys) - min(ys) == pytest.approx(1.2), "一字板应显示整根高低区间"

    def test_all_flat_series(self, qapp):
        """整段都是平开平收 —— 退化的退化"""
        df = _df(n=8)
        df["Open"] = 10.0
        df["Close"] = 10.0
        df["High"] = 10.3
        df["Low"] = 9.7
        _assert_matches_legacy(df)

    def test_colors_follow_up_down_convention(self, qapp):
        """涨红跌绿（中国习惯）—— 颜色不能因为批量化而错位"""
        from config import CHART_COLORS

        df = _df(n=40)
        ax_price, ax_volume = _draw(df=df)

        from matplotlib.colors import to_rgba_array

        is_up = (df["Close"] >= df["Open"]).to_numpy()
        expected = to_rgba_array(list(
            np.where(is_up, CHART_COLORS["up"], CHART_COLORS["down"])))
        expected_vol = to_rgba_array(list(
            np.where(is_up, CHART_COLORS["volume_up"], CHART_COLORS["volume_down"])))

        # 取到的可能是颜色字符串或 RGBA，统一归一化后比较
        # （LineCollection 用 get_colors()，PolyCollection 用 get_facecolor()）
        assert np.allclose(
            to_rgba_array(ax_price.collections[0].get_colors()), expected), "影线颜色错位"

        # 实体与成交量带 alpha（0.9 / 0.7），只比 RGB 通道
        body_rgb = to_rgba_array(ax_price.collections[1].get_facecolor())[:, :3]
        assert np.allclose(body_rgb, expected[:, :3]), "实体颜色错位"

        vol_rgb = to_rgba_array(ax_volume.collections[0].get_facecolor())[:, :3]
        assert np.allclose(vol_rgb, expected_vol[:, :3]), "成交量颜色错位"


# ============================================================
# 2. artist 数量与 K 线根数解耦
# ============================================================

class TestArtistCountDecoupledFromBarCount:
    """原实现在此处的症状：len(ax.lines) == n 且 len(ax.patches) == n"""

    @pytest.mark.parametrize("n", [50, 250, 1000])
    def test_no_per_candle_artists(self, qapp, n):
        ax_price, ax_volume = _draw(n=n)

        assert len(ax_price.patches) == 0, (
            f"{n} 根 K 线产生了 {len(ax_price.patches)} 个 Rectangle —— "
            "不应再逐根 ax.bar()")
        assert len(ax_price.collections) == 2, "蜡烛应由 2 个 collection 承载（影线+实体）"
        assert len(ax_volume.collections) == 1, "成交量应由 1 个 collection 承载"

        # 影线是 LineCollection，不是 n 条 Line2D（MA 线仍走 lines，数量固定）
        assert len(ax_price.lines) <= 4 + 2, (
            f"ax.lines 出现 {len(ax_price.lines)} 条 —— 影线不应再走 Line2D")

    def test_candle_artist_count_is_flat_across_sizes(self, qapp):
        """20 倍根数差，承载蜡烛/成交量的 artist 数量必须完全一致

        只统计 collections 与 patches —— MA 线的条数会随根数变化
        （不足 60 根时跳过 MA60），属预期行为，不计入本断言。
        """
        counts = []
        for n in (50, 1000):
            ax_price, ax_volume = _draw(n=n)
            counts.append((len(ax_price.collections), len(ax_price.patches),
                           len(ax_volume.collections), len(ax_volume.patches)))

        assert counts[0] == counts[1], (
            f"承载蜡烛的 artist 数量随根数变化: 50 根 {counts[0]} vs 1000 根 {counts[1]}")
        assert counts[0] == (2, 0, 1, 0)

    def test_geometry_count_still_matches_bars(self, qapp):
        """artist 变少了，但几何数量不能少 —— 每根仍要有一影线一实体"""
        ax_price, ax_volume = _draw(n=250)

        assert len(ax_price.collections[0].get_segments()) == 250
        assert len(ax_price.collections[1].get_paths()) == 250
        assert len(ax_volume.collections[0].get_paths()) == 250


# ============================================================
# 3. 自动缩放未被破坏
# ============================================================

class TestAutoscaleStillWorks:
    """add_collection 必须把几何纳入 dataLim，否则价格轴会被裁切"""

    def test_price_datalim_covers_all_highs_and_lows(self, qapp):
        df = _df(n=250)
        ax_price, _ = _draw(df=df)

        assert ax_price.dataLim.y0 <= df["Low"].min(), "价格轴下沿未覆盖最低价"
        assert ax_price.dataLim.y1 >= df["High"].max(), "价格轴上沿未覆盖最高价"

    def test_volume_datalim_starts_at_zero(self, qapp):
        df = _df(n=250)
        _, ax_volume = _draw(df=df)

        assert ax_volume.dataLim.y0 <= 0.0, "成交量应从 0 起"
        assert ax_volume.dataLim.y1 >= df["Volume"].max()

    def test_x_datalim_spans_all_bars(self, qapp):
        ax_price, _ = _draw(n=120)

        assert ax_price.dataLim.x0 <= 0
        assert ax_price.dataLim.x1 >= 119


# ============================================================
# 4. 缠论买卖点标注（2026-09-18 新增）
# ============================================================

def _daily_widget(df, marks) -> "ChartTabWidget":
    from ui.chart_widget import ChartTabWidget

    widget = ChartTabWidget("daily")
    widget.canvas.fig.clear()
    widget.chan_marks = marks
    widget._draw_kline_manual(df, "test")
    return widget


def _marks_for(df) -> dict:
    date = lambda i: df.index[i].strftime("%Y-%m-%d")     # noqa: E731
    return {
        "geometry": [
            {"date": date(5), "kind": "一买", "price": float(df["Low"].iloc[5])},
            {"date": date(9), "kind": "二卖", "price": float(df["High"].iloc[9])},
        ],
        "trades": [
            {"buy_date": date(20), "buy_price": float(df["Close"].iloc[20]),
             "sell_date": date(40), "sell_price": float(df["Close"].iloc[40]),
             "return_pct": 3.5},
            {"buy_date": date(50), "buy_price": float(df["Close"].iloc[50]),
             "sell_date": "", "sell_price": None, "return_pct": None},
        ],
    }


class TestChanMarksDrawing:
    """K 线图上的缠论买卖点标注：位置正确、不破坏蜡烛批量化"""

    def test_no_marks_keeps_artist_count(self, qapp):
        """没有标注时不能多出任何 artist（蜡烛批量化的既有契约）"""
        df = _df(n=60)
        widget = _daily_widget(df, {})

        ax_price = widget.canvas.fig.get_axes()[0]
        assert len(ax_price.collections) == 2
        assert len(ax_price.texts) == 0

    def test_marks_add_two_collections_and_labels(self, qapp):
        """买点一批 + 卖点一批（各 1 个 collection），文字逐点一条

        点不能逐个建 artist —— 250 根 K 线上逐点 scatter 会让缩放变卡，
        与蜡烛批量化是同一个理由。
        """
        df = _df(n=60)
        widget = _daily_widget(df, _marks_for(df))
        ax_price = widget.canvas.fig.get_axes()[0]

        assert len(ax_price.collections) == 4, "蜡烛 2 + 买点 1 + 卖点 1"
        # 几何 2 点 + 策略 买1 卖1 + 未平仓 1 = 5 条文字
        assert len(ax_price.texts) == 5

    def test_buy_below_low_sell_above_high(self, qapp):
        """买点标在当根低点下方、卖点标在当根高点上方（不能压住 K 线）"""
        df = _df(n=60)
        widget = _daily_widget(df, _marks_for(df))
        ax_price = widget.canvas.fig.get_axes()[0]

        offsets = [c.get_offsets() for c in ax_price.collections[2:]]
        assert len(offsets) == 2, "买点与卖点各一批"
        for (x, y) in offsets[0]:           # 先画买点那一批
            assert y < float(df["Low"].iloc[int(x)])
        for (x, y) in offsets[1]:
            assert y > float(df["High"].iloc[int(x)])

    def test_legend_lists_strategy_marks(self, qapp):
        df = _df(n=60)
        widget = _daily_widget(df, _marks_for(df))
        ax_price = widget.canvas.fig.get_axes()[0]

        labels = [t.get_text() for t in ax_price.get_legend().get_texts()]
        for expect in ("策略买点", "策略卖点", "日线买点", "日线卖点"):
            assert expect in labels, f"图例缺少「{expect}」"

    def test_dates_outside_chart_are_skipped(self, qapp):
        """标注日期不在图内 → 静默跳过，不抛异常也不画到第 0 根上"""
        df = _df(n=60)
        marks = {"geometry": [{"date": "1999-01-01", "kind": "一买", "price": 10.0}],
                 "trades": []}
        widget = _daily_widget(df, marks)
        ax_price = widget.canvas.fig.get_axes()[0]

        assert len(ax_price.collections) == 2
        assert len(ax_price.texts) == 0

    def test_only_daily_tab_draws_marks(self, qapp):
        """周线/月线不画 —— 缠论点是日线级别的，硬画会错位"""
        from ui.chart_widget import ChartTabWidget

        df = _df(n=60)
        widget = ChartTabWidget("weekly")
        widget.canvas.fig.clear()
        widget.chan_marks = _marks_for(df)
        widget._draw_kline_manual(df, "test")

        ax_price = widget.canvas.fig.get_axes()[0]
        assert len(ax_price.collections) == 2

    def test_set_chan_marks_redraws_only_with_data(self, qapp):
        """set_chan_marks 只在已有行情时重绘（否则空画布重绘没意义）"""
        from ui.chart_widget import ChartTabWidget

        widget = ChartTabWidget("daily")
        widget.klines = []
        widget.set_chan_marks({"geometry": [], "trades": []})   # 不抛异常
        assert widget.canvas.fig.get_axes() == [], "没有行情时不该重绘"
        assert widget.chan_marks == {"geometry": [], "trades": []}

    def test_no_six_pulse_keeps_old_artist_count(self, qapp):
        """没有六脉数据时不多出 collection、图例也不多出条目（老行为不变）"""
        df = _df(n=60)
        widget = _daily_widget(df, _marks_for(df))
        ax_price = widget.canvas.fig.get_axes()[0]

        assert len(ax_price.collections) == 4
        labels = [t.get_text() for t in ax_price.get_legend().get_texts()]
        assert "六脉买点" not in labels and "六脉卖点" not in labels

    def test_six_pulse_drawn_outside_chan_marks(self, qapp):
        """六脉神剑是独立策略：形状与缠论点不同、且画在更外圈

        两套策略的买点会落在同一根 bar 上（实测有重叠），如果形状/位置
        一样就看不出是两套信号 —— 这正是不该混读的那件事。
        """
        df = _df(n=60)
        date = lambda i: df.index[i].strftime("%Y-%m-%d")     # noqa: E731
        marks = _marks_for(df)
        marks["six_pulse"] = [
            # 与 geometry 的「一买」同一天，故意压在同一根 bar 上
            {"buy_date": date(5), "buy_price": float(df["Close"].iloc[5]),
             "sell_date": date(30), "sell_price": float(df["Close"].iloc[30]),
             "return_pct": -1.2},
        ]
        widget = _daily_widget(df, marks)
        ax_price = widget.canvas.fig.get_axes()[0]

        # 蜡烛 2 + 缠论（买/卖）2 + 六脉（买/卖）2 = 6
        assert len(ax_price.collections) == 6
        chan_buy, six_buy = ax_price.collections[2], ax_price.collections[-2]
        six_sell = ax_price.collections[-1]

        # 形状必须不同（三角 vs 菱形），否则图上看不出是两套信号
        assert (six_buy.get_paths()[0].vertices.shape[0]
                != chan_buy.get_paths()[0].vertices.shape[0])

        # 位置更外圈一档 —— 同一天的缠论买点与六脉买点不能叠在一起
        for (x, y) in six_buy.get_offsets():
            assert y < float(df["Low"].iloc[int(x)]) * 0.985, "六脉买点要在缠论买点外侧"
        for (x, y) in six_sell.get_offsets():
            assert y > float(df["High"].iloc[int(x)]) * 1.015, "六脉卖点要在缠论卖点外侧"

    def test_legend_lists_six_pulse(self, qapp):
        """图例要显式列出六脉两项，否则图上的菱形没有出处"""
        df = _df(n=60)
        marks = _marks_for(df)
        marks["six_pulse"] = [
            {"buy_date": df.index[15].strftime("%Y-%m-%d"), "buy_price": 1.0,
             "sell_date": "", "sell_price": None, "return_pct": None},
        ]
        widget = _daily_widget(df, marks)
        ax_price = widget.canvas.fig.get_axes()[0]

        labels = [t.get_text() for t in ax_price.get_legend().get_texts()]
        for expect in ("策略买点", "策略卖点", "日线买点", "日线卖点",
                       "六脉买点", "六脉卖点"):
            assert expect in labels, f"图例缺少「{expect}」"

    def test_six_pulse_only_when_daily_tab(self, qapp):
        """六脉也是日线级别的，周线页不画"""
        from ui.chart_widget import ChartTabWidget

        df = _df(n=60)
        marks = _marks_for(df)
        marks["six_pulse"] = [
            {"buy_date": df.index[15].strftime("%Y-%m-%d"), "buy_price": 1.0,
             "sell_date": "", "sell_price": None, "return_pct": None},
        ]
        widget = ChartTabWidget("weekly")
        widget.canvas.fig.clear()
        widget.chan_marks = marks
        widget._draw_kline_manual(df, "test")

        ax_price = widget.canvas.fig.get_axes()[0]
        assert len(ax_price.collections) == 2, "周线页不该画任何标注"


class TestChanMarksWiring:
    """ChartWidget 把标注只投给日线页，且拒绝串台"""

    def test_marks_go_to_daily_tab(self, qapp):
        from ui.chart_widget import ChartWidget

        widget = ChartWidget()
        widget.daily_tab.code = "600519"
        widget.weekly_tab.code = "600519"

        marks = {"geometry": [], "trades": [{"buy_date": "2026-01-05"}]}
        widget.set_chan_marks("600519", marks)

        assert widget.daily_tab.chan_marks == marks
        assert widget.weekly_tab.chan_marks == {}

    def test_marks_for_other_code_are_dropped(self, qapp):
        """后台算完时用户可能已切股票 —— 不是当前这只就丢掉"""
        from ui.chart_widget import ChartWidget

        widget = ChartWidget()
        widget.daily_tab.code = "600519"

        widget.set_chan_marks("000001", {"geometry": [], "trades": []})

        assert widget.daily_tab.chan_marks == {}
