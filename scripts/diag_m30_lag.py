# -*- coding: utf-8 -*-
"""30 分钟级别几何点的确认滞后 —— 策略真正的那一级扳机

    python scripts/diag_m30_lag.py                    # 全池，口径 A
    python scripts/diag_m30_lag.py --slice 4          # 抽样切片重算（口径 B）

为什么单独量 30 分钟级别
-----------------------
`diag_geom_lag.py` 量的是**日线**一买/二买的确认滞后（+4~+16 个交易日）。
但策略的入场扳机是 **30 分钟二买**（`chan_strategy` 在日线二买 ±10 交易日窗口内
找最近的次级别买点）。两个级别的滞后差一个数量级：

- 日线：一笔要跨 4~5 根日线 K，确认滞后按**交易日**算（个位数到十几）；
- 30 分钟：一天 8 根 bar，同样「4~5 根确认」只折合 **0.5~1 个交易日**。

所以「滞后会不会把买点废掉」取决于你**等哪一级确认**，必须把这个数拿出来。

口径 A（本脚本默认）：取该点所在笔的**下一笔终点**作确认时刻 —— 保守上界。
口径 B（`--slice`）：只喂到 t 重算，看该点最早何时被锁定（要求其后已有新笔）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import chan as chan_mod                       # noqa: E402
from core import chan_points                            # noqa: E402
from scan_pool import DEFAULT_CACHE, load_klines, load_pool   # noqa: E402

DEFAULT_POOL = Path(__file__).with_name("pool_liquid50.txt")
BARS_PER_DAY = 8            # A 股 30 分钟：9:30~11:30 + 13:00~15:00


def m30_lags(klines: list) -> list[dict]:
    """每个 30 分钟几何点 → 名义时刻 / 下一笔终点 / 滞后 bar 数"""
    res = chan_mod.build(klines, "30min")
    if res is None:
        return []
    bis = chan_mod.bis(res)
    pts = chan_points.buy_sell_points(bis, chan_mod.centers(res))
    pos = {k.date: i for i, k in enumerate(klines)}

    rows: list[dict] = []
    for p in pts:
        i = p["bi_idx"]
        nominal = str(p["dt"])
        if i + 1 >= len(bis):
            rows.append({"kind": p["kind"], "dt": nominal, "lag": None})
            continue
        confirm = str(bis[i + 1]["edt"])
        lag = (pos[confirm] - pos[nominal]) if confirm in pos else None
        rows.append({"kind": p["kind"], "dt": nominal, "confirm": confirm, "lag": lag})
    return rows


def slice_first_seen(klines: list, target_dt: str, kind: str,
                     *, max_extend: int = 48) -> int | None:
    """只喂到 t+k 根 bar 重算，返回该点**首次被锁定**的 k（bar 数）

    与日线版同样的坑：「出现」≠「锁定」—— 要求 ``bi_idx < len(bis)-1``。
    """
    times = [k.date for k in klines]
    if target_dt not in times:
        return None
    i0 = times.index(target_dt)
    for k in range(0, max_extend + 1):
        j = i0 + k
        if j >= len(times):
            break
        res = chan_mod.build(klines[: j + 1], "30min")
        if res is None:
            continue
        bis = chan_mod.bis(res)
        for p in chan_points.buy_sell_points(bis, chan_mod.centers(res)):
            if (str(p["dt"]) == target_dt and p["kind"] == kind
                    and p["bi_idx"] < len(bis) - 1):
                return k
    return None


def stat(vals: list[int]) -> dict:
    if not vals:
        return {"n": 0}
    v = sorted(vals)
    n = len(v)
    return {"n": n, "min": v[0], "median": v[n // 2],
            "p90": v[min(n - 1, int(n * 0.9))], "max": v[-1],
            "mean": round(sum(v) / n, 2),
            "le8": round(sum(1 for x in v if x <= BARS_PER_DAY) / n, 3)}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pool", type=Path, default=DEFAULT_POOL)
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    p.add_argument("--cache-days", type=int, default=3)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--slice", type=int, default=0, help="抽样 N 只做切片重算")
    p.add_argument("--out", type=Path, default=ROOT / "outputs" / "geom_lag_m30.md")
    a = p.parse_args(argv)

    pool = load_pool(a.pool)[: a.limit] if a.limit else load_pool(a.pool)

    all_rows: list[dict] = []
    slice_rows: list[dict] = []
    for i, (code, name) in enumerate(pool, 1):
        try:
            klines, _ = load_klines(code, cache_dir=a.cache_dir, cache_days=a.cache_days)
        except Exception as exc:
            print(f"[{i}/{len(pool)}] {code} 取数失败：{exc}", flush=True)
            continue
        rows = m30_lags(klines)
        for r in rows:
            r["code"] = code
        all_rows.extend(rows)
        print(f"[{i}/{len(pool)}] {code} {name}: 30分点 {len(rows)}", flush=True)

        if a.slice and len(slice_rows) // 3 < a.slice:
            picked = [r for r in rows if r["kind"] == "二买"][:3]
            for r in picked:
                k = slice_first_seen(klines, r["dt"], r["kind"])
                slice_rows.append({"code": code, "dt": r["dt"], "kind": r["kind"],
                                   "lag_bars": r["lag"], "first_seen": k})

    kinds = sorted({r["kind"] for r in all_rows})
    stats = {k: stat([r["lag"] for r in all_rows
                      if r["kind"] == k and r["lag"] is not None]) for k in kinds}

    lines: list[str] = []
    add = lines.append
    add("# 30 分钟级别几何点的确认滞后")
    add("")
    add(f"- 标的池：{len(pool)} 只（30 分钟，约 1970 根 / 247 交易日）")
    add(f"- 口径 A：该点所在笔的**下一笔终点** —— 保守上界（分型比整笔走得快）")
    add(f"- 一天 {BARS_PER_DAY} 根 30 分钟 bar ⇒ 交易日 = bar 数 / {BARS_PER_DAY}")
    add("")
    add("## 1. 滞后分布（口径 A）")
    add("")
    add("| 类别 | 样本 | 最小 bar | 中位 bar | P90 bar | 最大 bar | 中位(交易日) | ≤1 天占比 |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for k, s in stats.items():
        if not s.get("n"):
            continue
        add(f"| {k} | {s['n']} | {s['min']} | {s['median']} | {s['p90']} | {s['max']} | "
            f"{s['median'] / BARS_PER_DAY:.2f} | {s['le8']:.0%} |")
    add("")
    add("读法：`中位(交易日)` 就是「30 分钟信号发出后，多久才能确认它成立」的"
        "**交易日**量级。与日线级别（中位 6.5 个交易日）对比，就是"
        "「等哪一级确认」的代价差。")
    add("")

    if slice_rows:
        add("## 2. 切片重算抽样校验（口径 B）")
        add("")
        add("| 代码 | 点时刻 | 口径 A (bar) | 切片首次锁定 (bar) = 不早于 A |")
        add("| --- | --- | --- | --- |")
        for s in slice_rows:
            add(f"| {s['code']} | {s['dt']} | {s['lag_bars']} | "
                f"{'--（窗口内未锁定）' if s['first_seen'] is None else s['first_seen']} |")
        add("")

    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text("\n".join(lines), encoding="utf-8")

    print("\n" + "=" * 52)
    for k, s in stats.items():
        if s.get("n"):
            print(f"{k}: 样本 {s['n']} | 中位 {s['median']} bar "
                  f"({s['median'] / BARS_PER_DAY:.2f} 交易日) | P90 {s['p90']} bar")
    print(f"报告：{a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
