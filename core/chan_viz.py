# -*- coding: utf-8 -*-
"""Offline historical geometry viewer and reusable geometry marks.

CZSC supplies fractals/strokes/centers; chan_points supplies geometric points.
The retained daily/30min chan_strategy overlay uses final historical structures:
its endpoints and T+1 shifts are NOT a causal execution proof. Current observable
triple-buy trades come from core.triple_buy and are attached separately by the
GUI worker. This viewer preserves the old overlay only as labeled diagnostics.

Text nodes and inline JSON are escaped independently; template substitutions
run once. resources/chan_chart.html contains the presentation template and the
vendored ECharts license/provenance is in THIRD_PARTY_NOTICES.md.
"""
from __future__ import annotations

import argparse
import json
import html as html_utils
import re
import sys
from bisect import bisect_left
from pathlib import Path
from typing import Iterable, Optional, Sequence

import pandas as pd

from core import chan as chan_mod
from core import chan_points
from core import chan_strategy
from core.backtest.strategy import Action
from data.models import KLineData
from utils.logger import get_logger

logger = get_logger(__name__)

# 周期标签（用于标题与说明）
PERIOD_LABEL = {"1min": "1分钟", "5min": "5分钟", "15min": "15分钟",
                "30min": "30分钟", "60min": "60分钟", "daily": "日线"}
# 项目 period → akshare 新浪分钟接口的 period
_AK_MIN_PERIOD = {"1min": "1", "5min": "5", "15min": "15",
                  "30min": "30", "60min": "60"}

# 图上颜色（A 股惯例：买=暖色系，卖=绿色系）
STYLE = {
    "up": "#e53935", "down": "#26a69a",
    "center_fill": "rgba(96,125,139,0.16)",
    "center_line": "rgba(69,90,100,0.85)",
    # 日线中枢：主图层，实线框 —— 用来一眼看出「哪一段是震荡区」
    "center_daily_fill": "rgba(69,90,100,0.09)",
    "center_daily_line": "rgba(55,71,79,0.95)",
    "bi": "rgba(92,107,192,0.75)",
    "fx_top": "#e57373", "fx_bottom": "#81c784",
    "ma_short": "#ff9800", "ma_long": "#2196f3",
    "d1_line": "#8e24aa", "d2_line": "#ef6c00",
    # 日线级别买卖点（主图层）
    "daily": {"一买": "#7b1fa2", "二买": "#e65100", "三买": "#f9a825",
              "一卖": "#004d40", "二卖": "#1b5e20", "三卖": "#2e7d32"},
    # 30 分钟级别（次图层，默认关）
    "m30": {"一买": "#ba68c8", "二买": "#ffb74d", "三买": "#fff176",
            "一卖": "#4db6ac", "二卖": "#81c784", "三卖": "#a5d6a7"},
    "strategy_buy": "#d32f2f", "strategy_sell": "#2e7d32",
    "strategy_hit": "#1565c0",
}

# 图例顺序
POINT_ORDER = ("一买", "二买", "三买", "一卖", "二卖", "三卖")


# ======================================================================
# 行情获取
# ======================================================================

def normalize_code(code: str) -> str:
    """`600519` / `sh600519` / `600519.SH` → `sh600519`（新浪接口格式）"""
    c = str(code).strip().lower()
    if c.startswith(("sh", "sz", "bj")):
        return c
    if "." in c:
        num, _, suffix = c.partition(".")
        suffix = suffix.lower()
        if suffix in ("sh", "ss"):
            return "sh" + num.zfill(6)
        if suffix == "sz":
            return "sz" + num.zfill(6)
        if suffix == "bj":
            return "bj" + num.zfill(6)
        c = num
    c = c.zfill(6)
    if c[0] == "6":
        return "sh" + c
    if c[0] in ("0", "3"):
        return "sz" + c
    if c[0] in ("4", "8"):
        return "bj" + c
    return "sh" + c


