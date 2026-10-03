"""Observable daily windows and 5min first-buy events, replayed in time order.

Geometry may repaint; a later revision never rewrites an already observed fill.
The first eligible observed signal consumes its window even if later withdrawn.
Revisions describe current geometry, while entry timestamps/prices remain an
immutable record of the decision available at the time.
"""
from __future__ import annotations

import hashlib

import pandas as pd

from core import chan, chan_points


def event_id(*parts):
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:24]


def bar_time(value, period):
    time = pd.Timestamp(str(value))
    return time.normalize() + pd.Timedelta(hours=15) if period == "daily" else time


def window_candidates(bis, centers, max_amp=40.0):
    """候选依赖当前结构，绝不从最后完整历史中倒选成功回调。"""
    result = {}
    for point in chan_points.buy_sell_points(bis, centers):
        index, upper = point["bi_idx"], point.get("zg")
        if point["kind"] != "三买" or index != len(bis) - 1 or index < 1:
            continue
        breakout = bis[index - 1]
        if upper is None or upper <= 0 or breakout["direction"] != "Up":
            continue
        amplitude = (breakout["high"] / upper - 1) * 100
        if max_amp is not None and amplitude >= max_amp:
            continue
        anchor = event_id("daily-window", breakout["sdt"], breakout["edt"])
        result[anchor] = {"window_start": str(pd.Timestamp(breakout["edt"]))[:10],
                          "signal_date": str(pd.Timestamp(point["dt"]))[:10],
                          "amp": round(amplitude, 1), "zg": float(upper),
                          "breakout_high": float(breakout["high"])}
    return result


def replay_windows(daily, *, max_amp=40.0, recent_days=90):
    """Return all observed open/close intervals, including candidates later invalidated."""
    intervals, active = [], {}
    for bar, bis, centers in chan.iter_structures(daily, "daily"):
        now = bar_time(bar.date, "daily")
        candidates = window_candidates(bis, centers, max_amp)
        candidates = {key: value for key, value in candidates.items()
                      if now.normalize() - pd.Timestamp(value["window_start"]) <= pd.Timedelta(days=recent_days)}
        for key in active.keys() - candidates.keys():
            active[key]["closed_at"] = str(now)
        active = {key: value for key, value in active.items() if key in candidates}
        for key, candidate in candidates.items():
            if key not in active:
                opened = {**candidate, "available_at": str(now), "closed_at": None,
                          "window_id": event_id(key, now)}
                intervals.append(opened)
                active[key] = opened
    return intervals


def confirmed_points(bis, centers):
    """Current first-buy geometry with its following stroke actually present now."""
    result = {}
    for point in chan_points.buy_sell_points(bis, centers):
        index = point["bi_idx"]
        if point["kind"] == "一买" and index + 1 < len(bis):
            stroke = bis[index]
            key = event_id("5min-first-buy", stroke["sdt"])
            result[key] = {"geometry_dt": str(pd.Timestamp(point["dt"])),
                           "stroke_start": str(pd.Timestamp(stroke["sdt"])),
                           "geometry_price": float(point["price"])}
    return result


def _eligible(window, now):
    opened = pd.Timestamp(window["available_at"])
    closed = window.get("closed_at")
    return opened < now and (closed is None or now < pd.Timestamp(closed))


def _revise(event, point, now):
    active = point is not None
    changed = active != event["active"]
    if point is not None:
        changed = changed or any(event.get(key) != value for key, value in point.items())
    if changed:
        event.update(point or {})
        event.update(active=active, revision=event["revision"] + 1, updated_at=str(now))


def replay_entries(code, bars, windows, *, as_of=None):
    """Replay the same observable entries used by monitor and historical trades.

    Each window is explicit about when it first became available. Historical
    bars before that time are warm-up only; they cannot become retroactive fills.
    """
    events, consumed, seen_points = {}, set(), set()
    for bar, bis, centers in chan.iter_structures(bars, "5min"):
        now = bar_time(bar.date, "5min")
        if as_of is not None and now > pd.Timestamp(as_of):
            break
        points = confirmed_points(bis, centers)
        for event in events.values():
            _revise(event, points.get(event["point_id"]), now)
        for window in windows:
            if not _eligible(window, now):
                continue
            window_id = window["window_id"]
            for point_id, point in points.items():
                # An old already-confirmed point is not a new signal at window opening.
                if point_id in seen_points:
                    continue
                key = event_id(code, window_id, point_id)
                if key in events:
                    continue
                events[key] = {**point, "event_id": key, "point_id": point_id,
                               "window_id": window_id, "window_start": window["window_start"],
                               "code": code, "conf": str(now), "entry_dt": str(now),
                               "entry_price": float(bar.close), "active": True,
                               "strategy_entry": window_id not in consumed, "revision": 0,
                               "updated_at": str(now), "signal_date": window["signal_date"],
                               "amp": window["amp"], "zg": window["zg"]}
                consumed.add(window_id)
        seen_points.update(points)
    return sorted(events.values(), key=lambda event: (event["conf"], event["event_id"]))
