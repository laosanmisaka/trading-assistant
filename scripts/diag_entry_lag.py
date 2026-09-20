"""确认滞后对策略成绩的影响 —— 把入场日往后推 L 个交易日，重算逐笔收益

背景（KI-009）：几何买卖点的 `dt` 是**笔终点**，而笔终点要等后续反向笔成型
才被锁定。实测确认滞后 +4 ~ +16 个交易日（中位 ≈ 6.5），而策略当前只顺延 1 天
（`offset=1`）。问题：滞后会不会把买点废掉？

本脚本**不动任何判定逻辑**，只回答「同一批买点，如果晚 L 天才能确认并成交，
成绩会怎样」。做法：

1. 读 `scan_pool.py --dump-trades` 导出的逐笔明细（含成交日 / 成交价 / 收益率）；
2. 由 `卖价 = 成交价 × (1 + 收益率)` 反推卖价，保证 L=0 时复现原成绩；
3. 新成交价 = 原成交价 × open(成交日+L) / open(成交日)，即承受 L 天的价格位移；
4. 若确认时点已经越过卖点日 → 这笔**在实盘根本不该建仓**（确认时已被卖出条件
   打掉），记为「错过」，按不做处理（收益 0）。

输出两个口径：
- **口径 A（仅可成交笔）**：忽略错过的，看剩下这些笔的质量；
- **口径 B（含错过的机会成本，更接近实盘）**：错过的按 0 收益计入分母。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def load_daily(cache_dir: Path, code: str, suffix: str = "_1500") -> pd.DataFrame:
    """读日线（30 分钟合成）：date / open / high / low / close / volume"""
    path = cache_dir / f"{code}{suffix}.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    df = pd.read_csv(path)
    df["date"] = df["date"].astype(str).str[:10]
    return df.sort_values("date").reset_index(drop=True)


def _idx_of(df: pd.DataFrame, day: str) -> int | None:
    hit = df.index[df["date"] == day[:10]]
    return int(hit[0]) if len(hit) else None


def eval_trade(row: dict, df: pd.DataFrame, lag: int) -> dict:
    """给一笔交易在延迟 lag 个交易日下的结果"""
    entry_day = str(row["成交日"])[:10]
    ret = row.get("收益率%")
    if pd.isna(ret) or str(ret) == "":
        return {"状态": "未平仓", "收益": None}

    i0 = _idx_of(df, entry_day)
    if i0 is None:
        return {"状态": "成交日不在日线", "收益": None}

    p0 = float(row["成交价"])
    sell_day = str(row.get("卖点日", ""))[:10]
    p_sell = p0 * (1 + float(ret) / 100.0)      # 反推卖价

    j = i0 + lag
    if j >= len(df):
        return {"状态": "数据不足", "收益": None}
    new_entry_day = df["date"].iloc[j]

    # 确认时点已越过卖点日 → 实盘不会建仓
    if sell_day and new_entry_day >= sell_day:
        return {"状态": "错过", "收益": 0.0, "确认日": new_entry_day}

    ratio = float(df["open"].iloc[j]) / float(df["open"].iloc[i0])
    p_new = p0 * ratio
    return {
        "状态": "成交",
        "收益": round((p_sell - p_new) / p_new * 100, 3),
        "确认日": new_entry_day,
        "成本抬高%": round((ratio - 1) * 100, 3),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--trades", type=Path, default=ROOT / "outputs" / "trades_geom.csv")
    p.add_argument("--cache-dir", type=Path, default=ROOT / "outputs" / "cache_daily")
    p.add_argument("--lags", default="0,1,2,3,5,8,10,15,20")
    p.add_argument("--out", type=Path, default=ROOT / "outputs" / "entry_lag_impact.md")
    a = p.parse_args(argv)

    trades = pd.read_csv(a.trades)
    lags = [int(x) for x in a.lags.split(",") if x.strip()]

    daily: dict[str, pd.DataFrame] = {}
    for code in sorted(set(trades["代码"])):
        try:
            daily[code] = load_daily(a.cache_dir, code)
        except FileNotFoundError:
            pass

    detail: list[dict] = []
    rows: list[dict] = []
    for lag in lags:
        hits = []
        for _, t in trades.iterrows():
            df = daily.get(t["代码"])
            if df is None:
                continue
            r = eval_trade(t.to_dict(), df, lag)
            r.update({"代码": t["代码"], "lag": lag, "成交日": t["成交日"],
                      "原收益%": t.get("收益率%")})
            detail.append(r)
            hits.append(r)

        done = [h for h in hits if h["状态"] == "成交"]
        missed = [h for h in hits if h["状态"] == "错过"]
        if not done and not missed:
            continue

        rets = [h["收益"] for h in done]
        # 口径 B：错过的按 0 计入分母（机会成本）
        rets_b = [h["收益"] for h in done] + [0.0] * len(missed)

        rows.append({
            "滞后(交易日)": lag,
            "可成交笔数": len(done),
            "错过笔数": len(missed),
            "胜率%(仅成交)": round(len([r for r in rets if r > 0]) / len(rets) * 100, 1) if rets else None,
            "平均收益%(仅成交)": round(sum(rets) / len(rets), 2) if rets else None,
            "组合平均收益%(含错过)": round(sum(rets_b) / len(rets_b), 2) if rets_b else None,
            "成本抬高%(中位)": round(pd.Series([h["成本抬高%"] for h in done]).median(), 2) if done else None,
        })

    # ---- 报告 ----
    out: list[str] = []
    add = out.append
    add("# 确认滞后对策略成绩的影响")
    add("")
    add("- 逐笔来源：`outputs/trades_geom.csv`"
        f"（{len(trades)} 笔，来自 50 只池 / 247 交易日 / 窗口 40±10）")
    add("- 口径：**判定完全不变**，只把入场价按「晚 L 个交易日才确认」重算；"
        "确认时点已越过卖点日的笔记为**错过**（实盘不会建仓）")
    add("- L = 0 即当前代码口径（`offset=1`，已在原始 dt 基础上顺延 1 天）")
    add("")

    add("## 1. 汇总")
    add("")
    add("| " + " | ".join(rows[0].keys()) + " |")
    add("| " + " | ".join("---" for _ in rows[0]) + " |")
    for r in rows:
        add("| " + " | ".join("--" if v is None else str(v) for v in r.values()) + " |")
    add("")
    add("读法：`组合平均收益%` 是把错过的按 0 计入 —— **这才是实盘的期望**，"
        "因为它包括了「确认时机会已经过去」的代价。")
    add("")

    add("## 2. 逐笔（每个滞后档）")
    add("")
    add("| 滞后 | 代码 | 成交日 | 原收益% | 新确认日 | 状态 | 新收益% | 成本抬高% |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for d in detail:
        add(f"| {d['lag']} | {d['代码']} | {d['成交日']} | {d['原收益%']} | "
            f"{d.get('确认日', '')} | {d['状态']} | "
            f"{'' if d['收益'] is None else d['收益']} | {d.get('成本抬高%', '')} |")
    add("")

    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text("\n".join(out), encoding="utf-8")

    print(f"报告：{a.out}")
    for r in rows:
        print(f"  滞后 {r['滞后(交易日)']:>2} 日 → 可成交 {r['可成交笔数']:>2} 笔 / "
              f"错过 {r['错过笔数']:>2} 笔 / 胜率 {r['胜率%(仅成交)']}% / "
              f"平均 {r['平均收益%(仅成交)']}% / 含错过 {r['组合平均收益%(含错过)']}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
