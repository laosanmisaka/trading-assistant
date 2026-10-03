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
    return {"event_id": f"{code}|{window_start}|{conf}",
            "ts": "2026-09-30T15:10:48", "code": code, "conf": conf,
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
    days = bp.recent_trading_days(2, cache_dir=tmp_path, today=date(2026, 9, 30))
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


def test_calendar_current_session_and_holiday(tmp_path):
    from scripts.monitor_buy import _in_session
    from datetime import datetime
    (tmp_path / "daily_sh600519.csv").write_text("dt\n2026-09-29\n2026-11-01\n")
    assert bp.recent_trading_days(2, cache_dir=tmp_path, today=date(2026, 9, 30)) == ["2026-09-29", "2026-09-30"]
    assert bp.recent_trading_days(2, today=date(2026, 10, 7)) == ["2026-09-29", "2026-09-30"]
    assert not _in_session(datetime(2026, 10, 1, 10))
    assert not _in_session(datetime(2026, 9, 26, 10))
    assert _in_session(datetime(2026, 9, 30, 10))


def test_journal_revision_recovery_and_withdrawal(tmp_path, monkeypatch):
    from core import signal_journal as journal
    signals, checkpoint = tmp_path / "signals.jsonl", tmp_path / "state.json"
    event = {**_ev("sh600000", "2026-09-30 10:00:00"), "event_id": "stable",
             "window_id": "window", "active": True, "revision": 0,
             "entry_dt": "2026-09-30 10:00:00", "geometry_price": 9.0}
    state = journal.recover(signals, checkpoint)
    real_write = journal.atomic_json
    monkeypatch.setattr(journal, "atomic_json", lambda *a: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError):
        journal.commit_event(state, event, signals, checkpoint)
    monkeypatch.setattr(journal, "atomic_json", real_write)
    with signals.open("ab") as stream:
        stream.write(b'{"partial":"\xe4')
    state = journal.recover(signals, checkpoint)
    assert state["alerts"]["stable"] == event
    assert set(bp.recent_buy_points(signals, keep_dates=KEEP)) == {"600000"}
    revised = journal.merge_observation({**event, "entry_price": 12, "geometry_price": 8}, event, set())
    assert revised["entry_price"] == event["entry_price"] and revised["geometry_price"] == 8
    assert not journal.merge_observation({**event, "event_id": "another"}, None, {"window"})["strategy_entry"]
    journal.commit_event(state, {**revised, "active": False}, signals, checkpoint)
    assert bp.recent_buy_points(signals, keep_dates=KEEP) == {}
    assert journal.recover(signals, checkpoint)["alerts"]["stable"]["active"] is False
    saved_checkpoint = checkpoint.read_bytes()
    signals.rename(tmp_path / "signals.backup.jsonl")
    with pytest.raises(ValueError, match="日志缺失"):
        journal.recover(signals, checkpoint)
    assert checkpoint.read_bytes() == saved_checkpoint


def test_monitor_isolates_malformed_stock(tmp_path, monkeypatch):
    from scripts import monitor_buy as monitor
    from types import SimpleNamespace
    from datetime import datetime
    seen = []
    def request(url, **kwargs):
        bad = "symbol=bad" in url
        seen.append("bad" if bad else "good")
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: [
            {"day": "2026-09-30 10:00:00", "open": "NaN" if bad else "10",
             "high": "11", "low": "9", "close": "10", "volume": "100"}])
    monkeypatch.setattr(monitor.requests, "get", request)
    monkeypatch.setattr(monitor, "local_now", lambda: datetime(2026, 9, 30, 10, 1))
    args = SimpleNamespace(once=True, old_every=6, datalen=5001, sleep=0, no_toast=True,
                           state=tmp_path / "state.json", signals=tmp_path / "signals.jsonl")
    watch = {"items": [{"code": code, "hot": True, "window_id": code,
                         "available_at": "2026-09-29 15:00:00"} for code in ("bad", "good")]}
    state = {"schema": 3, "alerts": {}}
    assert monitor.run_round(watch, state, args, 1) == 0
    assert seen == ["bad", "good"] and state["round_failures"] == 1
    args.codes, args.watchlist = "", tmp_path / "watch.json"
    monkeypatch.setattr("core.trading_calendar.latest_closed_session", lambda: "2026-09-29")
    args.watchlist.write_text(json.dumps({**watch, "as_of": "2026-09-28"}))
    with pytest.raises(ValueError, match="过期"):
        monitor._load_watch(args)
    args.watchlist.write_text(json.dumps({"items": [], "as_of": "2026-09-29"}))
    assert monitor._load_watch(args)["items"] == []