def fetch_klines(code: str, period: str = "30min") -> list[KLineData]:
    """取分钟 K 线（前复权）

    用新浪源（`ak.stock_zh_a_minute`）—— 实测能取到约 1970 根 30 分钟
    K 线（约 247 个交易日），而东方财富的分钟接口只给最近约 250 根，
    不够 czsc 的 500 根预热。通达信（mootdx）端口在部分环境下不通。
    """
    if period not in _AK_MIN_PERIOD:
        raise ValueError(f"可视化只支持分钟周期，收到 {period!r}；"
                         f"可选 {sorted(_AK_MIN_PERIOD)}")
    try:
        import akshare as ak
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise ImportError("取行情需要 akshare：pip install akshare") from exc

    sym = normalize_code(code)
    raw = ak.stock_zh_a_minute(symbol=sym, period=_AK_MIN_PERIOD[period],
                               adjust="qfq")
    if raw is None or len(raw) == 0:
        raise RuntimeError(f"{sym} 未取到 {period} 行情")

    df = pd.DataFrame({
        "date": pd.to_datetime(raw["day"]),
        "open": pd.to_numeric(raw["open"], errors="coerce"),
        "high": pd.to_numeric(raw["high"], errors="coerce"),
        "low": pd.to_numeric(raw["low"], errors="coerce"),
        "close": pd.to_numeric(raw["close"], errors="coerce"),
        "volume": pd.to_numeric(raw["volume"], errors="coerce").fillna(0),
    }).dropna(subset=["open", "high", "low", "close"])
    df = df.sort_values("date").drop_duplicates("date", keep="last")

    return [KLineData(code=sym, date=str(r.date), open=float(r.open),
                      high=float(r.high), low=float(r.low), close=float(r.close),
                      volume=int(r.volume), period=period)
            for r in df.itertuples()]


def stock_name(code: str) -> str:
    """股票简称，取不到就返回代码（不抛异常）"""
    sym = normalize_code(code)
    try:
        import akshare as ak
        info = ak.stock_individual_info_em(symbol=sym[2:])
        row = info[info["item"] == "股票简称"]
        if len(row):
            return str(row.iloc[0]["value"])
    except Exception as exc:  # 网络/接口变更都不该挡住画图
        logger.debug(f"取股票简称失败 {sym}: {exc}")
    return sym


# ======================================================================
# 小工具
# ======================================================================

def _naive(series):
    """转成不带时区的 Timestamp

    czsc 输出的 dt 带 UTC 时区（墙钟时间仍是北京时间），直接与本地
    朴素时间比较会全不匹配，所以统一去掉时区再对齐。
    """
    s = pd.to_datetime(series)
    if isinstance(s, pd.Series):
        if getattr(s.dt, "tz", None) is not None:
            s = s.dt.tz_localize(None)
    elif getattr(s, "tz", None) is not None:
        s = s.tz_localize(None)
    return s


def _nearest_idx(dt, dt_list: Sequence, dt_keys: list) -> int:
    """dt → 在 dt_list 中的最近下标（找不到就取时间上最近的）"""
    try:
        t = _naive(pd.Timestamp(dt))
    except Exception:
        return -1
    key = t.value
    i = bisect_left(dt_keys, key)
    if i <= 0:
        return 0
    if i >= len(dt_keys):
        return len(dt_keys) - 1
    before, after = dt_keys[i - 1], dt_keys[i]
    return i - 1 if (key - before) <= (after - key) else i


def transitions(hit: pd.Series) -> list[int]:
    """状态跃迁点下标：False → True 的位置

    通用工具（`chan_strategy` 的信号提取同口径）。注意几何买卖点
    **不用**这个 —— 它只对「持续状态型」信号有意义。
    """
    flags = hit.fillna(False).astype(bool).tolist()
    out, prev = [], False
    for i, f in enumerate(flags):
        if f and not prev:
            out.append(i)
        prev = f
    return out


def _find_column(columns: Iterable[str], prefix: str, *tokens: str) -> Optional[str]:
    """按「前缀 + 关键字」定位 czsc 输出列（列名随参数变化，不能硬编码）"""
    for c in columns:
        if c.startswith(prefix) and all(t in c for t in tokens):
            return c
    return None


