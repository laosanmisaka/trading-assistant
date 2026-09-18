# -*- coding: utf-8 -*-
"""缠论 K 线可视化 —— 中枢 / 分型 / 笔 / 买卖点，以及多周期共振策略结果

单文件 HTML、内联 ECharts、离线可看。

======================================================================
图中画了什么
======================================================================

| 图层 | 来源 | 含义 |
|------|------|------|
| K 线 | 30 分钟原始行情 | 涨红跌绿（A 股惯例） |
| 中枢 | `CZSC.zs_list` | 矩形 = [zd, zg]，横跨 [sdt, edt] |
| 笔   | `CZSC.bi_list` | 分型端点连线 |
| 分型 | `CZSC.fx_list` | 顶/底分型散点（默认收起） |
| 日线买卖点 | `chan_points` 几何 | **主图层**：一/二/三 买与卖，位置在笔端点 |
| 30 分钟买卖点 | `chan_points` 几何 | 次图层，默认关闭（数量多、噪声大） |
| 策略日线条件 | `chan_strategy` 信号 | 竖线：日线一买 / 日线二买「生效日」 |
| 策略买卖点 | `chan_strategy.scan` | 三重共振触发 + 均线卖出 |
| 策略命中二买 | `chan_strategy` | 被策略真正用到的那个 30 分钟二买 bar |

======================================================================
三条必须在图上说清楚的口径（否则图会骗人）
======================================================================

1. **买卖点用「笔 + 中枢」的几何定义，不用 czsc 的 cxt_ 系列信号。**

   早期版本直接拿 `cxt_first_buy_V221126` 等信号做「关键字首次出现」
   来标买点，是错的。这些信号是**逐 bar 的择时状态**，不是买点事件：
   实测同一段下跌行情里「一买_9笔」/「其他」/「二卖」逐根 bar 交替，
   笔序号还在 9 → 5 → 11 之间跳（滚动窗口导致计数基准漂移）。按关键字
   计数得到一买 26 次，而几何口径只有 21 次 —— 多出来的是状态抖动。

   几何定义见 `core/chan_points.py`。卖点位置就是笔的端点（分型极值），
   所以图上的标记与笔的折线拐点严格重合。

2. **日线信号取「当日首根 bar 的值」，策略日线条件因此滞后 1 个交易日。**

   实测 418 个交易日中有 45 天，czsc 的日线二买会在**同一交易日内翻转**
   （当天日线 bar 未走完）。本图对日线信号按交易日取首根 bar 的值，
   它等价于「上一交易日收盘确认」的状态：实盘开盘即可读，无未来函数。

   注意这只影响**策略的日线条件**（竖线）；几何买卖点不受此限制。

3. **前 `init_n` 根（默认 500）是策略信号的预热区间。**

   只有 czsc 信号（策略用）需要预热；几何买卖点由全量笔/中枢算出，
   在全图上都有。图上用浅灰底纹标出预热区间。

======================================================================
性能（"拖着拖着就卡住"的处理）
======================================================================

- 渲染器开 `useDirtyRect`（脏矩形局部重绘，缩放/拖动只重画变化区域）
- 中枢、竖线、预热底纹改用 `markArea` / `markLine`（`silent: true`）
  挂在 K 线系列上。原实现用 `custom` 系列 + `renderItem`，dataZoom
  每帧都会重跑所有 renderItem，是卡顿主因
- K 线数据用紧凑数组（`[o,c,l,h,v,ma5,ma10]`）而非对象，JSON 体积与
  解析开销都明显下降
- 关掉 xAxis 的 splitLine、axisPointer 由 cross 改 line

======================================================================
用法
======================================================================

    python -m core.chan_viz sh688981
    python -m core.chan_viz 600519 --period 30 --out outputs/chan.html

或走根目录的 `visualize_chan.py` 包装脚本。需要 akshare（取行情）
与 czsc（算缠论），二者见 requirements.txt。
"""

from __future__ import annotations

import argparse
import json
import sys
from bisect import bisect_left
from pathlib import Path
from typing import Iterable, Optional, Sequence

import pandas as pd

