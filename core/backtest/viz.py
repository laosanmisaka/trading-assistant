# -*- coding: utf-8 -*-
"""回测报告图表 — K 线 + 买卖点标注 + 权益曲线（matplotlib，无头可跑）

输出 `matplotlib.figure.Figure`，两条消费路径：
  - 桌面端：`ui/backtest_tab.py` 把 Figure 嵌进 FigureCanvas 展示
  - 落盘：  `save_report_chart()` 存 PNG（Agg canvas，不依赖显示环境）

不 import pyplot、不碰后端 —— Figure 由调用方决定接到哪个 canvas 上。
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from config import CHART_COLORS
from data.models import KLineData

# 标注离当根 K 线极值的距离，与 ui/chart_widget 同一约定
_BUY_NEAR, _SELL_NEAR = 0.985, 1.015


def _chinese_font() -> str:
    from matplotlib import font_manager as fm
    available = {f.name for f in fm.fontManager.ttflist}
    for name in ("Microsoft YaHei", "SimHei", "WenQuanYi Micro Hei",
                 "Noto Sans CJK SC", "Source Han Sans SC", "SimSun"):
        if name in available:
            return name
    return "sans-serif"


def render_report_figure(report, daily: Sequence[KLineData],
                         marks: Sequence[dict] | None = None,
                         title: str = ""):
    """回测报告 → 双面板 Figure（上：K 线+买卖点；下：权益曲线+回撤）

    ``marks`` 为 ``[{"date", "kind", "price"}, ...]``（chan_viz 的标注格式），
    买点画在当根 K 线最低价下方（红色 ▲），卖点在最高价上方（绿色 ▼）。
    不传则只画 K 线与权益曲线。
    """
    from matplotlib.figure import Figure
    from matplotlib.patches import Rectangle

    daily = list(daily)
    fig = Figure(figsize=(13, 8), tight_layout=True)
    font = _chinese_font()
    ax_k, ax_eq = fig.subplots(2, 1, sharex=True,
                               gridspec_kw={"height_ratios": [3, 1.4]})

    # ---- 上面板：K 线 ----
    n = len(daily)
    date_idx = {str(k.date)[:10]: i for i, k in enumerate(daily)}
    up, down = CHART_COLORS["up"], CHART_COLORS["down"]
    for i, k in enumerate(daily):
        o, h, l, c = float(k.open), float(k.high), float(k.low), float(k.close)
        color = up if c >= o else down
        ax_k.vlines(i, l, h, color=color, linewidth=0.8, zorder=2)
        body_lo, body_hi = min(o, c), max(o, c)
        ax_k.add_patch(Rectangle(
            (i - 0.3, body_lo), 0.6, max(body_hi - body_lo, 1e-9),
            facecolor=color, edgecolor=color, linewidth=0.5, zorder=3))

    # ---- 买卖点标注 ----
    for m in marks or []:
        i = date_idx.get(str(m["date"])[:10])
        if i is None:
            continue
        kind = str(m["kind"])
        if kind.endswith("买"):
            ax_k.scatter([i], [float(daily[i].low) * _BUY_NEAR],
                         marker="^", color=up, s=90, zorder=5,
                         edgecolors="#fff", linewidths=0.6)
        else:
            ax_k.scatter([i], [float(daily[i].high) * _SELL_NEAR],
                         marker="v", color=down, s=90, zorder=5,
                         edgecolors="#fff", linewidths=0.6)

    ax_k.set_title(title or f"{report.code}  策略: {report.strategy}",
                   fontfamily=font, fontsize=13)
    ax_k.set_xlim(-1, n)
    ax_k.grid(alpha=0.25, linewidth=0.5)
    ax_k.set_ylabel("价格", fontfamily=font)

    # ---- 下面板：权益曲线 + 回撤阴影 ----
    curve = list(report.equity_curve or [])
    if curve:
        xs = list(range(min(len(curve), n)))
        ys = curve[:len(xs)]
        peak = []
        p = 0.0
        for e in ys:
            p = max(p, e)
            peak.append(p)
        ax_eq.plot(xs, ys, color="#1f77b4", linewidth=1.2, label="权益")
        ax_eq.fill_between(xs, ys, peak, color="#d62728", alpha=0.18,
                           label="回撤")
        ax_eq.axhline(report.initial_capital, color="#888", linewidth=0.8,
                      linestyle="--")
        ax_eq.legend(loc="upper left", prop={"family": font}, fontsize=9)
    ax_eq.set_ylabel("权益", fontfamily=font)
    ax_eq.grid(alpha=0.25, linewidth=0.5)

    # x 轴日期刻度（稀疏抽样，最多 10 个）
    if n:
        step = max(1, n // 10)
        ticks = list(range(0, n, step))
        ax_eq.set_xticks(ticks)
        ax_eq.set_xticklabels([str(daily[i].date)[:10] for i in ticks],
                              rotation=30, fontsize=8)

    # 摘要写到图上沿，读图不用翻报告
    summary = (f"胜率 {report.win_rate:.1%} ({report.win_trades}胜"
               f"/{report.loss_trades}负)   总收益 {report.total_return:+.2%}"
               f"   最大回撤 {report.max_drawdown:.2%}   "
               f"盈亏比 {'∞' if report.profit_factor == float('inf') else f'{report.profit_factor:.2f}'}")
    fig.suptitle(summary, fontsize=10, fontfamily=font, y=1.0)
    return fig


def save_report_chart(report, daily: Sequence[KLineData], path,
                      marks: Sequence[dict] | None = None,
                      title: str = "") -> Path:
    """渲染并保存 PNG，返回落盘路径（Agg canvas，无显示环境可用）"""
    from matplotlib.backends.backend_agg import FigureCanvasAgg

    fig = render_report_figure(report, daily, marks=marks, title=title)
    FigureCanvasAgg(fig)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=110)
    return out
