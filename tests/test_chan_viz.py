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
                         ma_short=5, ma_long=10)


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


def test_payload_has_no_signal_warmup(payload):
    """不再有信号预热区间（2026-09-18）

    旧版画一块浅灰底纹标出 czsc `cxt_*` 信号源前 `init_n` 根的预热禁区。
    策略改几何源后没有这个截断 —— 几何点由全量笔/中枢算出，从第一根就有效。
    """
    assert "warmup" not in payload
    assert "warmup" not in payload["meta"]
    assert "init_n" not in payload["meta"]


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


def test_payload_centers_two_levels(payload):
    """中枢分日线 / 30 分钟两层

    日线中枢是主图层（区间大、数量少，用来看「哪一段是震荡区」），
    30 分钟笔中枢是次图层默认收起。两者都用 [i0, i1, zd, zg] 表示。
    """
    n = payload["meta"]["bars"]
    assert "centers_daily" in payload
    for key in ("centers", "centers_daily"):
        for i0, i1, zd, zg in payload[key]:
            assert 0 <= i0 <= i1 < n
            assert zd <= zg
    assert payload["meta"]["counts"]["日线中枢"] == len(payload["centers_daily"])
    assert payload["meta"]["counts"]["30分中枢"] == len(payload["centers"])


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
        # 窗口口径 2026-09-18 改为 一买→二买 ≤40 交易日、二买↔30 分钟二买 ±10
        # （gap_2tom 有符号：<0 = 次级别买点早于日线二买）
        assert 1 <= t["buy"]["gap_1to2"] <= 40
        assert -10 <= t["buy"]["gap_2tom"] <= 10
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


def test_render_html_has_two_center_layers(payload, tmp_path):
    """两级中枢各自独立成层，图例才能分别开关

    markArea 属于 series —— 两层都挂在 K 线上的话只有一个开关，
    所以每层各挂在一个「无数据的散点系列」上。
    """
    html = render_html(payload, echarts_path=fake_echarts(tmp_path))
    assert "日线中枢" in html and "30分中枢" in html
    assert "'30分中枢': false" in html          # 30 分钟层默认收起
    assert "centerLayer('日线中枢'" in html
    assert "centerLayer('30分中枢'" in html


def test_render_html_states_and_notes(payload, tmp_path):
    html = render_html(payload, echarts_path=fake_echarts(tmp_path))
    # 关键口径必须在图上（否则图会骗人）
    assert "几何定义" in html
    assert "30 分钟级别默认关闭" in html
    # 策略窗口口径（2026-09-18 改为 40 / ±10）必须写在图上
    assert "40 个交易日内" in html
    assert "前后各 10 个交易日" in html
    # 日线点延后一个交易日生效（未来函数防线）
    assert "次一交易日" in html
    assert "日线MA5" in html and "日线MA10" in html
    # 策略命中的二买 bar 要单独高亮
    assert "策略命中二买" in html
    # 不再有信号预热区间（图层与配色都已删除）
    assert "CFG.warmup" not in html
    assert "warmupArea" not in html


def test_render_html_discloses_position_overlap(payload, tmp_path):
    """允许重叠持仓的口径必须写在图上

    每笔买点独立成笔、允许重叠（2026-09-18 定案），所以胜率/平均收益是
    「按笔统计」而非资金曲线收益。图上必须显式披露，否则数字会被误读。
    """
    html = render_html(payload, echarts_path=fake_echarts(tmp_path))
    assert "最大同时持仓" in html
    assert "重叠笔数" in html
    assert "按笔统计" in html and "不是资金曲线" in html


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


# ----------------------------------------------------------------------
# 图上标注导出（给桌面端 matplotlib 用）
# ----------------------------------------------------------------------

def _fake_payload_for_marks():
    """手搓一份 payload —— 只为验证「下标 → 日期」的换算，不涉及缠论"""
    return {
        "dates": ["2026-01-05 10:00", "2026-01-06 10:00", "2026-01-07 14:00"],
        "points": {
            "daily": {"一买": [[0, 10.0]], "三卖": [[2, 12.5]],
                      "二买": [[99, 1.0]]},          # 越界下标必须被丢掉
            "m30": {"二买": [[1, 11.0]]},             # 30 分钟层不进 UI 标注
        },
        "strategy": {"trades": [
            {"buy": {"dt": "2026-01-06 10:30", "price": 11.0},
             "sell": {"dt": "2026-01-07", "price": 12.0, "return_pct": 9.09}},
            {"buy": {"dt": "2026-01-07 14:00", "price": 12.5}, "sell": None},
        ]},
    }


def test_marks_from_payload_maps_index_to_date():
    from core.chan_viz import marks_from_payload

    marks = marks_from_payload(_fake_payload_for_marks())

    assert marks["geometry"] == [
        {"date": "2026-01-05", "kind": "一买", "price": 10.0},
        {"date": "2026-01-07", "kind": "三卖", "price": 12.5},
    ], "日线几何点要按 bar 下标换算成日期，越界下标要丢掉"

    assert marks["trades"] == [
        {"buy_date": "2026-01-06", "buy_price": 11.0,
         "sell_date": "2026-01-07", "sell_price": 12.0, "return_pct": 9.09},
        {"buy_date": "2026-01-07", "buy_price": 12.5,
         "sell_date": "", "sell_price": None, "return_pct": None},
    ], "未平仓那笔的 sell_date 必须是空串（不是 None 也不是乱码日期）"


def test_marks_from_payload_tolerates_empty_payload():
    """空 payload 不抛异常 —— UI 拿到空标注只是不画点"""
    from core.chan_viz import marks_from_payload

    assert marks_from_payload({}) == {"geometry": [], "trades": []}


def test_marks_from_klines_matches_payload(payload):
    """marks_from_klines 的点必须与 payload 同一批（同源，不另算一套）"""
    from core.chan_viz import marks_from_klines, marks_from_payload

    marks = marks_from_payload(payload)
    dates = {d[:10] for d in payload["dates"]}
    for p in marks["geometry"]:
        assert p["date"] in dates
        assert p["kind"].endswith(("买", "卖"))
    for t in marks["trades"]:
        assert t["buy_date"] in dates
    assert "meta" not in marks


def test_marks_from_klines_carries_meta():
    from core.chan_viz import marks_from_klines

    marks = marks_from_klines(make_klines(420), code="600000", name="测试股")
    assert marks["meta"]["code"] == "sh600000"
    assert set(marks) == {"geometry", "trades", "meta"}
