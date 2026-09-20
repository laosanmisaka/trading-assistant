# -*- coding: utf-8 -*-
"""「在场率不低，为什么收益差那么多」— 把时间口径和资金口径拆开

    python scripts/diag_exposure.py
    python scripts/diag_exposure.py --pool scripts/pool_liquid500.txt
    python scripts/diag_exposure.py --exit ma --ma-exit 20

问题
----
六脉分级出口在随机池上：**时间在场率 73.5%**，平均区间收益却只有 +11.23%，
而同池买入持有 +54.10%。在场时间占了大半，收益只有 1/5 —— 差在哪？

三个候选，逐一对账
------------------
1. **时间在场 ≠ 资金在场**：分级出口破 MA5 就减半，只留半个仓位，所以
   「有仓位的时间」很长，但「满仓等效的在场」短得多。
2. **在场质量**：在场日的平均日收益 vs 空仓日的平均日收益 —— 若空仓日
   反而更赚，说明择时把好日子躲掉了（负 alpha）。
3. **费用 + 减仓方向**：减半写在「破 MA5」上，上涨途中回调也触发 ⇒
   上涨段只拿半仓、下跌段等破 MA20 才走 ⇒ 截断盈利、放行亏损。

做法
----
逐日重建**仓位比例序列** `w_t`（T 收盘信号、T+1 生效），与逐日收益 `r_t`
对账，输出：

- `exp_time`  时间在场率 = P(w>0)
- `exp_cap`   **资金在场率** = E(w)      ← 关键，才是真的"投进去多少"
- `ret_strat`  策略收益（= ∏(1 + w_t·r_t)）
- `ret_hold`   同区间买入持有
- `ret_scaled` **恒定仓位基线** = 把仓位一直压在 E(w) 上的买入持有
  ⇒ 策略 vs 它的差 = 择时与费用的净贡献（负就是白折腾）
- 在场日均 r / 空仓日均 r / 半仓日均 r / 满仓日均 r
- 涨幅最大的 2% 交易日、跌幅最大的 2% 交易日上的平均仓位
  ⇒ 直接看「大涨日手里有几成货」
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

from core import six_pulse                                   # noqa: E402
from core.backtest.strategy import Action                    # noqa: E402
from eval_six_pulse import DAILY_CACHE, load_daily           # noqa: E402
from scan_pool import load_pool                              # noqa: E402


def position_series(daily, strat) -> np.ndarray:
    """逐日仓位比例（0~1），T 日信号 **T+1 生效**，与 `T+1 开盘成交` 口径一致"""
    by_date: dict[str, list] = {}
    for s in strat.generate_signals(daily):
        by_date.setdefault(s.date, []).append(s)

    n = len(daily)
    pos = np.zeros(n)
    cur = 0.0
    for i, k in enumerate(daily):
        for s in by_date.get(k.date, []):
            w = float(getattr(s, "weight", 1.0) or 1.0)
            if s.action == Action.BUY:
                cur = min(cur + w, 1.0)
            elif s.action == Action.SELL:
                cur = 0.0 if w >= 1.0 else max(cur - w, 0.0)
        pos[i] = cur
    return np.concatenate([[0.0], pos[:-1]])                 # 滞后一日生效


def diag_one(code: str, daily, strat, start: str | None) -> dict | None:
    n = len(daily)
    i0 = 0
    if start:
        i0 = next((i for i, k in enumerate(daily) if k.date >= start), n)
    if n - i0 < 30:
        return None

    closes = np.array([k.close for k in daily], dtype=float)
    r = np.zeros(n)
    r[1:] = closes[1:] / closes[:-1] - 1.0

    w = position_series(daily, strat)
    ww, rr = w[i0:], r[i0:]
    if ww.size < 30:
        return None

    on = ww > 1e-9
    half = on & (ww < 0.999)
    full = ww >= 0.999

    k = max(10, int(0.02 * rr.size))
    up = np.argsort(rr)[::-1][:k]
    dn = np.argsort(rr)[:k]

    return {
        "code": code,
        "bars": int(rr.size),
        "exp_time": float(on.mean()),
        "exp_cap": float(ww.mean()),
        "exp_cap_on": float(ww[on].mean()) if on.any() else 0.0,
        "ret_strat": float(np.prod(1.0 + ww * rr) - 1.0),
        "ret_hold": float(closes[-1] / closes[i0] - 1.0) if closes[i0] > 0 else 0.0,
        "ret_scaled": float(np.prod(1.0 + ww.mean() * rr) - 1.0),
        "r_all": float(rr.mean()),
        "r_on": float(rr[on].mean()) if on.any() else 0.0,
        "r_off": float(rr[~on].mean()) if (~on).any() else 0.0,
        "r_half": float(rr[half].mean()) if half.any() else 0.0,
        "r_full": float(rr[full].mean()) if full.any() else 0.0,
        "w_bigup": float(ww[up].mean()),
        "w_bigdn": float(ww[dn].mean()),
        "corr": float(np.corrcoef(ww, rr)[0, 1]) if ww.std() > 1e-12 else 0.0,
    }


def _p(v: float, nd: int = 2) -> str:
    return f"{v * 100:+.{nd}f}%"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="在场率 vs 收益 gap 诊断")
    p.add_argument("--pool", default=str(Path(__file__).with_name("pool_random500.txt")))
    p.add_argument("--days", type=int, default=1500)
    p.add_argument("--start", default="2025-01-01")
    p.add_argument("--entry-ma", type=int, default=20)
    p.add_argument("--exit", default="tiered", choices=["tiered", "ma"])
    p.add_argument("--ma-exit", type=int, default=20)
    p.add_argument("--cache-days", type=int, default=0)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--out", default="outputs/exposure_gap.md")
    args = p.parse_args(argv)

    kw = dict(entry_ma=args.entry_ma or None, trade_from=args.start or None)
    if args.exit == "ma":
        kw.update(exit_mode="ma", ma_exit=args.ma_exit)
    strat = six_pulse.SixPulseStrategy(**kw)

    pool = load_pool(Path(args.pool))
    if args.limit:
        pool = pool[: args.limit]

    print(f"池 {Path(args.pool).name}（{len(pool)} 只）| 出口 {args.exit} | "
          f"买点过滤 {args.entry_ma} | 区间 {args.start} 起", flush=True)

    rows: list[dict] = []
    for i, (code, nm) in enumerate(pool, 1):
        try:
            daily, _ = load_daily(code, args.days, cache_dir=DAILY_CACHE,
                                  cache_days=args.cache_days, polite_sleep=0.0)
        except Exception:                                     # noqa: BLE001
            continue
        d = diag_one(code, daily, strat, args.start or None)
        if d:
            d["name"] = nm
            rows.append(d)
        if i % 100 == 0:
            print(f"  [{i}/{len(pool)}] 已算 {len(rows)} 只", flush=True)

    if not rows:
        print("无样本")
        return 1

    df = pd.DataFrame(rows)
    m = df.mean(numeric_only=True)

    lines: list[str] = []
    add = lines.append
    add("# 在场率 vs 收益：gap 拆解")
    add("")
    add(f"- 池：`{Path(args.pool).name}`（有效 {len(df)} 只）")
    add(f"- 口径：出口 `{args.exit}`、买点过滤 MA{args.entry_ma}、统计区间 {args.start} 起")
    add(f"- 策略收益 = ∏(1 + w_t·r_t)，w 为逐日仓位比例（T+1 生效）")
    add("")
    add("## 1. 时间在场 ≠ 资金在场")
    add("")
    add("| 口径 | 数值 |")
    add("| --- | --- |")
    add(f"| **时间在场率** P(w>0) | **{m['exp_time']:.1%}** |")
    add(f"| **资金在场率** E(w) | **{m['exp_cap']:.1%}** |")
    add(f"| 有仓之日内的平均仓位 | {m['exp_cap_on']:.1%} |")
    add("")
    add(f"⇒ 有仓位的时间占 {m['exp_time']:.1%}，但**平均只投进去 {m['exp_cap']:.1%} 的钱**；"
        f"在场那些天平均也只有 {m['exp_cap_on']:.1%} 仓位 —— 剩下的都是减半后的半仓状态。")
    add("")
    add("## 2. 收益对账")
    add("")
    add("| 项目 | 平均区间收益 |")
    add("| --- | --- |")
    add(f"| 策略（实际） | **{_p(m['ret_strat'])}** |")
    add(f"| 买入持有（同池同区间） | {_p(m['ret_hold'])} |")
    add(f"| **恒定仓位基线**（仓位锁在 E(w)={m['exp_cap']:.1%} 的买入持有） | {_p(m['ret_scaled'])} |")
    add(f"| 择时+费用净贡献（策略 − 恒定仓位基线） | **{_p(m['ret_strat'] - m['ret_scaled'])}** |")
    add("")
    eff = m["ret_strat"] / m["exp_cap"] if m["exp_cap"] else 0.0
    add(f"- 把收益按投入资金归一：策略 **{_p(eff)}**/单位资金 vs 买入持有 "
        f"{_p(m['ret_hold'])}/单位资金")
    add("")
    add("## 3. 在哪几天手里有货")
    add("")
    add("| 交易日类型 | 平均日收益 | 平均仓位 |")
    add("| --- | --- | --- |")
    add(f"| 全部交易日 | {_p(m['r_all'], 3)} | {m['exp_cap']:.1%} |")
    add(f"| 有仓之日 | {_p(m['r_on'], 3)} | {m['exp_cap_on']:.1%} |")
    add(f"| 空仓之日 | {_p(m['r_off'], 3)} | 0.0% |")
    add(f"| 半仓之日 | {_p(m['r_half'], 3)} | ~50% |")
    add(f"| 满仓之日 | {_p(m['r_full'], 3)} | ~100% |")
    add(f"| **涨幅最大 2% 的日子** | — | **{m['w_bigup']:.1%}** |")
    add(f"| **跌幅最大 2% 的日子** | — | **{m['w_bigdn']:.1%}** |")
    add("")
    add(f"- 仓位与日收益的相关性 corr(w, r) = **{m['corr']:+.3f}**"
        f"（>0 = 涨时仓位高，择时加分；<0 = 涨时反而空/半仓）")
    add("")
    add("## 4. 逐标的分布")
    add("")
    add("| 分位 | 时间在场 | 资金在场 | 策略 | 买入持有 |")
    add("| --- | --- | --- | --- | --- |")
    for q in (0.1, 0.25, 0.5, 0.75, 0.9):
        add(f"| P{int(q*100)} | {df['exp_time'].quantile(q):.1%} "
            f"| {df['exp_cap'].quantile(q):.1%} "
            f"| {_p(df['ret_strat'].quantile(q))} "
            f"| {_p(df['ret_hold'].quantile(q))} |")
    add("")

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")

    print()
    print(f"有效标的 {len(df)} 只")
    print(f"时间在场 {m['exp_time']:.1%} | 资金在场 {m['exp_cap']:.1%} | 有仓日仓位 {m['exp_cap_on']:.1%}")
    print(f"策略 {_p(m['ret_strat'])} | 买入持有 {_p(m['ret_hold'])} | "
          f"恒定仓位基线 {_p(m['ret_scaled'])} | 择时净贡献 {_p(m['ret_strat']-m['ret_scaled'])}")
    print(f"在场日均 {_p(m['r_on'],3)} | 空仓日均 {_p(m['r_off'],3)} | "
          f"半仓日均 {_p(m['r_half'],3)} | 满仓日均 {_p(m['r_full'],3)}")
    print(f"大涨日仓位 {m['w_bigup']:.1%} | 大跌日仓位 {m['w_bigdn']:.1%} | corr {m['corr']:+.3f}")
    print(f"报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