def _resample_daily(df: pd.DataFrame) -> pd.DataFrame:
    """30 分钟 → 日线（实现已上移到 `core.chan.resample_daily`，此处保留旧名）"""
    return chan_mod.resample_daily(df)


def _to_klines(df: pd.DataFrame, period: str, code: str) -> list[KLineData]:
    """DataFrame → KLineData（实现已上移到 `core.chan.df_to_klines`）"""
    return chan_mod.df_to_klines(df, period=period, code=code)


def _daily_point_to_bar(day, price: float, side: str,
                        day_range: dict, lows: list, highs: list) -> int:
    """日线买卖点 → 30 分钟图上的 bar 下标

    日线笔端点是「某一天」，而 30 分钟图上是「某根 bar」。取该交易日内
    极值与日线端点价格最接近的那根（买点比 low、卖点比 high），
    这样标记会落在当天真正的低/高点附近，而不是随便压在第一根上。
    """
    key = pd.Timestamp(day).date()
    rng = day_range.get(key)
    if rng is None:
        return -1
    i0, i1 = rng
    vals = lows[i0:i1 + 1] if side == "buy" else highs[i0:i1 + 1]
    if not vals:
        return i0
    k = min(range(len(vals)), key=lambda j: abs(vals[j] - price))
    return i0 + k


# ======================================================================
# 数据组装
# ======================================================================

