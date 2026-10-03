"""日线可观察候选窗口 + 5min 首次确认一买的共享因果回放入口。

窗口只从逐日真实观察到的时刻开放，已成交事件不会被后来几何收敛删除。
持有默认 20×48 根已收口 5min bar；缺失数据必须先补齐，结果是逐点收益，
不是组合收益。研究旧数字的适用边界见 docs/reviews/REMEDIATION_2026-10-03.md。
"""
from __future__ import annotations

from pathlib import Path
import pandas as pd

from core.causal_signals import replay_windows, replay_entries
from data.models import KLineData

MIN5_BARS_PER_DAY = 48
DEFAULT_HOLD_DAYS = 20
DEFAULT_MAX_AMP = 40.0


def _ts(x) -> pd.Timestamp:
    return pd.Timestamp(str(x))


def load_cached(code: str, period: str, cache_dir: Path) -> list[KLineData] | None:
    """读 `fetch_min.py` 落的缓存 → KLineData；不存在/空文件返回 None

    停牌 bar 的空 volume（baostock 返空串）按 0 处理，不能 int(NaN) 抛异常。
    """
    f = Path(cache_dir) / f"{period}_{code}.csv"
    if not f.exists() or f.stat().st_size == 0:
        return None
    df = pd.read_csv(f, float_precision="round_trip").sort_values("dt").drop_duplicates("dt", keep="last")
    vol = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64")
    return [KLineData(code=code, date=str(r.dt), open=float(r.open),
                      high=float(r.high), low=float(r.low),
                      close=float(r.close), volume=int(v), period=period)
            for r, v in zip(df.itertuples(), vol)]


def breakout_amp(bis: list[dict], p: dict) -> float | None:
    """三买点的**突破幅度%** = 突破笔高点 ÷ 参照中枢 zg − 1

    突破笔 = 三买点所在回抽笔的前一根笔（czsc 笔严格交替，恒为 i−1）。
    """
    i, zg = p.get("bi_idx"), p.get("zg")
    if zg is None or i is None or i < 1 or i > len(bis):
        return None
    b = bis[i - 1]
    if str(b["direction"]) != "Up":
        return None
    hi, zg = float(b["high"]), float(zg)
    if hi <= 0 or zg <= 0:
        return None
    return (hi / zg - 1.0) * 100.0


def candidate_windows(code: str, cache_dir: Path, *, max_amp=DEFAULT_MAX_AMP, recent_days=90):
    """当前仍有效的窗口，含首次可用时刻和稳定 ID；不得回填旧 5min 信号。"""
    daily = load_cached(code, "daily", cache_dir)
    if not daily:
        return []
    windows = replay_windows(daily, max_amp=max_amp, recent_days=recent_days)
    return sorted((window for window in windows if window["closed_at"] is None),
                  key=lambda window: window["available_at"], reverse=True)


def replay_cached(code, cache_dir, *, max_amp=DEFAULT_MAX_AMP):
    """GUI、研究及监控共用的事件合同；保留失效候选和被撤销几何的入场记录。"""
    daily = load_cached(code, "daily", cache_dir)
    minute = load_cached(code, "5min", cache_dir)
    if not daily or not minute:
        return None
    windows = replay_windows(daily, max_amp=max_amp)
    events = replay_entries(code, minute, windows)
    return {"daily": daily, "minute": minute, "windows": windows, "events": events}


def trades_from_replay(replay, hold_days=DEFAULT_HOLD_DAYS):
    if hold_days <= 0:
        raise ValueError("hold_days must be positive")
    minute = replay["minute"]
    positions = {_ts(bar.date): i for i, bar in enumerate(minute)}
    trades = []
    for event in replay["events"]:
        if not event["strategy_entry"]:
            continue
        entry = positions[_ts(event["conf"])]
        exit_index = entry + hold_days * MIN5_BARS_PER_DAY
        end = minute[exit_index] if exit_index < len(minute) else None
        trades.append({"event_id": event["event_id"], "buy_date": event["conf"][:10],
                       "buy_time": event["conf"], "buy_price": event["entry_price"],
                       "sell_date": str(end.date)[:10] if end else "",
                       "sell_price": end.close if end else None,
                       "return_pct": (end.close / event["entry_price"] - 1) * 100 if end else None,
                       "amp": event["amp"], "signal_date": event["signal_date"],
                       "geometry_active": event["active"], "revision": event["revision"]})
    return trades


def triple_buy_trades(code: str, cache_dir: Path, *, hold_days=DEFAULT_HOLD_DAYS,
                      max_amp=DEFAULT_MAX_AMP, kinds=("一买",)):
    if kinds != ("一买",):
        raise ValueError("当前可观察事件策略只支持 5min 一买")
    replay = replay_cached(code, cache_dir, max_amp=max_amp)
    if replay is None:
        return None
    trades = trades_from_replay(replay, hold_days)
    return {"trades": trades, "candidates": len(replay["windows"]),
            "with_entry": len(trades), "events": replay["events"]}
