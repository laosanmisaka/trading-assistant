# -*- coding: utf-8 -*-
"""可观察日线候选 → 5min 一买首次可见确认，逐点统计及配对随机基准。

默认只一买、突破幅度 <40%、固定 20×48 根 5min bar。每个窗口先实际
出现才允许入场，包括后来失效的候选；已观察的入场不会被重绘删掉。
相同输入与 core.triple_buy / monitor_buy 共用事件合同。输入起点、
复权、数据修订仍影响结构，合成测试不能替代真实市场的逐时存档验证。
当根收盘成交是乐观假设，不含成本、流动性与滑点，不是资金曲线。
旧版研究数字和动态出场规则不代表本版本结果。历史出场函数只供回归。
"""
from __future__ import annotations

import argparse
import bisect
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from core import chan as chan_mod                       # noqa: E402
from core import chan_points                            # noqa: E402
from core.chan_viz import normalize_code                # noqa: E402
from data.models import KLineData                       # noqa: E402
from eval_triple_buy import load_pool, summarize        # noqa: E402

DEFAULT_CACHE = ROOT / "outputs" / "cache_min"
DEFAULT_POOL = ROOT / "scripts" / "pool_random500.txt"

MIN5_BARS_PER_DAY = 48            # A 股一天 240 分钟 → 48 根 5min
BUY_KINDS = ("一买", "二买")
STOP_LEVELS = (3.0, 5.0, 8.0)     # 止损档（%）
from core.research_stats import (BOOT_N, BOOT_SEED, metrics, cluster_bootstrap, block_bootstrap)


def load_cached(code: str, period: str, cache_dir: Path) -> list[KLineData] | None:
    """读 `fetch_min.py` 落的缓存 → KLineData；不存在则 None

    ⚠️ 不复用 `eval_triple_buy.load_cached`：那个版本 `int(r.volume)` 遇到
    **停牌 bar 的空 volume**（baostock 会返回空串）会抛 `ValueError: cannot
    convert float NaN to integer`（神华日线 2025-08 就有 10 行）。
    """
    f = Path(cache_dir) / f"{period}_{code}.csv"
    if not f.exists() or f.stat().st_size == 0:
        return None
    df = pd.read_csv(f)
    vol = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64")
    return [KLineData(code=code, date=str(r.dt), open=float(r.open), high=float(r.high),
                      low=float(r.low), close=float(r.close), volume=int(v), period=period)
            for r, v in zip(df.itertuples(), vol)]


# ======================================================================
# 工具
# ======================================================================

def ts(x) -> pd.Timestamp:
    return pd.Timestamp(str(x))


def bars_of(bis: list[dict]) -> dict:
    """笔序列 → ``{笔对象 id: 索引}``（czsc 的笔是 dict，用 id 定位）"""
    return {id(b): i for i, b in enumerate(bis)}


def first_bar_after(dts: list[pd.Timestamp], t: pd.Timestamp) -> int | None:
    """``t`` 之后第一根 bar 的索引（``t`` 本身也算在"之前"）"""
    i = bisect.bisect_right(dts, t)
    return i if i < len(dts) else None


def last_bar_at_or_before(dts: list[pd.Timestamp], t: pd.Timestamp) -> int | None:
    i = bisect.bisect_right(dts, t) - 1
    return i if i >= 0 else None


def hold_ret(closes: list[float], entry_idx: int | None, entry_px: float | None,
             hold_bars: int) -> float | None:
    """持有 ``hold_bars`` 根 bar（出场那根**收盘**）→ 百分比收益；越界返回 None

    入场价**单独传入**，不取 ``opens[entry_idx]``：5min 侧是「确认那根 bar 收盘价
    直接成交」，入场价既不是该 bar 的开盘、也不在下一根。
    """
    if entry_idx is None or entry_px is None or entry_px <= 0:
        return None
    j = entry_idx + hold_bars
    if j >= len(closes):
        return None
    return (closes[j] / entry_px - 1.0) * 100.0


