# -*- coding: utf-8 -*-
"""六脉买卖点图（`scripts/visualize_six_pulse.py`）的**落点对齐**测试

2026-09-21 新增。起因：图上「减半 / 清仓」标记整体比策略晚一根 K 线，
一眼就能看出"跟策略对不上"。

根因：`SixPulseStrategy.generate_signals()` 里 `emit()` 写的
`Signal.date` 是 `dates[i + 1]` —— **它已经是成交日**；而脚本按
"信号日 → 次日成交"的直觉又多加了一天，于是

- 每个标记右移一格（成交价还是成交日那天的，日期/价格自相矛盾）；
- 标记与**按成交日跳变的仓位阶梯线**错开一天；
- 绿点（共振未采用）本来是画在成交日的，与三角标记又差开一天。

本文件把落点钉死：**标记的 x 索引必须等于信号自带日期的索引**，
且**必须正好落在仓位阶梯线的跳变点上**。全部离线：合成 K 线，不触网。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from core.six_pulse import SixPulseStrategy
from core.backtest.strategy import Action
from tests.test_six_pulse import synth

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "visualize_six_pulse.py"


@pytest.fixture(scope="module")
def viz():
    """按路径加载脚本模块（`scripts/` 不是包）"""
    spec = importlib.util.spec_from_file_location("visualize_six_pulse", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["visualize_six_pulse"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def case(viz):
    """合成日线 + 跑一遍策略 + 生成图数据（一次算完，多个断言共用）"""
    daily = synth(300, seed=7)
    strat = SixPulseStrategy(entry_ma=20)
    sigs = strat.generate_signals(daily)
    assert sigs, "合成数据必须能出信号，否则本文件失去意义"
    inst = viz.collect(daily, strat, start=None)
    return daily, strat, sigs, inst


# ----------------------------------------------------------------------
# 落点
# ----------------------------------------------------------------------

def test_marks_land_exactly_on_signal_date(case):
    """标记的 x 索引 == 该信号成交日在日线上的索引（不许 ±1）"""
    daily, _strat, sigs, inst = case
    dates = [k.date for k in daily]
    idx = {d: i for i, d in enumerate(dates)}

    for key in ("buys", "reentries", "reduces", "stops"):
        for m in inst[key]:
            want = idx[m["dt"]]
            assert m["value"][0] == want, (
                f"{key} 标记 {m['dt']} 画在索引 {m['value'][0]}，"
                f"应为 {want} —— 又出现了 off-by-one")


def test_mark_dates_are_exactly_the_signal_dates(case):
    """标记的日期集合 == 策略信号日期集合；分类张数与 weight/状态机一致"""
    _daily, _strat, sigs, inst = case
    marked = ({m["dt"] for m in inst["buys"]} | {m["dt"] for m in inst["reentries"]}
              | {m["dt"] for m in inst["reduces"]} | {m["dt"] for m in inst["stops"]})
    assert marked == {s.date for s in sigs}

    buys = [s for s in sigs if s.action == Action.BUY]
    sells = [s for s in sigs if s.action == Action.SELL]
    assert len(inst["buys"]) + len(inst["reentries"]) == len(buys)
    assert len(inst["reduces"]) == sum(1 for s in sells if s.weight < 1.0)
    assert len(inst["stops"]) == sum(1 for s in sells if s.weight >= 1.0)
    assert len(inst["reduces"]) > 0 and len(inst["stops"]) > 0, (
        "分级出口下应当既能减半也能清仓，否则用例覆盖不到重点")


def test_marks_sit_on_position_steps(case):
    """每个标记都必须落在仓位阶梯线的**跳变点**上（买卖与仓位不许脱节）"""
    daily, _strat, _sigs, inst = case
    pos = inst["pos"]
    assert len(pos) == len(daily)

    for key in ("buys", "reentries", "reduces", "stops"):
        for m in inst[key]:
            i = m["value"][0]
            assert i > 0, "预热区之后才会有标记，不该落在第 0 根"
            assert pos[i] != pos[i - 1], (
                f"{key} 标记 {m['dt']}（索引 {i}）处仓位没变 "
                f"（{pos[i - 1]} → {pos[i]}）—— 标记与仓位曲线对不上")


def test_position_series_range(case):
    """仓位序列落在 [0,1]；每次清仓标记后仓位为 0、每次建仓标记后仓位 > 0"""
    daily, _strat, _sigs, inst = case
    pos = inst["pos"]
    assert len(pos) == len(daily)
    assert all(0.0 <= p <= 1.0 for p in pos)

    for m in inst["buys"]:
        assert pos[m["value"][0]] > 0
    for m in inst["stops"]:
        assert pos[m["value"][0]] == 0.0
    for m in inst["reduces"]:
        assert pos[m["value"][0]] == pytest.approx(0.5)


# ----------------------------------------------------------------------
# 共振未采用 / 裁剪
# ----------------------------------------------------------------------

def test_skipped_never_shares_a_day_with_an_entry(case):
    """灰点（共振未采用）不能和建仓标记同一天 —— 同一天就不叫"未采用"了"""
    daily, _strat, sigs, inst = case
    entry_days = {m["dt"] for m in inst["buys"]}
    assert not ({m["dt"] for m in inst["skipped"]} & entry_days)

    dates = [k.date for k in daily]
    assert all(m["dt"] in dates for m in inst["skipped"])


def test_start_crop_shifts_indices(viz):
    """裁剪到 start 之后：索引平移到新坐标系，且不越界"""
    daily = synth(300, seed=7)
    strat = SixPulseStrategy(entry_ma=20)
    start = daily[200].date
    inst = viz.collect(daily, strat, start=start)
    n = len(inst["dates"])
    assert inst["dates"][0] == start

    for key in ("buys", "reentries", "reduces", "stops", "skipped"):
        for m in inst[key]:
            assert 0 <= m["value"][0] < n, f"{key} 标记 {m['dt']} 越界"
            assert m["dt"] >= start
    assert len(inst["pos"]) == n
