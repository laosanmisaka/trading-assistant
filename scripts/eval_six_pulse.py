# -*- coding: utf-8 -*-
"""「六脉神剑」策略批量评估 —— 默认 520 只池 / 2025-01-01 起

    python scripts/eval_six_pulse.py                     # 默认口径（全套诊断）
    python scripts/eval_six_pulse.py --no-sweep          # 跳过出口对照扫（快）
    python scripts/eval_six_pulse.py --start ""          # 跑全历史（2020-07 起）
    python scripts/eval_six_pulse.py --entry-ma 0        # 关掉买点均线过滤
    python scripts/eval_six_pulse.py --pool scripts/pool_liquid50.txt   # 换回小池
    python scripts/eval_six_pulse.py --codes sh600519,sz000858
    python scripts/eval_six_pulse.py --out outputs/six_pulse.md

做什么
------
1. 读标的池（默认 `scripts/pool_liquid500.txt`，520 只）
2. 取日线（新浪源、**前复权**）并**落盘缓存**到 `outputs/cache_daily/`
3. 逐只跑 `core.six_pulse.SixPulseStrategy` + `core.backtest.engine`，
   给出轮级 / 笔级胜率、总收益 / 最大回撤 / 盈亏比，并与**买入持有**对照
4. 三项诊断（回答"亏在哪"）：
   - **A 指标共线性**：六项多头占比、两两相关、实际共振率 vs 独立假设乘积
   - **B 买点前瞻收益**：所有共振首日后 5/10/20 日收益 ⇒ 买点是否在追高
   - **C 出口对照扫**：分级出口 / 单线 MA5·10·20·30 / 固定持有 10·20·40 日
     ⇒ 买点错还是出口紧

⚠️ 口径（数字怎么读）
---------------------
- 每只标的**独立 100 万满仓**（整手买入），汇总时**等权平均** —— 这是
  "同一策略在 N 只票上分别满仓"的结果，**不是**组合资金曲线，收益不可相加。
- 初始资金 100 万是必需的：茅台一手 ≈ 15 万，10 万资金时 engine 因整手约束
  买不进任何一手，会**静默跳过**该股全部信号（被误读成"无信号"）。
- 佣金万 2.5（双边、最低 5 元）+ 印花税千 1（仅卖出）。
- T 日**收盘**出信号，**T+1 开盘**成交（两端对称，无未来函数）。
- 卖出默认走**分级出口**（破 MA5 卖半 / 站回 MA10 回补 / 破 MA20 清仓）。
  分级出口下一轮持仓会拆成多笔 ⇒ **跨出口比较看「轮级」**（`merge_stats`
  的 `episodes` / `ep_win_rate`），笔级胜率只在自己口径内可比。
- **统计区间**默认从 `--start`（2025-01-01）起：更早的日线只用来预热指标与
  均线，**不下单、不统计**（策略侧由 `SixPulseStrategy(trade_from=...)` 保证）。
  `--start ""` 可跑全历史。
- **买点过滤**默认 `--entry-ma 20`：共振首日**必须收盘站上 MA20** 才开仓。
  背景见 `docs/STRATEGY_SIX_PULSE.md` §3.4 —— 不过滤时 28% 的买点进场当天
  就已跌破 MA20 离场线，56% 的轮次在 3 日内被扫出。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:               # 允许 `python scripts/eval_six_pulse.py` 直接跑
    sys.path.insert(0, str(ROOT))

from core import chan_viz, six_pulse                        # noqa: E402
from core.backtest.engine import BacktestEngine             # noqa: E402
from data.models import KLineData                           # noqa: E402
from scan_pool import load_pool                             # noqa: E402

DEFAULT_POOL = Path(__file__).with_name("pool_liquid500.txt")
DAILY_CACHE = ROOT / "outputs" / "cache_daily"
DEFAULT_CAPITAL = 1_000_000.0
DEFAULT_START = "2025-01-01"
HORIZONS = (5, 10, 20)


# ======================================================================
# 取数（带落盘缓存）
# ======================================================================

def _cache_file(cache_dir: Path, sym: str, days: int) -> Path:
    return Path(cache_dir) / f"{sym}_{days}.csv"


def _dump_cache(path: Path, daily: list[KLineData]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({
        "date": [k.date for k in daily],
        "open": [k.open for k in daily],
        "high": [k.high for k in daily],
        "low": [k.low for k in daily],
        "close": [k.close for k in daily],
        "volume": [k.volume for k in daily],
    }).to_csv(path, index=False)


def _to_klines(df: pd.DataFrame, code: str) -> list[KLineData]:
    return [
        KLineData(code=code, date=str(r.date), open=float(r.open), high=float(r.high),
                  low=float(r.low), close=float(r.close), volume=int(r.volume),
                  period="daily")
        for r in df.itertuples(index=False)
    ]


def load_daily(
    code: str,
    days: int,
    *,
    cache_dir: Path = DAILY_CACHE,
    use_cache: bool = True,
    cache_days: int = 0,
    attempts: int = 3,
    polite_sleep: float = 1.0,
) -> tuple[list[KLineData], bool]:
    """取一只标的的日线 → ``(klines, 是否走了缓存)``

    缓存判据：文件存在、非空、且 mtime 距今不超过 `cache_days` 天
    （默认 0 = 只认今天）。日线当天不变，跨日复查用 `--cache-days N`，
    前提是**中间没有新交易日**。
    """
    sym = chan_viz.normalize_code(code)
    f = _cache_file(cache_dir, sym, days)

    if use_cache and f.exists() and f.stat().st_size > 0:
        age = (date.today() - datetime.fromtimestamp(f.stat().st_mtime).date()).days
        if age <= cache_days:
            return _to_klines(pd.read_csv(f, dtype={"date": str}), sym), True

    from data.market_data import fetch_kline

    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            daily = fetch_kline(sym, "daily", days=days)
            if not daily:
                raise RuntimeError("数据源正常应答但无日线（停牌或代码不存在）")
            _dump_cache(f, daily)
            if polite_sleep:
                time.sleep(polite_sleep)
            return daily, False
        except Exception as exc:                     # noqa: BLE001 — 网络源，重试
            last_exc = exc
            if attempt < attempts:
                time.sleep(2.0 * attempt)
    raise RuntimeError(f"取数失败：{last_exc}")


# ======================================================================
# 单只评估
# ======================================================================

def eval_one(code: str, daily: list[KLineData], strategy, capital: float,
             start: str | None = None) -> dict:
    """跑一只 → 结果行 dict（含笔级、区间级与买入持有基线）

    `start`（`"YYYY-MM-DD"`）：**统计区间起点**。数据仍全量喂给策略作指标预热，
    策略自身通过 `trade_from` 保证起点前不开仓 ⇒ 起点前资金恒为初始值，
    `total_return` / `max_drawdown` 天然就是起点后的成绩。
    只有 `bars`（在场时间占比的分母）与 `buy_hold`（基线）需要按 `start` 重算 ——
    否则 6 年分母会把 1.7 年的在场时间摊薄。
    """
    engine = BacktestEngine(initial_capital=capital)
    rep = engine.run_on_data(strategy, code, daily)

    idx = {k.date: i for i, k in enumerate(daily)}
    i0 = 0
    if start:
        i0 = next((i for i, k in enumerate(daily) if k.date >= start), len(daily))
        if i0 >= len(daily):
            i0 = len(daily) - 1                    # 区间内无数据：退化到最后一根
    holds = [
        idx[t.exit_date] - idx[t.entry_date]
        for t in rep.trades
        if t.entry_date in idx and t.exit_date in idx
    ]
    first, last = daily[i0].close, daily[-1].close
    return {
        "code": code,
        "bars": len(daily) - i0,
        "start": daily[i0].date,
        "end": daily[-1].date,
        "trades": rep.total_trades,
        "win_rate": rep.win_rate,
        "avg_pct": rep.avg_profit_pct,
        "total_return": rep.total_return,
        "max_dd": rep.max_drawdown,
        "profit_factor": rep.profit_factor,
        "avg_hold": (sum(holds) / len(holds)) if holds else 0.0,
        "hold_sum": sum(holds),
        "buy_hold": (last / first - 1.0) if first else 0.0,
        "open_position": rep.open_position,
        "_trades": rep.trades,
    }


def merge_stats(rows: list[dict]) -> dict:
    """把逐标的行合并成池级口径

    - **笔级**：所有标的的 trades 合并（胜率、平均每笔、盈亏比）
    - **轮级（episode）**：按「一轮持仓」归并。全进全出时一轮 = 一笔；
      分级出口（减半→回补→清仓）会把一轮拆成多笔，**笔级胜率会被拆细失真**，
      轮级才是「这一轮赚没赚」。对比不同出口规则时要看轮级。
    - **区间级**：逐标的收益率**等权平均**（策略 vs 买入持有）
    """
    trades = [t for r in rows for t in r["_trades"]]
    wins = [t for t in trades if t.profit > 0]
    losses = [t for t in trades if t.profit <= 0]
    gain = sum(t.profit for t in wins)
    loss = abs(sum(t.profit for t in losses))
    holds = [r["avg_hold"] for r in rows if r["trades"] > 0]

    # 轮级归并：episode 编号是每只标的各自从 1 开始的，必须带上 code
    episodes: dict[tuple, list] = {}
    for r in rows:
        for t in r["_trades"]:
            episodes.setdefault((r["code"], t.episode), []).append(t)
    ep_vals = list(episodes.values())
    ep_returns: list[float] = []
    for grp in ep_vals:
        cost = sum(t.quantity * t.entry_price for t in grp)
        if cost > 0:
            ep_returns.append(sum(t.profit for t in grp) / cost)
    ep_win = sum(1 for grp in ep_vals if sum(t.profit for t in grp) > 0)

    scanned = [r for r in rows if r["trades"] > 0]
    n = len(rows) or 1
    return {
        "codes": len(rows),
        "codes_with_trades": len(scanned),
        "trades": len(trades),
        "win_rate": (len(wins) / len(trades)) if trades else 0.0,
        "avg_pct": (sum(t.profit_pct for t in trades) / len(trades)) if trades else 0.0,
        "profit_factor": (gain / loss) if loss > 0 else float("inf"),
        "total_gain": gain,
        "total_loss": loss,
        "episodes": len(ep_vals),
        "ep_win_rate": (ep_win / len(ep_vals)) if ep_vals else 0.0,
        "ep_avg_pct": (sum(ep_returns) / len(ep_returns)) if ep_returns else 0.0,
        "avg_return": sum(r["total_return"] for r in rows) / n,
        "avg_buy_hold": sum(r["buy_hold"] for r in rows) / n,
        "avg_dd": sum(r["max_dd"] for r in rows) / n,
        "max_dd": max((r["max_dd"] for r in rows), default=0.0),
        "avg_hold": (sum(holds) / len(holds)) if holds else 0.0,
        "win_codes": sum(1 for r in rows if r["total_return"] > 0),
        # 在场时间占比 —— 用来看"收益是择时挣的还是在场时间挣的"
        "exposure": (sum(r["hold_sum"] for r in rows) / sum(r["bars"] for r in rows))
        if rows else 0.0,
    }


# ======================================================================
# 诊断
# ======================================================================

def collinearity(frames: list[pd.DataFrame], start: str | None = None
                 ) -> tuple[pd.DataFrame, float, float]:
    """六项指标的共线性 —— 返回 `(拼接后的布尔表, 实际共振率, 独立假设乘积)`

    实际共振率远高于独立假设乘积 ⇒ 六项并不独立，共振并不稀有。
    `start` 非空时只统计该日期起的样本（回测区间口径）。
    """
    if start:
        frames = [f[f["date"] >= start] for f in frames]
        frames = [f for f in frames if len(f)]
    sub = pd.concat([f[list(six_pulse.PULSE_NAMES)] for f in frames], ignore_index=True)
    ints = sub.astype(int)
    actual = float((ints.sum(axis=1) == 6).mean())
    independent = float(ints.mean().prod())
    return ints, actual, independent


def forward_returns(frames: list[pd.DataFrame], horizons=HORIZONS,
                    start: str | None = None) -> dict:
    """共振买点后 h 日收益，以及**同标的任意日**的同口径基准

    两端都用「T+1 开盘买入、T+1+h 收盘卖出」，基准是同一只标的上所有可比较
    起点的同口径收益均值 ⇒ 两者之差即**择时带来的超额**（≈0 就说明没有 alpha）。

    `frames` 为 `six_pulse.compute_indicators()` 的输出（含 OHLC 与信号列）。
    `start` 非空时起点不早于该日期（回测区间口径）。
    返回 ``{h: {"n", "codes", "mean", "median", "pos", "base_mean", "excess"}}``；
    均值与基准按**逐标的等权**，中位/为正占比按合并样本。
    """
    all_sig: dict[int, list[float]] = {h: [] for h in horizons}
    per_code: dict[int, list[tuple[float, float]]] = {h: [] for h in horizons}

    for df in frames:
        closes = df["close"].to_numpy(dtype=float)
        opens = df["open"].to_numpy(dtype=float)
        buy = df["buy_signal"].to_numpy()
        dates = df["date"].tolist()
        n = len(df)
        i0 = six_pulse.WARMUP
        if start:
            i0 = next((i for i, d in enumerate(dates) if d >= start), n)
            i0 = max(i0, six_pulse.WARMUP)
        for h in horizons:
            sig: list[float] = []
            base: list[float] = []
            for i in range(i0, n - 1):
                j = i + 1 + h
                if j >= n:
                    break
                entry = opens[i + 1]
                if entry <= 0:
                    continue
                r = closes[j] / entry - 1.0
                base.append(r)
                if bool(buy[i]):
                    sig.append(r)
            if sig and base:
                all_sig[h].extend(sig)
                per_code[h].append((float(np.mean(sig)), float(np.mean(base))))

    out: dict[int, dict] = {}
    for h in horizons:
        pairs = per_code[h]
        if not pairs:
            out[h] = {"n": 0}
            continue
        vals = np.asarray(all_sig[h], dtype=float)
        sig_mean = float(np.mean([p[0] for p in pairs]))
        base_mean = float(np.mean([p[1] for p in pairs]))
        out[h] = {
            "n": len(vals), "codes": len(pairs),
            "mean": sig_mean, "median": float(np.median(vals)),
            "pos": float((vals > 0).mean()),
            "base_mean": base_mean, "excess": sig_mean - base_mean,
        }
    return out


def exit_sweep(pool_data: list[tuple[str, list[KLineData]]], capital: float,
               start: str | None = None, entry_ma: int | None = 20) -> list[dict]:
    """出口对照扫 —— 买点固定，只换出口规则（买点过滤与区间起点保持一致）"""
    variants = [
        ("**分级：MA5 半 / 站回 MA10 补 / MA20 全**", dict(exit_mode="tiered")),
        ("分级但不回补：MA5 半 / MA20 全", dict(exit_mode="tiered", reentry=False)),
        ("单线 MA5", dict(exit_mode="ma", ma_exit=5)),
        ("单线 MA10", dict(exit_mode="ma", ma_exit=10)),
        ("单线 MA20", dict(exit_mode="ma", ma_exit=20)),
        ("单线 MA30", dict(exit_mode="ma", ma_exit=30)),
        ("固定持有 10 日", dict(hold_days=10)),
        ("固定持有 20 日", dict(hold_days=20)),
        ("固定持有 40 日", dict(hold_days=40)),
    ]
    rows: list[dict] = []
    for label, kw in variants:
        full = dict(kw, entry_ma=entry_ma, trade_from=start)
        strat = six_pulse.SixPulseStrategy(**full)
        per_code = [eval_one(code, daily, strat, capital, start=start)
                    for code, daily in pool_data]
        st = merge_stats(per_code)
        st["label"] = label
        rows.append(st)
    return rows


# ======================================================================
# 报告
# ======================================================================

def _pct(v: float, nd: int = 2) -> str:
    if v == float("inf"):
        return "∞"
    return f"{v * 100:+.{nd}f}%"


def _pf(v: float) -> str:
    return "∞" if v == float("inf") else f"{v:.2f}"


def render_report(rows: list[dict], frames: list[pd.DataFrame], names: dict[str, str],
                  head: dict, fwd: dict, sweep: list[dict], args) -> str:
    st = merge_stats(rows)
    lines: list[str] = []
    add = lines.append

    add("# 六脉神剑策略评估")
    add("")
    add(f"- 标的池：`{Path(args.pool).name}`（{st['codes']} 只）")
    add(f"- 数据：日线 {args.days} 根、**前复权**、新浪源"
        f"（{head['start']} ~ {head['end']}）")
    if args.start:
        add(f"- **统计区间：{head['start']} 起** —— {args.start} 之前的数据"
            f"仅用于指标与均线预热，不下单、不统计（`trade_from`）")
    add(f"- 资金口径：每只**独立 {args.capital:,.0f} 元满仓**（整手），汇总等权平均")
    add("- 费用：佣金万 2.5（双边、最低 5 元）、印花税千 1（卖出）")
    if args.entry_ma:
        add(f"- 买点：六指标共振首日 **且收盘站上 MA{args.entry_ma}**"
            f"（T 收盘确认）→ **T+1 开盘买入**")
    else:
        add("- 买点：六指标共振首日（**无均线过滤**，T 收盘确认）→ **T+1 开盘买入**")
    add("- 卖点：**分级出口**（T 收盘确认）→ **T+1 开盘成交**")
    add("  - 收盘破 **MA5** → 卖一半；")
    add("  - 已减半、且**先跌破过 MA10 又站回** → 买回一半（回补）；")
    add("  - 收盘破 **MA20** → 全部卖出；")
    add("  - MA20 优先于其余两档。旧口径「破 MA10 全清」见 §4 对照。")
    add("")

    add("## 1. 汇总")
    add("")
    add("| 指标 | 策略 | 买入持有（基线） |")
    add("| --- | --- | --- |")
    add(f"| 平均区间收益（等权） | **{_pct(st['avg_return'])}** | {_pct(st['avg_buy_hold'])} |")
    add(f"| 收益为正的标的 | {st['win_codes']} / {st['codes']} | — |")
    add(f"| 平均最大回撤 | {st['avg_dd']:.2%} | — |")
    add(f"| 最大回撤（单只最差） | {st['max_dd']:.2%} | — |")
    add(f"| **在场时间占比** | {st['exposure']:.1%} | 100% |")
    add("")
    add(f"**轮级口径**（{st['episodes']} 轮持仓）：胜率 **{st['ep_win_rate']:.1%}**、"
        f"平均每轮 **{_pct(st['ep_avg_pct'])}**。"
        f"分级出口会把一轮拆成多笔（减半 → 回补 → 清仓），"
        f"所以**对比不同出口要看轮级**，笔级胜率会被拆细。")
    add("")
    add(f"**笔级口径**（{st['codes_with_trades']} 只标的有交易、共 {st['trades']} 笔）："
        f"胜率 {st['win_rate']:.1%}、平均每笔 {_pct(st['avg_pct'])}、"
        f"盈亏比 {_pf(st['profit_factor'])}、平均持有 {st['avg_hold']:.1f} 个交易日。")
    add("")
    add(f"⚠️ 逐只独立满仓、等权平均 ⇒ **不是组合资金曲线**，收益不可相加。"
        f"平均持有 {st['avg_hold']:.1f} 日意味着单只一年换手约 "
        f"{252 / st['avg_hold']:.0f} 次，费用按双边 0.15% 量级计每年磨掉不少。")
    add("")

    # ---- 诊断 A ----
    ints, actual, independent = collinearity(frames, start=args.start or None)
    add("## 2. 诊断 A：六项指标共线性（共振到底稀不稀有）")
    add("")
    add("| 指标 | 多头天数占比 |")
    add("| --- | --- |")
    for name in six_pulse.PULSE_NAMES:
        add(f"| {name} | {ints[name].mean():.1%} |")
    add("")
    add(f"- **实际六项共振占比：{actual:.1%}**")
    add(f"- 若六项相互独立，应有占比：{independent:.2%}")
    add(f"- 倍数：**{actual / independent:.0f}×**"
        if independent > 0 else "- 独立假设乘积为 0，无法比较")
    add("")
    add("两两相关（同向率）：")
    add("")
    add("| | " + " | ".join(six_pulse.PULSE_NAMES) + " |")
    add("| --- |" + " --- |" * len(six_pulse.PULSE_NAMES))
    corr = ints.corr()
    for a in six_pulse.PULSE_NAMES:
        add(f"| {a} | " + " | ".join(f"{corr.loc[a, b]:.2f}" for b in six_pulse.PULSE_NAMES) + " |")
    add("")

    # ---- 诊断 B ----
    add("## 3. 诊断 B：买点有没有 alpha（共振买点 vs 同标的任意日）")
    add("")
    add("两端同口径：T+1 开盘买入、T+1+h 收盘卖出。**基准 = 同一只标的所有可比较"
        "起点的同口径收益均值**，超额 = 买点后均值 − 基准均值。")
    add("")
    add("| 持有 | 样本 | 买点后均值 | 中位 | 为正占比 | 任意日起基准 | **超额** |")
    add("| --- | --- | --- | --- | --- | --- | --- |")
    for h in HORIZONS:
        d = fwd.get(h) or {}
        if not d.get("n"):
            add(f"| {h} 日 | 0 | -- | -- | -- | -- | -- |")
            continue
        add(f"| {h} 日 | {d['n']} | {_pct(d['mean'])} | {_pct(d['median'])} "
            f"| {d['pos']:.1%} | {_pct(d['base_mean'])} | **{_pct(d['excess'])}** |")
    add("")

    # ---- 诊断 C ----
    if sweep:
        add("## 4. 诊断 C：出口对照扫（买点固定，只换卖出口）")
        add("")
        add("| 出口规则 | 轮数 | **轮胜率** | 轮均收益 | 平均区间收益 | 在场时间 | 最大单只回撤 |")
        add("| --- | --- | --- | --- | --- | --- | --- |")
        for s in sweep:
            add(f"| {s['label']} | {s['episodes']} | {s['ep_win_rate']:.1%} "
                f"| {_pct(s['ep_avg_pct'])} | {_pct(s['avg_return'])} "
                f"| {s['exposure']:.1%} | {s['max_dd']:.1%} |")
        add("")
        add("读法：以**轮胜率 / 轮均收益 / 平均区间收益**为主比较项（笔级受"
            "「一轮拆几笔」影响，跨出口不可比）。注意「出口越松、持有越久，成绩越"
            "靠近买入持有」这个基线关系 —— 若某个方向能显著超过买入持有，才说明"
            "这套出口真的在创造价值。")
        add("")

    # ---- 逐标的 ----
    nxt = 5 if sweep else 4
    add(f"## {nxt}. 逐标的明细（按区间收益排序）")
    add("")
    add("| 代码 | 名称 | 笔数 | 胜率 | 平均每笔 | 区间收益 | 最大回撤 | 平均持有 | 买入持有 |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in sorted(rows, key=lambda x: x["total_return"], reverse=True):
        nm = names.get(r["code"], "")
        add(f"| {r['code']} | {nm} | {r['trades']} | {r['win_rate']:.0%} | {_pct(r['avg_pct'])} "
            f"| {_pct(r['total_return'])} | {r['max_dd']:.1%} | {r['avg_hold']:.0f} 日 "
            f"| {_pct(r['buy_hold'])} |")
    add("")

    add(f"## {nxt + 1}. 口径与限制")
    add("")
    add("- **不是组合回测**：逐只独立满仓、等权平均；同一时点多只出信号时，"
        "真实组合做不到同时满仓。要看资金曲线得用 `core.backtest.engine` "
        "接组合层（截至本轮未做）。")
    add("- **区间可截断**：`--days` 控制取数长度（预热用），`--start` 控制**统计起点**。"
        "两者都会改变结果；`--start 2025-01-01` 之后只有一个多月的市场样本，"
        "**样本量大减、绝对数字别外推**。")
    add("- **买点过滤是样本内试出来的**：`entry_ma=20` 的阈值来自同一批标的的"
        "诊断（`outputs/exit_chase.md`），方向可信、幅度不可当承诺。")
    add("- **前复权数据**：新浪 `stock_zh_a_daily(adjust=\"qfq\")`，"
        "历史价格会随分红送股变化，跨日复跑数字可能微调。")
    add("- 未处理涨跌停无法成交、停牌、退市；整手约束下高价股的资金利用率偏低。")
    add("- 六项指标口径严格照 `lmsj.txt` 原文，**未做任何参数寻优**。")
    add("")
    return "\n".join(lines)


# ======================================================================
# CLI
# ======================================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="六脉神剑策略批量评估",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--pool", default=str(DEFAULT_POOL), help="标的池文件")
    p.add_argument("--days", type=int, default=1500, help="每只取多少根日线（默认 1500）")
    p.add_argument("--capital", type=float, default=DEFAULT_CAPITAL, help="单只初始资金")
    p.add_argument("--codes", default="", help="只跑指定代码（逗号分隔）")
    p.add_argument("--limit", type=int, default=0, help="只跑前 N 只（调试用）")
    p.add_argument("--cache-dir", default=str(DAILY_CACHE), help="日线缓存目录")
    p.add_argument("--cache-days", type=int, default=0, help="吃 N 天内的旧缓存（默认 0=只认今天）")
    p.add_argument("--no-cache", action="store_true", help="忽略缓存，强制重新取数")
    p.add_argument("--sleep", type=float, default=1.0, help="取数后的间隔秒数")
    p.add_argument("--attempts", type=int, default=3, help="单只取数重试次数")
    p.add_argument("--no-sweep", action="store_true", help="跳过出口对照扫")
    p.add_argument("--start", default=DEFAULT_START,
                   help=f"统计区间起点 YYYY-MM-DD（此前数据仅预热），默认 {DEFAULT_START}；"
                        f"传空字符串 = 跑全历史")
    p.add_argument("--entry-ma", type=int, default=20,
                   help="买点过滤均线周期，默认 20；0 = 关闭过滤")
    p.add_argument("--out", default="", help="报告输出路径（默认 outputs/six_pulse.md）")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    pool = load_pool(Path(args.pool))
    if args.codes:
        want = {chan_viz.normalize_code(c) for c in args.codes.split(",") if c.strip()}
        pool = [x for x in pool if x[0] in want]
    if args.limit:
        pool = pool[: args.limit]
    if not pool:
        print("标的池为空")
        return 1

    names = {code: nm for code, nm in pool}
    pool_data: list[tuple[str, list[KLineData]]] = []
    rows: list[dict] = []
    frames: list[pd.DataFrame] = []
    failures: list[tuple[str, str]] = []
    head = {"start": "", "end": ""}

    strat = six_pulse.SixPulseStrategy(entry_ma=args.entry_ma or None,
                                       trade_from=args.start or None)
    print(f"标的池 {len(pool)} 只 | 日线 {args.days} 根 | 资金 {args.capital:,.0f} | "
          f"出口 分级：MA5 半 / 站回 MA10 补 / MA20 全 | "
          f"买点过滤 {('MA%d' % args.entry_ma) if args.entry_ma else '关闭'} | "
          f"区间 {args.start or '全部'}")

    for i, (code, nm) in enumerate(pool, 1):
        try:
            daily, cached = load_daily(code, args.days, cache_dir=Path(args.cache_dir),
                                       use_cache=not args.no_cache,
                                       cache_days=args.cache_days,
                                       attempts=args.attempts, polite_sleep=args.sleep)
        except Exception as exc:                       # noqa: BLE001
            failures.append((code, str(exc)))
            print(f"  [{i}/{len(pool)}] {code} {nm} 取数失败：{exc}")
            continue

        row = eval_one(code, daily, strat, args.capital, start=args.start or None)
        rows.append(row)
        pool_data.append((code, daily))
        frames.append(six_pulse.compute_indicators(six_pulse.to_frame(daily)))
        if not head["start"]:
            head["start"], head["end"] = row["start"], row["end"]
        flag = "缓存" if cached else "取数"
        print(f"  [{i}/{len(pool)}] {code} {nm} {flag} {row['bars']}根 "
              f"笔数={row['trades']} 区间收益={row['total_return']:+.2%}", flush=True)

    if not rows:
        print("没有任何标的数据，无法出报告")
        return 1

    print("诊断：买点前瞻收益 …", flush=True)
    fwd = forward_returns(frames, start=args.start or None)

    sweep: list[dict] = []
    if not args.no_sweep:
        print("诊断：出口对照扫（9 种出口规则，只走本地缓存）…", flush=True)
        sweep = exit_sweep(pool_data, args.capital, start=args.start or None,
                           entry_ma=args.entry_ma or None)

    report = render_report(rows, frames, names, head, fwd, sweep, args)
    out = Path(args.out) if args.out else ROOT / "outputs" / "six_pulse.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")

    st = merge_stats(rows)
    print()
    print(f"汇总：{st['codes']} 只（{st['codes_with_trades']} 只有交易）/ "
          f"{st['episodes']} 轮 {st['trades']} 笔 | "
          f"轮胜率 {st['ep_win_rate']:.1%} 轮均 {st['ep_avg_pct']:+.2%} | "
          f"平均区间收益 {st['avg_return']:+.2%}（买入持有 {st['avg_buy_hold']:+.2%}）")
    if failures:
        print(f"⚠️ 取数失败 {len(failures)} 只：" + ", ".join(c for c, _ in failures))
    print(f"报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
