# -*- coding: utf-8 -*-
"""几何买卖点的「确认滞后」诊断 —— 回测成交时点到底用了多少未来信息

    python scripts/diag_geom_lag.py                      # 全池，用日线缓存
    python scripts/diag_geom_lag.py --limit 10
    python scripts/diag_geom_lag.py --slice 3            # 抽样做切片重算交叉验证

为什么需要这份诊断
------------------
`chan_strategy._effective_day` 只把日线几何点顺延 **1 个交易日**生效，理由是
「笔终点落在 T 的收盘价上，T+1 才能下单」。这只处理了**第一层**未来函数 ——
价格要等收盘才知道。还有**第二层**没处理：

    一笔的终点要等**后续反向笔成型**才会被锁定。T 当天 czsc 给出的最后一笔
    仍在延伸，T 只是「暂定终点」；只有反向笔走出来，T 才不再是可能被改写的点。

所以「T+1 生效」很可能仍然早于真实的可行动时刻。本脚本量它，用两个互相独立的
口径：

**A. 笔终点确认滞后（全量，廉价）**
   对每个几何点取它所在笔 i，看**下一笔 i+1 的终点**落在几个交易日后。
   这是「笔 i 被反向笔确认」的**保守上界**（分型比整笔走得快，真实确认更早）。

**B. 切片重算一致性（抽样，昂贵）**
   只把数据喂到 t 重算几何点，看该点**最早在哪一天被锁定**（所在笔后面已经
   出现新笔、不再是 czsc 的暂定末端笔）—— 这正是文档给
   cxt_* 信号源做过、而几何源一次都没做过的那个验证（见
   `docs/STRATEGY_CHAN_MULTIFREQ.md` §3.3：信号源 5/5 一致）。

两个数一起读：A 给上界、B 给实测。`offset=1` 能把多少点覆盖住，就是回测
乐观程度的直接度量。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:               # 允许 `python scripts/diag_geom_lag.py` 直接跑
    sys.path.insert(0, str(ROOT))

from core import chan as chan_mod                          # noqa: E402
from core import chan_points                               # noqa: E402
from data.models import KLineData                          # noqa: E402
from scan_pool import load_pool                            # noqa: E402

DEFAULT_POOL = Path(__file__).with_name("pool_liquid50.txt")
DAILY_CACHE = ROOT / "outputs" / "cache_daily"
DEFAULT_DAYS = 1500


# ======================================================================
# 取数
# ======================================================================

def to_klines(df: pd.DataFrame, code: str) -> list[KLineData]:
    return [
        KLineData(code=code, date=str(r.date), open=float(r.open), high=float(r.high),
                  low=float(r.low), close=float(r.close), volume=int(r.volume),
                  period="daily")
        for r in df.itertuples(index=False)
    ]


def load_daily(cache_dir: Path, sym: str, days: int) -> tuple[list[KLineData], bool]:
    """优先吃 `eval_six_pulse.py` 落的日线缓存（同格式），没有再联网取"""
    f = Path(cache_dir) / f"{sym}_{days}.csv"
    if f.exists() and f.stat().st_size > 0:
        return to_klines(pd.read_csv(f, dtype={"date": str}), sym), True
    from data.market_data import fetch_kline
    return fetch_kline(sym, "daily", days=days), False


# ======================================================================
# A. 笔终点确认滞后
# ======================================================================

def point_lags(klines: list[KLineData]) -> list[dict]:
    """每个几何点 → 名义日期 / 确认日期 / 滞后交易日数

    ``confirm_dt`` 取**下一笔的终点**（保守上界）；最后一笔之后没有下一笔，
    说明该点尚未被确认，记为 None。
    """
    res = chan_mod.build(klines, "daily")
    if res is None:
        return []
    bis = chan_mod.bis(res)
    centers = chan_mod.centers(res)
    pts = chan_points.buy_sell_points(bis, centers)

    day_list = [k.date for k in klines]
    pos = {d: i for i, d in enumerate(day_list)}

    rows: list[dict] = []
    for p in pts:
        i = p["bi_idx"]
        nominal = str(p["dt"])[:10]
        if nominal not in pos:
            continue
        if i + 1 >= len(bis):
            rows.append({"kind": p["kind"], "dt": nominal,
                         "confirm_dt": None, "lag": None})
            continue
        confirm = str(bis[i + 1]["edt"])[:10]
        lag = (pos[confirm] - pos[nominal]) if confirm in pos else None
        rows.append({"kind": p["kind"], "dt": nominal,
                     "confirm_dt": confirm, "lag": lag})
    return rows


def summarize(rows: list[dict], kind: str) -> dict:
    lags = sorted(r["lag"] for r in rows if r["kind"] == kind and r["lag"] is not None)
    if not lags:
        return {"kind": kind, "n": 0}
    n = len(lags)
    return {
        "kind": kind, "n": n,
        "median": lags[n // 2],
        "mean": sum(lags) / n,
        "p90": lags[min(n - 1, int(n * 0.9))],
        "max": lags[-1],
        "min": lags[0],
        "le1": sum(1 for x in lags if x <= 1) / n,
    }


# ======================================================================
# B. 切片重算（只喂到 t）
# ======================================================================

def slice_first_seen(
    klines: list[KLineData],
    target_dt: str,
    kind: str,
    *,
    max_extend: int = 30,
) -> int | None:
    """只喂到 `target_dt + k` 重算，返回该点**首次被锁定**的 k

    ⚠️ 「出现」不等于「锁定」：czsc 的 `bi_list[-1]` 是**正在延伸的暂定笔**，
    它的终点随时会被改写。所以这里要求 ``bi_idx < len(bis) - 1`` —— 该点所在
    笔后面**已经出现新笔**，它才不再是暂定的。

    （实测茅台：笔终点 2026-08-24 在喂到 08-28 时就作为最后一笔出现，但直到
    喂到 **09-08** 才因新笔 `Up 08-24→09-04` 出现而锁定 —— 滞后 11 个交易日。
    只看「出现」会误判成 +1 天。）
    """
    day_list = [k.date for k in klines]
    if target_dt not in day_list:
        return None
    i0 = day_list.index(target_dt)

    for k in range(0, max_extend + 1):
        j = i0 + k
        if j >= len(day_list):
            break
        res = chan_mod.build(klines[: j + 1], "daily")
        if res is None:
            continue
        bis = chan_mod.bis(res)
        pts = chan_points.buy_sell_points(bis, chan_mod.centers(res))
        for p in pts:
            if (str(p["dt"])[:10] == target_dt and p["kind"] == kind
                    and p["bi_idx"] < len(bis) - 1):
                return k
    return None


def slice_check(
    klines: list[KLineData],
    rows: list[dict],
    *,
    kinds=("一买", "二买"),
    max_extend: int = 30,
    cap: int = 6,
) -> list[dict]:
    """对抽样点做切片重算，返回 ``[{kind, dt, offset=1 可见?, first_seen}]``"""
    picked = [r for r in rows if r["kind"] in kinds][:cap]
    out: list[dict] = []
    for r in picked:
        seen = slice_first_seen(klines, r["dt"], r["kind"], max_extend=max_extend)
        out.append({"kind": r["kind"], "dt": r["dt"],
                    "first_seen": seen,
                    "visible_at_offset1": (seen is not None and seen <= 1)})
    return out


# ======================================================================
# 报告
# ======================================================================

def render(kind_stats: list[dict], slices: list[dict], rows: list[dict],
           head: dict, args) -> str:
    lines: list[str] = []
    add = lines.append

    add("# 几何买卖点的「确认滞后」诊断")
    add("")
    add(f"- 标的：`{Path(args.pool).name}`，实扫 {head['codes']} 只"
        f"（成功 {head['ok']} 只）")
    add(f"- 数据：日线 {DEFAULT_DAYS} 根（`eval_six_pulse.py` 的缓存），"
        f"{head['start']} ~ {head['end']}")
    add("- 口径 A：`confirm_dt` = **下一笔的终点** ⇒ 保守上界（分型比整笔快）")
    add("- 口径 B：切片重算 —— 只喂到 t，看点最早在哪天出现 ⇒ 实测值")
    add("")

    add("## 1. 口径 A：笔终点确认滞后（交易日）")
    add("")
    add("| 类别 | 样本 | 最小 | 中位 | 均值 | P90 | 最大 | **滞后 ≤ 1 天的占比** |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for s in kind_stats:
        if not s["n"]:
            add(f"| {s['kind']} | 0 | -- | -- | -- | -- | -- | -- |")
            continue
        add(f"| {s['kind']} | {s['n']} | {s['min']} | **{s['median']}** | "
            f"{s['mean']:.1f} | {s['p90']} | {s['max']} | {s['le1']:.0%} |")
    add("")
    add("读法：`滞后 ≤ 1 天`那一列就是**当前 `offset=1` 能覆盖住的比例**。"
        "偏低说明「T+1 生效」这个口径系统性地早于真实可行动时刻。")
    add("")

    if slices:
        add("## 2. 口径 B：切片重算（抽样）")
        add("")
        add("只喂到 `t` 重算，找该点**首次被锁定**（所在笔后面已出现新笔、不再是暂定笔）")
        add("的那一天。`offset=1` 够用 = 滞后 ≤ 1 个交易日。")
        add("")
        add("| 类别 | 点日期 | 切片下首次锁定（交易日） | offset=1 够用？ |")
        add("| --- | --- | --- | --- |")
        for s in slices:
            seen = "--（窗口内未锁定）" if s["first_seen"] is None else f"+{s['first_seen']}"
            add(f"| {s['kind']} | {s['dt']} | {seen} | "
                f"{'✓' if s['visible_at_offset1'] else '✗'} |")
        add("")

    add("## 3. 逐点明细")
    add("")
    add("| 类别 | 名义日期（笔终点） | 确认日期（下一笔终点） | 滞后（交易日） |")
    add("| --- | --- | --- | --- |")
    for r in rows:
        cd = r["confirm_dt"] or "未确认（数据尾部）"
        lag = "--" if r["lag"] is None else str(r["lag"])
        add(f"| {r['kind']} | {r['dt']} | {cd} | {lag} |")
    add("")
    return "\n".join(lines)


# ======================================================================
# CLI
# ======================================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="几何买卖点的确认滞后诊断",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--pool", default=str(DEFAULT_POOL))
    p.add_argument("--cache-dir", default=str(DAILY_CACHE))
    p.add_argument("--days", type=int, default=DEFAULT_DAYS)
    p.add_argument("--limit", type=int, default=0, help="只跑前 N 只")
    p.add_argument("--slice", type=int, default=0,
                   help="对前 N 只做切片重算交叉验证（慢，建议 1~3）")
    p.add_argument("--out", default="", help="报告输出（默认 outputs/geom_lag.md）")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    pool = load_pool(Path(args.pool))
    if args.limit:
        pool = pool[: args.limit]
    if not pool:
        print("标的池为空")
        return 1

    all_rows: list[dict] = []
    head = {"codes": len(pool), "ok": 0, "start": "", "end": ""}
    failures: list[tuple[str, str]] = []
    slices: list[dict] = []

    for i, (code, nm) in enumerate(pool, 1):
        try:
            from core.chan_viz import normalize_code
            sym = normalize_code(code)
            klines, cached = load_daily(Path(args.cache_dir), sym, args.days)
        except Exception as exc:                     # noqa: BLE001 — 网络源
            failures.append((code, str(exc)))
            continue
        if not klines:
            failures.append((code, "无日线"))
            continue

        rows = point_lags(klines)
        all_rows.extend([{**r, "code": code} for r in rows])
        head["ok"] += 1
        if not head["start"]:
            head["start"], head["end"] = klines[0].date, klines[-1].date

        if args.slice and i <= args.slice:
            slices.extend(slice_check(klines, rows))
        n1 = sum(1 for r in rows if r["kind"] == "一买")
        n2 = sum(1 for r in rows if r["kind"] == "二买")
        print(f"  [{i}/{len(pool)}] {code} {nm} {'缓存' if cached else '取数'} "
              f"一买={n1} 二买={n2}")

    if not all_rows:
        print("没有任何点，无法诊断")
        return 1

    kind_stats = [summarize(all_rows, k) for k in
                  ("一买", "二买", "一卖", "二卖", "三买", "三卖")]

    report = render(kind_stats, slices, all_rows, head, args)
    out = Path(args.out) if args.out else ROOT / "outputs" / "geom_lag.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")

    print()
    for s in kind_stats:
        if s["n"]:
            print(f"  {s['kind']}: n={s['n']} 中位滞后={s['median']} 天 "
                  f"P90={s['p90']} 最大={s['max']} 滞后≤1天占比={s['le1']:.0%}")
    if failures:
        print(f"⚠️ 失败 {len(failures)} 只：" + ", ".join(c for c, _ in failures))
    print(f"报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
