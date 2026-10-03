# -*- coding: utf-8 -*-
"""历史几何诊断：全历史结构倒选突破与回抽，含未来信息。

必须显式 --retrospective 才运行。这不是可交易回测，也不估计实盘前瞻概率。
cross 也取决于事后中枢与突破候选，不能称为无未来函数。生产策略与因果
回放入口为 core.triple_buy / scripts/eval_daily_5min.py。
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
DEFAULT_POOL = ROOT / "scripts" / "pool_random500.txt"

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


def baseline(closes: list[float], hold_bars: int, opens=None) -> dict | None:
    """同池随机入场基准：每根 bar 进的持有收益（全体 bar，非抽样）"""
    n = len(closes)
    if hold_bars <= 0 or n <= hold_bars:
        return None
    vals = [(b / a - 1.0) * 100.0
            for a, b in zip((opens if opens is not None else closes)[: n - hold_bars], closes[hold_bars:n])
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

    _historical_returns(cands, times, opens, closes, holds, bpd, pos)

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
    base = {h: baseline(closes, h * bpd, opens) for h in holds}
    return cands, rate, rets, base


def _historical_returns(cands, times, opens, closes, holds, bpd, pos):
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


def _returns_table(rows, holds):
    from core.research_stats import metrics
    lines = ["## 事后收益诊断（不可作实盘收益）", "",
             "| 持有 | 口径 | n | 胜率 | 均值% | 中位% | 点权随机基准% | 配对超额% |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for hold in holds:
        for label, prefix in (("事后 cross", "X"), ("成功条件 confirm", "C")):
            base = {row["代码"]: row["基准"][hold]["mean"] for row in rows if row["基准"].get(hold)}
            points = [{"code": row["代码"], "ret": point[f"{prefix}{hold}"]}
                      for row in rows for point in row["候选明细"]
                      if point.get(f"{prefix}{hold}") is not None and row["代码"] in base]
            estimate = metrics(points, base)
            if estimate:
                summary = summarize([point["ret"] for point in points])
                lines.append(f"| {hold} | {label} | {estimate['n']} | {summary['胜率']:.1%} | "
                             f"{_fmt(estimate['pt_mean'])} | {_fmt(summary['中位'])} | "
                             f"{_fmt(estimate['base'])} | {_fmt(estimate['ex_pt'])} |")
    return lines + [""]


def _feature_tables(rows, holds):
    frame = pd.DataFrame([point for row in rows for point in row["候选明细"]])
    lines = ["## 事后特征分档", ""]
    if frame.empty:
        return lines
    for feature, description in FEATURES:
        buckets = bucket_table(frame, feature, holds)
        if not buckets:
            continue
        lines += [f"### {feature}", "", description, "",
                  "| 档 | 区间 | 样本 | 事后形成率 | " + " | ".join(f"X{h}%" for h in holds) + " |",
                  "| --- | --- | --- | --- | " + " | ".join("---" for _ in holds) + " |"]
        for bucket in buckets:
            rate = bucket["形成率"]
            values = " | ".join(_fmt(bucket.get(f"X{h}")) for h in holds)
            lines.append(f"| {bucket['档']} | {bucket['区间']} | {bucket['样本']} | "
                         f"{_fmt(rate * 100) if rate is not None else '--'}% | {values} |")
        lines.append("")
    return lines


def render_report(rows, *, period, holds, pool_path, started, skipped, pool_size):
    lines = [f"# 三买历史几何诊断（{period}）", "",
             "**仅事后诊断：候选、中枢及笔均使用完整历史。cross 与 confirm 都含未来信息。**",
             "形成率是事后结构样本的描述，不是突破当时可得的前瞻概率；禁止据此证明交易 alpha。", "",
             f"生成：{started:%Y-%m-%d %H:%M}；池：{pool_path.name}；有效 {len(rows)}/{pool_size}；持有 {holds}", "",
             "## 事后形成率", "", "| 代码 | 候选 | 成功 | 失败 | 未定 | 形成率 |",
             "| --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        counts = row["形成率"]
        rate = counts["形成率"]
        lines.append(f"| {row['代码']} | {counts['候选']} | {counts['成功']} | {counts['失败']} | "
                     f"{counts['未定']} | {_fmt(rate * 100) if rate is not None else '--'}% |")
    lines += ["", "失败/未纳入：" + str(skipped), ""]
    lines += _returns_table(rows, holds) + _feature_tables(rows, holds)
    lines += ["随机基准与各口径均用开盘入场、固定根数后收盘出场；先逐信号减该股基准再汇总。",
              "confirm 的分母只有事后成功候选。两组统计均不具备可交易意义。", ""]
    return "\n".join(lines)


# ======================================================================
# 入口
# ======================================================================

def _selected_pool(args):
    if args.codes:
        return [(normalize_code(code.strip()), "") for code in args.codes.split(",") if code.strip()]
    return load_pool(args.pool)


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
    p.add_argument("--retrospective", action="store_true", help="确认只做含未来信息的历史几何诊断")
    a = p.parse_args(argv)
    if not a.retrospective:
        p.error("仅允许 --retrospective 历史诊断；可观察策略请用 eval_daily_5min.py")

    holds = [int(x) for x in a.holds.split(",") if x.strip()]
    pool = _selected_pool(a)
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
