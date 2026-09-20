# -*- coding: utf-8 -*-
"""诊断：出场线宽 vs 「卖飞 / 被洗下车」

    python scripts/diag_exit_chase.py                       # 全池，走缓存
    python scripts/diag_exit_chase.py --cache-days 1 --limit 10
    python scripts/diag_exit_chase.py --out outputs/exit_chase.md

回答的问题
----------
老三问：「破 20 日线空仓反而少赚，是不是错过主升段了？怎么才不被洗下车？」

测量四件事（都按**轮**，全进全出，一轮 = 一笔）：

1. **卖点离高点多远**：卖出成交价相对持仓期内最高价的回撤 —— 越大说明
   越晚出来（均线天然滞后）。
2. **卖完之后还涨多少**：出场后 5/10/20/40 个交易日的收盘收益，以及出场后
   20 日内的**最高涨幅**。前者是"卖飞"的直接度量，后者回答"是不是主升段"。
3. **多久能再上车**：卖出成交日到下一轮买入成交日的间隔（交易日）；以及
   出场后样本内**再没出现买点**的比例（六脉买点只在共振首日出现，
   趋势中 all6 持续为真 ⇒ 出一次场就再也拿不到信号）。
4. **收益归属**：持仓日的累计涨幅 vs 空仓日的累计涨幅 —— 看是不是
   "该在场的时候不在场"。

外加一档对照：**「MA20 出场 → 收盘站回 MA20 就回补」**（`--reentry` 口径），
检验"止损后允许在趋势内重新上车"能不能改善。

⚠️ 口径同 `eval_six_pulse.py`：逐只独立 100 万满仓、等权；买点固定为
六脉共振首日，只换出口。轮级才是跨出口可比口径。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import six_pulse                                    # noqa: E402
from core.backtest.strategy import Action, Signal, Strategy   # noqa: E402
from eval_six_pulse import (                                  # noqa: E402
    DAILY_CACHE, DEFAULT_CAPITAL, DEFAULT_POOL, eval_one, load_daily, merge_stats,
)
from scan_pool import load_pool                               # noqa: E402

HORIZONS = (5, 10, 20, 40)


# ======================================================================
# 信号 → 轮（买入信号, 卖出信号）
# ======================================================================

def episodes_of(strategy, daily) -> list[tuple[Signal, Signal]]:
    """策略信号按「买-卖」配对成轮；全进全出下一个买必跟一个卖"""
    sig = strategy.generate_signals(daily)
    out: list[tuple[Signal, Signal]] = []
    pending: Signal | None = None
    for s in sig:
        if s.action == Action.BUY:
            pending = s
        elif s.action == Action.SELL and pending is not None:
            out.append((pending, s))
            pending = None
    return out


def round_metrics(daily, eps, horizons=HORIZONS) -> list[dict]:
    """逐轮算「卖点位置 / 卖飞幅度 / 再上车延迟」"""
    idx = {k.date: i for i, k in enumerate(daily)}
    closes = np.array([k.close for k in daily], dtype=float)
    highs = np.array([k.high for k in daily], dtype=float)
    n = len(daily)

    rows: list[dict] = []
    for r, (b, s) in enumerate(eps):
        i, j = idx[b.date], idx[s.date]
        peak = float(highs[i:j + 1].max())
        peak_ret = peak / b.price - 1.0
        realized = s.price / b.price - 1.0

        row = {
            "ep": r + 1,
            "buy_date": b.date, "sell_date": s.date,
            "hold": j - i,
            "realized": realized,
            "peak_ret": peak_ret,
            "dd_from_peak": (s.price / peak - 1.0) if peak > 0 else np.nan,
            "capture": (realized / peak_ret) if peak_ret > 0.01 else np.nan,
            "bars_left": n - 1 - j,
        }
        for h in horizons:
            k = j + h
            row[f"post_{h}"] = (closes[k] / s.price - 1.0) if k < n else np.nan
        nxt = highs[j + 1: min(j + 21, n)]
        row["post_peak20"] = (float(nxt.max()) / s.price - 1.0) if nxt.size else np.nan
        rows.append(row)

    # 再上车：本轮卖出 → 下一轮买入
    for r, (b, s) in enumerate(eps):
        if r + 1 < len(eps):
            nb = eps[r + 1][0]
            rows[r]["gap"] = idx[nb.date] - idx[s.date]
            rows[r]["reentry_premium"] = nb.price / s.price - 1.0
        else:
            rows[r]["gap"] = np.nan          # 末轮：看下面 last_unfilled
            rows[r]["reentry_premium"] = np.nan

    # 末轮之后是否还有机会：出场时身后还剩 ≥20 根，却再没等到买点
    for r, (b, s) in enumerate(eps):
        if r == len(eps) - 1:
            rows[r]["last_unfilled"] = bool((n - 1 - idx[s.date]) >= 20)
    return rows


def entry_position(daily) -> dict:
    """**买入时**收盘价相对各条均线的位置 —— 解释「为什么轮这么短」

    若买点当天收盘价已经在 MAx **下方**，那条 MAx 出场线几乎立刻触发
    （价格不需要下跌，一进场就已满足离场条件）。这就是「1~3 日超短轮」的来源。
    """
    df = six_pulse.to_frame(daily)
    ind = six_pulse.compute_indicators(df)
    closes = df["close"].to_numpy(dtype=float)
    buy = ind["buy_signal"].to_numpy()
    out: dict[int, dict] = {}
    for ma in (5, 10, 20, 30, 60):
        line = pd.Series(closes).rolling(ma).mean().to_numpy()
        idxs = [i for i in range(six_pulse.WARMUP, len(closes) - 1)
                if buy[i] and not np.isnan(line[i])]
        if not idxs:
            continue
        rel = np.array([closes[i] / line[i] - 1.0 for i in idxs], dtype=float)
        out[ma] = {"n": len(idxs), "below": float((rel < 0).mean()),
                   "median": float(np.median(rel))}
    return out


# ======================================================================
# 外挂对照：MA20 出场 → 站回 MA20 回补
# ======================================================================

def holding_buckets(metrics: list[dict]) -> list[dict]:
    """按持有天数分桶看轮数与收益贡献 —— 回答「主升段有没有吃到」

    `logsum` 是该桶所有轮的对数收益之和（近似可加的"贡献"）。
    """
    edges = [(1, 3), (4, 10), (11, 20), (21, 10 ** 6)]
    out: list[dict] = []
    total = len(metrics) or 1
    for lo, hi in edges:
        sub = [m for m in metrics if lo <= m["hold"] <= hi]
        if not sub:
            continue
        rets = np.array([m["realized"] for m in sub], dtype=float)
        out.append({
            "label": f"{lo}~{hi}" if hi < 10 ** 6 else f"≥{lo}",
            "n": len(sub),
            "share": len(sub) / total,
            "win": float((rets > 0).mean()),
            "mean": float(rets.mean()),
            "logsum": float(np.log1p(np.clip(rets, -0.99, None)).sum()),
        })
    return out


class FixedSignals(Strategy):
    """把预算好的信号交给 engine —— 用来跑「不属于任何策略类」的对照口径"""

    name = "fixed"

    def __init__(self, sigs: list[Signal], name: str = "fixed"):
        self._sigs = list(sigs)
        self.name = name

    def generate_signals(self, daily) -> list[Signal]:
        return list(self._sigs)


def ma_reentry_signals(daily, ma: int = 20, entry_ma: int | None = None,
                       warmup: int = six_pulse.WARMUP) -> list[Signal]:
    """破 MA`ma` 清仓 → 收盘**站回** MA`ma` 就回补（不限时间）

    首次建仓仍然只认六脉共振首日；「站回」只用于止损后的再上车，
    避免在从未有过买点的下跌趋势里乱接刀。

    `entry_ma` 不为 None 时，买点额外要求**收盘价站在该均线上方** ——
    用来修「买进来时价格已在出场线下方、一进场就被扫出去」这个不自洽。
    """
    df = six_pulse.to_frame(daily)
    ind = six_pulse.compute_indicators(df, ma_exit=ma)
    closes = df["close"].to_numpy(dtype=float)
    opens = df["open"].to_numpy(dtype=float)
    dates = df["date"].tolist()
    ma_line = pd.Series(closes).rolling(ma).mean().to_numpy()
    entry_line = (pd.Series(closes).rolling(entry_ma).mean().to_numpy()
                  if entry_ma else None)
    buy = ind["buy_signal"].to_numpy()
    n = len(dates)

    sigs: list[Signal] = []
    holding = False
    armed = False                      # 被止损过、等站回
    for i in range(warmup, n):
        c, m = closes[i], ma_line[i]
        if np.isnan(m):
            continue
        if not holding:
            if i + 1 >= n:
                break
            fresh = buy[i] and (entry_line is None or (
                not np.isnan(entry_line[i]) and c > entry_line[i]))
            if fresh:
                sigs.append(Signal(date=dates[i + 1], action=Action.BUY,
                                   price=float(opens[i + 1]), reason="六脉共振首日"))
                holding, armed = True, False
            elif armed and c > m:
                sigs.append(Signal(date=dates[i + 1], action=Action.BUY,
                                   price=float(opens[i + 1]), reason=f"站回 MA{ma}"))
                holding, armed = True, False
        else:
            if i + 1 >= n:
                break
            if c < m:
                sigs.append(Signal(date=dates[i + 1], action=Action.SELL,
                                   price=float(opens[i + 1]), reason=f"破 MA{ma}"))
                holding, armed = False, True
    return sigs


# ======================================================================
# 报告
# ======================================================================

def _pct(x: float) -> str:
    return "--" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:+.2%}"


def _agg(vals: list[float]) -> dict:
    a = np.array([v for v in vals if not (v is None or np.isnan(v))], dtype=float)
    if a.size == 0:
        return {"n": 0, "mean": np.nan, "median": np.nan, "p25": np.nan,
                "p75": np.nan, "pos": np.nan}
    return {
        "n": int(a.size), "mean": float(a.mean()), "median": float(np.median(a)),
        "p25": float(np.percentile(a, 25)), "p75": float(np.percentile(a, 75)),
        "pos": float((a > 0).mean()),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--days", type=int, default=1500)
    ap.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    ap.add_argument("--cache-days", type=int, default=1)
    ap.add_argument("--pool", default=str(DEFAULT_POOL))
    ap.add_argument("--out", default="outputs/exit_chase.md")
    args = ap.parse_args()

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        # load_pool 返回 (代码, 名称) 二元组
        codes = [c for c, _ in load_pool(Path(args.pool))]
    if args.limit:
        codes = codes[: args.limit]

    pool_data: list[tuple[str, list]] = []
    for code in codes:
        try:
            daily, _cached = load_daily(code, args.days, cache_dir=DAILY_CACHE,
                                        cache_days=args.cache_days)
        except Exception as exc:                      # noqa: BLE001 — 缺缓存就不参与
            print(f"  跳过 {code}：{exc}")
            continue
        pool_data.append((code, daily))
    if not pool_data:
        raise SystemExit("池内无可用数据")
    print(f"池 {len(pool_data)} 只 | {pool_data[0][1][0].date} ~ {pool_data[0][1][-1].date}")

    ma_variants = [("MA5", 5), ("MA10", 10), ("MA20", 20), ("MA30", 30)]
    tiered_variants = [
        ("分级 MA5/10/20（现默认）", dict(exit_mode="tiered")),
        ("分级 MA10/20/30（放宽一档）", dict(exit_mode="tiered", ma_reduce=10,
                                          ma_reentry=20, ma_stop=30)),
    ]

    # ---------- §1 出口对照（轮级） ----------
    exit_rows: list[dict] = []
    per_variant_metrics: dict[str, list[dict]] = {}

    # 买入时价格相对各条均线的位置（解释超短轮）
    entry_pos: dict[int, list[dict]] = {}
    for _code, daily in pool_data:
        for ma_key, st_ in entry_position(daily).items():
            entry_pos.setdefault(ma_key, []).append(st_)

    for label, ma in ma_variants:
        strat = six_pulse.SixPulseStrategy(exit_mode="ma", ma_exit=ma)
        per_code = [eval_one(c, d, strat, args.capital) for c, d in pool_data]
        st = merge_stats(per_code)
        st["label"] = label
        exit_rows.append(st)
        mets: list[dict] = []
        for code, daily in pool_data:
            eps = episodes_of(strat, daily)
            mets.extend(round_metrics(daily, eps))
        per_variant_metrics[label] = mets

    for label, kw in tiered_variants:
        strat = six_pulse.SixPulseStrategy(**kw)
        per_code = [eval_one(c, d, strat, args.capital) for c, d in pool_data]
        st = merge_stats(per_code)
        st["label"] = label
        exit_rows.append(st)

    # 固定持有 40 日（对照"根本不看均线"）
    strat40 = six_pulse.SixPulseStrategy(hold_days=40)
    per_code = [eval_one(c, d, strat40, args.capital) for c, d in pool_data]
    st = merge_stats(per_code)
    st["label"] = "固定持有 40 日"
    exit_rows.append(st)

    # ---------- §7 对照：回补 / 买点加均线过滤 ----------
    reentry_rows: list[dict] = []
    reentry_specs = [
        ("MA10 出场 + 站回 MA10 回补", dict(ma=10)),
        ("MA20 出场 + 站回 MA20 回补", dict(ma=20)),
        ("MA20 出场 + 买点加 C>MA20 过滤", dict(ma=20, entry_ma=20)),
        ("MA20 出场 + 买点加 C>MA60 过滤", dict(ma=20, entry_ma=60)),
    ]
    for label, kw in reentry_specs:
        per_code = []
        mets: list[dict] = []
        for code, daily in pool_data:
            s = FixedSignals(ma_reentry_signals(daily, **kw), name=label)
            per_code.append(eval_one(code, daily, s, args.capital))
            eps = episodes_of(s, daily)
            mets.extend(round_metrics(daily, eps))
        st = merge_stats(per_code)
        st["label"] = label
        reentry_rows.append(st)
        per_variant_metrics[label] = mets

    base_ret = float(np.mean([
        (d[-1].close / d[0].close - 1.0) for _, d in pool_data
    ]))

    # ---------- 写报告 ----------
    L: list[str] = []
    A = L.append
    A("# 出场诊断：卖飞与被洗下车的度量")
    A("")
    A(f"- 池：{len(pool_data)} 只 | 日线 {args.days} 根 | "
      f"{pool_data[0][1][0].date} ~ {pool_data[0][1][-1].date}")
    A("- 买点固定为**六脉共振首日**，只换出口；逐只独立 100 万满仓、等权")
    A(f"- **买入持有基线（等权）：{base_ret:+.2%}**")
    A("- 一轮 = 一次完整持仓（全进全出）；跨出口比较看**轮级**")
    A("")

    A("## 1. 各出口的轮级成绩")
    A("")
    A("| 出口 | 轮数 | 轮胜率 | 轮均收益 | 平均区间收益 | 在场时间 |")
    A("| --- | --- | --- | --- | --- | --- |")
    for s in exit_rows:
        A(f"| {s['label']} | {s['episodes']} | {s['ep_win_rate']:.1%} "
          f"| {_pct(s['ep_avg_pct'])} | {_pct(s['avg_return'])} | {s['exposure']:.1%} |")
    for s in reentry_rows:
        A(f"| {s['label']} | {s['episodes']} | {s['ep_win_rate']:.1%} "
          f"| {_pct(s['ep_avg_pct'])} | {_pct(s['avg_return'])} | {s['exposure']:.1%} |")
    A(f"| **买入持有（基线）** | 1 | -- | -- | **{_pct(base_ret)}** | 100% |")
    A("")

    A("## 2. 卖点位置：是「在高点出来」还是「回撤一大截才出来」")
    A("")
    A("| 出口 | 轮数 | 卖出价相对持仓最高价 | 捕获率（实现收益/最高浮盈） | 中位持有 |")
    A("| --- | --- | --- | --- | --- |")
    for label, _ in ma_variants:
        m = per_variant_metrics[label]
        if not m:
            continue
        dd = _agg([r["dd_from_peak"] for r in m])
        cap = _agg([r["capture"] for r in m])
        hold = _agg([r["hold"] for r in m])
        A(f"| {label} | {len(m)} | {_pct(dd['median'])}（中位） | "
          f"{cap['median']:.2f} | {hold['median']:.0f} 交易日 |")
    A("")
    A("读法：`卖出价相对持仓最高价` 越接近 0 说明越贴近顶部离场；")
    A("越负说明均线滞后越严重 —— 价格已经掉下来一大截才触发。")
    A("")

    A("## 3. 持有期分桶：主升段吃到了没有")
    A("")
    for label in ("MA5", "MA10", "MA20"):
        m = per_variant_metrics.get(label, [])
        if not m:
            continue
        A(f"**{label}**")
        A("")
        A("| 持有（交易日） | 轮数 | 占比 | 轮胜率 | 轮均收益 | 对数贡献合计 |")
        A("| --- | --- | --- | --- | --- | --- |")
        for b in holding_buckets(m):
            A(f"| {b['label']} | {b['n']} | {b['share']:.1%} | {b['win']:.1%} "
              f"| {_pct(b['mean'])} | {b['logsum']:+.2f} |")
        holds = _agg([r["hold"] for r in m])
        A("")
        A(f"中位持有 {holds['median']:.0f} 日、均值 {holds['mean']:.1f} 日、"
          f"P75 {holds['p75']:.0f} 日（**均值远大于中位 = 分布极度右偏**）")
        A("")

    A("## 4. 卖完之后还涨多少（卖飞的直接度量）")
    A("")
    A("| 出口 | 出场后 5 日 | 后 10 日 | 后 20 日 | 后 40 日 | 出场后 20 日内最高 |")
    A("| --- | --- | --- | --- | --- | --- |")
    for label, _ in ma_variants:
        m = per_variant_metrics.get(label, [])
        if not m:
            continue
        cells = []
        for h in HORIZONS:
            cells.append(_pct(_agg([r[f"post_{h}"] for r in m])["median"]))
        pk = _agg([r["post_peak20"] for r in m])
        A(f"| {label} | " + " | ".join(cells) + f" | {_pct(pk['median'])}（中位） |")
    A("")
    for label in ("MA5", "MA20"):
        m = per_variant_metrics.get(label, [])
        if not m:
            continue
        a20 = _agg([r["post_20"] for r in m])
        pk = _agg([r["post_peak20"] for r in m])
        A(f"- **{label}**：出场后 20 日仍上涨的比例 **{a20['pos']:.1%}**；"
          f"出场后 20 日内最高价高于卖出价的比例 **{pk['pos']:.1%}**")
    A("")

    A("## 5. 多久才能再上车")
    A("")
    A("| 出口 | 轮数 | 卖出→下次买入（交易日） | 中位 | P75 | 再买入价高于卖出价的比例 | 中位溢价 | 出场后再无买点（末轮，身后≥20 根） |")
    A("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for label, _ in ma_variants:
        m = per_variant_metrics.get(label, [])
        if not m:
            continue
        gaps = [r["gap"] for r in m if not np.isnan(r.get("gap", np.nan))]
        g = _agg(gaps)
        prem = _agg([r["reentry_premium"] for r in m])
        unfilled = [r for r in m if r.get("last_unfilled")]
        A(f"| {label} | {len(m)} | {g['mean']:.1f} | {g['median']:.0f} | {g['p75']:.0f} "
          f"| {prem['pos']:.1%} | {_pct(prem['median'])} "
          f"| {len(unfilled)}/{len(m)} |")
    A("")

    A("## 6. 收益 vs 在场时间：择时加分了吗")
    A("")
    A("| 出口 | 平均区间收益 | 在场时间 | 单位在场收益（收益 ÷ 在场） |")
    A("| --- | --- | --- | --- |")
    for s in exit_rows + reentry_rows:
        exp = s["exposure"]
        unit = (s["avg_return"] / exp) if exp > 0 else float("nan")
        A(f"| {s['label']} | {_pct(s['avg_return'])} | {exp:.1%} | {_pct(unit)} |")
    A(f"| **买入持有（基线）** | **{_pct(base_ret)}** | 100% | **{_pct(base_ret)}** |")
    A("")
    A("`单位在场收益` = 平均区间收益 ÷ 在场时间占比，即**每一份市场暴露换回多少收益**。"
      "若各档出口都低于买入持有的基线值，说明择时不只是没加分、而是**减分**，"
      "出口之间的高低主要取决于「敢在场多久」。")
    A("")

    A("## 7. 对照：回补 与 买点加均线过滤")
    A("")
    A("| 口径 | 轮数 | 轮胜率 | 轮均收益 | 平均区间收益 | 在场时间 |")
    A("| --- | --- | --- | --- | --- | --- |")
    for s in reentry_rows:
        A(f"| {s['label']} | {s['episodes']} | {s['ep_win_rate']:.1%} "
          f"| {_pct(s['ep_avg_pct'])} | {_pct(s['avg_return'])} | {s['exposure']:.1%} |")
    A("")
    A("`站回回补` 把暴露度加回来（收益随在场时间涨）；`买点过滤` 是把"
      "「一买进来就已在出场线下方」那批信号直接剔除，看信号质量有没有提升。")
    A("")

    A("## 8. 为什么轮这么短：买点当天价格已经在线下")
    A("")
    A("| 均线 | 共振买点样本 | 买点当天收盘**低于**该均线的比例 | 中位偏离 |")
    A("| --- | --- | --- | --- |")
    for ma_key in (5, 10, 20, 30, 60):
        lst = entry_pos.get(ma_key, [])
        if not lst:
            continue
        n_sig = sum(x["n"] for x in lst)
        below = float(np.average([x["below"] for x in lst],
                                 weights=[x["n"] for x in lst]))
        med = float(np.median([x["median"] for x in lst]))
        A(f"| MA{ma_key} | {n_sig} | **{below:.1%}** | {med:+.2%} |")
    A("")
    A("读法：买点当天收盘价若已在 MA20 下方，「跌破 MA20 才卖」这条出场线"
      "**在买入那一刻就已经满足** —— 次一交易日立刻被扫出去，"
      "这正是 §3 里 1~3 日超短轮的来源。")
    A("")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"报告：{out}")


if __name__ == "__main__":
    main()
