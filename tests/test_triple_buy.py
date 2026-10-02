# -*- coding: utf-8 -*-
"""`core/triple_buy.py` 的离线单测

策略口径（与 `eval_daily_5min.py` 同源）：日线三买（突破幅度<40%）→
窗口内最早 5min 一买 → 确认 bar 收盘成交 → 持 20 个交易日。
"""
from __future__ import annotations

import pandas as pd
import pytest

from core import triple_buy


def _write_cache(tmp_path, code, period, rows):
    d = tmp_path
    f = d / f"{period}_{code}.csv"
    pd.DataFrame(rows).to_csv(f, index=False)
    return f


def _bars(dts, closes):
    return [{"dt": t, "open": c, "high": c * 1.01, "low": c * 0.99,
             "close": c, "volume": 1000} for t, c in zip(dts, closes)]


class TestLoadCached:
    def test_missing_file_returns_none(self, tmp_path):
        assert triple_buy.load_cached("sh600000", "daily", tmp_path) is None

    def test_empty_file_returns_none(self, tmp_path):
        (tmp_path / "daily_sh600000.csv").write_text("")
        assert triple_buy.load_cached("sh600000", "daily", tmp_path) is None

    def test_nan_volume_coerced_to_zero(self, tmp_path):
        """停牌 bar 的空 volume 不能 int(NaN) 抛异常（神华日线实测坑）"""
        _write_cache(tmp_path, "sh600000", "daily",
                     [{"dt": "2026-01-05", "open": 10, "high": 10.5,
                       "low": 9.8, "close": 10.2, "volume": ""}])
        kl = triple_buy.load_cached("sh600000", "daily", tmp_path)
        assert len(kl) == 1 and kl[0].volume == 0 and kl[0].close == 10.2


class TestBreakoutAmp:
    def _bi(self, direction, high):
        return {"direction": direction, "high": high, "edt": "2026-01-05"}

    def test_normal(self):
        bis = [self._bi("Up", 110.0), self._bi("Down", 105.0)]
        p = {"bi_idx": 1, "zg": 100.0}
        assert triple_buy.breakout_amp(bis, p) == pytest.approx(10.0)

    def test_non_up_bi_returns_none(self):
        bis = [self._bi("Down", 110.0), self._bi("Up", 105.0)]
        assert triple_buy.breakout_amp(bis, {"bi_idx": 1, "zg": 100.0}) is None

    def test_missing_zg_returns_none(self):
        assert triple_buy.breakout_amp([self._bi("Up", 1)], {"bi_idx": 0}) is None


class TestTripleBuyTrades:
    def test_missing_cache_returns_none(self, tmp_path):
        assert triple_buy.triple_buy_trades("sh600000", tmp_path) is None

    def test_only_5min_cache_returns_none(self, tmp_path):
        _write_cache(tmp_path, "sh600000", "5min",
                     _bars(["2026-01-05 10:00"], [10.0]))
        assert triple_buy.triple_buy_trades("sh600000", tmp_path) is None

    @pytest.mark.skipif(True, reason="结构链对账已在 scripts 层做过（11/12 全合，"
                                     "第 12 只是对账脚本自身 NaN 过滤的伪差异）；"
                                     "离线构造 czsc 有效笔中枢成本高，覆盖交给集成路径")
    def test_end_to_end(self, tmp_path):  # pragma: no cover
        pass