def build_payload(
    klines: Sequence[KLineData],
    code: str = "",
    *,
    period: str = "30min",
    name: str = "",
    ma_short: int = 5,
    ma_long: int = 10,
) -> dict:
    """把行情算成图表要的全部数据（纯数据，不含渲染）"""
    df = chan_mod.klines_to_df(klines)
    if len(df) < 3:
        raise ValueError(f"K 线不足（{len(df)} 根），无法计算缠论结构")
    df = df.copy()
    df["dt"] = _naive(df["dt"])

    dt_list = list(df["dt"])
    dt_keys = [t.value for t in dt_list]
    dates = [t.strftime("%Y-%m-%d %H:%M") for t in dt_list]
    days = [t.date() for t in dt_list]
    n = len(dt_list)

    o = pd.to_numeric(df["open"], errors="coerce").tolist()
    c = pd.to_numeric(df["close"], errors="coerce").tolist()
    h = pd.to_numeric(df["high"], errors="coerce").tolist()
    lo = pd.to_numeric(df["low"], errors="coerce").tolist()
    v = pd.to_numeric(df["vol"], errors="coerce").fillna(0).tolist()

    # 每个交易日在全量序列上的 [首根, 末根]
    day_range: dict = {}
    for i, d in enumerate(days):
        if d in day_range:
            day_range[d][1] = i
        else:
            day_range[d] = [i, i]

    # ---- 1. 30 分钟缠论结构 + 几何买卖点 ----
    cr = chan_mod.build(klines, period=period)
    bis_raw = chan_mod.bis(cr) if cr else []
    centers_raw = chan_mod.centers(cr) if cr else []
    fx_raw = chan_mod.fractals(cr) if cr else []
    if not cr:
        logger.warning(f"{code}: 缠论结构算不出来（K 线不足）")

    pts_m30: list[dict] = []
    for p in chan_points.buy_sell_points(
            bis_raw, centers_raw, bars=df,
            require_divergence=chan_strategy.DEFAULT_REQUIRE_DIVERGENCE):
        i = _nearest_idx(p["dt"], dt_list, dt_keys)
        pts_m30.append({"kind": p["kind"], "idx": i, "price": p["price"],
                        "divergence": p.get("divergence")})

    # ---- 2. 日线缠论结构 + 几何买卖点（映射回 30 分钟下标） ----
    daily_df = _resample_daily(df)
    pts_daily: list[dict] = []
    cr_d = None
    if len(daily_df) >= 3:
        cr_d = chan_mod.build(_to_klines(daily_df, "daily", code), period="daily")
        if cr_d:
            for p in chan_points.buy_sell_points(
                    chan_mod.bis(cr_d), chan_mod.centers(cr_d),
                    bars=daily_df,
                    require_divergence=chan_strategy.DEFAULT_REQUIRE_DIVERGENCE):
                side = "buy" if p["kind"].endswith("买") else "sell"
                i = _daily_point_to_bar(p["dt"], p["price"], side,
                                        day_range, lo, h)
                if i >= 0:
                    pts_daily.append({"kind": p["kind"], "idx": i,
                                      "price": p["price"],
                                      "divergence": p.get("divergence")})
    logger.info(f"{code}: 几何买卖点 日线 {len(pts_daily)} 个 / "
                f"30 分钟 {len(pts_m30)} 个（笔 {len(bis_raw)}、"
                f"30 分钟中枢 {len(centers_raw)}）")

    def pack(points: list[dict]) -> dict:
        g: dict[str, list] = {k: [] for k in chan_points.KINDS}
        for p in points:
            item = [p["idx"], round(float(p["price"]), 3)]
            # 一买/一卖的背驰标注：1=背驰确认 0=未背驰；未判定/不适用则省略，
            # 保持 [idx, price] 两元素旧格式（下游按 p[0]/p[1] 取值，兼容）
            div = p.get("divergence")
            if div is not None:
                item.append(1 if div else 0)
            g[p["kind"]].append(item)
        return {k: v for k, v in g.items() if v}

    # ---- 3. 中枢 / 笔 / 分型 ----
    centers = []
    for zs in centers_raw:
        i0 = _nearest_idx(zs["sdt"], dt_list, dt_keys)
        i1 = _nearest_idx(zs["edt"], dt_list, dt_keys)
        if i1 < i0:
            i0, i1 = i1, i0
        centers.append([i0, max(i1, i0), round(float(zs["low"]), 3),
                        round(float(zs["high"]), 3)])

    # ---- 3b. 日线中枢（主图层）----
    # 2026-09-18 老三指出：图上只画 30 分钟笔中枢时，一个中枢只有几十根 bar、
    # 一年下来 20 多个碎框，看不出「哪一段是震荡区」。他要的是**日线级别**中枢
    # —— 用同一份数据合成日线、在日线上算笔与中枢，再按交易日映射回 30 分钟下标。
    # 实测（紫金矿业 246 个交易日）：30 分钟笔中枢 26 个 → 日线中枢 5 个，
    # 且日线中枢的 [zd, zg] 与他手绘的三个框逐一对上（中框 [31.64, 34.39]
    # vs 手绘 [31.62, 34.69]）。日线 czsc 对象原本就为算日线买卖点而建，
    # 这里只是把它的中枢也取出来，不额外增加计算。
    centers_daily = []
    if cr_d:
        for zs in chan_mod.centers(cr_d):
            r0 = day_range.get(pd.Timestamp(str(zs["sdt"])[:10]).date())
            r1 = day_range.get(pd.Timestamp(str(zs["edt"])[:10]).date())
            if r0 is None or r1 is None:
                continue
            centers_daily.append([r0[0], r1[1],
                                  round(float(zs["low"]), 3),
                                  round(float(zs["high"]), 3)])

    bi_points: list[list] = []
    for b in bis_raw:
        # 注意：chan.bis() 的 start_idx/end_idx 基准是 czsc 的 bars_raw
        # （实测比输入少 7 根），不能直接当全量下标用 —— 必须按 dt 对齐。
        i0 = _nearest_idx(b["sdt"], dt_list, dt_keys)
        i1 = _nearest_idx(b["edt"], dt_list, dt_keys)
        if i0 < 0 or i1 < 0:
            continue
        if str(b["direction"]) == "Up":
            a, z = [i0, round(float(b["low"]), 3)], [i1, round(float(b["high"]), 3)]
        else:
            a, z = [i0, round(float(b["high"]), 3)], [i1, round(float(b["low"]), 3)]
        if not bi_points or bi_points[-1] != a:
            bi_points.append(a)
        bi_points.append(z)

    fx = {"top": [], "bottom": []}
    for f in fx_raw:
        i = _nearest_idx(f["dt"], dt_list, dt_keys)
        fx["top" if f["kind"] == "top" else "bottom"].append(
            [i, round(float(f["price"]), 3)])

    # ---- 4. 日线 MA（卖出线）：按日线收盘算，再广播回各 bar ----
    tmp = pd.DataFrame({"day": days, "close": c})
    day_close = tmp.groupby("day", sort=True)["close"].last()
    ma_s = day_close.rolling(ma_short).mean()
    ma_l = day_close.rolling(ma_long).mean()
    ma5 = tmp["day"].map(ma_s).tolist()
    ma10 = tmp["day"].map(ma_l).tolist()

    # ---- 5. 策略结果 ----
    # 策略层 2026-09-18 起默认走**几何源**（`chan_points` 的判定），不再用
    # czsc 的 `cxt_*` 择时信号。这里复用本函数已经建好的两套缠论结构
    # （cr / cr_d）直接推点事件，不再让策略层重建一遍 czsc 对象 ——
    # 建对象是全链路最慢的一步。
    geo = chan_strategy.points_from_structures(
        bis_raw, centers_raw,
        chan_mod.bis(cr_d) if cr_d else [],
        chan_mod.centers(cr_d) if cr_d else [],
        days, sorted(set(days)), dt_list,
        bars_m30=df, bars_daily=daily_df)
    strat = chan_strategy.scan_from_frame(
        df, code=code, source=chan_strategy.SOURCE_GEOMETRY, points=geo,
        ma_short=ma_short, ma_long=ma_long)

    def _day_first_idx(day) -> int:
        rng = day_range.get(pd.Timestamp(day).date())
        return rng[0] if rng else -1

    def _day_last_idx(day) -> int:
        # 卖出按当日收盘价成交，所以标在当天最后一根 bar 上
        rng = day_range.get(pd.Timestamp(day).date())
        return rng[1] if rng else -1

    trades = []
    for t in strat.trades:
        b_idx = _nearest_idx(t.buy.dt, dt_list, dt_keys)
        sig_idx = _nearest_idx(t.buy.signal_dt, dt_list, dt_keys)
        sell = None
        if t.sell is not None:
            sell = {
                "idx": _day_last_idx(t.sell.dt),
                "dt": str(t.sell.dt),
                "price": round(t.sell.price, 3),
                "ma": t.sell.ma,
                "hold_days": t.sell.hold_days,
                "confirm_dt": str(t.sell.confirm_dt),
                "confirm_idx": _day_last_idx(t.sell.confirm_dt),
                "return_pct": (round(t.return_pct, 2)
                               if t.return_pct is not None else None),
            }
        trades.append({
            "buy": {"idx": b_idx, "dt": str(t.buy.dt)[:16],
                    "price": round(t.buy.price, 3),
                    "signal_idx": sig_idx,
                    "signal_dt": str(t.buy.signal_dt)[:16],
                    "d1": str(t.buy.d1), "d2": str(t.buy.d2),
                    "gap_1to2": t.buy.gap_1to2, "gap_2tom": t.buy.gap_2tom},
            "sell": sell,
        })

    m30_hits = [i for i in (_nearest_idx(x, dt_list, dt_keys)
                            for x in strat.m30_buy2_bars)
                if i >= 0 and any(t["buy"]["signal_idx"] == i for t in trades)]

    # ---- 6. 默认视窗 ----
    # 铺满全量：主图层是**日线**级别的中枢与买卖点（一个中枢跨数月、一年
    # 只有 5~9 个点），聚焦到几百根 bar 反而看不出结构。抠细节用底部滑条
    # 或滚轮缩放。
    # 策略买卖点用 32px 的大圆点、尺寸不随缩放变化，全量视图下依然清晰，
    # 所以「看结构」和「对策略点」不冲突。
    # （旧版默认贴在最右端 —— 向右拖 1 像素即触边界，是「拖着拖着拖不动」
    # 的根因；中间版本改成聚焦交易窗口，只为对策略点，现在不必了。）
    start, end = 0, n - 1

    packed_daily = pack(pts_daily)
    packed_m30 = pack(pts_m30)
    counts = {
        "日线中枢": len(centers_daily), "30分中枢": len(centers),
        "笔": len(bis_raw),
        "顶分型": len(fx["top"]), "底分型": len(fx["bottom"]),
        "策略买点": len(trades),
        "日线买卖点": sum(len(x) for x in packed_daily.values()),
        "30分钟买卖点": sum(len(x) for x in packed_m30.values()),
    }

    payload = {
        "meta": {
            "code": normalize_code(code),
            "name": name,
            "period": period,
            "period_label": PERIOD_LABEL.get(period, period),
            "bars": n,
            "trading_days": int(len(day_close)),
            "ma_short": ma_short,
            "ma_long": ma_long,
            "first_dt": dates[0],
            "last_dt": dates[-1],
            "counts": counts,
            "strategy_summary": strat.summary(),
            "daily_points": {k: len(v) for k, v in pack(pts_daily).items()},
            "m30_points": {k: len(v) for k, v in pack(pts_m30).items()},
        },
        "style": STYLE,
        "price_min": round(min(lo), 3),
        "price_max": round(max(h), 3),
        "dates": dates,
        # 紧凑数组而非对象：JSON 体积小 ~60%，浏览器解析更快
        # 列序：[open, close, low, high, vol, ma5, ma10]
        "bars": [
            [round(float(o[i]), 3), round(float(c[i]), 3),
             round(float(lo[i]), 3), round(float(h[i]), 3),
             float(v[i]),
             None if pd.isna(ma5[i]) else round(float(ma5[i]), 3),
             None if pd.isna(ma10[i]) else round(float(ma10[i]), 3)]
            for i in range(n)
        ],
        "centers": centers,
        "centers_daily": centers_daily,
        "bi_points": bi_points,
        "fractals": fx,
        "points": {"daily": packed_daily, "m30": packed_m30},
        "strategy": {
            "d1_days": [i for i in (_day_first_idx(d) for d in strat.buy1_days)
                        if i >= 0],
            "d2_days": [i for i in (_day_first_idx(d) for d in strat.buy2_days)
                        if i >= 0],
            "m30_hits": m30_hits,
            "trades": trades,
        },
        "zoom": {"startValue": start, "endValue": end},
    }
    return payload


