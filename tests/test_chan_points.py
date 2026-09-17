# -*- coding: utf-8 -*-
"""缠论买卖点几何计算（core/chan_points.py）的单元测试

不依赖 czsc / 网络：直接构造笔与中枢的 dict，验证判定规则。

注意断言只针对**关心的类别**：一组笔往往同时会导出别的类别
（例如中枢上方的向上笔突破，既是三买的前提，也可能是一卖），
所以不能用「结果全等」来断言。
"""

from __future__ import annotations

import pandas as pd

from core.chan_points import KINDS, buy_sell_points, group_by_kind


def bi(direction: str, sdt: str, edt: str, high: float, low: float) -> dict:
    return {"direction": direction, "high": high, "low": low,
            "sdt": pd.Timestamp(sdt), "edt": pd.Timestamp(edt),
            "start_idx": -1, "end_idx": -1}


def zs(sdt: str, edt: str, low: float, high: float) -> dict:
    return {"high": high, "low": low, "mid": (high + low) / 2,
            "sdt": pd.Timestamp(sdt), "edt": pd.Timestamp(edt), "is_valid": True}


def count(points, kind: str) -> int:
    return sum(1 for p in points if p["kind"] == kind)


def pick(points, kind: str):
    got = [p for p in points if p["kind"] == kind]
    assert got, f"没有 {kind}"
    return got[0]


# ----------------------------------------------------------------------
# 边界
# ----------------------------------------------------------------------

def test_too_few_bis_returns_empty():
    assert buy_sell_points([], []) == []
    assert buy_sell_points([bi("Down", "2024-01-01", "2024-01-02", 10, 9)], []) == []


def test_no_center_means_no_first_point():
    """一买是「下跌趋势的终结」，没有中枢就谈不上趋势 —— 不能判成一买

    数据最开头那段没有前置中枢，修复前会把每个向下笔都标成一买。
    """
    bis = [bi("Down", "2024-01-01", "2024-01-02", 110, 100),
           bi("Up", "2024-01-02", "2024-01-03", 112, 100),
           bi("Down", "2024-01-03", "2024-01-04", 112, 90)]
    assert buy_sell_points(bis, []) == []


# ----------------------------------------------------------------------
# 一买 / 二买
# ----------------------------------------------------------------------

def test_first_and_second_buy():
    centers = [zs("2024-01-01", "2024-01-05", 100, 105)]
    bis = [
        bi("Down", "2024-01-05", "2024-01-08", 110, 96),   # 跌破 100 → 一买
        bi("Up",   "2024-01-08", "2024-01-10", 108, 96),
        bi("Down", "2024-01-10", "2024-01-12", 108, 99),   # 99 > 96 → 二买
        bi("Up",   "2024-01-12", "2024-01-15", 112, 99),
    ]
    pts = buy_sell_points(bis, centers)
    assert count(pts, "一买") == 1
    assert count(pts, "二买") == 1
    fb, sb = pick(pts, "一买"), pick(pts, "二买")
    assert fb["price"] == 96 and fb["dt"] == pd.Timestamp("2024-01-08")
    assert sb["price"] == 99 and sb["dt"] == pd.Timestamp("2024-01-12")


def test_first_buy_needs_breaking_below_center():
    """只创新低但没跌破中枢下沿 → 不算一买（是中枢内的震荡，不是趋势终结）"""
    centers = [zs("2024-01-01", "2024-01-05", 100, 105)]
    bis = [bi("Down", "2024-01-05", "2024-01-08", 110, 102),
           bi("Up",   "2024-01-08", "2024-01-10", 108, 102),
           bi("Down", "2024-01-10", "2024-01-12", 108, 101)]   # 新低但仍在 100 上方
    assert count(buy_sell_points(bis, centers), "一买") == 0


def test_second_buy_rejected_when_new_low():
    """一买之后又创了新低 → 不是二买"""
    centers = [zs("2024-01-01", "2024-01-05", 100, 105)]
    bis = [
        bi("Down", "2024-01-05", "2024-01-08", 110, 96),
        bi("Up",   "2024-01-08", "2024-01-10", 108, 96),
        bi("Down", "2024-01-10", "2024-01-12", 108, 90),   # 90 < 96
        bi("Up",   "2024-01-12", "2024-01-15", 110, 90),
    ]
    pts = buy_sell_points(bis, centers)
    assert count(pts, "一买") == 1
    assert count(pts, "二买") == 0


