# -*- coding: utf-8 -*-
"""策略买点信号的消费侧：读 monitor 落的 jsonl → 近两个交易日的买点 → GUI 分组

信号的唯一来源是 `scripts/monitor_buy.py` 追加的
`outputs/monitor_signals.jsonl`（与回测链同源判定，本模块只消费、不判定）。
只有 ``strategy_entry=true``（窗口内**第 1 个** 5min 一买确认）才算策略买点，
其后的同窗口一买是「非首选」，不进分组。

「前一天和当前」按 **conf（5min 确认 bar 的时刻，即买点出现时间）** 过滤，
不是按报警时间 ts —— 补报的旧点不该混进今天的名单。交易日历从日线缓存
末尾取（`recent_trading_days`），没有缓存时退化成「剔除周末」的近似。

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
    """最近 n 个交易日（升序，含今天如果今天已收盘进缓存）

    从日线缓存末尾读真实交易日；缓存缺失时退化：从今天往前跳过周末取 n 天
    （节假日会算错，只是兜底）。
    """
    today = today or date.today()
    if cache_dir.exists():
        f = cache_dir / "daily_sh600519.csv"
        if not f.exists():
            files = sorted(cache_dir.glob("daily_*.csv"))
            f = files[-1] if files else f
        if f.exists() and f.stat().st_size > 0:
            try:
                df = pd.read_csv(f, usecols=["dt"])
                days = [str(x)[:10] for x in df["dt"].iloc[-n:]]
                if days:
                    return days
            except Exception:
                pass
    days, d = [], today
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    return sorted(days)


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
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
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
