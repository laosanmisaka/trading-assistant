# -*- coding: utf-8 -*-
"""策略买点消费侧（`core/buy_points.py`）的纯逻辑测试

不联网：jsonl 用临时文件构造，DB 用 conftest 的 temp_db。
为什么值得测：分组是**全量接管**语义，过滤口径写错（比如按报警时间
ts 而不是买点时间 conf、或把「非首选」算进来）会把过期/次要信号
顶到用户面前，或者把真买点静默清掉。
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from core import buy_points as bp


def _write_jsonl(path, events: list[dict]) -> None:
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n"
                            for e in events),
                    encoding="utf-8")


def _ev(code, conf, *, strategy_entry=True, entry_price=10.0,
        window_start="2026-09-01", amp=12.3, signal_date="2026-09-10"):
    return {"ts": "2026-09-30T15:10:48", "code": code, "conf": conf,
            "window_start": window_start, "entry_price": entry_price,
            "signal_date": signal_date, "amp": amp, "zg": 9.0,
            "nth": 1, "strategy_entry": strategy_entry}


KEEP = {"2026-09-29", "2026-09-30"}


# ----------------------------------------------------------------------
# recent_buy_points
# ----------------------------------------------------------------------

def test_keeps_only_strategy_entry(tmp_path):
    f = tmp_path / "sig.jsonl"
    _write_jsonl(f, [
        _ev("sh600519", "2026-09-30 10:05:00", strategy_entry=True),
        _ev("sz000001", "2026-09-30 11:00:00", strategy_entry=False),  # 非首选
    ])
    pts = bp.recent_buy_points(f, keep_dates=KEEP)
    assert set(pts) == {"600519"}


def test_filters_by_conf_not_alert_time(tmp_path):
    """补报（ts 是今天、conf 是上周）的旧点不能混进近两日名单"""
    f = tmp_path / "sig.jsonl"
    _write_jsonl(f, [
        _ev("sh600519", "2026-09-22 10:05:00"),   # conf 太旧
        _ev("sz000001", "2026-09-29 14:40:00"),   # 前一交易日
        _ev("sh601899", "2026-09-30 09:55:00"),   # 今天
    ])
    pts = bp.recent_buy_points(f, keep_dates=KEEP)
    assert set(pts) == {"000001", "601899"}


def test_code_normalized_to_6_digits(tmp_path):
    f = tmp_path / "sig.jsonl"
    _write_jsonl(f, [_ev("sh600519", "2026-09-30 10:05:00")])
    pts = bp.recent_buy_points(f, keep_dates=KEEP)
    assert "600519" in pts
    assert pts["600519"]["conf"] == "2026-09-30 10:05:00"
    assert pts["600519"]["entry_price"] == 10.0


def test_same_code_keeps_latest_conf(tmp_path):
    f = tmp_path / "sig.jsonl"
    _write_jsonl(f, [
        _ev("sh600519", "2026-09-29 10:05:00", entry_price=10.0),
        _ev("sh600519", "2026-09-30 13:35:00", entry_price=10.5),
    ])
    pts = bp.recent_buy_points(f, keep_dates=KEEP)
    assert pts["600519"]["conf"] == "2026-09-30 13:35:00"
    assert pts["600519"]["entry_price"] == 10.5


def test_missing_file_and_bad_lines(tmp_path):
    assert bp.recent_buy_points(tmp_path / "none.jsonl", keep_dates=KEEP) == {}
    f = tmp_path / "sig.jsonl"
    f.write_text("not json\n\n" + json.dumps({"strategy_entry": True}) + "\n",
                 encoding="utf-8")
    assert bp.recent_buy_points(f, keep_dates=KEEP) == {}


# ----------------------------------------------------------------------
# recent_trading_days
# ----------------------------------------------------------------------

def test_trading_days_from_daily_cache(tmp_path):
    (tmp_path / "daily_sh600519.csv").write_text(
        "dt,open,high,low,close,volume\n"
        "2026-09-26,1,1,1,1,1\n"
        "2026-09-29,1,1,1,1,1\n"
        "2026-09-30,1,1,1,1,1\n",
        encoding="utf-8")
    days = bp.recent_trading_days(2, cache_dir=tmp_path)
    assert days == ["2026-09-29", "2026-09-30"]


def test_trading_days_fallback_skips_weekend(tmp_path):
    # 2026-09-30 是周三；缓存缺失时往前数 2 个工作日
    days = bp.recent_trading_days(2, cache_dir=tmp_path,
                                  today=date(2026, 9, 30))
    assert days == ["2026-09-29", "2026-09-30"]
    # 周一往前数应跳过周末
    days = bp.recent_trading_days(2, cache_dir=tmp_path,
                                  today=date(2026, 10, 12))  # 周一
    assert days == ["2026-10-09", "2026-10-12"]


# ----------------------------------------------------------------------
# sync_buy_points_group（temp_db，不碰真实库）
# ----------------------------------------------------------------------

def test_sync_group_full_takeover(tmp_path, temp_db):
    from data.database import get_all_groups, get_stocks_by_group
    f = tmp_path / "sig.jsonl"
    _write_jsonl(f, [
        _ev("sh600519", "2026-09-30 10:05:00"),
        _ev("sz000001", "2026-09-29 14:40:00"),
        _ev("sh601899", "2026-09-22 09:55:00"),          # 太旧，不进组
        _ev("sz002523", "2026-09-30 11:00:00",
            strategy_entry=False),                        # 非首选，不进组
    ])
    st = bp.sync_buy_points_group(signals_path=f, keep_dates=KEEP)
    assert st == {"added": 2, "removed": 0, "total": 2}
    g = next(g for g in get_all_groups() if g.name == bp.BUY_GROUP_NAME)
    assert {s.code for s in get_stocks_by_group(g.id)} == {"600519", "000001"}

    # 两日窗口滚动：600519 的 conf 滚出窗口 → 移出分组
    _write_jsonl(f, [_ev("sz000001", "2026-09-29 14:40:00")])
    st = bp.sync_buy_points_group(signals_path=f, keep_dates=KEEP)
    assert st == {"added": 0, "removed": 1, "total": 1}
    assert {s.code for s in get_stocks_by_group(g.id)} == {"000001"}
