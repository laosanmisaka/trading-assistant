# -*- coding: utf-8 -*-
"""缠论可视化（core/chan_viz.py）的单元测试

不联网：行情用合成 K 线，ECharts 库文件用临时文本桩，
只验证「数据组装」与「HTML 渲染」这两块纯逻辑。
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta

import pytest

from core.chan_viz import (
    _find_column, _naive, _nearest_idx,
    build_payload, normalize_code, render_html, transitions,
)
from data.models import KLineData


# ----------------------------------------------------------------------
# 合成 K 线
# ----------------------------------------------------------------------

def make_klines(n: int = 400, freq_per_day: int = 8, start=date(2025, 1, 6)):
    """造一段能算出笔/中枢的 30 分钟 K 线（含趋势与回撤，不是纯随机）"""
    out, d, t = [], start, datetime(2025, 1, 6, 10, 0)
    price = 100.0
    bars_in_day = 0
    for i in range(n):
        wave = 6.0 * math.sin(i / 23.0) + 2.5 * math.sin(i / 7.0)
        price = 100.0 + wave + i * 0.02
        o = price - 0.25
        c = price + 0.25
        out.append(KLineData(
            code="TEST", date=str(t), open=round(o, 3),
            high=round(max(o, c) + 0.4, 3), low=round(min(o, c) - 0.4, 3),
            close=round(c, 3), volume=10000 + i, period="30min"))
        t += timedelta(minutes=30)
        bars_in_day += 1
        if bars_in_day == freq_per_day:
            bars_in_day = 0
            d += timedelta(days=1)
            while d.weekday() >= 5:
                d += timedelta(days=1)
            t = datetime(d.year, d.month, d.day, 10, 0)
    return out


def fake_echarts(tmp_path):
    p = tmp_path / "echarts.min.js"
    p.write_text("/* fake echarts */\n", encoding="utf-8")
    return str(p)


# ----------------------------------------------------------------------
# 代码归一化
# ----------------------------------------------------------------------

@pytest.mark.parametrize("raw,expect", [
    ("600519", "sh600519"),
    ("sh600519", "sh600519"),
    ("SH600519", "sh600519"),
    ("600519.SH", "sh600519"),
    ("000001", "sz000001"),
    ("000001.SZ", "sz000001"),
    ("300750", "sz300750"),
    ("688981", "sh688981"),
    ("430047", "bj430047"),
])
def test_normalize_code(raw, expect):
    assert normalize_code(raw) == expect


# ----------------------------------------------------------------------
# 状态跃迁（与 chan_strategy 的口径必须一致）
# ----------------------------------------------------------------------

def test_transitions_only_marks_edges():
    """持续状态只在第一次变真时标一次，不能每根 bar 都标"""
    import pandas as pd
    hit = pd.Series([False, False, True, True, True, False, True])
    assert transitions(hit) == [2, 6]


def test_transitions_all_false():
    import pandas as pd
    assert transitions(pd.Series([False, False])) == []


def test_transitions_handles_nan():
    """czsc 预热区间是 NaN，不能当成 True"""
    import pandas as pd
    hit = pd.Series([float("nan"), True, True])
    assert transitions(hit) == [1]


# ----------------------------------------------------------------------
# 时间对齐
# ----------------------------------------------------------------------

def test_naive_strips_timezone():
    """czsc 输出的 dt 带 UTC 时区，必须去时区后才能与本地时间对齐"""
    import pandas as pd
    s = pd.Series(pd.to_datetime(["2026-05-18 11:00:00+00:00"]))
    assert _naive(s).dt.tz is None
    assert str(_naive(s).iloc[0]) == "2026-05-18 11:00:00"


def test_nearest_idx_hits_exact_and_nearest():
    import pandas as pd
    base = pd.to_datetime(["2026-01-05 10:00", "2026-01-05 10:30",
                           "2026-01-05 11:00", "2026-01-05 13:30"])
    dt_list = list(base)
    keys = [t.value for t in dt_list]
    assert _nearest_idx(dt_list[2], dt_list, keys) == 2
    # 11:05 落在 11:00 与 13:30 之间；11:00 更近
    assert _nearest_idx(pd.Timestamp("2026-01-05 11:05"), dt_list, keys) == 2
    # 12:50 更靠近 13:30
    assert _nearest_idx(pd.Timestamp("2026-01-05 12:50"), dt_list, keys) == 3
    # 越界向两端收敛，不抛 IndexError
    assert _nearest_idx(pd.Timestamp("2000-01-01"), dt_list, keys) == 0
    assert _nearest_idx(pd.Timestamp("2099-01-01"), dt_list, keys) == 3


# ----------------------------------------------------------------------
# 列名定位
# ----------------------------------------------------------------------

def test_find_column_matches_prefix_and_tokens():
    cols = ["30分钟_D1B_BUY1", "30分钟_D1B_SELL1",
            "30分钟_D1#SMA#21_BS2辅助V230320",
            "30分钟_D1#SMA#34_BS3辅助V230318", "日线_D1B_BUY1"]
    assert _find_column(cols, "30分钟_D1", "BUY1") == "30分钟_D1B_BUY1"
    assert _find_column(cols, "30分钟_D1", "SELL1") == "30分钟_D1B_SELL1"
    assert _find_column(cols, "30分钟_D1", "BS2辅助V230320") \
        == "30分钟_D1#SMA#21_BS2辅助V230320"
    # 三买辅助 与 二买辅助 不能串
    assert _find_column(cols, "30分钟_D1", "BS3辅助V230318") \
        == "30分钟_D1#SMA#34_BS3辅助V230318"
    # 日线列不会被 30 分钟的匹配吃掉
    assert _find_column(cols, "日线_D1", "BUY1") == "日线_D1B_BUY1"
    assert _find_column(cols, "30分钟_D1", "NOTHING") is None


# ----------------------------------------------------------------------
# payload 组装
# ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def payload():
    kl = make_klines(420)
    return build_payload(kl, code="600000", name="测试股", period="30min",
                         init_n=60, ma_short=5, ma_long=10)


def test_payload_basic_shape(payload):
    m = payload["meta"]
    assert m["code"] == "sh600000"
    assert m["name"] == "测试股"
    assert m["bars"] == len(payload["dates"]) == len(payload["bars"])
    assert m["trading_days"] > 40
    # bars 是紧凑数组 [open, close, low, high, vol, ma5, ma10]
    assert len(payload["bars"][0]) == 7
    assert payload["bars"][0][0] > 0


def test_payload_bars_columns_consistent(payload):
    """紧凑数组的列序不能错位：low <= open/close <= high"""
    for b in payload["bars"]:
        o, c, low, high = b[0], b[1], b[2], b[3]
        assert low <= min(o, c) + 1e-6
        assert high >= max(o, c) - 1e-6
        assert b[4] >= 0


def test_payload_warmup_matches_init_n(payload):
    """预热区间只约束策略信号

    几何买卖点由全量笔/中枢算出，**不受预热影响**（旧版在这里断言
    「预热区间内不得出现买卖点」，是因为当时用的是 czsc 信号）。
    """
    assert payload["warmup"] == payload["meta"]["warmup"]
    assert payload["meta"]["init_n"] == 60
    assert payload["warmup"] == 60
    n = payload["meta"]["bars"]
    for t in payload["strategy"]["trades"]:
        assert t["buy"]["idx"] >= payload["warmup"]


def test_payload_indices_in_range(payload):
    n = payload["meta"]["bars"]
    for i0, i1, zd, zg in payload["centers"]:
        assert 0 <= i0 < n and 0 <= i1 < n
        assert i0 <= i1
        assert zd <= zg
    for series in ("top", "bottom"):
        for idx, price in payload["fractals"][series]:
            assert 0 <= idx < n
            assert price > 0
    for idx, price in payload["bi_points"]:
        assert 0 <= idx < n
        assert price > 0
    for level in ("daily", "m30"):
        for arr in payload["points"][level].values():
            for idx, price in arr:
                assert 0 <= idx < n and price > 0


def test_payload_points_two_levels(payload):
    """买卖点分日线 / 30 分钟两层，kind 只能是六类之一"""
    from core.chan_points import KINDS
    pts = payload["points"]
    assert set(pts) == {"daily", "m30"}
    for level in ("daily", "m30"):
        for kind, arr in pts[level].items():
            assert kind in KINDS
            assert len(arr) > 0            # 空的一类会被 pack 丢掉


def test_payload_geometric_points_are_sparse(payload):
    """几何买卖点的位置是笔端点，数量必须远少于 K 线根数

    回归：早期版本用 cxt_* 信号的关键字跃迁，状态抖动导致标记爆炸。
    """
    n = payload["meta"]["bars"]
    total = sum(len(v) for v in payload["points"]["m30"].values())
    assert total < n / 4, f"30 分钟买卖点 {total} 个，对 {n} 根 K 线来说过多"


def test_payload_bi_is_a_connected_chain(payload):
    """笔的端点是首尾相接的折线（否则画出来会断开）"""
    pts = payload["bi_points"]
    assert len(pts) >= 4
    for a, b in zip(pts, pts[1:]):
        assert a[1] != b[1]


def test_payload_strategy_keys(payload):
    st = payload["strategy"]
    assert set(st) >= {"d1_days", "d2_days", "m30_hits", "trades"}
    for i in st["d1_days"] + st["d2_days"] + st["m30_hits"]:
        assert 0 <= i < payload["meta"]["bars"]
    for t in st["trades"]:
        assert t["buy"]["idx"] >= 0
        assert 1 <= t["buy"]["gap_1to2"] <= 15
        assert 0 <= t["buy"]["gap_2tom"] <= 2
        if t["sell"]:
            assert t["sell"]["idx"] > t["buy"]["idx"]
            assert t["sell"]["ma"] in ("MA5", "MA10")


def test_payload_strategy_hit_lands_on_signal_bar(payload):
    """「策略买卖点对不上」的回归防线

    策略买点必须正好落在「信号 bar 的下一根」（entry_delay_bars=1），
    而 m30_hits 必须就是那些信号 bar —— 三者是同一条因果链上的点。
    """
    st = payload["strategy"]
    sig = {t["buy"]["signal_idx"] for t in st["trades"]}
    for i in st["m30_hits"]:
        assert i in sig, "高亮的二买 bar 不是任何一笔成交的信号 bar"
    for t in st["trades"]:
        assert t["buy"]["signal_idx"] >= 0
        assert t["buy"]["idx"] == t["buy"]["signal_idx"] + 1


def test_payload_zoom_within_range(payload):
    z = payload["zoom"]
    n = payload["meta"]["bars"]
    assert 0 <= z["startValue"] < z["endValue"] <= n - 1


def test_payload_rejects_too_few_bars():
    with pytest.raises(ValueError):
        build_payload(make_klines(2), code="TEST")


# ----------------------------------------------------------------------
# HTML 渲染
# ----------------------------------------------------------------------

def test_render_html_embeds_payload_and_echarts(payload, tmp_path):
    html = render_html(payload, echarts_path=fake_echarts(tmp_path))
    assert html.startswith("<!DOCTYPE html>")
    assert "fake echarts" in html
    assert "测试股" in html and "sh600000" in html

    m = re.search(r"var CFG = (\{.*?\});\nvar ST", html, re.S)
    assert m, "内联 payload 没找到"
    data = json.loads(m.group(1))
    assert data["meta"]["bars"] == payload["meta"]["bars"]
    assert len(data["bars"]) == len(payload["bars"])


def test_render_html_no_custom_series(payload, tmp_path):
    """卡顿修复的回归防线

    中枢 / 竖线 / 预热底纹必须用 markArea、markLine（silent），
    不能用 custom 系列 + renderItem —— 后者在 dataZoom 时每帧重跑
    renderItem，是「拖着拖着卡住」的主因。
    """
    html = render_html(payload, echarts_path=fake_echarts(tmp_path))
    assert "renderItem: function" not in html
    assert "type: 'custom'" not in html
    assert "markArea" in html and "markLine" in html
    assert html.count("silent: true") >= 2


def test_render_html_enables_dirty_rect(payload, tmp_path):
    """脏矩形渲染：缩放/拖动只重画变化区域"""
    html = render_html(payload, echarts_path=fake_echarts(tmp_path))
    assert "useDirtyRect: true" in html
    assert "animation: false" in html


def test_render_html_markarea_uses_category_values(payload, tmp_path):
    """markArea / markLine 的 xAxis 用类目值，不用下标数字

    category 轴上类目值可确定匹配；数字下标在不同 ECharts 版本里
    解释不一致（曾导致中枢不显示）。
    """
    html = render_html(payload, echarts_path=fake_echarts(tmp_path))
    assert "xAxis: dates[" in html
    assert "yAxis: z[2]" in html and "yAxis: z[3]" in html


def test_render_html_states_and_notes(payload, tmp_path):
    html = render_html(payload, echarts_path=fake_echarts(tmp_path))
    # 关键口径必须在图上（否则图会骗人）
    assert "几何定义" in html
    assert "30 分钟级别默认关闭" in html
    assert "czsc 预热区间" in html
    assert "日线MA5" in html and "日线MA10" in html
    # 策略命中的二买 bar 要单独高亮
    assert "策略命中二买" in html


def test_render_html_defaults_hide_minor_layers(payload, tmp_path):
    """分型与 30 分钟买卖点默认收起（数量多，会盖住 K 线）"""
    html = render_html(payload, echarts_path=fake_echarts(tmp_path))
    assert "'顶分型': false" in html and "'底分型': false" in html
    assert "legendSelected['30分' + k] = false" in html


def test_render_html_legend_keeps_strategy_visible(payload, tmp_path):
    """图例必须让「策略买点 / 策略卖点」常驻可见

    系列近 20 个，一行放不下。曾用 type:'scroll'，被折进「1/2」第二页，
    默认只显示到 30 分三卖，策略买卖点要手动翻页才看得到 ——
    而这两个点正是读图重点。改为限宽换行 + 显式优先级排序。
    """
    html = render_html(payload, echarts_path=fake_echarts(tmp_path))
    assert "type: 'scroll'" not in html
    assert "width: '97%'" in html
    # 策略两点排在图例数据数组最前
    assert "var LEGEND_FIRST = ['策略买点', '策略卖点'" in html


def test_render_html_missing_echarts_raises(payload, tmp_path):
    with pytest.raises(FileNotFoundError):
        render_html(payload, echarts_path=str(tmp_path / "nope.js"))
