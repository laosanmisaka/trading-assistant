# -*- coding: utf-8 -*-
"""三买点基线统计 —— 有多少个点、成功率多少、止损放哪儿

    python scripts/eval_triple_buy.py                                  # 30min，50 只池
    python scripts/eval_triple_buy.py --period 5min --pool scripts/pool_random500.txt
    python scripts/eval_triple_buy.py --entry confirm --stop-zg        # 可交易口径 + 结构止损
    python scripts/eval_triple_buy.py --holds 1,3,5,10,20 --limit 5 --dump-trades

为什么先做这个
--------------
三买从来没有进过策略层（`core/chan_strategy` 只取日线一买/二买 + 30 分钟二买，
三买只进 `counts` 计数）。所以「三买值不值得做」在产品里**一个数都没有**。
本脚本用现成的几何判定直接测量，不改任何现有代码。

两种入场口径（差一个「等确认」的距离）
--------------------------------------
- ``--entry ideal``（默认，**不可交易的上界**）：以三买点本身的价格成交，
  即回抽笔的 ``low``。这是「如果我能精确抓到那个低点」的理论天花板，
  用来判断**判定逻辑本身有没有区分度**。
- ``--entry confirm``（**可交易口径**）：三买点 = 回抽笔（bis[m]）的终点，
  它要等**下一根笔**（bis[m+1]）走完才锁定 ⇒ 在 ``bis[m+1].edt`` 之后的
  第一根 bar 开盘成交（A 股 T+1）。

  ⚠️ 这仍是**乐观**估计：`diag_m30_lag.py` 量到 30 分钟级确认滞后中位
  9 根 bar（口径 A = 下一笔终点作确认时刻），与这里同口径；真实可交易时点
  不会早于它，只会更晚。

出场与止损
----------
- 出场：``--holds`` 给出若干个**交易日**的固定持有期，各自独立统计
  （不是累加，也不是资金曲线）。
- 止损：``--stop-zg`` 时，持有期内只要有 bar 的 ``low`` 跌破该三买所依据的
  中枢上沿 ``zg``，即视为结构失效、以 ``zg`` 成交（保守，忽略跳空）。

⚠️ 统计口径
------------
- 按**点**统计，**不是资金曲线**。同一只标的的三买点可以密集出现、持有期
  互相重叠，收益不可相加。这与 `scan_pool.py` 的既有口径一致。
- 基准列为**同池随机入场**：每只标的所有 bar 上「持有 N 日」的收益中位数。
  没有它，胜率 50% 这种数字没法解释（牛市里随便买都赚）。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import chan as chan_mod                       # noqa: E402
from core import chan_points                            # noqa: E402
from core.chan_viz import normalize_code                # noqa: E402
from data.models import KLineData                       # noqa: E402

DEFAULT_CACHE = ROOT / "outputs" / "cache_min"
DEFAULT_POOL = ROOT / "scripts" / "pool_liquid50.txt"

#: 级别 → 一个交易日有多少根 bar（A 股一天 240 分钟）
BARS_PER_DAY = {"1min": 240, "5min": 48, "15min": 16, "30min": 8, "60min": 4}


# ======================================================================
# 读取
# ======================================================================

def load_pool(path: Path) -> list[tuple[str, str]]:
    pool: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        code = normalize_code(parts[0])
        if code in seen:
            continue
        seen.add(code)
        pool.append((code, parts[1] if len(parts) > 1 else ""))
    return pool


def load_cached(code: str, period: str, cache_dir: Path) -> list[KLineData] | None:
    """读 `fetch_min.py` 落的缓存 CSV → KLineData；不存在则 None"""
    f = Path(cache_dir) / f"{period}_{code}.csv"
    if not f.exists() or f.stat().st_size == 0:
        return None
    df = pd.read_csv(f)
    return [KLineData(code=code, date=str(r.dt), open=float(r.open),
                      high=float(r.high), low=float(r.low), close=float(r.close),
                      volume=int(r.volume), period=period)
            for r in df.itertuples()]


# ======================================================================
# 收益统计
# ======================================================================

def summarize(rets: list[float]) -> dict:
    """收益率序列 → 分布摘要（空样本返回 None 值，不抛异常）"""
    if not rets:
        return {"n": 0, "胜率": None, "平均": None, "中位": None,
                "平均盈": None, "平均亏": None, "盈亏比": None,
                "最佳": None, "最差": None}
    s = pd.Series(rets, dtype=float)
    wins, losses = s[s > 0], s[s <= 0]
    pl = (wins.mean() / abs(losses.mean())) if len(losses) and losses.mean() != 0 else None
    return {
        "n": len(rets),
        "胜率": round(float((s > 0).mean()), 4),
        "平均": round(float(s.mean()), 3),
        "中位": round(float(s.median()), 3),
        "平均盈": round(float(wins.mean()), 3) if len(wins) else None,
        "平均亏": round(float(losses.mean()), 3) if len(losses) else None,
        "盈亏比": round(float(pl), 2) if pl is not None else None,
        "最佳": round(float(s.max()), 2),
        "最差": round(float(s.min()), 2),
    }


def baseline_returns(closes: list[float], hold: int) -> list[float]:
    """同池随机入场基准：每根 bar 入场、持有 ``hold`` 根 bar 的收益率（%）

    近似「不看任何信号，随便哪天买」的成绩。样本是全体 bar，不是抽样，
    所以它的均值就是该标的在该区间的平均持有收益。
    """
    n = len(closes)
    if hold <= 0 or n <= hold:
        return []
    out: list[float] = []
    for i in range(n - hold):
        a, b = closes[i], closes[i + hold]
        if a > 0 and b > 0:          # 0 价 bar（停牌占位）不是有效样本，跳过
            out.append((b / a - 1) * 100)
    return out


# ======================================================================
# 单只统计
# ======================================================================

def eval_one(code: str, klines: list[KLineData], period: str, *,
             holds_days: list[int], entry: str, stop_zg: bool,
             lookback: int) -> tuple[list[dict], dict, chan_mod.ChanResult | None]:
    """逐点评估三买 → ``(逐点明细, 逐持有期汇总, ChanResult)``

    返回的汇总里每个持有期一个 dict，含 ``rets`` / ``stops`` / ``mae``
    三组原始序列（由调用方再做分布统计）。
    """
    bpd = BARS_PER_DAY[period]
    res = chan_mod.build(klines, period)
    if res is None:
        return [], {}, None

    bis = chan_mod.bis(res)
    centers = chan_mod.centers(res)
    closes = [float(k.close) for k in klines]
    lows = [float(k.low) for k in klines]
    opens = [float(k.open) for k in klines]
    times = [str(k.date) for k in klines]
    pos = {t: i for i, t in enumerate(times)}

    pts = [p for p in chan_points.buy_sell_points(bis, centers, lookback)
           if p["kind"] == "三买"]

    details: list[dict] = []
    buckets: dict[int, dict] = {
        d: {"rets": [], "stops": 0, "expired": 0, "mae": [], "mfe": []}
        for d in holds_days
    }

    # 该三买所依据的中枢：取「edt 严格早于该点」的最后一个（与判定同口径）
    def ref_zg(dt) -> float | None:
        z = chan_points._last_center_before(centers, dt)
        return float(z["high"]) if z else None

    for p in pts:
        i_pt = pos.get(str(p["dt"]))
        if i_pt is None:
            continue

        # ---- 入场 ----
        if entry == "ideal":
            i_in, price_in = i_pt, float(p["price"])
        else:                                   # confirm：等下一笔终点之后再开盘
            j = p["bi_idx"] + 1
            if j >= len(bis):
                continue                        # 末尾未确认，无法交易
            t_confirm = str(bis[j]["edt"])
            i_c = pos.get(t_confirm)
            if i_c is None or i_c + 1 >= len(closes):
                continue
            i_in, price_in = i_c + 1, opens[i_c + 1]
        if not price_in:
            continue

        zg = ref_zg(p["dt"])

        # ---- 逐持有期 ----
        for d in holds_days:
            hold = d * bpd
            i_out = i_in + hold
            if i_out >= len(closes):
                buckets[d]["expired"] += 1
                continue

            seg_low = lows[i_in + 1: i_out + 1]
            seg_high = [closes[k] for k in range(i_in + 1, i_out + 1)]

            # 止损：持有期内最早跌破 zg 的那根 bar，以 zg 成交
            stop_i = None
            if stop_zg and zg is not None:
                for k in range(i_in + 1, i_out + 1):
                    if lows[k] < zg:
                        stop_i = k
                        break

            if stop_i is not None:
                ret = (zg / price_in - 1) * 100
                buckets[d]["stops"] += 1
                mae = (min(seg_low[: stop_i - i_in]) / price_in - 1) * 100 if stop_i > i_in + 1 else 0.0
                mfe = (max(seg_high[: stop_i - i_in]) / price_in - 1) * 100 if stop_i > i_in + 1 else 0.0
                exit_t, exited = times[stop_i], "止损"
            else:
                ret = (closes[i_out] / price_in - 1) * 100
                mae = (min(seg_low) / price_in - 1) * 100
                mfe = (max(seg_high) / price_in - 1) * 100
                exit_t, exited = times[i_out], "到期"

            buckets[d]["rets"].append(ret)
            buckets[d]["mae"].append(mae)
            buckets[d]["mfe"].append(mfe)
            details.append({
                "代码": code, "持有日": d, "三买时刻": times[i_pt],
                "入场时刻": times[i_in], "入场价": round(price_in, 3),
                "zg": round(zg, 3) if zg is not None else "",
                "出场时刻": exit_t, "出场方式": exited,
                "收益率%": round(ret, 2), "MAE%": round(mae, 2), "MFE%": round(mfe, 2),
            })

    summary = {
        d: {
            "三买点数": len(pts),
            "已平仓": len(buckets[d]["rets"]),
            "未到期": buckets[d]["expired"],
            "止损数": buckets[d]["stops"],
            "止损率": (round(buckets[d]["stops"] / len(buckets[d]["rets"]), 4)
                       if buckets[d]["rets"] else None),
            **summarize(buckets[d]["rets"]),
            "MAE中位": (round(float(pd.Series(buckets[d]["mae"]).median()), 2)
                        if buckets[d]["mae"] else None),
            "MAE_P10": (round(float(pd.Series(buckets[d]["mae"]).quantile(0.10)), 2)
                        if buckets[d]["mae"] else None),
            "MFE中位": (round(float(pd.Series(buckets[d]["mfe"]).median()), 2)
                        if buckets[d]["mfe"] else None),
        }
        for d in holds_days
    }
    return details, summary, res


# ======================================================================
# 报告
# ======================================================================

def _fmt(v, nd=2) -> str:
    if v is None:
        return "--"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def render_report(rows: list[dict], details: list[dict], *, period: str,
                  pool_path: Path, entry: str, stop_zg: bool,
                  holds_days: list[int], started: datetime,
                  n_bars_total: int, n_days_total: int) -> str:
    L: list[str] = []
    add = L.append
    add(f"# 三买点基线统计（{period}）")
    add("")
    add(f"- 生成时间：{started:%Y-%m-%d %H:%M}")
    add(f"- 标的池：`{pool_path.name}`（{len(rows)} 只）")
    add(f"- 入场口径：**{entry}**" + (
        "（以三买点本身的价格成交 —— 不可交易的理论上界，用于判断判定逻辑有无区分度）"
        if entry == "ideal" else
        "（等回抽笔之后的下一笔终点确认，再下一根 bar 开盘成交 —— 乐观的可交易口径）"))
    add(f"- 止损：{'跌破所依据中枢的 zg 即出场（以 zg 成交）' if stop_zg else '不设（只看固定持有期）'}")
    add(f"- 数据：{n_bars_total:,} 根 bar / 约 {n_days_total:,} 个交易日（来自 `outputs/cache_min/`）")
    add("")
    add("⚠️ **按点统计，不是资金曲线**。同一只标的三买点可密集出现、持有期互相重叠，"
        "收益不可相加；这与其他诊断脚本口径一致。")
    add("")

    add("## 1. 逐持有期汇总（**全池聚合**）")
    add("")
    add("| 持有(交易日) | 三买点数 | 已平仓 | 未到期 | 止损数 | 止损率 | 胜率 | 平均% | 中位% | "
        "盈亏比 | 最佳% | 最差% |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    total_pts = sum(r.get("三买点", 0) for r in rows)
    for d in holds_days:
        sub = [x for x in details if x["持有日"] == d]
        rets = [x["收益率%"] for x in sub]
        s = summarize(rets)
        expired = sum(r["汇总"].get(d, {}).get("未到期", 0) for r in rows)
        n_stop = sum(1 for x in sub if x["出场方式"] == "止损")
        if not s["n"]:
            continue
        add(f"| {d} | {total_pts} | {s['n']} | {expired} | {n_stop} | "
            f"{n_stop / s['n']:.1%} | {s['胜率']:.1%} | {_fmt(s['平均'])} | {_fmt(s['中位'])} | "
            f"{_fmt(s['盈亏比'])} | {_fmt(s['最佳'])} | {_fmt(s['最差'])} |")
    add("")
    add("注：胜率/平均/盈亏比由**逐点收益序列**直接计算（不是各标的平均的平均）；"
        "「未到期」= 该持有期超出数据末尾、不计入统计。")
    add("")

    add("## 2. 对照：同池随机入场（不看信号）")
    add("")
    add("| 持有(交易日) | 随机入场 平均% | 随机入场 中位% | 三买 平均% | 超额% |")
    add("| --- | --- | --- | --- | --- |")
    for d in holds_days:
        base_means = [r["基准"].get(d, {}).get("mean") for r in rows
                      if r["基准"].get(d, {}).get("mean") is not None]
        base_meds = [r["基准"].get(d, {}).get("median") for r in rows
                     if r["基准"].get(d, {}).get("median") is not None]
        sig = [r["汇总"].get(d, {}).get("平均") for r in rows
               if r["汇总"].get(d, {}).get("平均") is not None]
        if not base_means or not sig:
            continue
        bm, bmed = sum(base_means) / len(base_means), sum(base_meds) / len(base_meds)
        sm = sum(sig) / len(sig)
        add(f"| {d} | {bm:.2f} | {bmed:.2f} | {sm:.2f} | {sm - bm:+.2f} |")
    add("")
    add("读法：**超额≤0 就说明三买点没有择时价值** —— 随机哪天买都一样甚至更好。"
        "基准是该池每只标的所有 bar 上「持有 N 日」收益的中位数／均值（全体 bar，非抽样），"
        "再按标的**等权**平均（不按样本量加权，免得大样本标的吃掉小样本标的）。")
    add("")

    add("## 3. 逐标的")
    add("")
    cols = ["代码", "名称", "bar数", "交易日", "笔", "中枢", "三买点"]
    add("| " + " | ".join(cols) + " |")
    add("| " + " | ".join("---" for _ in cols) + " |")
    for r in rows:
        add("| " + " | ".join(_fmt(r.get(c)) for c in cols) + " |")
    add("")

    add("## 4. MAE / MFE（用来定止损位）")
    add("")
    add("| 持有(交易日) | MAE 中位% | MAE P10% | MFE 中位% | MFE P90% |")
    add("| --- | --- | --- | --- | --- |")
    for d in holds_days:
        sub = [x for x in details if x["持有日"] == d]
        if not sub:
            continue
        mae = pd.Series([x["MAE%"] for x in sub], dtype=float)
        mfe = pd.Series([x["MFE%"] for x in sub], dtype=float)
        add(f"| {d} | {mae.median():.2f} | {mae.quantile(0.10):.2f} | "
            f"{mfe.median():.2f} | {mfe.quantile(0.90):.2f} |")
    add("")
    add("MAE = 入场后的最大浮亏（负数），决定止损容差；MFE = 最大浮盈，"
        "决定止盈空间。**MAE 中位比平均收益更能说明「能不能拿得住」。**")
    add("")
    return "\n".join(L)


# ======================================================================
# 入口
# ======================================================================

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="三买点基线统计（点数 / 成功率 / 止损位）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--period", default="30min",
                   choices=list(BARS_PER_DAY.keys()))
    p.add_argument("--pool", type=Path, default=DEFAULT_POOL)
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    p.add_argument("--codes", default="", help="只统计指定代码（逗号分隔）")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--entry", choices=["ideal", "confirm"], default="ideal",
                   help="ideal=以三买点价格成交（上界）；confirm=等下一笔确认后开盘成交")
    p.add_argument("--stop-zg", action="store_true",
                   help="跌破所依据中枢的 zg 即止损（以 zg 成交）")
    p.add_argument("--holds", default="1,3,5,10,20", help="持有期（交易日，逗号分隔）")
    p.add_argument("--lookback", type=int, default=chan_points.DEFAULT_LOOKBACK)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--dump-trades", type=Path, default=None, help="逐点明细 CSV")
    a = p.parse_args(argv)

    holds_days = [int(x) for x in a.holds.split(",") if x.strip()]
    if a.codes:
        pool = [(normalize_code(c), "") for c in (x.strip() for x in a.codes.split(",")) if c]
    else:
        pool = load_pool(a.pool)
    if a.limit:
        pool = pool[: a.limit]

    started = datetime.now()
    print(f"标的池 {len(pool)} 只 | {a.period} | 入场 {a.entry} | "
          f"止损 {'zg' if a.stop_zg else '无'} | 持有 {holds_days}")

    rows: list[dict] = []
    details: list[dict] = []
    n_bars_total = n_days_total = 0

    for i, (code, name) in enumerate(pool, 1):
        klines = load_cached(code, a.period, a.cache_dir)
        if not klines:
            rows.append({"代码": code, "名称": name, "bar数": 0, "交易日": 0,
                         "笔": 0, "中枢": 0, "三买点": 0, "汇总": {}, "基准": {},
                         "错误": "无缓存"})
            print(f"[{i}/{len(pool)}] {code} {name}: ✗ 无缓存（先跑 fetch_min.py）",
                  flush=True)
            continue

        try:
            det, summ, res = eval_one(code, klines, a.period, holds_days=holds_days,
                                      entry=a.entry, stop_zg=a.stop_zg,
                                      lookback=a.lookback)
        except Exception as exc:
            rows.append({"代码": code, "名称": name, "bar数": len(klines), "交易日": 0,
                         "笔": 0, "中枢": 0, "三买点": 0, "汇总": {}, "基准": {},
                         "错误": f"{type(exc).__name__}: {exc}"})
            print(f"[{i}/{len(pool)}] {code} {name}: ✗ {exc}", flush=True)
            continue

        if res is None:
            rows.append({"代码": code, "名称": name, "bar数": len(klines), "交易日": 0,
                         "笔": 0, "中枢": 0, "三买点": 0, "汇总": {}, "基准": {},
                         "错误": "缠论计算返回空（K 线不足）"})
            print(f"[{i}/{len(pool)}] {code} {name}: ✗ K 线不足", flush=True)
            continue

        days = len({str(k.date)[:10] for k in klines})
        n_bars_total += len(klines)
        n_days_total += days

        closes = [float(k.close) for k in klines]
        base = {}
        for d in holds_days:
            rets = baseline_returns(closes, d * BARS_PER_DAY[a.period])
            if rets:
                s = pd.Series(rets)
                base[d] = {"mean": round(float(s.mean()), 3),
                           "median": round(float(s.median()), 3)}
        rows.append({
            "代码": code, "名称": name, "bar数": len(klines), "交易日": days,
            "笔": res.bi_count, "中枢": res.center_count,
            "三买点": summ[holds_days[0]]["三买点数"] if summ else 0,
            "汇总": summ, "基准": base,
        })
        details.extend(det)
        n_pt = rows[-1]["三买点"]
        print(f"[{i}/{len(pool)}] {code} {name}: 三买 {n_pt} 个", flush=True)

    name = (f"triple_buy_{a.period}_{a.entry}"
            + ("_stopzg" if a.stop_zg else "") + ".md")
    out = Path(a.out) if a.out else (ROOT / "outputs" / name)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(rows, details, period=a.period, pool_path=a.pool,
                                 entry=a.entry, stop_zg=a.stop_zg,
                                 holds_days=holds_days, started=started,
                                 n_bars_total=n_bars_total, n_days_total=n_days_total),
                   encoding="utf-8")

    if a.dump_trades:
        d = pd.DataFrame(details)
        dd = Path(a.dump_trades)
        dd.parent.mkdir(parents=True, exist_ok=True)
        d.to_csv(dd, index=False, encoding="utf-8-sig")
        print(f"逐点明细：{dd}（{len(d)} 行）")

    total_pts = sum(r.get("三买点", 0) for r in rows)
    print("\n" + "=" * 56)
    print(f"三买点合计 {total_pts} 个 | 报告：{out}")
    for d in holds_days:
        agg = [r["汇总"][d] for r in rows if r["汇总"].get(d, {}).get("n")]
        if not agg:
            continue
        n = sum(x["n"] for x in agg)
        wr = sum(x["胜率"] * x["n"] for x in agg) / n
        mean = sum(x["平均"] * x["n"] for x in agg) / n
        print(f"  持有 {d} 日：{n} 笔 | 胜率 {wr:.1%} | 平均 {mean:+.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