# ======================================================================
# 图上标注（UI 复用）
# ======================================================================

def marks_from_payload(payload: dict) -> dict:
    """payload → 「按日期」的买卖点标注（给 UI 的 matplotlib 日线图用）

    UI 的 K 线图（`ui/chart_widget.py`）画的是**日线**、横轴是日期，
    而 payload 里的点坐标是 **30 分钟 bar 下标**，所以要在这里换一次算。

    返回
    ----
    ``{"geometry": [{"date", "kind", "price"}, ...],
       "trades":   [{"buy_date", "buy_price", "sell_date", "sell_price",
                     "return_pct"}, ...]}``

    - ``geometry``：日线级别的几何买卖点（一/二/三 买与卖）—— 与 HTML 图上
      的「日线买卖点」是同一批点
    - ``trades``：策略实际成交（`chan_strategy` 三重共振买入 + 均线卖出）
    - ``sell_date`` 为空串 = 数据末尾仍未平仓（收益也是 None）
    """
    dates = payload.get("dates") or []
    geo: list[dict] = []
    for kind, points in (payload.get("points", {}).get("daily") or {}).items():
        for p in points:
            idx, price = p[0], p[1]      # 第三元素（背驰标注）UI 标注不用
            if 0 <= idx < len(dates):
                geo.append({"date": str(dates[idx])[:10], "kind": kind,
                            "price": float(price)})

    trades: list[dict] = []
    for t in payload.get("strategy", {}).get("trades", []):
        buy = t.get("buy") or {}
        sell = t.get("sell") or {}
        trades.append({
            "buy_date": str(buy.get("dt", ""))[:10],
            "buy_price": float(buy.get("price") or 0.0),
            "sell_date": str(sell.get("dt", ""))[:10],
            "sell_price": (float(sell["price"])
                           if sell.get("price") is not None else None),
            "return_pct": sell.get("return_pct"),
        })

    geo.sort(key=lambda p: p["date"])
    trades.sort(key=lambda t: t["buy_date"])
    return {"geometry": geo, "trades": trades}


