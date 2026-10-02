# -*- coding: utf-8 -*-
"""日线三买 + 5min 一买联立策略的**单只**信号链（可复用模块）

策略口径（2026-09-28 定稿，证据见 `outputs/better_buy_sell_20260928.md`）：

1. 日线 czsc 出三买候选：突破笔越过中枢上沿 zg，且**突破幅度 < 40%**
   （幅度 = 突破笔高点 ÷ zg − 1，闸门在窗口打开那一刻，无未来函数）；
2. 回调窗口 = [突破笔终点, 日线可成交确认时刻)，窗口内**最早的 5min 一买**
   确认即入场（只要一买 —— 二买实测均值 −4.71%，已剔除）；
3. **成交价 = 5min 一买确认那根 bar 的收盘价**（baostock time 是 bar 结束
   时刻，确认瞬间收盘价已知，无未来函数）；
4. **固定持有 20 个交易日**，第 entry + 20×48 根 5min bar 收盘卖出。
   无止损无止盈（动态止损 −0.32%、固定止盈全线 ≤ 不止盈，均已实测否掉）。

本模块从 `scripts/eval_daily_5min.py` 抽出，供 GUI 标注 worker 与盘中监控
脚本共用 —— 评估脚本仍保留自己的统计口径（基准/bootstrap/报告），
**信号判定两处同源**，不许各写一份。

数据：`outputs/cache_min/{daily,5min}_<code>.csv`（baostock，`fetch_min.py`
落的缓存）。GUI 里任意股票若没有 5min 缓存，返回 None 由调用方如实提示。
"""
from __future__ import annotations

import bisect
from pathlib import Path

import pandas as pd

from core import chan as chan_mod
from core import chan_points
from data.models import KLineData

MIN5_BARS_PER_DAY = 48            # A 股一天 240 分钟 → 48 根 5min
DEFAULT_HOLD_DAYS = 20
DEFAULT_MAX_AMP = 40.0            # 突破幅度闸门（%）， None = 不过滤


def _ts(x) -> pd.Timestamp:
    return pd.Timestamp(str(x))


def load_cached(code: str, period: str, cache_dir: Path) -> list[KLineData] | None:
    """读 `fetch_min.py` 落的缓存 → KLineData；不存在/空文件返回 None

    停牌 bar 的空 volume（baostock 返空串）按 0 处理，不能 int(NaN) 抛异常。
    """
    f = Path(cache_dir) / f"{period}_{code}.csv"
    if not f.exists() or f.stat().st_size == 0:
        return None
    df = pd.read_csv(f)
    vol = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64")
    return [KLineData(code=code, date=str(r.dt), open=float(r.open),
                      high=float(r.high), low=float(r.low),
                      close=float(r.close), volume=int(v), period=period)
            for r, v in zip(df.itertuples(), vol)]


def _confirm_time(bis: list[dict], bi_idx: int):
    """几何点的**可成交确认时刻** = 该笔之后第一根笔的终点（KI-009：
    笔要锁定必须等后续反向笔成型）"""
    if bi_idx + 1 < len(bis):
        return _ts(bis[bi_idx + 1]["edt"])
    return None


def _last_bar_at_or_before(dts: list[pd.Timestamp], t: pd.Timestamp) -> int | None:
    i = bisect.bisect_right(dts, t) - 1
    return i if i >= 0 else None


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


def candidate_windows(code: str, cache_dir: Path, *,
                     max_amp: float | None = DEFAULT_MAX_AMP,
                     recent_days: int = 90) -> list[dict]:
    """日线三买的**活跃候选窗口**（供盘中监控层消费，与回测链同源）

    活跃 = 三买点的**确认笔**（bi_idx+1）还没走完 ⇒ `_confirm_time` 为 None
    —— 窗口一旦确认就关闭，之后的 5min 一买不再是策略入场点
    （回测链里这表现为 `[t_lo, t_dc)` 半开区间）。

    只保留窗口起点在最近 ``recent_days`` 个自然日内的：更老的开放窗口
    理论上仍有效，但三买点离突破已太远，按策略已无监控价值。

    返回（按窗口起点倒序）：
    ``[{"window_start", "signal_date", "amp", "zg", "breakout_high"}...]``
    —— ``window_start`` = 突破笔终点（= 回调窗口的左端）；数据不足返回 []。
    """
    kd = load_cached(code, "daily", cache_dir)
    if not kd:
        return []
    rd = chan_mod.build(kd, "daily")
    if rd is None:
        return []
    bis_d = chan_mod.bis(rd)
    zs_d = chan_mod.centers(rd)
    if not bis_d:
        return []

    cutoff = _ts(kd[-1].date) - pd.Timedelta(days=recent_days)
    out: list[dict] = []
    for p in chan_points.buy_sell_points(bis_d, zs_d):
        if p["kind"] != "三买":
            continue
        i = p.get("bi_idx")
        if i is None or i < 1:
            continue
        if _confirm_time(bis_d, i) is not None:
            continue                      # 确认笔已走完 → 窗口已关闭
        t_lo = _ts(bis_d[i - 1]["edt"])
        if t_lo < cutoff:
            continue
        amp = breakout_amp(bis_d, p)
        if max_amp is not None and (amp is None or amp >= max_amp):
            continue
        out.append({
            "window_start": str(t_lo)[:10],
            "signal_date": str(_ts(p["dt"]))[:10],
            "amp": round(amp, 1) if amp is not None else None,
            "zg": float(p["zg"]),
            "breakout_high": float(bis_d[i - 1]["high"]),
        })
    out.sort(key=lambda w: w["window_start"], reverse=True)
    return out


