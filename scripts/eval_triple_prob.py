# -*- coding: utf-8 -*-
"""三买**反向统计**：用「突破候选」当分母，消掉循环论证

    python scripts/eval_triple_prob.py --period daily
    python scripts/eval_triple_prob.py --period daily --pool scripts/pool_liquid50.txt \
        --holds 5,10,20 --out outputs/triple_prob_daily.md

要解决的问题
------------
`eval_triple_buy.py` / `eval_daily_5min.py` 的分母是「**已知最终形成三买**的点」，
分子是「这些点之后赚钱的比例」。这**不是交易者面对的问题**：实盘站在「价格刚
突破中枢上沿」那一刻，你不知道后面会不会形成三买。条件里已经含了答案，
所以那类胜率天然偏高（循环论证）。

这里换成**前瞻口径**：

- **候选**（分母）= 一根向上笔向上突破**已完成中枢**的上沿（``high > zg``，
  且该中枢 ``edt <= 突破笔 edt``）。参照中枢取「突破笔终点之前最近的那个」；
  **一个中枢只认第一次有效突破**（与 `buy_sell_points` 的三买口径一致），
  否则趋势延续的后续向上笔会被重复数成新机会、把分母吹大。
- **成败**（分子）= 突破后**第一根向下笔**的回抽低点是否**不回到中枢内**：
  ``low > zg`` = **成三买**；``low <= zg`` = **失败**（被中枢拉回去）。
- 候选时刻**当下可判**：突破笔终点即候选成立时刻，不需要等后续笔。
  ⇒ 「形成率」= P(成三买 | 已突破)，才是交易者真正面对的概率。

⚠️ 与 `chan_points.buy_sell_points()` 的口径差异：那边是 ``for z in centers``
**逐中枢**扫描（同一根突破笔被多个中枢各判一次 ⇒ 重复产点，即 KI-010 同源）；
这里按**突破笔**唯一化。所以本报告的「三买数」会**略少于**正向脚本的去重前数字。

三种入场口径
------------
全部按「信号出现后的**第一根 bar 开盘价**」成交（A 股 T+1）：

| 口径 | 入场时点 | 有无未来函数 | 位置 |
| --- | --- | --- | --- |
| ``cross`` | 中枢收口后第一根**收盘价 > 上沿**的 bar 之后 | **无**（收盘即知） | 高（买在突破瞬间） |
| ``confirm`` | 回抽笔走完、确认未破中枢之后 | 有滞后（KI-009，等反向笔锁定） | 低（买在回调低点）|

``cross`` 是这套打法里**唯一没有未来函数**的入场，代价是买得高；``confirm``
就是现在拿去跑的「日线确认」口径。两者之差 = **确认的代价**。

特征分档（回答「什么时候概率大」）
----------------------------------
候选时刻就能算出的量：突破幅度、中枢宽度、突破笔涨幅、突破力度比
（突破笔 MACD 面积 ÷ 前一向上笔）、中枢笔数。按等频三分位分档看形成率。

⚠️ 统计口径
------------
- 按**点**统计，不是资金曲线；同一标的候选可密集出现、持有期重叠。
- 中枢由 `chan.centers()`（czsc）给出。中枢一旦收口，其 zg/zd 不再变化，
  所以「用 z 的 zg 判 t > z.edt 之后的行情」**不是**未来函数。
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

#: 级别 → 一个交易日多少根 bar（daily 单独算 1）
BPD = {"daily": 1, "1min": 240, "5min": 48, "15min": 16, "30min": 8, "60min": 4}

#: 分档用的候选特征（列名, 说明）
FEATURES = [
    ("突破幅度%", "突破笔高点高出上沿多少"),
    ("中枢宽度%", "(zg-zd)/zg，中枢越厚说明震荡越宽"),
    ("突破笔涨幅%", "突破笔自身 high/low-1"),
    ("力度比", "突破笔 MACD 面积 ÷ 前一向上笔（封顶 20）；>1 = 突破放量"),
    ("中枢笔数", "中枢由几根笔构成"),
]


# ======================================================================
# 读取
# ======================================================================

def load_cached(code: str, period: str, cache_dir: Path) -> list[KLineData] | None:
    """读 `fetch_min.py` 落的缓存 → KLineData；不存在则 None

    volume 用 coerce+fillna：baostock 在**停牌 bar** 会给空串，直接 ``int()`` 会崩。
    """
    f = Path(cache_dir) / f"{period}_{code}.csv"
    if not f.exists() or f.stat().st_size == 0:
        return None
    df = pd.read_csv(f)
    vol = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64")
    return [KLineData(code=code, date=str(r.dt), open=float(r.open), high=float(r.high),
                      low=float(r.low), close=float(r.close), volume=int(v), period=period)
            for r, v in zip(df.itertuples(), vol)]


def _ts(v) -> pd.Timestamp:
    return pd.Timestamp(str(v))


def last_center_at_or_before(centers, dt) -> dict | None:
    """``edt <= dt`` 的最后一个中枢

    比 `chan_points._last_center_before`（严格 ``<``）宽一格：中枢的最后一根
    延伸笔 ``edt`` 恰好等于突破笔 ``edt`` 时（"末中枢提前收口"），
    三买判定要求 ``b.edt >= z.edt`` ⇒ 这里也必须含相等。
    """
    target = _ts(dt)
    found = None
    for z in centers:                      # centers 已按 edt 升序
        if _ts(z["edt"]) <= target:
            found = z
        else:
            break
    return found


def hold_ret(opens: list[float], closes: list[float],
             entry_idx: int | None, hold_bars: int) -> float | None:
    if entry_idx is None:
        return None
    j = entry_idx + hold_bars
    if entry_idx >= len(opens) or j >= len(closes):
        return None
    a = opens[entry_idx]
    if not a or a <= 0:
        return None
    return (closes[j] / a - 1.0) * 100.0


def baseline(closes: list[float], hold_bars: int) -> dict | None:
    """同池随机入场基准：每根 bar 进的持有收益（全体 bar，非抽样）"""
    n = len(closes)
    if hold_bars <= 0 or n <= hold_bars:
        return None
    vals = [(b / a - 1.0) * 100.0
            for a, b in zip(closes[: n - hold_bars], closes[hold_bars:n])
            if a > 0 and b > 0]
    if not vals:
        return None
    s = pd.Series(vals, dtype=float)
    return {"n": len(vals), "mean": float(s.mean()), "median": float(s.median())}


# ======================================================================
# 候选
# ======================================================================

def find_candidates(bis, centers, *, bars=None) -> list[dict]:
    """每个「向上突破中枢上沿」的候选 → 成败判定 + 候选时刻特征"""
    if len(bis) < 3:
        return []

    areas = chan_points.bi_macd_areas(bis, bars)
    prev_up: dict[int, int] = {}           # 向上笔 index → 上一个向上笔 index
    last = None
    for i, b in enumerate(bis):
        if str(b["direction"]) == "Up":
            if last is not None:
                prev_up[i] = last
            last = i

    out: list[dict] = []
    seen_center: set[tuple[str, str]] = set()
    for j, b in enumerate(bis):
        if str(b["direction"]) != "Up":
            continue
        z = last_center_at_or_before(centers, b["edt"])
        if z is None:
            continue
        zkey = (str(_ts(z["sdt"])), str(_ts(z["edt"])))
        if zkey in seen_center:
            continue                        # 一个中枢只认第一次向上离开
        zg, zd = float(z["high"]), float(z["low"])
        high = float(b["high"])
        if high <= zg:
            continue
        seen_center.add(zkey)

        m = next((k for k in range(j + 1, len(bis))
                  if str(bis[k]["direction"]) == "Down"), None)
        if m is None:
            status = "未定"
        else:
            status = "成功" if float(bis[m]["low"]) > zg else "失败"

        pj = prev_up.get(j)
        a_j, a_p = (areas or {}).get(j), (areas or {}).get(pj)
        # 封顶 20：前一向上笔的 MACD 面积可以接近 0（横盘里的小笔），比值能飙到 1e4，
        # 直接把分档区间拉爆。>20 一律按 20 计。
        ratio = min(a_j / a_p, 20.0) if (a_j and a_p and a_p > 0) else None

        out.append({
            "bi_idx": j,
            "dt": _ts(b["edt"]),
            "中枢edt": _ts(z["edt"]),
            "zg": zg, "zd": zd,
            "突破高": high,
            "突破幅度%": (high / zg - 1.0) * 100.0,
            "中枢宽度%": (zg - zd) / zg * 100.0,
            "突破笔涨幅%": (high / float(b["low"]) - 1.0) * 100.0 if float(b["low"]) > 0 else None,
            "力度比": ratio,
            "中枢笔数": len(z.get("bis") or []),
            "状态": status,
            "回抽低": float(bis[m]["low"]) if m is not None else None,
            "回抽时刻": _ts(bis[m]["edt"]) if m is not None else None,
        })
    return out


# ======================================================================
# 单只
# ======================================================================

def eval_one(code: str, klines: list[KLineData], period: str, *,
             holds: list[int]) -> tuple[list[dict], dict, dict, dict]:
    """→ ``(候选明细, 形成率汇总, 收益汇总, 基准)``"""
    res = chan_mod.build(klines, period)
    if res is None:
        return [], {}, {}, {}

    bis = chan_mod.bis(res)
    centers = chan_mod.centers(res)
    cands = find_candidates(bis, centers, bars=klines)

    dts = [_ts(k.date) for k in klines]
    opens = [float(k.open) for k in klines]
    closes = [float(k.close) for k in klines]
    times = [str(k.date) for k in klines]
    # ⚠️ 键必须与 find_candidates 里 `_ts()` 的字符串形式一致：缓存 CSV 的 dt 可能是
    # "2020-01-02"，而 `str(Timestamp)` 是 "2020-01-02 00:00:00" —— 直接混用会**全部落空**
    # （表现：所有收益列都是空）。所以两边都过 `_ts()`。
    pos = {str(_ts(t)): i for i, t in enumerate(times)}
    bpd = BPD[period]

    for c in cands:
        # ---- cross 入场：中枢收口之后第一根「收盘站上上沿」的 bar ----
        i0 = pos.get(str(c["中枢edt"]))
        i_cross = None
        if i0 is not None:
            for k in range(i0 + 1, len(closes)):
                if closes[k] > c["zg"]:
                    i_cross = k
                    break
        e_x = i_cross + 1 if (i_cross is not None and i_cross + 1 < len(closes)) else None
        c["cross时刻"] = times[i_cross] if i_cross is not None else None

        # ---- confirm 入场：回抽笔确认未破中枢之后（只对成功的候选有定义） ----
        e_c = None
        if c["状态"] == "成功":
            i_m = pos.get(str(c["回抽时刻"]))
            if i_m is not None and i_m + 1 < len(closes):
                e_c = i_m + 1

        for h in holds:
            nb = h * bpd
            c[f"X{h}"] = hold_ret(opens, closes, e_x, nb)      # cross 口径
            c[f"C{h}"] = hold_ret(opens, closes, e_c, nb)      # confirm 口径

    n_ok = sum(1 for c in cands if c["状态"] == "成功")
    n_bad = sum(1 for c in cands if c["状态"] == "失败")
    n_und = sum(1 for c in cands if c["状态"] == "未定")
    formed = n_ok + n_bad
    rate = {"候选": len(cands), "成功": n_ok, "失败": n_bad, "未定": n_und,
            "形成率": (n_ok / formed) if formed else None}

    rets: dict[int, dict] = {}
    for h in holds:
        xs = [c[f"X{h}"] for c in cands if c.get(f"X{h}") is not None]
        cs = [c[f"C{h}"] for c in cands if c.get(f"C{h}") is not None]
        rets[h] = {"cross": summarize(xs), "confirm": summarize(cs)}
    base = {h: baseline(closes, h * bpd) for h in holds}
    return cands, rate, rets, base


# ======================================================================
# 报告
# ======================================================================

def _fmt(v, nd=2) -> str:
    if v is None:
        return "--"
    try:
        if pd.isna(v):
            return "--"
    except (TypeError, ValueError):
        pass
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def bucket_table(df: pd.DataFrame, feat: str, holds: list[int]) -> list[dict]:
    """按特征等频三分位分档 → 每档的形成率与 cross 收益

    ``duplicates="drop"`` 会在分位点重复时减少档数（小样本常见），所以档名
    按位置映射成 低/中/高，而不是硬写 3 个 label（那会抛 ValueError）。
    """
    d = df.dropna(subset=[feat])
    if len(d) < 12 or d[feat].nunique() < 3:
        return []
    try:
        lab = pd.qcut(d[feat], 3, duplicates="drop")
    except (ValueError, IndexError):
        return []
    cats = list(lab.cat.categories)
    if len(cats) < 2:
        return []
    if len(cats) == 3:
        name_of = dict(zip(cats, ["低", "中", "高"]))
    else:
        name_of = {cats[0]: "低", cats[-1]: "高"}
        for c in cats[1:-1]:
            name_of[c] = "中"

    rows = []
    for iv in cats:
        sub = d[lab == iv]
        ok = int((sub["状态"] == "成功").sum())
        bad = int((sub["状态"] == "失败").sum())
        rec = {"档": name_of[iv], "样本": len(sub),
               "区间": f"{sub[feat].min():.2f} ~ {sub[feat].max():.2f}",
               "形成率": (ok / (ok + bad)) if (ok + bad) else None}
        for h in holds:
            v = sub[f"X{h}"].dropna()
            rec[f"X{h}"] = float(v.mean()) if len(v) else None
        rows.append(rec)
    return rows


def render_report(rows: list[dict], *, period: str, holds: list[int],
                  pool_path: Path, started: datetime,
                  skipped: list[tuple[str, str]], pool_size: int) -> str:
    L: list[str] = []
    add = L.append
    add(f"# 三买**反向统计**（{period}）：突破之后，成三买的概率有多大")
    add("")
    add(f"- 生成时间：{started:%Y-%m-%d %H:%M}")
    add(f"- 标的池：`{pool_path.name}`（{len(rows)} / {pool_size} 只有效）")
    add(f"- 持有期（交易日）：{', '.join(str(h) for h in holds)}")
    add("")
    if skipped:
        add(f"- ⚠️ 未纳入的 {len(skipped)} 只：" +
            "；".join(f"`{c}`（{w}）" for c, w in skipped))
        add("")

    # ---- 汇总 ----
    allc = [c for r in rows for c in r["候选明细"]]
    df = pd.DataFrame(allc) if allc else pd.DataFrame()
    n_cand = sum(r["形成率"]["候选"] for r in rows)
    n_ok = sum(r["形成率"]["成功"] for r in rows)
    n_bad = sum(r["形成率"]["失败"] for r in rows)
    n_und = sum(r["形成率"]["未定"] for r in rows)
    formed = n_ok + n_bad

    add("## 1. 突破 → 三买 的形成率（无循环论证）")
    add("")
    add(f"- 候选（向上突破中枢上沿）：**{n_cand}** 次")
    if formed:
        add(f"- 其中 **{n_ok}** 次形成三买（回抽不回中枢）、**{n_bad}** 次失败（被拉回中枢）"
            f" ⇒ **形成率 {n_ok/formed:.1%}**")
    add(f"- 数据末尾尚未走完回抽笔、状态未定：{n_und} 次（不计入形成率）")
    add("")
    add("读法：这是**前瞻概率** —— 站在突破那一刻，后面成三买的机会有多大。"
        "实盘要猜的就是它，分母里不再含答案。")
    add("")

    # ---- 特征分档 ----
    add("## 2. 特征分档：什么条件下概率大")
    add("")
    if df.empty:
        add("（无候选）")
    else:
        for feat, desc in FEATURES:
            bt = bucket_table(df, feat, holds)
            if not bt:
                continue
            add(f"### {feat}")
            add("")
            add(f"_{desc}_")
            add("")
            add("| 档 | 区间 | 样本 | 形成率 | " +
                " | ".join(f"cross {h}日%" for h in holds) + " |")
            add("| --- | --- | --- | --- | " + " | ".join("---" for _ in holds) + " |")
            for r in bt:
                add(f"| {r['档']} | {r['区间']} | {r['样本']} | {_fmt(r['形成率']*100, 1) if r['形成率'] is not None else '--'} | "
                    + " | ".join(_fmt(r.get(f'X{h}')) for h in holds) + " |")
            add("")

    # ---- 收益对比 ----
    add("## 3. 两种入场口径的收益（vs 同池随机入场）")
    add("")
    add("| 持有(交易日) | 口径 | 样本 | 胜率 | 平均% | 中位% | 随机基准% | 超额% |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for h in holds:
        for side, key in (("cross（突破即买，无未来函数）", "cross"),
                          ("confirm（回抽确认后买，滞后）", "confirm")):
            agg = [r["汇总"][h][key] for r in rows
                   if r["汇总"].get(h, {}).get(key, {}).get("n")]
            if not agg:
                continue
            n = sum(s["n"] for s in agg)

            def wavg(k):
                pairs = [(s[k], s["n"]) for s in agg
                         if s.get(k) is not None and not pd.isna(s[k])]
                return (sum(v * w for v, w in pairs) / sum(w for _, w in pairs)) if pairs else None
            bv = [r["基准"][h]["mean"] for r in rows
                  if r.get("基准", {}).get(h, {}).get("mean") is not None]
            bm = sum(bv) / len(bv) if bv else None
            mv = wavg("平均")
            ex = f"{mv - bm:+.2f}" if (mv is not None and bm is not None) else "--"
            add(f"| {h} | {side} | {n} | {wavg('胜率'):.1%} | {_fmt(mv)} | {_fmt(wavg('中位'))} | "
                f"{_fmt(bm)} | {ex} |")
    add("")
    add("- `cross`：中枢收口后**第一根收盘站上上沿**的 bar 之后开盘买入 —— 收盘即知，"
        "**没有未来函数**，是这套打法唯一严格可交易的入场。")
    add("- `confirm`：回抽笔走完、确认未破中枢之后买入 —— 位置更低，但要等反向笔锁定"
        "（KI-009 的滞后），且**分母只含最终成功的候选**（失败的那些根本等不到这个信号）。")
    add("")

    # ---- 逐标的 ----
    add("## 4. 逐标的")
    add("")
    add("| 代码 | 候选 | 成功 | 失败 | 未定 | 形成率 |")
    add("| --- | --- | --- | --- | --- | --- |")
    for r in rows:
        s = r["形成率"]
        add(f"| {r['代码']} | {s['候选']} | {s['成功']} | {s['失败']} | {s['未定']} | "
            f"{_fmt(s['形成率']*100, 1) if s['形成率'] is not None else '--'} |")
    add("")
    add("---")
    add(f"生成命令：`python scripts/eval_triple_prob.py {' '.join(sys.argv[1:])}`")
    return "\n".join(L) + "\n"


# ======================================================================
# 入口
# ======================================================================

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="三买反向统计（突破候选 → 形成率）",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--period", default="daily", choices=list(BPD.keys()))
    p.add_argument("--pool", type=Path, default=DEFAULT_POOL)
    p.add_argument("--codes", default="")
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    p.add_argument("--holds", default="5,10,20")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--out", default="")
    a = p.parse_args(argv)

    holds = [int(x) for x in a.holds.split(",") if x.strip()]
    if a.codes:
        pool = [(normalize_code(c), "") for c in (x.strip() for x in a.codes.split(",")) if c]
    else:
        pool = load_pool(a.pool)
    if a.limit:
        pool = pool[: a.limit]
    if not pool:
        print("标的池为空", file=sys.stderr)
        return 2

    out = Path(a.out) if a.out else ROOT / "outputs" / f"triple_prob_{a.period}.md"
    started = datetime.now()
    print(f"三买反向统计：{len(pool)} 只 | {a.period} | 持有 {holds} | 缓存 {a.cache_dir}")

    rows: list[dict] = []
    skipped: list[tuple[str, str]] = []
    for i, (code, name) in enumerate(pool, 1):
        klines = load_cached(code, a.period, a.cache_dir)
        if not klines:
            skipped.append((code, "缓存缺失"))
            print(f"[{i}/{len(pool)}] {code} ✗ 缓存缺失", flush=True)
            continue
        try:
            cands, rate, rets, base = eval_one(code, klines, a.period, holds=holds)
        except Exception as exc:
            skipped.append((code, f"{type(exc).__name__}: {exc}"))
            print(f"[{i}/{len(pool)}] {code} ✗ {exc}", flush=True)
            continue
        if not cands:
            skipped.append((code, "无突破候选（结构不足）"))
            print(f"[{i}/{len(pool)}] {code} 跳过（无候选）", flush=True)
            continue
        rows.append({"代码": code, "名称": name, "候选明细": cands,
                     "形成率": rate, "汇总": rets, "基准": base})
        fr = rate["形成率"]
        print(f"[{i}/{len(pool)}] {code} {name}: 候选 {rate['候选']} | "
              f"成功 {rate['成功']} / 失败 {rate['失败']}"
              + (f" | 形成率 {fr:.1%}" if fr is not None else ""), flush=True)

    if not rows:
        print("无有效样本", file=sys.stderr)
        return 1

    text = render_report(rows, period=a.period, holds=holds, pool_path=a.pool,
                         started=started, skipped=skipped, pool_size=len(pool))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")

    n_ok = sum(r["形成率"]["成功"] for r in rows)
    n_bad = sum(r["形成率"]["失败"] for r in rows)
    print("\n" + "=" * 56)
    print(f"候选 {sum(r['形成率']['候选'] for r in rows)} | 成功 {n_ok} | 失败 {n_bad}"
          + (f" | 形成率 {n_ok/(n_ok+n_bad):.1%}" if (n_ok + n_bad) else ""))
    print(f"报告：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