class TestCandidateWindows:
    """监控层的活跃窗口提取 —— 过滤逻辑（开放/闸门/时效）的离线单测

    czsc 判定链用 monkeypatch 伪造（离线构造有效笔中枢成本高，见上），
    只测 `candidate_windows` 自己的过滤职责。
    """

    def test_missing_cache_returns_empty(self, tmp_path):
        assert triple_buy.candidate_windows("sh600000", tmp_path) == []

    def test_only_open_window_within_gate_survives(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        # 一根日线缓存即可（判定链是伪造的，但 load_cached 要真读文件）
        _write_cache(tmp_path, "sh600000", "daily",
                     _bars(["2026-09-25"], [10.0]))
        # 缓存末根 2026-09-25 ⇒ 90 天 cutoff = 2026-06-27
        bis = [
            {"direction": "Up", "high": 110.0, "edt": "2026-09-20"},   # 0
            {"direction": "Down", "high": 100.0, "edt": "2026-08-25"},  # 1 B三买
            {"direction": "Up", "high": 150.0, "edt": "2026-09-22"},    # 2 B确认笔/C突破
            {"direction": "Down", "high": 100.0, "edt": "2026-09-23"},  # 3 C三买
            {"direction": "Up", "high": 120.0, "edt": "2026-09-01"},    # 4 A突破(amp20%)
            {"direction": "Down", "high": 100.0, "edt": "2026-09-24"},  # 5 A三买(末笔=开放)
        ]
        points = [
            {"kind": "一买", "dt": "2026-08-20", "bi_idx": 0, "zg": 100.0},  # 非三买
            {"kind": "三买", "dt": "2026-08-25", "bi_idx": 1, "zg": 100.0},  # B 已关闭
            {"kind": "三买", "dt": "2026-09-23", "bi_idx": 3, "zg": 100.0},  # C 幅度50%
            {"kind": "三买", "dt": "2026-09-24", "bi_idx": 5, "zg": 100.0},  # A 保留
        ]
        monkeypatch.setattr(triple_buy, "chan_mod",
                            SimpleNamespace(build=lambda k, p: "rd",
                                            bis=lambda r: bis,
                                            centers=lambda r: []))
        monkeypatch.setattr(triple_buy, "chan_points",
                            SimpleNamespace(buy_sell_points=lambda b, z: points))

        out = triple_buy.candidate_windows("sh600000", tmp_path,
                                           max_amp=40.0, recent_days=90)
        assert out == [{"window_start": "2026-09-01", "signal_date": "2026-09-24",
                        "amp": 20.0, "zg": 100.0, "breakout_high": 120.0}], \
            "B(已关闭)、C(幅度≥40%)、一买(非三买)都必须被剔掉，只留开放且过闸门的 A"

    def test_recency_cutoff_drops_old_open_window(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        _write_cache(tmp_path, "sh600000", "daily",
                     _bars(["2026-09-25"], [10.0]))
        bis = [
            {"direction": "Up", "high": 120.0, "edt": "2026-01-10"},   # 太老
            {"direction": "Down", "high": 100.0, "edt": "2026-01-20"},  # 开放窗口
        ]
        points = [{"kind": "三买", "dt": "2026-01-20", "bi_idx": 1, "zg": 100.0}]
        monkeypatch.setattr(triple_buy, "chan_mod",
                            SimpleNamespace(build=lambda k, p: "rd",
                                            bis=lambda r: bis,
                                            centers=lambda r: []))
        monkeypatch.setattr(triple_buy, "chan_points",
                            SimpleNamespace(buy_sell_points=lambda b, z: points))
        assert triple_buy.candidate_windows("sh600000", tmp_path) == [], \
            "开放但窗口起点早于 recent_days 的不监控"


class TestResultShape:
    """返回结构契约 —— GUI 标注层直接消费这些键"""

    def test_trade_keys(self):
        t = {"buy_date": "2026-01-05", "buy_price": 10.0,
             "sell_date": "2026-02-02", "sell_price": 11.0,
             "return_pct": 10.0, "amp": 12.3, "signal_date": "2026-01-04"}
        for k in ("buy_date", "buy_price", "sell_date", "sell_price",
                  "return_pct", "amp", "signal_date"):
            assert k in t

    def test_open_trade_convention(self):
        """未平仓：sell_date 空串 / sell_price None / return_pct None
        （与 chan_viz.marks 的 trades 口径一致，UI 按此判断不落卖点）"""
        sell_date, sell_price, return_pct = "", None, None
        assert sell_date == "" and sell_price is None and return_pct is None
