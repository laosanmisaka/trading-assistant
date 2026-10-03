# -*- coding: utf-8 -*-
"""策略买点信号的消费侧：读 monitor 落的 jsonl → 近两个交易日的买点 → GUI 分组

信号的唯一来源是 `scripts/monitor_buy.py` 追加的
`outputs/monitor_signals.jsonl`（与回测链同源判定，本模块只消费、不判定）。
只有 ``strategy_entry=true``（窗口内**第 1 个** 5min 一买确认）才算策略买点，
其后的同窗口一买是「非首选」，不进分组。

「前一天和当前」按 **conf（5min 确认 bar 的时刻，即买点出现时间）** 过滤，
不是按报警时间 ts —— 补报的旧点不该混进今天的名单。交易日由有版本范围的 XSHG 日历提供，
盘中即包含当日，不依赖滞后的日线缓存，不以工作日猜测假日。

GUI 分组「策略买点」由 `sync_buy_points_group` **全量接管**：新买点补进来、
滚出两日窗口的移出去。用户手动跟踪的票别放这个分组（次日会被清掉）。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SIGNALS = ROOT / "outputs" / "monitor_signals.jsonl"
DEFAULT_CACHE = ROOT / "outputs" / "cache_min"

#: 策略买点在 GUI 里的自动分组名（全量接管，勿手改）
BUY_GROUP_NAME = "策略买点"


def to_code6(code: str) -> str:
    """``sh600519`` → ``600519``（GUI 库的存储格式）；无前缀原样返回"""
    c = str(code).strip().lower()
    return c[2:] if c[:2] in ("sh", "sz", "bj") else c


def recent_trading_days(n: int = 2, *,
                        cache_dir: Path = DEFAULT_CACHE,
                        today: date | None = None) -> list[str]:
    """交易所日历近 n 日，盘中即包含当日；cache_dir 留作兼容旧调用。"""
    from core.trading_calendar import recent_sessions
    return recent_sessions(n, today)


def recent_buy_points(signals_path: Path = DEFAULT_SIGNALS, *,
                      keep_dates: set[str] | None = None,
                      today: date | None = None) -> dict[str, dict]:
    """近两日策略买点 → ``{6位代码: 记录}``（同一只取 conf 最新的一条）

    记录字段：``conf``（买点出现时间）、``entry_price``、``window_start``、
    ``amp``、``signal_date``。``keep_dates`` 缺省用 `recent_trading_days(2)`。
    jsonl 不存在/行为空/字段缺失时跳过该行，不抛异常。
    """
    if keep_dates is None:
        keep_dates = set(recent_trading_days(2, today=today))
    out: dict[str, dict] = {}
    p = Path(signals_path)
    if not p.exists():
        return out
    from core.signal_journal import read_events
    # Fold revisions/retractions first, then date and eligibility filters.
    for ev in read_events(p).values():
        if not ev.get("event_id") or not ev.get("active", True):
            continue
        if not ev.get("strategy_entry"):
            continue
        conf = str(ev.get("conf") or "")
        if conf[:10] not in keep_dates:
            continue
        code = to_code6(ev.get("code", ""))
        if not code:
            continue
        if code not in out or conf > out[code]["conf"]:
            out[code] = {"conf": conf,
                         "entry_price": ev.get("entry_price"),
                         "window_start": ev.get("window_start", ""),
                         "amp": ev.get("amp"),
                         "signal_date": ev.get("signal_date", "")}
    return out


def sync_buy_points_group(*, signals_path: Path = DEFAULT_SIGNALS,
                          keep_dates: set[str] | None = None,
                          today: date | None = None,
                          group_name: str = BUY_GROUP_NAME) -> dict:
    """近两日策略买点同步成 GUI 分组 → ``{"added", "removed", "total"}``

    全量接管语义同 `scripts/daily_routine.sync_watchlist_group`；其他分组
    不碰，移出分组不影响交易记录（trades 按 stock_code 关联）。
    ``keep_dates`` 缺省取最近 2 个交易日（`recent_trading_days`）。
    """
    from data.database import (init_db, get_all_groups, add_group,
                               get_stocks_by_group, add_stock, remove_stock,
                               get_stock_name)
    init_db()
    pts = recent_buy_points(signals_path, keep_dates=keep_dates, today=today)
    groups = {g.name: g for g in get_all_groups()}
    g = groups.get(group_name) or add_group(group_name)

    have = {s.code: s for s in get_stocks_by_group(g.id)}
    added = removed = 0
    for code in pts:
        if code not in have:
            add_stock(code, get_stock_name(code) or "", g.id)
            added += 1
    for code, s in have.items():
        if code not in pts:
            remove_stock(s.id)
            removed += 1
    return {"added": added, "removed": removed, "total": len(pts)}