def marks_from_klines(klines, code: str = "", name: str = "") -> dict:
    """行情 → 图上标注（``build_payload`` + ``marks_from_payload`` 的组合）

    额外带上 ``meta``（代码 / 名称 / bar 数 / 交易日 / 各级点数），
    供状态栏与排查用；画图只看 geometry 与 trades。
    """
    payload = build_payload(klines, code=code, name=name)
    marks = marks_from_payload(payload)
    marks["meta"] = payload.get("meta", {})
    return marks


# ======================================================================
# 渲染
# ======================================================================

def _echarts_source(echarts_path: Optional[str] = None) -> str:
    """ECharts 库源码（默认取 resources/echarts.min.js，内联进 HTML）"""
    path = Path(echarts_path) if echarts_path else (
        Path(__file__).resolve().parent.parent / "resources" / "echarts.min.js")
    if not path.exists():
        raise FileNotFoundError(
            f"找不到 ECharts 库：{path}。请先下载 echarts.min.js 放进 resources/")
    return path.read_text(encoding="utf-8")


_HTML = (Path(__file__).resolve().parents[1] / "resources" / "chan_chart.html").read_text(encoding="utf-8")


def render_html(payload: dict, echarts_path: Optional[str] = None) -> str:
    """payload → 单文件 HTML（内联 ECharts，离线可看）"""
    meta = payload["meta"]
    title = (f"{meta['name']}（{meta['code']}）"
             f"{meta['period_label']}缠论标注图")
    counts = meta["counts"]
    dp, mp = meta["daily_points"], meta["m30_points"]
    chips = [
        ("K线", f"{meta['bars']} 根 / {meta['trading_days']} 交易日"),
        ("区间", f"{meta['first_dt']} → {meta['last_dt']}"),
        ("日线中枢", counts.get("日线中枢", 0)),
        ("30分中枢", counts.get("30分中枢", 0)),
        ("笔", counts.get("笔", 0)),
        ("分型", f"顶 {counts.get('顶分型', 0)} / 底 {counts.get('底分型', 0)}"),
        ("日线买点", f"一 {dp.get('一买', 0)} / 二 {dp.get('二买', 0)} / "
                     f"三 {dp.get('三买', 0)}"),
        ("日线卖点", f"一 {dp.get('一卖', 0)} / 二 {dp.get('二卖', 0)} / "
                     f"三 {dp.get('三卖', 0)}"),
        ("30分买点", f"一 {mp.get('一买', 0)} / 二 {mp.get('二买', 0)} / "
                     f"三 {mp.get('三买', 0)}"),
        ("30分卖点", f"一 {mp.get('一卖', 0)} / 二 {mp.get('二卖', 0)} / "
                     f"三 {mp.get('三卖', 0)}"),
        ("策略买点", counts.get("策略买点", 0)),
    ]
    meta_html = "".join(
        f'<span class="box">{k}：<b>{html_utils.escape(str(v))}</b></span>' for k, v in chips)
    s = meta["strategy_summary"]
    if s.get("买点数"):
        # 允许重叠持仓（见 chan_strategy docstring「持仓与统计口径」），
        # 所以要显式标出同时持仓笔数与重叠笔数 —— 否则「胜率/平均收益」
        # 会被当成组合收益读，而它是按笔统计的。
        conc, ov = s.get("最大同时持仓", 1), s.get("重叠笔数", 0)
        conc_color = "#c62828" if (conc or 0) > 1 else "#333"
        meta_html += ('<span class="box">已平仓：<b>{}</b>　胜率：<b>{}%</b>　'
                      '平均：<b>{}%</b>　平均持有：<b>{} 交易日</b></span>'
                      '<span class="box">最大同时持仓：'
                      '<b style="color:{}">{}</b>　重叠笔数：<b>{}</b>'
                      '<span style="color:#888">（按笔统计，非组合收益）</span>'
                      '</span>').format(*map(lambda v: html_utils.escape(str(v)), (
            s["已平仓"], s["胜率%"], s["平均收益%"], s["平均持有交易日"],
            conc_color, conc, ov)))

    note = (
        "<b>历史结构诊断的阅读边界</b><ol>"
        "<li>日线与分钟中枢分级展示；几何点位于笔端点，不等于实时观察或成交时刻。</li>"
        "<li>旧多周期叠加使用完整历史结构。把端点顺延一根 bar 或一天，仍不能消除结构确认滞后；"
        "cross、日线一/二买以及 30min 命中均不得据此声称无未来信息。</li>"
        "<li>图中旧策略收益按点统计，可重叠，不是资金曲线。当前 daily→5min 一买策略使用"
        "core.triple_buy 的首次观察事件，与此旧多周期诊断不同。</li>"
        "<li>输入来源、复权、覆盖与版本影响结构；新浪实时与缓存历史不保证逐点相同。</li></ol>"
    )

    encoded = json.dumps(payload, ensure_ascii=False)
    for char in ("<", ">", "&", "\u2028", "\u2029"):
        encoded = encoded.replace(char, "\\u%04x" % ord(char))
    substitutions = {"__TITLE__": html_utils.escape(title), "__META__": meta_html,
                     "__NOTE__": note, "__PAYLOAD__": encoded,
                     "__ECHARTS__": _echarts_source(echarts_path),
                     "POINT_KEYS": json.dumps(POINT_ORDER)}
    # Substitute only template tokens, never re-process a user's literal token.
    return re.sub("|".join(map(re.escape, substitutions)),
                  lambda match: substitutions[match.group()], _HTML)


