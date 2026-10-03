"""Markdown presentation of daily/5min research; statistics live in core/."""
from datetime import datetime
import sys

import numpy as np

from core.research_stats import metrics, cluster_bootstrap, block_bootstrap
from core.research_summary import (collect_points, base_of, trail_points, trail_base_of,
                                   robustness, mae_table)
from scripts.eval_triple_buy import summarize


def fmt(value):
    return "--" if value is None else f"{value:.2f}"


def interval(result):
    if result is None:
        return "--"
    return f"[{result['lo']:+.2f}, {result['hi']:+.2f}] / {result['p_le0']:.1%}"


def _overview(rows):
    lines = ["## 覆盖与逐标的", "",
             "| 代码 | 日线笔 | 观察窗口 | 覆盖窗口 | 有入场 | 幅度中位% |",
             "| --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        lines.append(f"| {row['代码']} | {row['日线笔']} | {row['三买点']} | "
                     f"{row['5min覆盖点数']} | {row['有候选']} | {fmt(row.get('幅度中位'))} |")
    return lines + [""]


def _returns(rows, holds):
    lines = ["## 收益与同池随机入场基准", "",
             "| 持有日 | 级别 | 点数 | 胜率 | 均值% | 中位% | 盈亏比 | 最佳% | 最差% | 点权基准% | 点均超额% | 等权收益% | 等权基准% | 等权超额% |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for hold in holds:
        for side in ("日线", "5min"):
            base = base_of(rows, side, hold)
            points = [p for p in collect_points(rows, side, hold) if p["code"] in base]
            estimate = metrics(points, base)
            if not estimate:
                continue
            summary = summarize([p["ret"] for p in points])
            lines.append(f"| {hold} | {side} | {estimate['n']} | {summary['胜率']:.1%} | "
                         f"{fmt(summary['平均'])} | {fmt(summary['中位'])} | {fmt(summary['盈亏比'])} | "
                         f"{fmt(summary['最佳'])} | {fmt(summary['最差'])} | {fmt(estimate['base'])} | "
                         f"{fmt(estimate['ex_pt'])} | {fmt(estimate['eqw'])} | "
                         f"{fmt(estimate['base_eqw'])} | {fmt(estimate['ex_eqw'])} |")
    return lines + ["", "点均超额按每个信号减去该标的基准后求均值；标的等权列另用等权基准。",
                    "中位/极值/盈亏比由完整逐点样本计算，不平均各股的分位数。", ""]


def _robustness(rows, holds):
    lines = ["## 稳健性", "",
             "| 持有日 | 级别 | 标的数 | 非重叠点数 | 超额% | cluster 95% / P≤0 | block 95% / P≤0 | 剔前5点超额% | 剔最好标的超额% |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for hold in holds:
        for side in ("日线", "5min"):
            result = robustness(rows, side, hold)
            estimate = result["base"]
            if not estimate:
                continue
            independent = result["indep"] or {}
            dropped = result["drop_top5"] or {}
            stock = result.get("drop_best_stock") or {}
            lines.append(f"| {hold} | {side} | {estimate['n_stock']} | {independent.get('n', 0)} | "
                         f"{fmt(estimate['ex_pt'])} | {interval(result['boot'])} | "
                         f"{interval(result['boot_time'])} | {fmt(dropped.get('ex_pt'))} | "
                         f"{fmt(stock.get('ex_pt'))} |")
    return lines + ["", "两种 bootstrap 都估计逐点配对超额，分别按股票/自然季度重抽。",
                    "持有期不重叠不代表统计独立；这些区间不处理选样、未来信息或跨市场外推。", ""]


def _excursions(rows, holds):
    fields = ["adv_med", "sync_med", "net_med", "mae_med", "mae_p10", "mfe_med",
              "mean", "base", "sl3_mean", "sl5_mean", "sl8_mean"]
    lines = ["## 价优、回撤与历史止损对照", "",
             "| 持有日 | 级别 | n | 价优中位% | 同期基准中位% | 净差中位% | MAE中位% | MAE p10% | MFE中位% | 均值% | 点权基准% | SL3% | SL5% | SL8% |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for hold in holds:
        for side in ("日线", "5min"):
            result = mae_table(rows, side, hold)
            if result:
                values = " | ".join(fmt(result.get(field)) for field in fields)
                lines.append(f"| {hold} | {side} | {result['n']} | {values} |")
    return lines + ["", "MAE/MFE 使用对应级别的持有区间；5min 收盘入场不计入场 bar 的高低点。",
                    "止损为历史研究对照，跳空按开盘、同 bar 冲突按先止损；当前默认策略不启用止损。", ""]


def _details(rows, holds):
    fields = ["代码", "窗口起", "三买点", "窗口关闭", "最早买点", "entry_price", "event_id"] + [f"M{hold}" for hold in holds]
    lines = ["## 逐点明细（前 40 条，完整样本使用 --dump-points）", "",
             "| " + " | ".join(fields) + " |", "| " + " | ".join(["---"] * len(fields)) + " |"]
    records = [detail for row in rows for detail in row["明细"]][:40]
    for record in records:
        values = [str(record.get(key, "--")).replace("|", "\\|") for key in fields]
        lines.append("| " + " | ".join(values) + " |")
    return lines + [""]


def _trail(rows, holds, config):
    if not config:
        return []
    horizon = max(holds)
    ids = {(p["code"], p["idx"]) for p in trail_points(rows)}
    fixed = [p for p in collect_points(rows, "5min", horizon) if (p["code"], p["idx"]) in ids]
    cases = [("动态止损", trail_points(rows), trail_base_of(rows)),
             ("只减半", trail_points(rows, "TRh"), trail_base_of(rows, "halfonly")),
             (f"同批固定持有 {horizon} 日", fixed, base_of(rows, "5min", horizon))]
    lines = ["## 历史动态出场对照", "", f"参数：`{config}`", "",
             "| 方式 | n | 均值% | 点权基准% | 超额% | cluster 95% / P≤0 | block 95% / P≤0 |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for name, points, base in cases:
        estimate = metrics(points, base)
        if estimate:
            lines.append(f"| {name} | {estimate['n']} | {fmt(estimate['pt_mean'])} | "
                         f"{fmt(estimate['base'])} | {fmt(estimate['ex_pt'])} | "
                         f"{interval(cluster_bootstrap(points, base))} | "
                         f"{interval(block_bootstrap(points, base))} |")
    return lines + ["", "动态规则与固定持有只是历史对照，不据此重新启用已否定策略。", ""]


def build_report(rows, *, min_mode, holds, pool_size=0, skipped=None, min_breakout=0.0, trail=None):
    lines = ["# 日线与 5min 研究报告", "", f"生成时间：{datetime.now():%Y-%m-%d %H:%M}",
             f"样本：{len(rows)} / {pool_size or len(rows)}；持有日：{holds}；min_mode={min_mode}；最小突破幅度={min_breakout}%", "",
             "**这是按点统计，收益不可相加为资金曲线。** 窗口及入场按逐 bar 首次观察时刻记录，包含后来失效的候选。",
             "几何重绘不抹掉历史入场；当根收盘是乐观成交假设，未计费用/滑点。",
             f"方法：observable-v1；突破上限：{rows[0].get('max_amp', '--')}%；持有单位：48 根 5min bar/日。",
             "输入缺口、复权、幸存者选样及当前历史数据的修订仍会影响结果，不据此直接断言实盘 alpha。", ""]
    if skipped:
        lines += ["未纳入的标的（不隐藏失败）：", ""] + [f"- {code}: {why}" for code, why in skipped] + [""]
    lines += _overview(rows) + _returns(rows, holds) + _robustness(rows, holds)
    lines += _excursions(rows, holds) + _details(rows, holds) + _trail(rows, holds, trail)
    lines += ["同池随机入场基准按各股全部可用入场时刻的持有收益计算；不保证样本时段匹配。",
              f"生成参数：`{' '.join(sys.argv[1:])}`", ""]
    return "\n".join(lines)
