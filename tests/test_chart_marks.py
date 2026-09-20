# -*- coding: utf-8 -*-
"""K 线图缠论标注的显示契约（`ui/chart_widget.py`）

钉住 2026-09-20 修掉的一个确定性 bug：**换股票时不清旧标注**。

`_draw_chan_marks` 是按「日期字符串 → 横轴下标」映射的（`date_to_x`），而所有
A 股共用同一份交易日历 —— 旧股票的买卖点日期几乎必然能在新股票的日线里命中
下标，于是「股票 A 的买卖点被画到股票 B 的图上」。叠加 `main_window` 在单飞
期间静默丢弃新请求（A 的 worker 还在跑时双击 B），这些错标会残留到手动刷新。

本文件只覆盖「换股票必须清空」这一条；单飞排队补算在 `tests/test_ui_threading.py`。
"""

import sys

import pytest


@pytest.fixture(scope="module")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


class _FakeManager:
    """数据管理器替身 —— 不给 K 线，只让 `load_data` 走完换股票流程"""

    def get_minute_klines_from_db(self, code, period):
        return []

    def get_klines(self, code, period, days):
        return []


@pytest.fixture
def chart(qapp, monkeypatch):
    import data.market_data_manager as mdm

    monkeypatch.setattr(mdm, "get_data_manager", lambda: _FakeManager())
    from ui.chart_widget import ChartTabWidget

    return ChartTabWidget(period="daily")


MARKS = {
    "geometry": [{"date": "2026-01-05", "kind": "二买", "price": 10.0}],
    "trades": [{"buy_date": "2026-01-05", "buy_price": 10.0}],
    "six_pulse": [{"buy_date": "2026-01-05", "buy_price": 10.0}],
    "meta": {"source": "geometry"},
}


def test_switching_stock_clears_marks(chart):
    """换股票 → 上一只的标注必须立刻清空（不能等新标注算好才覆盖）"""
    chart.load_data("600519")
    chart.set_chan_marks(MARKS)
    assert chart.chan_marks == MARKS

    chart.load_data("000001")

    assert chart.chan_marks == {}, (
        "换股票必须清掉上一只的缠论标注 —— 否则 A 的买卖点日期会命中 B 的"
        "横轴下标，把 A 的点画到 B 的图上")
    assert chart.chan_marks is not None, "清空后必须仍是 dict，_draw_chan_marks 会 .get"


def test_initial_marks_are_empty(chart):
    """新建图表页不应带着任何标注"""
    assert chart.chan_marks == {}


def test_marks_cleared_even_when_new_stock_has_no_data(chart):
    """新股票取不到数据时也必须清空 —— 更不能留着上一只的点装成"有标注" """
    chart.load_data("600519")
    chart.set_chan_marks(MARKS)

    chart.load_data("999999")          # 替身返回空数据

    assert chart.chan_marks == {}
    assert chart.klines == []