def save_html(html: str, out_path) -> Path:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path


def generate(code: str, out_path=None, *, period: str = "30min",
             ma_short: int = 5,
             ma_long: int = 10, echarts_path: Optional[str] = None,
             name: Optional[str] = None,
             klines: Optional[Sequence[KLineData]] = None) -> Path:
    """取行情 → 算缠论与策略 → 出 HTML，返回文件路径"""
    if klines is None:
        klines = fetch_klines(code, period=period)
    if not name:
        name = stock_name(code)
    payload = build_payload(klines, code=code, period=period, name=name,
                            ma_short=ma_short, ma_long=ma_long)
    html = render_html(payload, echarts_path=echarts_path)
    if out_path is None:
        out_path = Path("outputs") / f"chan_{payload['meta']['code']}_{period}.html"
    path = save_html(html, out_path)
    logger.info(f"已生成 {path}（{payload['meta']['bars']} 根 K 线，"
                f"日线中枢 {len(payload['centers_daily'])} 个 / "
                f"30分中枢 {len(payload['centers'])} 个，"
                f"策略买点 {len(payload['strategy']['trades'])} 个）")
    return path


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="生成缠论标注 K 线图（单文件 HTML，离线可看）")
    ap.add_argument("code", help="股票代码，如 sh688981 / 600519")
    ap.add_argument("--period", default="30min",
                    choices=sorted(PERIOD_LABEL), help="K 线周期（默认 30min）")
    ap.add_argument("--out", default=None, help="输出 HTML 路径")
    ap.add_argument("--name", default=None,
                    help="股票简称（默认联网取，取不到就用代码）")
    ap.add_argument("--ma-short", type=int, default=5)
    ap.add_argument("--ma-long", type=int, default=10)
    args = ap.parse_args(argv)

    path = generate(args.code, args.out, period=args.period,
                    ma_short=args.ma_short,
                    ma_long=args.ma_long, name=args.name)
    print(path)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