from core import chan as chan_mod
from core import chan_points
from core.chan_strategy import DEFAULT_INIT_N, scan as scan_strategy
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
    "warmup": "rgba(0,0,0,0.04)",
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
    """30 分钟 → 日线（open/high/low/close/vol）"""
    d = (df.set_index("dt")
           .resample("1D")
           .agg(open=("open", "first"), high=("high", "max"),
                low=("low", "min"), close=("close", "last"),
                vol=("vol", "sum"))
           .dropna()
           .reset_index())
    return d


def _to_klines(df: pd.DataFrame, period: str, code: str) -> list[KLineData]:
    return [KLineData(code=code or "UNKNOWN", date=str(r.dt), open=float(r.open),
                      high=float(r.high), low=float(r.low), close=float(r.close),
                      volume=int(r.vol), period=period)
            for r in df.itertuples()]


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
    init_n: int = DEFAULT_INIT_N,
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
    for p in chan_points.buy_sell_points(bis_raw, centers_raw):
        i = _nearest_idx(p["dt"], dt_list, dt_keys)
        pts_m30.append({"kind": p["kind"], "idx": i, "price": p["price"]})

    # ---- 2. 日线缠论结构 + 几何买卖点（映射回 30 分钟下标） ----
    daily_df = _resample_daily(df)
    pts_daily: list[dict] = []
    if len(daily_df) >= 3:
        cr_d = chan_mod.build(_to_klines(daily_df, "daily", code), period="daily")
        if cr_d:
            for p in chan_points.buy_sell_points(chan_mod.bis(cr_d),
                                                 chan_mod.centers(cr_d)):
                side = "buy" if p["kind"].endswith("买") else "sell"
                i = _daily_point_to_bar(p["dt"], p["price"], side,
                                        day_range, lo, h)
                if i >= 0:
                    pts_daily.append({"kind": p["kind"], "idx": i,
                                      "price": p["price"]})
    logger.info(f"{code}: 几何买卖点 日线 {len(pts_daily)} 个 / "
                f"30 分钟 {len(pts_m30)} 个（笔 {len(bis_raw)}、中枢 {len(centers_raw)}）")

    def pack(points: list[dict]) -> dict:
        g: dict[str, list] = {k: [] for k in chan_points.KINDS}
        for p in points:
            g[p["kind"]].append([p["idx"], round(float(p["price"]), 3)])
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
    strat = scan_strategy(klines, code=code, init_n=init_n,
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
    # 有成交：框住两笔交易的前后各留 80 / 120 根，聚焦到策略本身。
    # 视窗别开太大（旧版 740 根）—— 一是 K 线太密看不清，二是拖动时
    # 一屏变化幅度小、手感发涩。其余区间用拖动 / 滚轮看。
    if trades:
        first = min(t["buy"]["idx"] for t in trades)
        idxs = [t["buy"]["idx"] for t in trades] + \
               [t["sell"]["idx"] for t in trades
                if t["sell"] and t["sell"]["idx"] >= 0]
        last = max(idxs) if idxs else first
        start = max(0, first - 80)
        end = min(n - 1, last + 120)
        if end <= start:
            end = min(n - 1, start + 200)
    else:
        start = max(0, int(n * 0.72))
        end = n - 1

    packed_daily = pack(pts_daily)
    packed_m30 = pack(pts_m30)
    counts = {
        "中枢": len(centers), "笔": len(bis_raw),
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
            "init_n": init_n,
            "warmup": min(init_n, n),
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
        "warmup": min(init_n, n),
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


_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>__TITLE__</title>
<style>
  html,body{margin:0;padding:0;background:#f4f5f7;color:#222;
    font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;}
  #wrap{max-width:1720px;margin:0 auto;padding:16px 20px 32px;}
  h1{font-size:19px;margin:0 0 8px;font-weight:600;}
  .meta{font-size:12px;color:#555;line-height:1.9;margin-bottom:10px;}
  .meta b{color:#111;font-weight:600;}
  .meta .box{display:inline-block;background:#fff;border:1px solid #e3e6ea;
    border-radius:4px;padding:2px 8px;margin:0 6px 6px 0;}
  .note{font-size:12px;color:#555;background:#fff;border:1px solid #e3e6ea;
    border-left:3px solid #ff9800;border-radius:6px;padding:10px 14px;
    margin-bottom:12px;line-height:1.85;}
  .note ol{margin:6px 0 0 18px;padding:0;}
  #chart{width:100%;height:940px;background:#fff;border:1px solid #e3e6ea;
    border-radius:6px;contain:layout paint size;
    user-select:none;-webkit-user-select:none;touch-action:none;}
</style>
</head>
<body>
<div id="wrap">
  <h1>__TITLE__</h1>
  <div class="meta">__META__</div>
  <div class="note">__NOTE__</div>
  <div id="chart"></div>
</div>
<script>__ECHARTS__</script>
<script>
var CFG = __PAYLOAD__;
var ST = CFG.style, dates = CFG.dates, bars = CFG.bars;

// 紧凑数组列序
var O = 0, C = 1, L = 2, H = 3, V = 4, MA5 = 5, MA10 = 6;

function scatter(name, pts, color, size, rotate, offset, z) {
  return {
    name: name, type: 'scatter', xAxisIndex: 0, yAxisIndex: 0,
    data: pts, symbol: 'triangle', symbolSize: size || 11,
    symbolRotate: rotate || 0, symbolOffset: offset || [0, 0],
    itemStyle: {color: color, borderColor: '#fff', borderWidth: 0.8},
    z: z || 7, animation: false, emphasis: {scale: 1.6}
  };
}

// ---- 各 bar 上的标记（tooltip 用），从 points 反查 ----
var markAt = {};
['daily', 'm30'].forEach(function (lv) {
  var g = CFG.points[lv] || {};
  Object.keys(g).forEach(function (k) {
    g[k].forEach(function (p) {
      var i = p[0];
      (markAt[i] = markAt[i] || []).push((lv === 'daily' ? '日线' : '30分') + k);
    });
  });
});

// ---- 日线买卖点：主图层（大标记） ----
var dailyBuy = [], dailySell = [];
POINT_KEYS.forEach(function (k) {
  var pts = (CFG.points.daily || {})[k] || [];
  var isBuy = k.indexOf('买') >= 0;
  if (!pts.length) { return; }
  (isBuy ? dailyBuy : dailySell).push([k, pts]);
});

// ---- 区域标注：预热底纹 + 中枢（都用 markArea，silent 不参与 hover） ----
// 注意 xAxis 用类目值（日期字符串）而不是下标数字：category 轴上类目值
// 是确定可匹配的，数字下标在不同 ECharts 版本里解释不一致。
var areaData = CFG.centers.map(function (z) {
  return [{xAxis: dates[z[0]], yAxis: z[2]},
          {xAxis: dates[z[1]], yAxis: z[3]}];
});
if (CFG.warmup > 1) {
  areaData.unshift([
    {xAxis: dates[0], yAxis: CFG.price_min,
     itemStyle: {color: ST.warmup, borderWidth: 0}},
    {xAxis: dates[CFG.warmup - 1], yAxis: CFG.price_max}
  ]);
}

// ---- 策略日线条件「生效日」竖线（markLine，silent） ----
var lineData = CFG.strategy.d1_days.map(function (i) {
  return {xAxis: dates[i], lineStyle: {color: ST.d1_line, width: 2, opacity: 0.5}};
}).concat(CFG.strategy.d2_days.map(function (i) {
  return {xAxis: dates[i], lineStyle: {color: ST.d2_line, width: 2, opacity: 0.5}};
}));

var series = [
  {
    name: 'K线', type: 'candlestick', xAxisIndex: 0, yAxisIndex: 0, z: 5,
    data: bars.map(function (b) { return [b[O], b[C], b[L], b[H]]; }),
    itemStyle: {color: ST.up, color0: ST.down,
      borderColor: ST.up, borderColor0: ST.down},
    // 中枢改用 markArea 挂在 K 线上（原实现用 custom 系列 + renderItem，
    // dataZoom 每帧重跑 renderItem，是拖动卡顿的主因）
    markArea: areaData.length ? {
      silent: true, animation: false,
      itemStyle: {color: ST.center_fill, borderColor: ST.center_line,
                  borderWidth: 1, borderType: [4, 3], opacity: 0.9},
      data: areaData
    } : undefined,
    markLine: lineData.length ? {
      silent: true, symbol: 'none', animation: false, label: {show: false},
      data: lineData
    } : undefined
  },
  {
    name: '笔', type: 'line', xAxisIndex: 0, yAxisIndex: 0, z: 2,
    data: CFG.bi_points, showSymbol: false, symbol: 'none',
    lineStyle: {color: ST.bi, width: 1.2},
    itemStyle: {color: ST.bi}, smooth: false, animation: false
  },
  {
    name: '日线MA5', type: 'line', xAxisIndex: 0, yAxisIndex: 0, z: 4,
    data: bars.map(function (b, i) { return [i, b[MA5]]; }),
    showSymbol: false, symbol: 'none', connectNulls: false, animation: false,
    lineStyle: {color: ST.ma_short, width: 1.4}, itemStyle: {color: ST.ma_short}
  },
  {
    name: '日线MA10', type: 'line', xAxisIndex: 0, yAxisIndex: 0, z: 4,
    data: bars.map(function (b, i) { return [i, b[MA10]]; }),
    showSymbol: false, symbol: 'none', connectNulls: false, animation: false,
    lineStyle: {color: ST.ma_long, width: 1.4}, itemStyle: {color: ST.ma_long}
  },
  {
    name: '顶分型', type: 'scatter', xAxisIndex: 0, yAxisIndex: 0, z: 6,
    data: CFG.fractals.top, symbol: 'circle', symbolSize: 5.5, animation: false,
    itemStyle: {color: ST.fx_top, borderColor: '#fff', borderWidth: 0.5}
  },
  {
    name: '底分型', type: 'scatter', xAxisIndex: 0, yAxisIndex: 0, z: 6,
    data: CFG.fractals.bottom, symbol: 'circle', symbolSize: 5.5, animation: false,
    itemStyle: {color: ST.fx_bottom, borderColor: '#fff', borderWidth: 0.5}
  }
];

// 日线买卖点：每类一个系列，买点在 low 下方、卖点在 high 上方
dailyBuy.forEach(function (item) {
  var k = item[0];
  series.push(scatter('日线' + k, item[1], ST.daily[k], 15, 0, [0, 10], 9));
});
dailySell.forEach(function (item) {
  var k = item[0];
  series.push(scatter('日线' + k, item[1], ST.daily[k], 15, 180, [0, -10], 9));
});

// 30 分钟买卖点：次图层，默认关闭
POINT_KEYS.forEach(function (k) {
  var pts = (CFG.points.m30 || {})[k] || [];
  if (!pts.length) { return; }
  var isBuy = k.indexOf('买') >= 0;
  series.push(scatter('30分' + k, pts, ST.m30[k], 9,
    isBuy ? 0 : 180, isBuy ? [0, 7] : [0, -7], 8));
});

// 策略真正命中的 30 分钟二买 bar：蓝框高亮（把「策略为什么在这里买」钉出来）
if (CFG.strategy.m30_hits.length) {
  series.push({
    name: '策略命中二买', type: 'scatter', xAxisIndex: 0, yAxisIndex: 0, z: 15,
    data: CFG.strategy.m30_hits.map(function (i) {
      return [i, bars[i] ? bars[i][L] : 0];
    }),
    symbol: 'rect', symbolSize: [16, 34], symbolOffset: [0, 24],
    itemStyle: {color: 'transparent', borderColor: ST.strategy_hit,
                borderWidth: 2, borderType: [3, 2]},
    animation: false, silent: true
  });
}

// 策略买卖点：大号圆点 + 白字「买 / 卖」
function stratMarker(name, data, color, dy, text) {
  return {
    name: name, type: 'scatter', xAxisIndex: 0, yAxisIndex: 0, z: 20,
    data: data, symbol: 'circle', symbolSize: 32, symbolOffset: [0, dy],
    itemStyle: {color: color, borderColor: '#fff', borderWidth: 1.6,
                shadowBlur: 4, shadowColor: 'rgba(0,0,0,0.25)'},
    label: {show: true, position: 'inside', color: '#fff', fontSize: 14,
            fontWeight: 'bold', formatter: text},
    animation: false, emphasis: {scale: 1.15}
  };
}
var sBuy = [], sSell = [];
CFG.strategy.trades.forEach(function (t) {
  sBuy.push([t.buy.idx, t.buy.price]);
  if (t.sell && t.sell.idx >= 0) { sSell.push([t.sell.idx, t.sell.price]); }
});
if (sBuy.length) {
  series.push(stratMarker('策略买点', sBuy, ST.strategy_buy, 30, '买'));
}
if (sSell.length) {
  series.push(stratMarker('策略卖点', sSell, ST.strategy_sell, -30, '卖'));
}

// 成交量（副图）
series.push({
  name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, z: 2,
  data: bars.map(function (b, i) {
    return {value: [i, b[V] / 100],
            itemStyle: {color: b[C] >= b[O] ? ST.up : ST.down, opacity: 0.75}};
  }),
  barWidth: '60%', animation: false
});

var counts = CFG.meta.counts;

// 图例顺序 = 判读优先级。条目近 20 个、一行放不下，ECharts 会折行；
// 若按 series 原顺序排，「策略买点 / 策略卖点」会被挤到末尾一行，
// 而它们恰恰是这张图最需要看的两个点，所以显式排序提到最前。
var LEGEND_FIRST = ['策略买点', '策略卖点', '策略命中二买', '成交点',
                    '日线一买', '日线二买', '日线三买',
                    '日线一卖', '日线二卖', '日线三卖'];
var legendData = series.map(function (s) { return s.name; });
legendData.sort(function (a, b) {
  var ia = LEGEND_FIRST.indexOf(a), ib = LEGEND_FIRST.indexOf(b);
  if (ia < 0) { ia = LEGEND_FIRST.length; }
  if (ib < 0) { ib = LEGEND_FIRST.length; }
  return ia - ib;
});

// 默认只开日线级别；30 分钟级别与分型收起（数量多，会盖住 K 线）
var legendSelected = {'顶分型': false, '底分型': false};
POINT_KEYS.forEach(function (k) { legendSelected['30分' + k] = false; });

var option = {
  animation: false,
  backgroundColor: '#ffffff',
  title: {
    text: counts['策略买点'] > 0
      ? '三重共振买点 ' + counts['策略买点'] + ' 次（默认视窗已定位到首笔买点附近）'
      : '本区间未出现三重共振买点（默认视窗为最近一段）',
    left: 'center', top: 6,
    textStyle: {fontSize: 13, fontWeight: 'normal', color: '#555'}
  },
  legend: {
    // 不用 type:'scroll' —— 它会把放不下的条目折进「1/2」第二页，
    // 点一次才看得到；改成限定宽度让它自动换行，全部条目常驻可见。
    top: 26, left: 'center', width: '97%', data: legendData,
    textStyle: {color: '#333', fontSize: 11}, itemWidth: 14, itemHeight: 9,
    itemGap: 8, selected: legendSelected
  },
  tooltip: {
    trigger: 'axis', transitionDuration: 0,
    axisPointer: {type: 'line', lineStyle: {color: '#aaa'}},
    backgroundColor: 'rgba(255,255,255,0.97)', borderColor: '#d0d4d9',
    textStyle: {color: '#222', fontSize: 12},
    formatter: function (ps) {
      var idx = ps[0].dataIndex, b = bars[idx];
      if (!b) { return ''; }
      var s = '<div style="font-size:12px;line-height:1.7">';
      s += '<b>' + dates[idx] + '</b><br/>';
      s += '开 ' + b[O].toFixed(2) + '&nbsp;&nbsp;高 ' + b[H].toFixed(2) + '<br/>';
      s += '低 ' + b[L].toFixed(2) + '&nbsp;&nbsp;收 ' +
           '<b style="color:' + (b[C] >= b[O] ? ST.up : ST.down) + '">' +
           b[C].toFixed(2) + '</b><br/>';
      s += '量 ' + (b[V] / 100).toFixed(0) + ' 手<br/>';
      if (b[MA5]) {
        s += '<span style="color:' + ST.ma_short + '">日线MA5 ' +
             b[MA5].toFixed(2) + '</span>&nbsp;&nbsp;';
        s += '<span style="color:' + ST.ma_long + '">MA10 ' +
             b[MA10].toFixed(2) + '</span><br/>';
      }
      var mk = markAt[idx];
      if (mk && mk.length) {
        s += '<span style="color:#c62828">缠论买卖点：' + mk.join('、') +
             '</span><br/>';
      }
      CFG.strategy.trades.forEach(function (x) {
        if (x.buy.idx === idx) {
          s += '<div style="margin-top:4px;padding-top:4px;' +
               'border-top:1px dashed #ccc">' +
               '<b style="color:' + ST.strategy_buy + '">策略买点</b> @' +
               x.buy.price + '<br/>日线一买 ' + x.buy.d1 + ' → 日线二买 ' +
               x.buy.d2 + '（间隔 ' + x.buy.gap_1to2 + ' 日）<br/>' +
               '→ 30 分钟二买 ' + x.buy.gap_2tom + ' 日内确认，' +
               '信号 bar ' + x.buy.signal_dt + '</div>';
        }
        if (x.sell && x.sell.idx === idx) {
          s += '<div style="margin-top:4px;padding-top:4px;' +
               'border-top:1px dashed #ccc">' +
               '<b style="color:' + ST.strategy_sell + '">策略卖点</b> @' +
               x.sell.price + '<br/>' + x.sell.ma + ' 被跌破（确认日 ' +
               x.sell.confirm_dt + '），持有 ' + x.sell.hold_days +
               ' 交易日</div>';
        }
      });
      s += '</div>';
      return s;
    }
  },
  grid: [
    {left: 64, right: 26, top: 82, height: 610},
    {left: 64, right: 26, top: 720, height: 150}
  ],
  xAxis: [
    {type: 'category', gridIndex: 0, data: dates, boundaryGap: true,
     axisLine: {lineStyle: {color: '#bbb'}}, axisTick: {show: false},
     axisLabel: {show: false}, splitLine: {show: false},
     axisPointer: {label: {show: false}}},
    {type: 'category', gridIndex: 1, data: dates, boundaryGap: true,
     axisLine: {lineStyle: {color: '#bbb'}}, axisTick: {show: false},
     axisLabel: {color: '#666', fontSize: 10, formatter: function (v) {
       return v.slice(0, 10); }},
     splitLine: {show: false}}
  ],
  yAxis: [
    {scale: true, gridIndex: 0, splitLine: {lineStyle: {color: '#f2f3f5'}},
     axisLine: {lineStyle: {color: '#bbb'}}, axisTick: {show: false},
     axisLabel: {color: '#555', fontSize: 11},
     splitArea: {show: false}},
    {gridIndex: 1, splitLine: {show: false},
     axisLine: {lineStyle: {color: '#bbb'}}, axisTick: {show: false},
     axisLabel: {color: '#888', fontSize: 10,
       formatter: function (v) { return (v / 10000).toFixed(0) + '万手'; }}}
  ],
  dataZoom: [
    {type: 'inside', xAxisIndex: [0, 1],
     startValue: CFG.zoom.startValue, endValue: CFG.zoom.endValue,
     zoomOnMouseWheel: true, moveOnMouseMove: true},
    {type: 'slider', xAxisIndex: [0, 1], bottom: 14, height: 30,
     startValue: CFG.zoom.startValue, endValue: CFG.zoom.endValue,
     borderColor: '#ddd', fillerColor: 'rgba(100,150,220,0.15)',
     handleStyle: {color: '#78909c'},
     labelFormatter: function (v) { return dates[v] ? dates[v].slice(0, 10) : ''; }}
  ],
  series: series
};

// 脏矩形渲染：拖动 / 缩放时只重画变化的区域（ECharts 5 特性）。
// 与 markArea / markLine 组合已实测截图无副作用；不要与 progressive
// 同时用 —— 分帧渲染的后续帧会被脏矩形判定为「没变化」而跳过。
var chart = echarts.init(document.getElementById('chart'), null,
                         {renderer: 'canvas', useDirtyRect: true});
chart.setOption(option);
window.addEventListener('resize', function () { chart.resize(); });
</script>
</body>
</html>
"""


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
        ("中枢", counts.get("中枢", 0)),
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
        f'<span class="box">{k}：<b>{v}</b></span>' for k, v in chips)
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
                      '</span>').format(
            s["已平仓"], s["胜率%"], s["平均收益%"], s["平均持有交易日"],
            conc_color, conc, ov)

    note = (
        "<b>怎么读这张图</b>"
        "<ol>"
        "<li><b>买卖点用「笔 + 中枢」的几何定义</b>，位置就是笔的端点"
        "（所以标记与折线拐点重合）。"
        "<b>不用</b> czsc 的 <code>cxt_*</code> 信号 —— 那些是逐 bar 的"
        "择时状态，会在同段行情里反复闪断（实测「一买」和「二卖」逐根 bar "
        "交替、笔序号在 9→5→11 间跳），按关键字计数会把状态抖动数成买点。</li>"
        "<li><b>日线级别是主图层</b>（大三角），<b>30 分钟级别默认关闭</b>"
        "（图例点开）。同一段行情里日线的买卖点比 30 分钟少一个数量级 —— "
        "「买卖点太多」多半是级别选小了。</li>"
        "<li><b>紫色/橙色竖线</b>是策略用的日线条件「生效日」，取"
        "「当日首根 bar 上的日线值」，等价于上一交易日收盘确认的状态。"
        "实测日线信号在日内会翻转（418 个交易日中 45 天），所以不能取"
        "当天任意时点的值。这个取法实盘开盘即可读，<b>无未来函数</b>，"
        "代价是滞后 1 个交易日。</li>"
        "<li><b>蓝色虚线方框</b>是策略真正命中的那个 30 分钟二买 bar；"
        "<b>策略买点</b>＝日线一买 → 15 个交易日内日线二买 → 2 个交易日内 "
        "该二买 bar 收盘确认，成交顺延 1 根 bar；<b>策略卖点</b>＝收盘破"
        "日线 MA5 后改用 MA10、两条都被跌破后卖出，成交顺延 1 个交易日。</li>"
        "<li><b>每笔买点独立成一笔交易，允许重叠持仓</b>（前一笔没平仓，"
        "新买点照开）。所以上方的 胜率 / 平均收益 是<b>按笔统计</b>，"
        "<b>不是资金曲线</b> —— 同一时刻多笔持仓时，这些收益不能相加成"
        "组合收益。顶部「最大同时持仓」标出实际重叠了几笔，大于 1 时标红。"
        "这是为了先把「信号本身对不对」验清楚，资金约束留到组合回测再接。</li>"
        f"<li>左侧浅灰底纹是 <b>czsc 预热区间（前 {payload['warmup']} 根）</b>："
        "只有策略用的信号需要预热，几何买卖点由全量笔/中枢算出，不受影响。</li>"
        "</ol>"
        "<div style='margin-top:6px;color:#888'>"
        "行情：新浪财经前复权分钟 K 线；缠论：czsc 1.0.1 "
        "（中枢 / 笔 / 分型直接取自 zs_list / bi_list / fx_list）。</div>"
    )

    html = (_HTML
            .replace("__TITLE__", title)
            .replace("__META__", meta_html)
            .replace("__NOTE__", note)
            .replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
            .replace("__ECHARTS__", _echarts_source(echarts_path))
            .replace("POINT_KEYS", json.dumps(POINT_ORDER)))
    return html


def save_html(html: str, out_path) -> Path:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path


def generate(code: str, out_path=None, *, period: str = "30min",
             init_n: int = DEFAULT_INIT_N, ma_short: int = 5,
             ma_long: int = 10, echarts_path: Optional[str] = None,
             name: Optional[str] = None,
             klines: Optional[Sequence[KLineData]] = None) -> Path:
    """取行情 → 算缠论与策略 → 出 HTML，返回文件路径"""
    if klines is None:
        klines = fetch_klines(code, period=period)
    if not name:
        name = stock_name(code)
    payload = build_payload(klines, code=code, period=period, name=name,
                            init_n=init_n, ma_short=ma_short, ma_long=ma_long)
    html = render_html(payload, echarts_path=echarts_path)
    if out_path is None:
        out_path = Path("outputs") / f"chan_{payload['meta']['code']}_{period}.html"
    path = save_html(html, out_path)
    logger.info(f"已生成 {path}（{payload['meta']['bars']} 根 K 线，"
                f"中枢 {len(payload['centers'])} 个，"
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
    ap.add_argument("--init-n", type=int, default=DEFAULT_INIT_N,
                    help=f"czsc 预热根数（默认 {DEFAULT_INIT_N}）")
    ap.add_argument("--ma-short", type=int, default=5)
    ap.add_argument("--ma-long", type=int, default=10)
    args = ap.parse_args(argv)

    path = generate(args.code, args.out, period=args.period,
                    init_n=args.init_n, ma_short=args.ma_short,
                    ma_long=args.ma_long, name=args.name)
    print(path)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