def triple_buy_trades(code: str, cache_dir: Path, *,
                      hold_days: int = DEFAULT_HOLD_DAYS,
                      max_amp: float | None = DEFAULT_MAX_AMP,
                      kinds: tuple[str, ...] = ("一买",)
                      ) -> dict | None:
    """单只标的跑完整信号链 → 逐笔交易 + 候选统计；数据不足返回 None

    返回
    ----
    ``{"trades": [{"buy_date", "buy_price", "sell_date", "sell_price",
                   "return_pct", "amp", "signal_date"}...],
       "candidates": 三买候选数（过闸门后）,
       "with_entry": 其中有 5min 入场的点数}``

    - ``buy_date`` = 5min 一买**确认 bar** 的日期；``buy_price`` = 该 bar 收盘价
    - ``sell_date``/``sell_price`` = 持有 ``hold_days`` 个交易日后的收盘；
      数据末尾持有窗不满时为空串/None（``return_pct`` 同样 None）
    - ``signal_date`` = 日线三买点日期（回调低点），供图上标候选位置
    - ``amp`` = 突破幅度%（可能为 None —— 突破笔方向异常时算不出）
    """
    kd = load_cached(code, "daily", cache_dir)
    k5 = load_cached(code, "5min", cache_dir)
    if not kd or not k5:
        return None

    rd = chan_mod.build(kd, "daily")
    r5 = chan_mod.build(k5, "5min")
    if rd is None or r5 is None:
        return None

    bis_d = chan_mod.bis(rd)
    zs_d = chan_mod.centers(rd)
    pts_d = [p for p in chan_points.buy_sell_points(bis_d, zs_d)
             if p["kind"] == "三买"]
    if max_amp is not None:
        pts_d = [p for p in pts_d
                 if (a := breakout_amp(bis_d, p)) is not None and a < max_amp]

    bis_5 = chan_mod.bis(r5)
    zs_5 = chan_mod.centers(r5)
    pts_5 = chan_points.buy_sell_points(bis_5, zs_5)

    dt5 = [_ts(k.date) for k in k5]
    cl5 = [float(k.close) for k in k5]
    cov_lo = dt5[0]

    # 5min 侧候选：只要一买（定稿口径），确认时刻 = 反向笔终点，
    # 成交价 = 确认那根 bar 的收盘价（无未来函数）
    cand5 = []
    for q in pts_5:
        if q["kind"] not in kinds:
            continue
        tconf = _confirm_time(bis_5, q["bi_idx"])
        if tconf is None:
            continue
        e5 = _last_bar_at_or_before(dt5, tconf)
        if e5 is None:
            continue
        cand5.append({"conf": tconf, "entry": e5})
    cand5.sort(key=lambda x: x["conf"])

    hold_bars = hold_days * MIN5_BARS_PER_DAY
    trades: list[dict] = []
    for p in pts_d:
        i = p["bi_idx"]
        if i < 1:
            continue
        t_lo = _ts(bis_d[i - 1]["edt"])         # 窗口起 = 突破笔终点
        t_pt = _ts(p["dt"])                     # 三买点（回调低点）
        t_dc = _confirm_time(bis_d, i)          # 日线可成交确认时刻
        if t_dc is None or t_pt < cov_lo:
            continue                            # 5min 未覆盖 / 无法确认
        inwin = [c for c in cand5 if t_lo <= c["conf"] < t_dc]
        if not inwin:
            continue

        e5 = inwin[0]["entry"]
        px5 = cl5[e5]
        j = e5 + hold_bars
        closed = j < len(cl5)
        trades.append({
            "buy_date": str(dt5[e5])[:10],
            "buy_price": round(px5, 3),
            "sell_date": str(dt5[j])[:10] if closed else "",
            "sell_price": round(cl5[j], 3) if closed else None,
            "return_pct": (round((cl5[j] / px5 - 1.0) * 100.0, 2)
                           if closed and px5 > 0 else None),
            "amp": (lambda a: round(a, 1) if a is not None else None)(
                breakout_amp(bis_d, p)),
            "signal_date": str(t_pt)[:10],
        })

    return {"trades": trades,
            "candidates": len(pts_d),
            "with_entry": len(trades)}