def excursion(highs: list[float], lows: list[float], entry_idx: int | None,
              entry_px: float | None, hold_bars: int,
              *, incl_entry_bar: bool = True) -> tuple[float, float] | None:
    """入场后持有窗内的 ``(MAE%, MFE%)``

    ``incl_entry_bar``：入场价是**开盘价**时，入场那根 bar 的极值也在暴露窗口内
    （``True``）；是**收盘价**时那根 bar 已经走完（``False``，窗口自下一根起）。
    出场在 ``entry_idx + hold_bars`` 那根的**收盘**，所以右端是闭的。
    """
    if entry_idx is None or entry_px is None or entry_px <= 0:
        return None
    j = entry_idx + hold_bars
    lo_i = entry_idx if incl_entry_bar else entry_idx + 1
    if lo_i > j or j >= len(lows):
        return None
    lo = min(lows[lo_i: j + 1])
    hi = max(highs[lo_i: j + 1])
    return (lo / entry_px - 1.0) * 100.0, (hi / entry_px - 1.0) * 100.0


def stop_ret(opens: list[float], lows: list[float], closes: list[float],
             entry_idx: int | None, entry_px: float | None, hold_bars: int,
             stop_pct: float, *, incl_entry_bar: bool = True) -> float | None:
    """触价止损口径的收益（%）

    窗口内首次跌破止损价即出：正常按止损价成交；**跳空低开**（该 bar 开盘
    已在止损价之下）按开盘价成交 —— 这是乐观/悲观的分界，不能假装能挂在
    止损价上。全程未破则与 ``hold_ret`` 同口径（第 N 根收盘出）。
    """
    if entry_idx is None or entry_px is None or entry_px <= 0:
        return None
    j = entry_idx + hold_bars
    if j >= len(closes):
        return None
    stop = entry_px * (1.0 + stop_pct / 100.0)
    for k in range(entry_idx if incl_entry_bar else entry_idx + 1, j + 1):
        if lows[k] <= stop:
            return (min(opens[k], stop) / entry_px - 1.0) * 100.0
    return (closes[j] / entry_px - 1.0) * 100.0


def baseline_ret(entry_px: list[float], exit_px: list[float],
                 hold_bars: int) -> dict | None:
    """同池随机入场基准：**每根 bar** 都按该级别的入场价买、持有 ``hold_bars``
    根后按该级别的出场价卖

    与信号口径**完全同构**，唯一区别是不挑时点：日线侧传 ``(open, close)``
    （开盘买）；5min 侧传 ``(close, close)``（确认 bar 收盘买）。不同构的基准
    没有意义 —— 拿 open 进的基准去比 close 进的信号，差的那一截是隔夜跳空，
    不是择时能力。取全体 bar（非抽样）⇒ 均值就是该区间的平均持有收益。
    """
    n = min(len(entry_px), len(exit_px))
    if hold_bars <= 0 or n <= hold_bars:
        return None
    vals = [(b / a - 1.0) * 100.0
            for a, b in zip(entry_px[: n - hold_bars], exit_px[hold_bars:n])
            if a and a > 0 and b and b > 0]      # 0 价 bar（停牌占位）不算有效样本
    if not vals:
        return None
    s = pd.Series(vals, dtype=float)
    return {"n": len(vals), "mean": float(s.mean()), "median": float(s.median())}


# ======================================================================
# 动态止损（10 日换线 + 15% 减半）
# ======================================================================

TRAIL_HALF_WEIGHT = 0.5          # 收益触及 half_profit 时减掉的仓位
TRAIL_MA = 20                    # 第二阶段跟随的日线均线周期