def test_second_buy_only_takes_first_pullback():
    """二买只取一买之后的第一根向下笔，后面的回调不再重复标记"""
    centers = [zs("2024-01-01", "2024-01-05", 100, 105)]
    bis = [
        bi("Down", "2024-01-05", "2024-01-08", 110, 96),
        bi("Up",   "2024-01-08", "2024-01-10", 110, 96),
        bi("Down", "2024-01-10", "2024-01-12", 110, 99),   # 二买
        bi("Up",   "2024-01-12", "2024-01-15", 115, 99),
        bi("Down", "2024-01-15", "2024-01-18", 115, 102),  # 不重复标
    ]
    pts = buy_sell_points(bis, centers)
    assert count(pts, "二买") == 1
    assert pick(pts, "二买")["dt"] == pd.Timestamp("2024-01-12")


# ----------------------------------------------------------------------
# 三买 / 三卖
# ----------------------------------------------------------------------

def test_third_buy():
    centers = [zs("2024-01-01", "2024-01-05", 100, 105)]
    bis = [
        bi("Up",   "2024-01-05", "2024-01-08", 115, 105),  # 向上突破 105
        bi("Down", "2024-01-08", "2024-01-10", 115, 108),  # 回调 108 > 105
    ]
    pts = buy_sell_points(bis, centers)
    assert count(pts, "三买") == 1
    tp = pick(pts, "三买")
    assert tp["price"] == 108 and tp["direction"] == "Down"


def test_third_buy_rejected_when_falling_back_into_center():
    centers = [zs("2024-01-01", "2024-01-05", 100, 105)]
    bis = [
        bi("Up",   "2024-01-05", "2024-01-08", 115, 105),
        bi("Down", "2024-01-08", "2024-01-10", 115, 103),  # 回到中枢内
    ]
    assert count(buy_sell_points(bis, centers), "三买") == 0


def test_third_sell():
    centers = [zs("2024-01-01", "2024-01-05", 100, 105)]
    bis = [
        bi("Down", "2024-01-05", "2024-01-08", 100, 92),   # 向下突破 100
        bi("Up",   "2024-01-08", "2024-01-10", 97, 92),    # 反弹 97 < 100
    ]
    pts = buy_sell_points(bis, centers)
    assert count(pts, "三卖") == 1
    tp = pick(pts, "三卖")
    assert tp["price"] == 97 and tp["direction"] == "Up"


# ----------------------------------------------------------------------
# 卖点对称性
# ----------------------------------------------------------------------

def test_first_and_second_sell_mirror_buy():
    centers = [zs("2024-01-01", "2024-01-05", 100, 105)]
    bis = [
        bi("Up",   "2024-01-05", "2024-01-08", 112, 105),  # 升破 105 → 一卖
        bi("Down", "2024-01-08", "2024-01-10", 112, 108),
        bi("Up",   "2024-01-10", "2024-01-12", 110, 108),  # 110 < 112 → 二卖
        bi("Down", "2024-01-12", "2024-01-15", 110, 100),
    ]
    pts = buy_sell_points(bis, centers)
    assert count(pts, "一卖") == 1
    assert count(pts, "二卖") == 1
    fs, ss = pick(pts, "一卖"), pick(pts, "二卖")
    assert fs["price"] == 112 and fs["direction"] == "Up"   # 卖点取 high
    assert ss["price"] == 110


# ----------------------------------------------------------------------
# 结构与排序
# ----------------------------------------------------------------------

def test_group_by_kind_has_all_six_keys():
    g = group_by_kind([])
    assert tuple(g) == KINDS
    assert all(v == [] for v in g.values())


def test_points_sorted_by_time():
    centers = [zs("2024-01-01", "2024-01-05", 100, 105)]
    bis = [
        bi("Down", "2024-01-05", "2024-01-08", 110, 96),
        bi("Up",   "2024-01-08", "2024-01-10", 108, 96),
        bi("Down", "2024-01-10", "2024-01-12", 108, 99),
        bi("Up",   "2024-01-12", "2024-01-15", 112, 99),
        bi("Down", "2024-01-15", "2024-01-18", 112, 90),
        bi("Up",   "2024-01-18", "2024-01-20", 115, 90),
    ]
    dts = [p["dt"] for p in buy_sell_points(bis, centers)]
    assert dts == sorted(dts)


def test_bi_idx_points_back_to_source_bi():
    """bi_idx 必须能索引回传入的笔序列（画图要拿它定位）"""
    centers = [zs("2024-01-01", "2024-01-05", 100, 105)]
    bis = [
        bi("Down", "2024-01-05", "2024-01-08", 110, 96),
        bi("Up",   "2024-01-08", "2024-01-10", 108, 96),
        bi("Down", "2024-01-10", "2024-01-12", 108, 99),
    ]
    for p in buy_sell_points(bis, centers):
        assert bis[p["bi_idx"]]["edt"] == p["dt"]
