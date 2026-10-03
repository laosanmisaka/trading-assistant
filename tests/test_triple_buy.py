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



def _synthetic(n=600, start="2023-05-09"):
    import numpy as np
    from data.models import KLineData
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0, .5, n))
    op = close + rng.normal(0, .2, n)
    hi = np.maximum(op, close) + rng.uniform(0, .5, n)
    lo = np.minimum(op, close) - rng.uniform(0, .5, n)
    volume = rng.integers(10_000, 1_000_000, n)
    times = []
    for day in pd.bdate_range(start, periods=(n + 47) // 48):
        for hour, minute in ((9, 35), (13, 5)):
            times.extend(pd.date_range(day + pd.Timedelta(hours=hour, minutes=minute), periods=24, freq="5min"))
    return [KLineData(code="TEST", date=str(times[i]), open=float(op[i]), high=float(hi[i]),
                      low=float(lo[i]), close=float(close[i]), volume=int(volume[i]), period="5min")
            for i in range(n)]


def _save_bars(path, period, bars):
    import dataclasses
    rows = [{**dataclasses.asdict(bar), "dt": bar.date} for bar in bars]
    _write_cache(path, "TEST", period, rows)


def test_observable_pipeline_and_prefix_stability(tmp_path):
    """Real CZSC: daily windows, monitor, research, chart trades, prefix confirmations."""
    from dataclasses import replace
    from core import chan
    from core.causal_signals import replay_windows, confirmed_points, replay_entries
    from scripts.monitor_buy import detect_entries
    from scripts.eval_daily_5min import analyze_one
    minute = _synthetic()
    daily = [replace(bar, date=str(day.date()), period="daily")
             for bar, day in zip(minute, pd.bdate_range("2022-01-03", periods=len(minute)))]
    _save_bars(tmp_path, "daily", daily)
    _save_bars(tmp_path, "5min", minute)
    replay = triple_buy.replay_cached("TEST", tmp_path)
    assert len(replay["windows"]) == 2  # Includes a candidate which disappears and reopens.
    events = replay["events"]
    assert events and any(not event["active"] for event in events)
    assert detect_entries("TEST", minute, replay["windows"], pd.Timestamp(minute[-1].date)) == events
    trades = triple_buy.triple_buy_trades("TEST", tmp_path, hold_days=1)["trades"]
    row = analyze_one("TEST", tmp_path, holds=[1])
    details = [d for d in row["明细"] if d["event_id"]]
    assert [d["event_id"] for d in details] == [t["event_id"] for t in trades]
    assert [d["M1"] for d in details] == pytest.approx([t["return_pct"] for t in trades])
    _assert_prefix_observability(minute, daily, replay)


def _assert_prefix_observability(minute, daily, replay):
    from core import chan
    from core.causal_signals import replay_windows, replay_entries, confirmed_points
    events = replay["events"]
    first = events[0]
    stop = next(i for i, bar in enumerate(minute) if bar.date == first["conf"]) + 1
    prefix = replay_entries("TEST", minute[:stop], replay["windows"])
    assert prefix[0]["conf"] == first["conf"]
    assert prefix[0]["entry_price"] == first["entry_price"]
    assert not replay_entries("TEST", minute[:stop - 1], replay["windows"])
    structure = chan.build(minute[:stop], "5min")
    assert first["point_id"] in confirmed_points(chan.bis(structure), chan.centers(structure))
    assert pd.Timestamp(first["conf"]) > pd.Timestamp(first["geometry_dt"])
    daily_prefix = [bar for bar in daily if bar.date <= first["conf"][:10]]
    assert replay_windows(daily_prefix)[0]["available_at"] == replay["windows"][0]["available_at"]
    later_window = {**replay["windows"][0], "available_at": first["conf"], "closed_at": None}
    assert not replay_entries("TEST", minute[:stop], [later_window])
    # Stable identity is anchored to the stroke, not its index in a rolling array.
    trimmed = replay_entries("TEST", minute[50:], replay["windows"])
    common = set(e["event_id"] for e in trimmed) & set(e["event_id"] for e in events)
    assert common


def test_candidate_windows_only_current_and_within_gate(tmp_path):
    from dataclasses import replace
    daily = [replace(bar, date=str(day.date()), period="daily")
             for bar, day in zip(_synthetic(), pd.bdate_range("2022-01-03", periods=600))]
    prefix = [bar for bar in daily if bar.date <= "2023-05-12"]
    _save_bars(tmp_path, "daily", prefix)
    assert len(triple_buy.candidate_windows("TEST", tmp_path)) == 1
    assert triple_buy.candidate_windows("TEST", tmp_path, max_amp=1) == []
    assert triple_buy.candidate_windows("TEST", tmp_path, recent_days=1) == []
    _save_bars(tmp_path, "daily", daily)
    assert triple_buy.candidate_windows("TEST", tmp_path) == []