def trail_exit(hi: list[float], lo: list[float], op: list[float],
               cl: list[float], dts: list[pd.Timestamp],
               entry_idx: int | None, cost: float | None, *,
               zg: float | None = None,
               ma_prev: list[float | None] | None = None,
               dtd: list[pd.Timestamp] | None = None,
               trail_days: int = 10, trail_stop: float = 5.0,
               half_profit: float = 15.0, max_hold: int = 60,
               mode: str = "stage") -> dict | None:
    """动态止损 + 止盈减半：逐 5min bar 推进的持仓模拟

    入场价 = ``entry_idx`` 那根 bar 的**收盘价**（``cost``），暴露自**下一根**起
    （成交那根已经走完，与 ``excursion`` 的 ``incl_entry_bar=False`` 同源）。

    ``mode="stage"``（完整规则）：
    - 第 1~``trail_days`` 个交易日（每 48 根 bar 记 1 日）：``stop = max(cost×(1−trail_stop%), zg)``；
    - 第 ``trail_days+1`` 个交易日起：``stop = max(cost, 前一交易日收盘的 MA20)``；
    - 全程：``high ≥ cost×(1+half_profit%)`` → 按该价卖 ``TRAIL_HALF_WEIGHT``（只触发一次）；
    - ``low ≤ stop`` → 剩余全清，**跳空低开按该 bar 开盘价**成交（与 ``stop_ret`` 同口径）；
    - 同根 bar 内两者都触 → **按先止损处理**（bar 内部无法分辨先后，取悲观）。

    ``mode="halfonly"``：只做止盈减半、不设止损 ⇒ 用来拆出止损线单独贡献了多少。

    ⚠️ ``ma_prev`` 必须是**前一交易日收盘后**才已知的 MA20（调用方已 ``shift(1)``）。
    日内用当日 MA20 是未来函数 —— 盘中并不知道当天收盘价。

    ⚠️ 持仓窗不足 ``max_hold`` 个交易日的点返回 ``None``（与 ``hold_ret`` 越界
    同口径：不拿截断窗口冒充到期收益）。

    收益按**现金流口径** ``Σ(卖价×权重)/cost − 1`` —— 减半后剩半仓，两段成交价
    不能简单平均。
    """
    if entry_idx is None or cost is None or cost <= 0:
        return None
    n_bars = max_hold * MIN5_BARS_PER_DAY
    end = entry_idx + n_bars
    if end >= len(cl):
        return None

    stop1 = cost * (1.0 - trail_stop / 100.0)
    if zg is not None and not pd.isna(zg) and float(zg) > 0:
        stop1 = max(stop1, float(zg))        # 阶段 1 取较高者（更紧）
    half_px = cost * (1.0 + half_profit / 100.0)

    remain, half_done = 1.0, False
    legs: list[tuple[float, float, str]] = []
    cur_day, stop2 = None, cost
    k = entry_idx
    for k in range(entry_idx + 1, end + 1):
        if mode == "stage":
            day_no = (k - entry_idx - 1) // MIN5_BARS_PER_DAY + 1
            if day_no <= trail_days:
                stop: float | None = stop1
            else:
                d = dts[k].date()
                if d != cur_day:             # 每个交易日只需查一次 MA20
                    cur_day = d
                    stop2 = cost
                    if ma_prev is not None and dtd is not None:
                        j = bisect.bisect_right(dtd, ts(d)) - 1
                        if 1 <= j <= len(ma_prev):
                            m = ma_prev[j - 1]       # 前一交易日收盘才已知
                            if m is not None and not pd.isna(m) and m > 0:
                                stop2 = max(cost, float(m))
                stop = stop2
        else:
            stop = None
        if stop is not None and lo[k] <= stop:
            legs.append((remain, min(op[k], stop), "止损"))
            remain = 0.0
            break
        if not half_done and hi[k] >= half_px:
            legs.append((TRAIL_HALF_WEIGHT, half_px, "止盈减半"))
            remain -= TRAIL_HALF_WEIGHT
            half_done = True
    if remain > 1e-9:
        legs.append((remain, cl[end], "到期"))
        k = end

    gross = sum(w * px for w, px, _ in legs)
    bars = k - entry_idx
    return {"ret": (gross / cost - 1.0) * 100.0,
            "reason": legs[-1][2],
            "half": half_done,
            "bars": bars,
            "days": bars / MIN5_BARS_PER_DAY,
            "legs": legs}


