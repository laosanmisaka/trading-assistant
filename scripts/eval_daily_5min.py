# -*- coding: utf-8 -*-
"""日线 + 5min 两级联立：日线锁中枢与突破，5min 在回调窗口内找买点

    python scripts/eval_daily_5min.py --codes sh601899 --holds 5,10,20
    python scripts/eval_daily_5min.py --pool scripts/pool_liquid50.txt --out outputs/daily5min.md
    python scripts/eval_daily_5min.py --min-breakout 10 --out outputs/daily5min_f10.md
    python scripts/eval_daily_5min.py --codes sh601899 --dump-points outputs/_pts.csv

为什么是日线 + 5min（而不是 30min + 5min）
------------------------------------------
czsc 判笔所需的 bar 数与级别**无关**（实测：日线 12.4~14.0 根/笔、30min 13.8~15.2、
5min 14.4~15.8）。所以「本级别一笔在次级别上有几根笔」≈ **级别比**：

| 配对 | 级别比 | 回调窗口内次级别结构 | 实测覆盖窗口内出买点的比例 |
| --- | --- | --- | --- |
| 30min → 5min | 6x（回调笔偏短，只剩 3） | 3 笔 / 0 中枢 | 18% |
| 日线 → 30min | 8x（只剩 5.3） | 5 笔 | 13.5% |
| **日线 → 5min** | **42x（窗口内 30 根笔）** | **结构充足** | **71%** |

**级别差一级不够**（回调笔长度只有平均笔的一半），必须差两级。日线一笔 ≈ 14 个
交易日 ≈ 42 根 5min 笔 ⇒ 5min 在这个窗口里能走出完整的走势类型，判定才有依托。

两种"提前量"口径（都算，别混）
------------------------------
- **相对日线确认时刻**：日线三买点 = 回抽笔终点，要等**下一根笔**走完才锁定
  （实测滞后中位 13 自然日 / 约 9 交易日）。5min 的确认滞后只有几十分钟。
  这才是「比按缠论标准做法真正能成交的时刻早多少」——**可交易口径**。
- **相对日线买点本身**：相对"精确抓到那个低点"，速度优势更小。

突破幅度过滤（``--min-breakout``）
----------------------------------
`eval_triple_prob.py` 的反向统计给出了唯一强区分特征：**贴着中枢上沿突破**
（幅度 <10%）成三买只有 78.8%，**>10% 就 98.7%+**。这个量在**突破那一刻**就能算
（中枢已收口、zg 固定），所以可以直接当实盘闸门用：

    日线向上突破中枢上沿 → 幅度 ≥ N% 才继续 → 5min 回调窗口内找买点

⚠️ 幅度 = 突破笔高点 ÷ **判定时那个中枢**的 zg − 1。参照中枢取自
`chan_points.buy_sell_points()` 随三买点带出的 ``zg`` —— 不能从返回结果反推，
因为同一根回抽笔可能同时是多个中枢的三买，每个的 zg 不同。

收益口径
--------
- **5min 侧入场 = 确认那根 bar 的收盘价（确认即买）**。5min 的"确认" = 买点所在笔
  之后第一根反向笔走完，其终点 ``edt`` 恰好是某根 5min bar 的收盘时刻
  （baostock 的 ``time`` 标记 bar 结束时刻），此刻 ``close`` 已经知道 ⇒ 当根收盘
  成交，**不额外等下一根 bar**。跨 14:55 的确认若等下一根就是次一交易日 09:35，
  白等一整晚。
- **日线侧入场 = 确认后次一交易日的开盘价**。日线确认发生在当日收盘之后，
  物理上没有"当根收盘价"可成交，只能次日开盘 —— 这不是保守，是制度。
- 出场：``--holds`` 给若干个**交易日**的固定持有期，各自独立统计，统一在第 N 根
  （5min 侧 N×48）bar 的**收盘**出。
- 基准：同池同级别、**与信号口径同构**的随机入场（日线 open 进、5min close 进，
  均 close 出），取全体 bar 的均值。基准与信号不同构，差出来的就是隔夜跳空，
  会被误读成择时能力。
- ⚠️ 5min 侧按收盘价成交是**乐观口径**：假设确认瞬间能拿到当根收盘价，未计滑点。

三档 5min 侧口径
----------------
``--min-mode``：
- ``any``：一买 + 二买（最宽，信号最多）
- ``one``：只一买
- ``one_div``：一买 **且带背驰**（``require_divergence=True``，需传 bars）

回撤拆解与统计功效（报告 §5~§7）
--------------------------------
- **MAE / MFE**：入场后持有窗内的最大不利/有利偏移（各自级别的 low/high）。
  5min 侧是收盘价入场 ⇒ 入场那根 bar 已经走完，窗口**自下一根起算**；日线侧是
  开盘价入场 ⇒ 入场那根 bar 的极值也在窗口内。价优（提前进场的价格优势）到
  日线确认时刻还剩多少，直接回答「优势是不是被入场后的回撤吃掉了」。
- **止损档**：−3% / −5% / −8% 触价止损（跳空低开按开盘价成交），与无止损并列。
- **统计功效**：点之间**不独立**（同一标的、持有期重叠），所以超额 +1.44% 这类
  数字必须配
  ① 按标的 **cluster bootstrap** 的 95% 区间；
  ② **贪心去重叠**后的独立样本数（同标的持有窗不重叠才保留）；
  ③ 剔除贡献最大的 5 个点 / 最好的 1 只标的后重算；
  ④ **按标的等权**（现报告是样本加权，数据长的标的权重被放大）。

⚠️ 统计口径
------------
- 按**点**统计，不是资金曲线。同一标的的候选点密集、持有期重叠，收益不可相加。
- 5min 的历史下限是 **2020-01-02**（baostock 实测），日线要**多取前置**给结构
  warm-up，否则窗口开头的日线笔是残缺的。
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
DEFAULT_POOL = ROOT / "scripts" / "pool_liquid50.txt"

MIN5_BARS_PER_DAY = 48            # A 股一天 240 分钟 → 48 根 5min
BUY_KINDS = ("一买", "二买")
STOP_LEVELS = (3.0, 5.0, 8.0)     # 止损档（%）
BOOT_N = 1000                     # cluster bootstrap 次数
BOOT_SEED = 20260922              # 固定种子 ⇒ 报告可复现


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


def daily_confirm_time(bis: list[dict], bi_idx: int):
    """日线几何点的**可成交确认时刻** = 该笔之后第一根笔的终点

    ⚠️ 三买点 = 回抽笔的终点，笔要锁定必须等后续反向笔成型（KI-009）。
    所以 ``bi_idx + 1`` 的 ``edt`` 才是最早能确认的时刻。
    """
    if bi_idx + 1 < len(bis):
        return ts(bis[bi_idx + 1]["edt"])
    return None


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

def analyze_one(code: str, cache_dir: Path, *, holds: list[int],
                min_mode: str = "any", verbose: bool = False,
                diag: dict | None = None,
                min_breakout: float = 0.0) -> dict | None:
    """日线出三买 → 5min 在回调窗口内找买点 → 逐点明细 + 汇总

    ``diag`` 若传入（dict），返回 None 时写入 ``reason`` 说明原因 —— 报告必须
    如实列出「池子里哪几只没进统计、为什么」，不能静默少几只。
    """
    def note(msg: str) -> None:
        if diag is not None:
            diag["reason"] = msg

    kd = load_cached(code, "daily", cache_dir)
    k5 = load_cached(code, "5min", cache_dir)
    if not kd or not k5:
        note(f"缓存缺失（daily {'有' if kd else '无'} / 5min {'有' if k5 else '无'}）")
        return None

    rd = chan_mod.build(kd, "daily")
    if rd is None:
        note("日线 K 线不足，czsc 建不出结构")
        return None
    bis_d = chan_mod.bis(rd)
    zs_d = chan_mod.centers(rd)
    pts_all = [p for p in chan_points.buy_sell_points(bis_d, zs_d) if p["kind"] == "三买"]
    amps = {id(p): breakout_amp(bis_d, p) for p in pts_all}

    # ---- 突破幅度过滤（--min-breakout）：闸门在「突破那一刻」，无额外未来函数 ----
    pts_d, n_drop = [], 0
    for p in pts_all:
        a = amps[id(p)]
        if min_breakout > 0 and (a is None or a < min_breakout):
            n_drop += 1
            continue
        pts_d.append(p)

    r5 = chan_mod.build(k5, "5min")
    if r5 is None:
        note("5min K 线不足，czsc 建不出结构")
        return None
    bis_5 = chan_mod.bis(r5)
    zs_5 = chan_mod.centers(r5)
    need_div = min_mode == "one_div"
    pts_5 = chan_points.buy_sell_points(bis_5, zs_5,
                                        bars=k5 if need_div else None,
                                        require_divergence=need_div)

    dt5 = [ts(k.date) for k in k5]
    dtd = [ts(k.date) for k in kd]
    op5 = [float(k.open) for k in k5]
    hi5 = [float(k.high) for k in k5]
    lo5 = [float(k.low) for k in k5]
    cl5 = [float(k.close) for k in k5]
    opd = [float(k.open) for k in kd]
    hid = [float(k.high) for k in kd]
    lod = [float(k.low) for k in kd]
    cld = [float(k.close) for k in kd]
    idx5 = {id(b): i for i, b in enumerate(bis_5)}
    cov_lo = dt5[0]

    # ---- 5min 侧候选：按 min-mode 过滤 ----
    if min_mode in ("one", "one_div"):
        want = ("一买",)
    else:
        want = BUY_KINDS
    cand5 = []
    for q in pts_5:
        if q["kind"] not in want:
            continue
        i5 = idx5[id(bis_5[q["bi_idx"]])] if id(bis_5[q["bi_idx"]]) in idx5 else q["bi_idx"]
        tconf5 = daily_confirm_time(bis_5, q["bi_idx"])
        if tconf5 is None:
            continue
        # 5min 可成交：**确认那根 bar 的收盘价直接成交**
        #   确认时刻 = 反向笔终点 = 该 bar 的 dt（baostock 的 time 是 bar 结束时刻），
        #   此刻 close 已经知道 ⇒ 用当根收盘价成交，无未来函数。
        #   不是「等下一根 bar 开盘」—— 那会白白多等 5 分钟，跨 14:55 确认时
        #   甚至要等到次一交易日 09:35。
        entry5 = last_bar_at_or_before(dt5, tconf5)
        if entry5 is None:
            continue                            # 确认时刻早于 5min 覆盖起点
        cand5.append({"kind": q["kind"], "dt": ts(q["dt"]), "conf": tconf5,
                      "entry": entry5, "price": float(q["price"]),
                      "div": q.get("divergence")})
    cand5.sort(key=lambda x: x["dt"])

    # ---- 逐日线三买点 ----
    details = []
    _base5_cache: dict[int, dict | None] = {}   # 提前量 → 同池同窗基准（记忆化）
    for p in pts_d:
        i = p["bi_idx"]
        if i < 1:
            continue
        t_lo = ts(bis_d[i - 1]["edt"])          # 回调窗口起（= 突破笔终点）
        t_pt = ts(p["dt"])                      # 三买点（回调低点）
        t_dc = daily_confirm_time(bis_d, i)     # 日线可成交确认时刻
        if t_dc is None or t_pt < cov_lo:
            continue                            # 5min 未覆盖 / 无法确认

        entry_d = first_bar_after(dtd, t_dc)
        # 窗口内 5min 候选（按 5min 的**确认时刻**落在 [窗口起, 日线确认时刻) 内）
        inwin = [c for c in cand5 if t_lo <= c["conf"] < t_dc]

        rec = {
            "代码": code, "窗口起": t_lo, "三买点": t_pt, "日线确认": t_dc,
            "突破幅度%": amps.get(id(p)), "5min候选": len(inwin),
            "最早买点": inwin[0]["dt"] if inwin else None,
            "最早类型": inwin[0]["kind"] if inwin else None,
        }
        # 提前量：日线确认 index(日线) vs 5min 最早候选 index(5min)
        e5 = inwin[0]["entry"] if inwin else None
        px5 = cl5[e5] if e5 is not None else None              # 5min 入场价 = 确认 bar 收盘
        px_d = opd[entry_d] if entry_d is not None else None   # 日线入场价 = 次日开盘
        rec["_entry5"], rec["_entry5px"] = e5, px5
        rec["_entry_d"], rec["_entry_dpx"] = entry_d, px_d
        if inwin:
            j_dc = last_bar_at_or_before(dt5, t_dc)
            rec["提前_5mbar"] = None if (e5 is None or j_dc is None) else (j_dc - e5)
            rec["提前_日"] = (rec["提前_5mbar"] / MIN5_BARS_PER_DAY
                              if rec["提前_5mbar"] is not None else None)
            if px_d is not None and px5 is not None:
                rec["价优%"] = (px_d / px5 - 1.0) * 100.0
                # 价优的同池同期对照：光是"早持有这么多根 bar"能拿到多少（= beta 部分）
                # 提前_5mbar 就是这段时间的 5min bar 数，拿它当持有期算同池随机基准
                lead_bars = rec["提前_5mbar"]
                if lead_bars and lead_bars > 0:
                    b = _base5_cache.get(lead_bars)
                    if b is None:
                        b = baseline_ret(cl5, cl5, lead_bars)
                        _base5_cache[lead_bars] = b
                    rec["同期基准%"] = b["median"] if b else None
                else:
                    rec["同期基准%"] = None
            else:
                rec["价优%"] = None
                rec["同期基准%"] = None
        else:
            rec["提前_5mbar"] = None
            rec["提前_日"] = None
            rec["价优%"] = None
            rec["同期基准%"] = None

        # 收益：日线确认口径（**次日开盘**进）/ 5min 确认口径（**确认 bar 收盘**进）
        for h in holds:
            rec[f"D{h}"] = hold_ret(cld, entry_d, px_d, h)
            ex_d = excursion(hid, lod, entry_d, px_d, h)
            rec[f"DMAE{h}"], rec[f"DMFE{h}"] = ex_d if ex_d else (None, None)
            for s in STOP_LEVELS:
                rec[f"DSL{h}_{int(s)}"] = stop_ret(opd, lod, cld, entry_d, px_d, h,
                                                   -abs(s))
            rec[f"M{h}"] = hold_ret(cl5, e5, px5, h * MIN5_BARS_PER_DAY)
            ex = excursion(hi5, lo5, e5, px5, h * MIN5_BARS_PER_DAY,
                           incl_entry_bar=False)
            rec[f"MAE{h}"], rec[f"MFE{h}"] = ex if ex else (None, None)
            for s in STOP_LEVELS:
                rec[f"SL{h}_{int(s)}"] = stop_ret(op5, lo5, cl5, e5, px5,
                                                  h * MIN5_BARS_PER_DAY, -abs(s),
                                                  incl_entry_bar=False)
        details.append(rec)
        if verbose:
            print(f"  窗口 {str(t_lo)[:10]} → 三买 {str(t_pt)[:10]} | 日线确认 "
                  f"{str(t_dc)[:10]} | 幅度 {rec['突破幅度%'] if rec['突破幅度%'] is None else round(rec['突破幅度%'], 1)}% | "
                  f"5min候选 {len(inwin)} | "
                  f"提前 {rec['提前_日'] if rec['提前_日'] is not None else '--'} 日",
                  flush=True)

    if not details:
        if not pts_all:
            note("日线全期未产出三买点")
        elif n_drop and not pts_d:
            note(f"日线三买 {len(pts_all)} 个，全部被突破幅度过滤（<{min_breakout}%）剔除")
        else:
            note(f"日线三买 {len(pts_d)} 个（另有 {n_drop} 个被幅度过滤），"
                 f"全部早于 5min 覆盖起点 {str(cov_lo)[:10]} 或落在末尾无法确认 ⇒ 无可用样本")
        return None

    # ---- 同池随机入场基准（两个级别各配**与自己同构**的口径） ----
    #      日线：每根 bar 开盘进、持有 N 日收盘出
    #      5min：每根 bar **收盘**进、持有 N 日收盘出（与「确认即买」同构）
    base: dict[str, dict[int, dict]] = {"日线": {}, "5min": {}}
    for h in holds:
        bd = baseline_ret(opd, cld, h)
        if bd is not None:
            base["日线"][h] = bd
        b5 = baseline_ret(cl5, cl5, h * MIN5_BARS_PER_DAY)
        if b5 is not None:
            base["5min"][h] = b5

    # ---- 汇总 ----
    summ = {}
    for h in holds:
        d_rets = [r[f"D{h}"] for r in details if r.get(f"D{h}") is not None]
        m_rets = [r[f"M{h}"] for r in details if r.get(f"M{h}") is not None]
        summ[h] = {"日线": summarize(d_rets), "5min": summarize(m_rets)}
    leads = [r["提前_日"] for r in details if r.get("提前_日") is not None]
    advs = [r["价优%"] for r in details if r.get("价优%") is not None]
    amps_kept = [r["突破幅度%"] for r in details if r.get("突破幅度%") is not None]
    return {
        "代码": code,
        "日线bars": len(kd), "日线笔": len(bis_d), "日线中枢": len(zs_d),
        "三买点": len(pts_d), "被幅度剔除": n_drop, "5min覆盖点数": len(details),
        "覆盖起": str(cov_lo)[:10], "5minbars": len(k5),
        "有候选": sum(1 for r in details if r["5min候选"] > 0),
        "提前中位": round(pd.Series(leads).median(), 2) if leads else None,
        "价优中位": round(pd.Series(advs).median(), 2) if advs else None,
        "幅度中位": round(pd.Series(amps_kept).median(), 2) if amps_kept else None,
        "汇总": summ, "基准": base, "明细": details,
    }


# ======================================================================
# 统计功效 / 稳健性
# ======================================================================

def collect_points(rows: list[dict], side: str, h: int) -> list[dict]:
    """汇总所有标的的逐点收益 → ``[{code, ret, idx, dt}]``

    ``idx`` = 该点的入场 bar 序号（日线侧用日线 bar，5min 侧用 5min bar），
    去重叠时要用它判断两个持有窗是否重叠；``dt`` 用于时间分块。
    """
    key = f"D{h}" if side == "日线" else f"M{h}"
    ikey = "_entry_d" if side == "日线" else "_entry5"
    tkey = "日线确认" if side == "日线" else "最早买点"
    out = []
    for r in rows:
        for d in r["明细"]:
            v, i = d.get(key), d.get(ikey)
            if v is None or i is None:
                continue
            try:
                if pd.isna(v):
                    continue
            except (TypeError, ValueError):
                continue
            out.append({"code": d["代码"], "ret": float(v), "idx": int(i),
                        "dt": d.get(tkey)})
    return out


def base_of(rows: list[dict], side: str, h: int) -> dict[str, float]:
    return {r["代码"]: float(r["基准"][side][h]["mean"]) for r in rows
            if r.get("基准", {}).get(side, {}).get(h, {}).get("mean") is not None}


def deoverlap(points: list[dict], hold_bars: int) -> list[dict]:
    """同一标的内贪心去重叠：与上一条已保留的持有窗重叠的点丢弃

    持有期重叠会让「194 个样本」远小于 194 个独立机会。保留规则是按入场
    时间排序、只留窗不重叠的，得到的是**近似独立**的样本数。
    """
    by: dict[str, list[dict]] = {}
    for p in points:
        by.setdefault(p["code"], []).append(p)
    kept: list[dict] = []
    for ps in by.values():
        ps.sort(key=lambda x: x["idx"])
        last = None
        for p in ps:
            if last is None or p["idx"] >= last + hold_bars:
                kept.append(p)
                last = p["idx"]
    return kept


def metrics(points: list[dict], base: dict[str, float]) -> dict | None:
    """一次统计：点数 / 标的数 / 点均 / 等权标的均 / 基准 / 超额

    ⚠️ 「点均」与报告 §3 的样本加权平均是**同一个数**（各标的先取均值再按点数
    加权 == 全体点直接平均）。「等权标的均」是另一种口径：每只标的权重相同，
    免得数据长的标的吃掉数据短的。
    """
    pts = [p for p in points if p["code"] in base]
    if not pts:
        return None
    by: dict[str, list[float]] = {}
    for p in pts:
        by.setdefault(p["code"], []).append(p["ret"])
    codes = sorted(by)
    pt_mean = float(np.mean([p["ret"] for p in pts]))
    eqw = float(np.mean([np.mean(by[c]) for c in codes]))
    bm = float(np.mean([base[c] for c in codes]))
    return {"n": len(pts), "n_stock": len(codes), "pt_mean": pt_mean, "eqw": eqw,
            "base": bm, "ex_pt": pt_mean - bm, "ex_eqw": eqw - bm}


def cluster_bootstrap(points: list[dict], base: dict[str, float], *,
                      n_boot: int = BOOT_N, seed: int = BOOT_SEED) -> dict | None:
    """按**标的**有放回重抽 → 超额的 95% 区间 + P(超额 ≤ 0)

    点之间不独立（同标的重叠、日内相关），所以只能以标的为**聚类**重抽。
    统计量 = 重抽集合的「点均 − 基准均」（与 ``metrics`` 的 ``ex_pt`` 同定义）。
    """
    pts = [p for p in points if p["code"] in base]
    codes = sorted({p["code"] for p in pts})
    if len(codes) < 5 or not pts:
        return None
    by: dict[str, list[float]] = {}
    for p in pts:
        by.setdefault(p["code"], []).append(p["ret"])
    rng = np.random.default_rng(seed)
    k = len(codes)
    draws = np.empty(n_boot, dtype=float)
    for b in range(n_boot):
        pick = rng.integers(0, k, size=k)
        vals, bs = [], []
        for j in pick:
            c = codes[int(j)]
            vals.extend(by[c])
            bs.append(base[c])
        draws[b] = float(np.mean(vals)) - float(np.mean(bs))
    lo, hi = np.percentile(draws, [2.5, 97.5])
    return {"lo": float(lo), "hi": float(hi), "p_le0": float(np.mean(draws <= 0.0)),
            "n_boot": n_boot}


def block_bootstrap(points: list[dict], base: dict[str, float], *,
                    n_boot: int = BOOT_N, seed: int = BOOT_SEED) -> dict | None:
    """按**时间块**（自然季度）有放回重抽 → 超额的 95% 区间

    为什么还要这个：按标的的 cluster bootstrap 只剔掉**截面**噪声，剔不掉
    **时间**噪声 —— 48 只标的全落在同一段 2.7 年行情里，点之间高度同涨同跌
    （一个市场态）。真正稀缺的自由度是「几段行情」，不是「几只股票」。
    按季度重抽会把这一层依赖反映进区间里，得到的区间通常宽得多。
    """
    pts = [p for p in points if p["code"] in base and p.get("dt") is not None]
    if not pts:
        return None
    blocks: dict[str, list[dict]] = {}
    for p in pts:
        t = pd.Timestamp(str(p["dt"]))
        blocks.setdefault(f"{t.year}Q{(t.month - 1) // 3 + 1}", []).append(p)
    keys = sorted(blocks)
    if len(keys) < 4:
        return None
    rng = np.random.default_rng(seed)
    draws = np.empty(n_boot, dtype=float)
    for b in range(n_boot):
        pick = rng.integers(0, len(keys), size=len(keys))
        vals, bs = [], []
        for j in pick:
            sub = blocks[keys[int(j)]]
            vals.extend(p["ret"] for p in sub)
            bs.extend(base[p["code"]] for p in sub)
        draws[b] = float(np.mean(vals)) - float(np.mean(bs))
    lo, hi = np.percentile(draws, [2.5, 97.5])
    return {"lo": float(lo), "hi": float(hi), "p_le0": float(np.mean(draws <= 0.0)),
            "n_boot": n_boot, "n_block": len(keys)}


def robustness(rows: list[dict], side: str, h: int) -> dict:
    """一组稳健性指标：截面 bootstrap / 时间块 bootstrap / 剔前 5 点 / 剔最好标的"""
    base = base_of(rows, side, h)
    pts = collect_points(rows, side, h)
    hold_bars = h if side == "日线" else h * MIN5_BARS_PER_DAY
    out: dict = {"base": metrics(pts, base),
                 "indep": metrics(deoverlap(pts, hold_bars), base),
                 "boot": cluster_bootstrap(pts, base),
                 "boot_time": block_bootstrap(pts, base)}
    # 剔除贡献最大的 5 个点
    top5 = set()
    for p in sorted(pts, key=lambda x: -x["ret"])[:5]:
        top5.add(id(p))
    out["drop_top5"] = metrics([p for p in pts if id(p) not in top5], base)
    # 剔除表现最好的 1 只标的
    by: dict[str, list[float]] = {}
    for p in pts:
        by.setdefault(p["code"], []).append(p["ret"])
    if by:
        best = max(by, key=lambda c: float(np.mean(by[c])))
        out["drop_best_stock"] = metrics([p for p in pts if p["code"] != best], base)
        out["drop_best_stock_name"] = best
    return out


def mae_table(rows: list[dict], side: str, h: int) -> dict | None:
    """回撤拆解：MAE/MFE 分位 + 各档止损结果 +（5min 侧）价优拆解

    ⚠️ 两侧各用**自己级别**的 bar 算 MAE：日线侧 ``DMAE{h}``（日线 low），
    5min 侧 ``MAE{h}``（5min low）。早先版本两者共用 5min 的 MAE，等于拿
    "5min 入场的回撤"去描述"日线入场的回撤"，是错的。
    """
    if side == "5min":
        ret_k, mae_k, mfe_k, sl_k = f"M{h}", f"MAE{h}", f"MFE{h}", f"SL{h}"
    else:
        ret_k, mae_k, mfe_k, sl_k = f"D{h}", f"DMAE{h}", f"DMFE{h}", f"DSL{h}"

    pts = []
    for r in rows:
        for d in r["明细"]:
            v = d.get(ret_k)
            if v is None:
                continue
            try:
                if pd.isna(v):
                    continue
            except (TypeError, ValueError):
                continue
            pts.append(d)
    if not pts:
        return None

    def col(k, src=None):
        out = []
        for d in (src if src is not None else pts):
            v = d.get(k)
            if v is None:
                continue
            try:
                if pd.isna(v):
                    continue
            except (TypeError, ValueError):
                continue
            out.append(float(v))
        return np.array(out) if out else None

    rec: dict = {"n": len(pts)}
    rets = col(ret_k)
    if rets is not None and len(rets):
        rec["mean"] = float(np.mean(rets))
    mae, mfe = col(mae_k), col(mfe_k)
    if mae is not None and len(mae):
        rec["mae_med"] = float(np.median(mae))
        rec["mae_p10"] = float(np.percentile(mae, 10))
        rec["touch"] = {s: float(np.mean(mae <= -abs(s))) for s in STOP_LEVELS}
    if mfe is not None and len(mfe):
        rec["mfe_med"] = float(np.median(mfe))
    for s in STOP_LEVELS:
        v = col(f"{sl_k}_{int(s)}")
        if v is not None and len(v):
            rec[f"sl{int(s)}_mean"] = float(np.mean(v))
    bm = _base_pt(rows, side, h)
    if bm is not None:
        rec["base"] = bm

    if side == "5min":
        adv, sync = col("价优%"), col("同期基准%")
        if adv is not None and len(adv):
            rec["adv_med"] = float(np.median(adv))
            if sync is not None and len(sync) == len(adv):
                rec["sync_med"] = float(np.median(sync))
                # 价优 − 同池同窗基准 = 真正"买得早"带来的超额（剔掉 beta）
                rec["net_med"] = float(np.median(adv - sync))
        # 价优对最终收益有预测力吗
        both = [(d.get("价优%"), d.get(ret_k)) for d in pts
                if d.get("价优%") is not None and d.get(ret_k) is not None]
        both = [(a, b) for a, b in both if not pd.isna(a) and not pd.isna(b)]
        if len(both) > 2:
            rec["adv_corr"] = float(np.corrcoef([a for a, _ in both],
                                                [b for _, b in both])[0, 1])
    return rec


# ======================================================================
# 报告
# ======================================================================

def _fmt(v, nd=2):
    if v is None:
        return "--"
    try:
        if pd.isna(v):
            return "--"
    except (TypeError, ValueError):
        pass
    return f"{v:.{nd}f}"


def _pct(v, nd=1):
    return "--" if v is None else f"{v:.{nd}%}"


def _ex(m: dict | None) -> str:
    """取一次统计的超额（点均口径）并格式化；无样本给 ``--``"""
    return f"{m['ex_pt']:+.2f}" if m else "--"


def build_report(rows: list[dict], *, min_mode: str, holds: list[int],
                 pool_size: int = 0,
                 skipped: list[tuple[str, str]] | None = None,
                 min_breakout: float = 0.0) -> str:
    L: list[str] = []
    add = L.append
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    add("# 日线 + 5min 两级联立：三买点与成功率")
    add("")
    add(f"- 生成时间：{now}")
    add(f"- 5min 口径：`--min-mode {min_mode}`（**确认 bar 收盘价即买**，不等下一根）")
    add("- 日线口径：确认后**次一交易日开盘**进（日线确认在收盘后，只能次日）")
    add(f"- 突破幅度过滤：{'**关闭**' if min_breakout <= 0 else f'**≥ {min_breakout:g}%**'}")
    add(f"- 持有期（交易日）：{', '.join(str(h) for h in holds)}")
    add(f"- 标的数：**{len(rows)}**" + (f" / 池子共 {pool_size} 只" if pool_size else ""))
    if skipped:
        add("")
        add(f"- ⚠️ **未纳入统计的 {len(skipped)} 只**（本节以下所有结论都不含它们）：")
        for code, why in skipped:
            add(f"  - `{code}`：{why}")
    add("")

    n_d = sum(r["三买点"] for r in rows)
    n_drop = sum(r.get("被幅度剔除", 0) for r in rows)
    n_cov = sum(r["5min覆盖点数"] for r in rows)
    n_hit = sum(r["有候选"] for r in rows)
    add("## 1. 概览")
    add("")
    add(f"- 日线三买点合计：**{n_d}** 个（其中 {n_cov} 个落在 5min 覆盖区间内）"
        + (f"；另有 **{n_drop}** 个被突破幅度过滤剔除" if n_drop else ""))
    if n_cov:
        add(f"- 回调窗口内出现 5min 买点的点：**{n_hit} / {n_cov} = {n_hit/n_cov:.1%}**")
    leads = [r["提前中位"] for r in rows if r.get("提前中位") is not None]
    advs = [r["价优中位"] for r in rows if r.get("价优中位") is not None]
    amps = [r.get("幅度中位") for r in rows if r.get("幅度中位") is not None]
    if leads:
        add(f"- 提前量（相对日线确认时刻）中位：**{pd.Series(leads).median():.2f} 交易日**")
    if advs:
        add(f"- 入场价优势中位（日线次日开盘价 ÷ 5min 确认 bar 收盘价 − 1）："
            f"**{pd.Series(advs).median():.2f}%**")
    if amps:
        add(f"- 突破幅度中位：**{pd.Series(amps).median():.2f}%**")
    add("")
    add("读法：**覆盖率**说明这条路有多少机会可用；**提前量**才是它的全部价值 —— "
        "日线自己确认要等下一根笔（实测滞后中位约 9 交易日），5min 只要几十分钟。")
    add("")

    add("## 2. 逐标的")
    add("")
    add("| 代码 | 日线笔 | 日线中枢 | 日线三买点 | 被幅度剔除 | 5min覆盖点 | 有候选 | 覆盖率 | "
        "提前中位(交易日) | 价优中位% | 幅度中位% |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        cov = f"{r['有候选']/r['5min覆盖点数']:.0%}" if r["5min覆盖点数"] else "--"
        add(f"| {r['代码']} | {r['日线笔']} | {r['日线中枢']} | {r['三买点']} | "
            f"{r.get('被幅度剔除', 0)} | "
            f"{r['5min覆盖点数']} | {r['有候选']} | {cov} | "
            f"{_fmt(r['提前中位'])} | {_fmt(r['价优中位'])} | {_fmt(r.get('幅度中位'))} |")
    add("")

    add("## 3. 收益对比（日线确认 vs 5min 确认，同口径）")
    add("")
    add("| 持有(交易日) | 口径 | 样本 | 胜率 | 平均% | 中位% | 盈亏比 | 最佳% | 最差% | "
        "随机基准% | 超额% |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for h in holds:
        for side in ("日线", "5min"):
            rets = []
            for r in rows:
                s = r["汇总"].get(h, {}).get(side, {})
                if s.get("n"):
                    rets.append(s)
            if not rets:
                continue
            n = sum(s["n"] for s in rets)
            # 样本加权合并（各标的点数不同，不能简单平均）；nan 也要滤掉
            def wavg(key):
                pairs = [(s[key], s["n"]) for s in rets
                         if s.get(key) is not None and not pd.isna(s[key])]
                if not pairs:
                    return None
                return sum(v * w for v, w in pairs) / sum(w for _, w in pairs)
            # 随机基准：该级别全体 bar 随机入场 → **按标的等权**平均（不按样本量加权，
            # 免得数据长的标的吃掉数据短的）
            bv = [r["基准"][side][h]["mean"] for r in rows
                  if r.get("基准", {}).get(side, {}).get(h, {}).get("mean") is not None]
            bm = sum(bv) / len(bv) if bv else None
            mv = wavg("平均")
            ex = f"{mv - bm:+.2f}" if (mv is not None and bm is not None) else "--"
            add(f"| {h} | {side} | {n} | {wavg('胜率'):.1%} | {_fmt(mv)} | "
                f"{_fmt(wavg('中位'))} | {_fmt(wavg('盈亏比'))} | {_fmt(wavg('最佳'))} | "
                f"{_fmt(wavg('最差'))} | {_fmt(bm)} | {ex} |")
    add("")
    add("⚠️ 胜率是**样本加权**（各标的点数不等），不是逐标的平均。"
        "持有期互相重叠，不是独立机会数 —— 见 §5、§6。")
    add("")
    add("读法：**超额 ≤ 0 就说明这套择时没有价值** —— 同池随便哪天买都一样甚至更好。"
        "基准 = 该级别全体 bar 上「按该级别的入场价买（日线 open / 5min close）、"
        "持有 N 日收盘卖」的收益，与信号口径完全同构。")
    add("")

    add("## 4. 统计功效：这些点到底有几个是独立的")
    add("")
    add("| 持有 | 口径 | 点数 | 标的数 | 独立点数 | 点均% | 等权标的% | 基准% | "
        "超额(点均)% | 超额(等权)% |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    rob: dict[tuple[str, int], dict] = {}
    for h in holds:
        for side in ("日线", "5min"):
            rb = robustness(rows, side, h)
            rob[(side, h)] = rb
            b, ind = rb["base"], rb["indep"]
            if not b:
                continue
            add(f"| {h} | {side} | {b['n']} | {b['n_stock']} | "
                f"{(ind['n'] if ind else 0)} | {_fmt(b['pt_mean'])} | {_fmt(b['eqw'])} | "
                f"{_fmt(b['base'])} | {b['ex_pt']:+.2f} | {b['ex_eqw']:+.2f} |")
    add("")
    add("「独立点数」= 同一标的的**持有窗互不重叠**才保留（贪心）后的样本数。"
        "它是这套统计真正的自由度，比「点数」小一个量级。")
    add("")

    add("## 5. 稳健性：超额经不经得起折腾")
    add("")
    add("| 持有 | 口径 | 超额% | 标的cluster 95% | P(≤0) | 季度block 95% | P(≤0) | "
        "剔贡献前5点 | 剔最好标的 |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for h in holds:
        for side in ("日线", "5min"):
            rb = rob.get((side, h), {})
            b, bt, bt2 = rb.get("base"), rb.get("boot"), rb.get("boot_time")
            if not b:
                continue
            rng = (f"[{bt['lo']:+.2f}, {bt['hi']:+.2f}]" if bt else "--")
            p0 = _pct(bt["p_le0"]) if bt else "--"
            rng2 = (f"[{bt2['lo']:+.2f}, {bt2['hi']:+.2f}]" if bt2 else "--")
            p02 = _pct(bt2["p_le0"]) if bt2 else "--"
            d5 = rb.get("drop_top5")
            dbs = rb.get("drop_best_stock")
            add(f"| {h} | {side} | {b['ex_pt']:+.2f} | {rng} | {p0} | {rng2} | {p02} | "
                f"{_ex(d5)} | {_ex(dbs)} |")
    add("")
    add(f"两种 bootstrap 各 {BOOT_N} 次有放回重抽（种子 {BOOT_SEED}，固定可复现），"
        "统计量 = 重抽集合的「点均 − 基准均」：")
    add("")
    add("- **标的 cluster**：以**股票**为聚类重抽 —— 只剔掉截面噪声（同一只股票的"
        "点彼此相关）。")
    add("- **季度 block**：以**自然季度**为块重抽 —— 剔掉**时间/市场态**噪声。48 只"
        "标的全落在同一段行情里，这才是更诚实的那个区间。")
    add("")
    add("读法：**两个区间都跨 0 ⇒ 超额在统计上不可区分于 0**，只是一个样本内数字。"
        "若 cluster 区间为正但 block 区间跨 0，说明这套打法**只在这段行情里成立**，"
        "换一段行情没有证据。")
    add("")

    add("## 6. 回撤拆解：价格优势去哪了 + 止损")
    add("")
    add("| 持有 | 口径 | n | 价优中位% | 同期基准中位% | 择时净优势% | MAE中位% | MAE 10分位% | "
        "MFE中位% | 触及-3% | 触及-5% | 触及-8% | 均值% | 基准% | -3%止损% | -5%止损% | "
        "-8%止损% |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | "
        "--- | --- | --- | --- | --- |")
    mae_rows: list[tuple[str, int, dict]] = []
    for h in holds:
        for side in ("日线", "5min"):
            mt = mae_table(rows, side, h)
            if not mt:
                continue
            mae_rows.append((side, h, mt))
            t = mt.get("touch") or {}
            add(f"| {h} | {side} | {mt['n']} | {_fmt(mt.get('adv_med'))} | "
                f"{_fmt(mt.get('sync_med'))} | {_fmt(mt.get('net_med'))} | "
                f"{_fmt(mt.get('mae_med'))} | {_fmt(mt.get('mae_p10'))} | "
                f"{_fmt(mt.get('mfe_med'))} | "
                f"{_pct(t.get(3.0))} | {_pct(t.get(5.0))} | {_pct(t.get(8.0))} | "
                f"{_fmt(mt.get('mean'))} | {_fmt(mt.get('base'))} | "
                f"{_fmt(mt.get('sl3_mean'))} | {_fmt(mt.get('sl5_mean'))} | "
                f"{_fmt(mt.get('sl8_mean'))} |")
    add("")
    add("列义：**MAE** = 入场后持有窗内的最大不利偏移（含出场那根 bar 的 low；两侧各用"
        "自己级别的 bar。5min 侧是收盘价入场 ⇒ 自入场 bar 的**下一根**起算，日线侧是"
        "开盘价入场 ⇒ 含入场那根）；**MFE** = 最大有利偏移。**触及其档** = MAE 已到该"
        "止损线的比例。止损列按触价成交、跳空低开按开盘价成交。")
    add("")
    # ---- 价优拆解：用户的推断「优势被回撤吃掉」 ----
    for side, h, mt in mae_rows:
        if side != "5min" or mt.get("adv_med") is None:
            continue
        adv, sync, net = mt.get("adv_med"), mt.get("sync_med"), mt.get("net_med")
        mae = mt.get("mae_med")
        add(f"- **价优拆解（持有 {h} 日）**：早入场比日线确认便宜 **{adv:+.2f}%**，"
            f"其中同池同期随机持有就能拿到 **{sync:+.2f}%**（beta）"
            + (f" ⇒ **择时净优势只有 {net:+.2f}%**。" if net is not None else "。"))
        if mae is not None:
            if net is not None and abs(mae) >= abs(net):
                verdict = ("，与择时净优势**同量级** ⇒ 早进场拿到的价差，在持有期内"
                           "基本要被同规模的回撤走一遍")
            elif net is not None:
                verdict = (f"，比择时净优势小 {abs(net) - abs(mae):.2f} 个百分点"
                           " ⇒ 这一档价差没被回撤吃掉")
            else:
                verdict = ""
            add(f"  而入场后 MAE 中位 **{mae:+.2f}%**{verdict}。")
        if mt.get("adv_corr") is not None:
            add(f"  价优与该点最终收益的相关性 **{mt['adv_corr']:+.2f}**"
                + ("（≈0 ⇒ 价优大不代表最终赚得多，价优本身没有预测力）"
                   if abs(mt["adv_corr"]) < 0.2 else "。"))
        break
    add("")
    add("读法：**择时净优势 ≈ 0 或为负**时，「提前 15 天进场」这件事本身不产生收益，"
        "它只是把买点挪到了更低的位置；而 MAE 说明这个低位置在持有期内会被重新回踩。"
        "止损列若普遍**低于**同口径无止损收益，说明这套打法里止损是负贡献"
        "（打掉的多数后来都涨回来了）。")
    add("")

    add("## 7. 逐点明细（前 40 条）")
    add("")
    add("| 代码 | 窗口起 | 日线三买点 | 日线确认 | 幅度% | 5min候选 | 最早买点 | 类型 | "
        "提前(交易日) | 价优% | D5 | M5 | MAE5 |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    cnt = 0
    for r in rows:
        for d in r["明细"]:
            if cnt >= 40:
                break
            cnt += 1
            add(f"| {d['代码']} | {str(d['窗口起'])[:10]} | {str(d['三买点'])[:10]} | "
                f"{str(d['日线确认'])[:10]} | {_fmt(d.get('突破幅度%'))} | {d['5min候选']} | "
                f"{str(d['最早买点'])[:16] if d['最早买点'] is not None else '--'} | "
                f"{d['最早类型'] or '--'} | {_fmt(d['提前_日'])} | {_fmt(d['价优%'])} | "
                f"{_fmt(d.get('D5'))} | {_fmt(d.get('M5'))} | {_fmt(d.get('MAE5'))} |")
        if cnt >= 40:
            break
    add("")
    add("D5 = 日线确认入场（次日开盘）后持有 5 交易日收益；M5 = 5min 确认入场"
        "（当根收盘）后持有 5 交易日收益；MAE5 = 5min 入场后 5 交易日内的最大不利偏移"
        "（自入场 bar 下一根起算）。")
    add("")
    add("---")
    add(f"生成命令：`python scripts/eval_daily_5min.py {' '.join(sys.argv[1:])}`")
    return "\n".join(L) + "\n"


def _base_pt(rows: list[dict], side: str, h: int) -> float | None:
    bv = [r["基准"][side][h]["mean"] for r in rows
          if r.get("基准", {}).get(side, {}).get(h, {}).get("mean") is not None]
    return sum(bv) / len(bv) if bv else None


# ======================================================================
# 主流程
# ======================================================================

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


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="日线 + 5min 两级联立：三买点与成功率",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pool", type=Path, default=DEFAULT_POOL)
    p.add_argument("--codes", default="", help="只算指定代码（逗号分隔，覆盖 --pool）")
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    p.add_argument("--holds", default="5,10,20", help="持有交易日，逗号分隔")
    p.add_argument("--min-mode", default="any", choices=["any", "one", "one_div"],
                   help="5min 侧口径：any=一/二买，one=只一买，one_div=一买且背驰")
    p.add_argument("--min-breakout", type=float, default=0.0,
                   help="日线突破幅度闸门（%）：突破笔高点÷参照中枢zg−1 低于此值的点剔除；0=不过滤")
    p.add_argument("--dump-points", default="", help="逐点结果写 CSV（路径）")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--out", default="", help="报告输出路径")
    p.add_argument("--verbose", action="store_true")
    a = p.parse_args(argv)

    holds = [int(x) for x in a.holds.split(",") if x.strip()]
    if a.codes:
        pool = [(normalize_code(c), "") for c in
                (x.strip() for x in a.codes.split(",")) if c]
    else:
        pool = load_pool(a.pool)
    if a.limit:
        pool = pool[: a.limit]
    if not pool:
        print("标的池为空", file=sys.stderr)
        return 2

    out = Path(a.out) if a.out else (
        ROOT / "outputs" / f"daily5min_{a.min_mode}.md")
    print(f"日线+5min 联立：{len(pool)} 只 | 持有 {holds} | 口径 {a.min_mode} | "
          f"幅度闸门 {'关' if a.min_breakout <= 0 else f'>={a.min_breakout:g}%'} | "
          f"缓存 {a.cache_dir}")

    rows: list[dict] = []
    skipped: list[tuple[str, str]] = []
    for i, (code, name) in enumerate(pool, 1):
        diag: dict = {}
        try:
            r = analyze_one(code, a.cache_dir, holds=holds, min_mode=a.min_mode,
                            verbose=a.verbose, diag=diag,
                            min_breakout=a.min_breakout)
        except Exception as exc:
            why = f"{type(exc).__name__}: {exc}"
            skipped.append((code, why))
            print(f"[{i}/{len(pool)}] {code} ✗ {why}", flush=True)
            continue
        if r is None:
            why = diag.get("reason", "未知原因")
            skipped.append((code, why))
            print(f"[{i}/{len(pool)}] {code} 跳过（{why}）", flush=True)
            continue
        rows.append(r)
        cov = f"{r['有候选']/r['5min覆盖点数']:.0%}" if r["5min覆盖点数"] else "--"
        print(f"[{i}/{len(pool)}] {code} {name}: 日线三买 {r['三买点']} 个"
              + (f"（滤掉 {r['被幅度剔除']}）" if r.get("被幅度剔除") else "")
              + f" / 5min 覆盖 {r['5min覆盖点数']} 个 / 有候选 {r['有候选']} ({cov}) / "
              f"提前中位 {_fmt(r['提前中位'])} 日", flush=True)

    if not rows:
        print("无有效样本（先跑 fetch_min.py 取 daily 与 5min 缓存）", file=sys.stderr)
        return 1

    text = build_report(rows, min_mode=a.min_mode, holds=holds,
                        pool_size=len(pool), skipped=skipped,
                        min_breakout=a.min_breakout)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"\n报告 → {out}")
    if a.dump_points:
        n = dump_points(rows, Path(a.dump_points), holds)
        print(f"逐点 CSV → {a.dump_points}（{n} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