def trail_baseline(hi: list[float], lo: list[float], op: list[float],
                   cl: list[float], dts: list[pd.Timestamp], *,
                   ma_prev: list[float | None] | None = None,
                   dtd: list[pd.Timestamp] | None = None,
                   trail_days: int = 10, trail_stop: float = 5.0,
                   half_profit: float = 15.0, max_hold: int = 60,
                   n_samples: int = 80, mode: str = "stage") -> dict | None:
    """同池随机入场 + **同一套动态止损** → 同构基准（时间轴上均匀抽样）

    动态止损要逐 bar 模拟，全 bar 穷举的代价是每点 ``max_hold×48`` 步 × 几万根
    bar，承受不起；改为在有效入场区间上**均匀**取 ``n_samples`` 个点。均匀覆盖
    时间轴 ⇒ 均值无偏，只是不再逐 bar 精确。

    ⚠️ 与信号口径的**唯一**差异：随机入场没有「参照中枢」⇒ 第一阶段止损线只有
    ``cost×(1−trail_stop%)``（比 ``max(cost×0.95, zg)`` 更松）。基准因此更容易
    扛过回调、数值偏高 ⇒ 信号超额被**低估**，方向上不会高估这套规则。
    """
    n_bars = max_hold * MIN5_BARS_PER_DAY
    last = len(cl) - n_bars - 1
    if last <= 0 or n_samples <= 0:
        return None
    step = max(1, last // n_samples)
    vals: list[float] = []
    for i in range(0, last, step):
        r = trail_exit(hi, lo, op, cl, dts, i, cl[i], zg=None, ma_prev=ma_prev,
                       dtd=dtd, trail_days=trail_days, trail_stop=trail_stop,
                       half_profit=half_profit, max_hold=max_hold, mode=mode)
        if r is not None and r["ret"] is not None:
            vals.append(float(r["ret"]))
    if not vals:
        return None
    s = pd.Series(vals, dtype=float)
    return {"n": len(vals), "mean": float(s.mean()), "median": float(s.median())}


def breakout_amp(bis: list[dict], p: dict) -> float | None:
    """三买点的**突破幅度%** = 突破笔高点 ÷ 参照中枢 zg − 1

    突破笔 = 三买点所在回抽笔的**前一根笔**（czsc 的笔严格交替，故恒为 ``i-1``）。
    ``zg`` 由 `chan_points.buy_sell_points()` 随点带出（判定时的那个中枢）。
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


# ======================================================================
# 单只分析
# ======================================================================

def _event_detail(code, window, events, minute, holds):
    import core.triple_buy as strategy
    eligible = [event for event in events if event["window_id"] == window["window_id"]]
    event = next((event for event in eligible if event["strategy_entry"]), None)
    times = [ts(bar.date) for bar in minute]
    index = times.index(ts(event["conf"])) if event else None
    price = event["entry_price"] if event else None
    high, low, close, op = ([float(getattr(bar, field)) for bar in minute]
                           for field in ("high", "low", "close", "open"))
    detail = {"代码": code, "window_id": window["window_id"],
              "窗口起": window["available_at"], "三买点": window["signal_date"],
              "窗口关闭": window["closed_at"], "突破幅度%": window["amp"],
              "5min候选": len(eligible), "最早买点": event["conf"] if event else None,
              "最早类型": "一买" if event else None, "_entry5": index, "_entry5px": price,
              "event_id": event["event_id"] if event else None,
              "entry_price": price, "entry_dt": event["conf"] if event else None,
              "geometry_active": event["active"] if event else None}
    for hold in holds:
        bars = hold * strategy.MIN5_BARS_PER_DAY
        detail[f"M{hold}"] = hold_ret(close, index, price, bars)
        extremes = excursion(high, low, index, price, bars, incl_entry_bar=False)
        detail[f"MAE{hold}"], detail[f"MFE{hold}"] = extremes or (None, None)
        for stop in STOP_LEVELS:
            detail[f"SL{hold}_{int(stop)}"] = stop_ret(op, low, close, index, price, bars,
                                                      -stop, incl_entry_bar=False)
    return detail


def analyze_one(code: str, cache_dir: Path, *, holds: list[int], min_mode="one",
                verbose=False, diag=None, min_breakout=0.0, trail=None, max_amp=40.0):
    """Only observable windows and first-buy events; old strategy variants are rejected."""
    from core.triple_buy import replay_cached
    if min_mode != "one" or min_breakout != 0 or trail is not None:
        raise ValueError("当前策略只支持 one + max_amp 上限 + 固定持有；旧变体请查历史版本")
    if not holds or any(hold <= 0 for hold in holds):
        raise ValueError("持有期必须为正整数")
    replay = replay_cached(code, cache_dir, max_amp=max_amp)
    if replay is None:
        if diag is not None:
            diag["reason"] = "daily/5min 缓存缺失"
        return None
    daily, minute = replay["daily"], replay["minute"]
    start, end = ts(minute[0].date), ts(minute[-1].date)
    windows = [w for w in replay["windows"] if ts(w["available_at"]) < end
               and (w["closed_at"] is None or ts(w["closed_at"]) > start)]
    details = [_event_detail(code, w, replay["events"], minute, holds) for w in windows]
    return _research_row(code, daily, minute, windows, details, holds, max_amp)


def _research_row(code, daily, minute, windows, details, holds, max_amp):
    closes = [float(bar.close) for bar in minute]
    start = ts(minute[0].date)
    base = {h: baseline_ret(closes, closes, h * MIN5_BARS_PER_DAY) for h in holds}
    structure = chan_mod.build(daily, "daily")
    amplitudes = [w["amp"] for w in windows]
    return {"代码": code, "日线bars": len(daily), "日线笔": len(chan_mod.bis(structure)) if structure else 0,
            "日线中枢": len(chan_mod.centers(structure)) if structure else 0, "三买点": len(windows),
            "被幅度剔除": 0, "5min覆盖点数": len(details), "覆盖起": str(start)[:10],
            "5minbars": len(minute), "有候选": sum(d["_entry5"] is not None for d in details),
            "提前中位": None, "价优中位": None,
            "幅度中位": float(np.median(amplitudes)) if amplitudes else None,
            "汇总": {h: {"日线": summarize([]), "5min": summarize([
                d[f"M{h}"] for d in details if d[f"M{h}"] is not None])} for h in holds},
            "基准": {"日线": {}, "5min": {h: b for h, b in base.items() if b}},
            "基准TR": {}, "明细": details, "method": "observable-v1", "max_amp": max_amp}


# ======================================================================
# 统计功效 / 稳健性
# ======================================================================

from core.research_summary import (collect_points, base_of, trail_points, trail_base_of,
                                   deoverlap, robustness, mae_table)
from scripts.daily_report import build_report, fmt as _fmt


def dump_points(rows: list[dict], path: Path, holds: list[int]) -> int:
    """逐点结果落 CSV（供外部复核 / 自己拿去做别的统计）"""
    recs: list[dict] = []
    for r in rows:
        for d in r["明细"]:
            rec = {k: v for k, v in d.items() if not k.startswith("_")}
            recs.append(rec)
    if not recs:
        return 0
    df = pd.DataFrame(recs)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return len(df)


def main(argv=None):
    parser = argparse.ArgumentParser(description="可观察日线窗口 + 5min 一买逐点研究")
    parser.add_argument("--pool", type=Path, default=DEFAULT_POOL)
    parser.add_argument("--codes", default="")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--holds", default="20")
    parser.add_argument("--min-mode", default="one", choices=["one"])
    parser.add_argument("--max-amp", type=float, default=40.0)
    parser.add_argument("--dump-points", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/daily5min_observable.md")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    holds = [int(x) for x in args.holds.split(",") if x.strip()]
    pool = ([(normalize_code(c.strip()), "") for c in args.codes.split(",") if c.strip()]
            if args.codes else load_pool(args.pool))
    if args.limit:
        pool = pool[:args.limit]
    if not pool or not holds or min(holds) <= 0:
        parser.error("标的池不能为空，持有期必须为正数")
    rows, skipped = [], []
    for code, name in pool:
        diag = {}
        try:
            result = analyze_one(code, args.cache_dir, holds=holds, diag=diag,
                                 max_amp=args.max_amp, verbose=args.verbose)
        except Exception as error:
            skipped.append((code, f"{type(error).__name__}: {error}"))
            continue
        if result is None:
            skipped.append((code, diag.get("reason", "无可用数据")))
        else:
            rows.append(result)
    if not rows:
        print(f"无有效样本：{skipped}", file=sys.stderr)
        return 1
    report = build_report(rows, min_mode="one", holds=holds, pool_size=len(pool), skipped=skipped)
    from utils.atomic import atomic_text
    from core.research_evidence import write_manifest
    manifest = args.out.with_suffix(".manifest.json")
    write_manifest(manifest, codes=[code for code, _ in pool], cache_dir=args.cache_dir,
                   parameters={"holds": holds, "max_amp": args.max_amp, "min_mode": "one"},
                   pool=None if args.codes else args.pool)
    report += f"\n复现清单：[{manifest.name}]({manifest.name})\n"
    atomic_text(args.out, report)
    if args.dump_points:
        dump_points(rows, Path(args.dump_points), holds)
    print(f"报告 → {args.out}；{len(rows)} 只成功，{len(skipped)} 只失败")
    return 1 if skipped else 0


if __name__ == "__main__":
    sys.exit(main())
