# -*- coding: utf-8 -*-
"""六脉神剑买卖点图（单文件离线 HTML，ECharts 内联）

    python scripts/visualize_six_pulse.py --random 3
    python scripts/visualize_six_pulse.py sh600519 sz000858
    python scripts/visualize_six_pulse.py --random 3 --no-filter --start ""

画什么
------
一只股票一张图，上下两个子图**共享 X 轴**：

- **上：日线 K 线 + MA5 / MA10 / MA20**（分级出口的三条线）+ 成交点
  - **建仓** 红实心三角 ▲（六脉共振首日 + 收盘站上 MA20）
  - **回补** 橙三角 ▲（已在场、曾破 MA10 又站回 ⇒ 买回半仓）
  - **减半** 绿倒三角 ▼（收盘破 MA5）
  - **清仓** 深绿菱形 ◆（收盘破 MA20）
  - **共振未采用** 灰点 ●（出了六脉共振但没开仓：被 MA20 过滤挡掉，或当时已持仓）
- **下：仓位比例**（0~100% 阶梯线）—— 直接看"钱有没有在里面"

口径与 `scripts/eval_six_pulse.py` **完全一致**：日线前复权、T 日收盘确认 →
**T+1 开盘成交**、买点默认须站上 MA20、出口默认分级（MA5 半 / 站回 MA10 补 / MA20 全）。
价格用该日 `low×0.985`/`high×1.015` 做视觉偏移，真实成交价在 tooltip 里。

两个容易看成"画错"的点（都不是错）
----------------------------------
1. **标记落在成交日，而成交日是破线日的次一交易日**。所以"破 MA5 减半"的绿三角
   看起来总比 MA5 的交点晚一根 K 线 —— 这是 T+1 成交延迟，不是标错。
   判定依据看**前一根**的收盘价与均线：那根才是触发日。
2. `Strategy.generate_signals()` 返回的 `Signal.date` **已经是成交日**
   （`core/six_pulse.py::emit` 里写的 `dates[i + 1]`），落点直接取该日索引，
   **不要再加一天**。2026-09-21 修过这个 off-by-one：早先版本在成交日上又
   `+1`，标记整体右移一格，且与下面**仓位阶梯线**（按成交日跳变）差开一天，
   看起来就是"减仓/卖出跟策略对不上"。`tests/test_visualize_six_pulse.py`
   有回归保护。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = Path(__file__).resolve().parent
for _p in (str(SCRIPTS), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import six_pulse                                    # noqa: E402
from core.backtest.strategy import Action                     # noqa: E402
from eval_six_pulse import DAILY_CACHE, load_daily            # noqa: E402
from scan_pool import load_pool                                # noqa: E402

ECHARTS = ROOT / "resources" / "echarts.min.js"

UP = "#D0021B"       # 涨 / 买
DOWN = "#1D9E75"     # 跌 / 卖
STOP = "#0F6E56"
REENTRY = "#E8A33D"
MUTED = "#B4B2A9"


def collect(daily, strat, start: str | None) -> dict:
    """跑一遍策略 → 图所需的全部序列（含逐日仓位）"""
    n = len(daily)
    dates = [k.date for k in daily]
    ohlc = [[round(float(k.open), 3), round(float(k.close), 3),
             round(float(k.low), 3), round(float(k.high), 3)] for k in daily]

    df = six_pulse.compute_indicators(six_pulse.to_frame(daily),
                                      entry_ma=strat.entry_ma)
    ma5 = [None if not np.isfinite(v) else round(float(v), 3) for v in df["ma_reduce"]]
    ma10 = [None if not np.isfinite(v) else round(float(v), 3) for v in df["ma_reentry"]]
    ma20 = [None if not np.isfinite(v) else round(float(v), 3) for v in df["ma_stop"]]

    raw_sig = [bool(v) for v in df["buy_signal"]]        # 原始共振首日（未过滤）

    #: `Signal.date` 在 `generate_signals` 里写的就是**成交日**（T 日确认 → T+1
    #: 开盘成交），所以按日期取到的索引 `i` 本身就是落点，不要再 `+1`。
    sig_by_date: dict[str, list] = {}
    for s in strat.generate_signals(daily):
        sig_by_date.setdefault(s.date, []).append(s)

    pos = 0.0
    pos_series: list[float] = []
    buys: list[dict] = []
    reentries: list[dict] = []
    reduces: list[dict] = []
    stops: list[dict] = []

    def anchored(i: int, kind: str) -> float:
        """标记的 y 锚点 —— 买入压到当日低点下方、卖出抬到当日高点上方"""
        return (float(daily[i].low) * 0.985 if kind in ("buy", "reentry")
                else float(daily[i].high) * 1.015)

    def point(i: int, kind: str, s) -> dict:
        # `trig` = 触发日（成交日的前一根，策略就是在那天收盘判定的）
        return {"value": [i, round(anchored(i, kind), 3)],
                "dt": dates[i], "px": round(float(s.price), 3),
                "trig": dates[i - 1] if i > 0 else "",
                "why": s.reason}

    for i, k in enumerate(daily):
        for s in sig_by_date.get(k.date, []):
            # 防御：`Signal.date` 必须是成交日，其 `price` 就是当天的开盘价。
            # 若哪天策略把成交挪走（改成收盘价/次日再延迟），这里会立刻报警，
            # 免得图悄悄错位。
            assert abs(float(s.price) - float(k.open)) < 1e-6, (
                f"Signal.date={s.date} 的 price={s.price} 不是当天开盘价 "
                f"{k.open} ⇒ 图上的落点口径已与策略脱节")
            w = float(getattr(s, "weight", 1.0) or 1.0)
            if s.action == Action.BUY:
                # 空仓时是建仓、持仓时是回补 —— 与策略状态机同一判据
                if pos <= 1e-9:
                    buys.append(point(i, "buy", s))
                else:
                    reentries.append(point(i, "reentry", s))
                pos = min(pos + w, 1.0)
            else:
                if w >= 1.0:
                    stops.append(point(i, "stop", s))
                    pos = 0.0
                else:
                    reduces.append(point(i, "reduce", s))
                    pos = max(pos - w, 0.0)
        pos_series.append(round(pos, 3))

    # 出了共振但没开仓的位置（被过滤挡掉 / 当时已持仓）
    acted = {x["dt"] for x in buys}
    skipped: list[dict] = []
    for i, flag in enumerate(raw_sig):
        if not flag or i + 1 >= n:
            continue
        if dates[i + 1] in acted:
            continue
        skipped.append({"value": [i + 1, round(float(daily[i + 1].close), 3)],
                        "dt": dates[i + 1]})

    i0 = 0
    if start:
        i0 = next((i for i, d in enumerate(dates) if d >= start), n)

    def cut_points(items):
        """裁到显示区间，并把 x 索引平移到裁剪后的坐标系"""
        return [dict(x, value=[x["value"][0] - i0, x["value"][1]])
                for x in items if x["value"][0] >= i0]

    return {
        "dates": dates[i0:],
        "ohlc": ohlc[i0:],
        "ma5": ma5[i0:], "ma10": ma10[i0:], "ma20": ma20[i0:],
        "buys": cut_points(buys),
        "reentries": cut_points(reentries),
        "reduces": cut_points(reduces),
        "stops": cut_points(stops),
        "skipped": cut_points(skipped),
        "pos": pos_series[i0:],
    }


TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>
  html,body{margin:0;padding:0;background:#fff;font-family:"Microsoft YaHei",sans-serif}
  #chart{width:100vw;height:100vh}
  .tip{position:absolute;left:12px;bottom:8px;font-size:12px;color:#888;z-index:9}
</style>
</head>
<body>
<div id="chart"></div>
<div class="tip">__FOOT__</div>
<script>__ECHARTS__</script>
<script>
var D = __DATA__;
var S = D.dates.length > 320 ? Math.max(0, (1 - 320 / D.dates.length) * 100) : 0;

function mk(name, arr, color, size, rotate, opa) {
  return {
    name: name, type: 'scatter', xAxisIndex: 0, yAxisIndex: 0,
    data: arr, symbol: 'triangle', symbolSize: size, symbolRotate: rotate || 0,
    itemStyle: {color: color, opacity: opa === undefined ? 1 : opa},
    tooltip: {formatter: function (p) {
      var d = p.data;
      return '<b>' + name + '</b><br>'
        + (d.trig ? '触发 ' + d.trig + '　' : '')
        + '成交 ' + d.dt + ' @ ' + d.px
        + (d.why ? '<br>' + d.why : '');
    }},
    z: 10
  };
}

var option = {
  animation: false,
  backgroundColor: '#fff',
  title: {text: '__TITLE__', left: 12, top: 4,
          textStyle: {fontSize: 14, fontWeight: 500, color: '#333'}},
  legend: {top: 6, right: 14, itemWidth: 12, itemHeight: 8, textStyle: {fontSize: 11}},
  tooltip: {trigger: 'axis', axisPointer: {type: 'cross', label: {fontSize: 10}},
            confine: true},
  axisPointer: {link: [{xAxisIndex: 'all'}]},
  grid: [{left: 62, right: 24, top: 46, height: '58%'},
         {left: 62, right: 24, top: '76%', height: '14%'}],
  xAxis: [
    {type: 'category', data: D.dates, gridIndex: 0, boundaryGap: true,
     axisLabel: {show: false}, axisTick: {show: false},
     axisLine: {lineStyle: {color: '#ccc'}}},
    {type: 'category', data: D.dates, gridIndex: 1, boundaryGap: true,
     axisLabel: {fontSize: 10, color: '#888'}, axisTick: {show: false},
     axisLine: {lineStyle: {color: '#ccc'}}}
  ],
  yAxis: [
    {scale: true, gridIndex: 0, splitLine: {lineStyle: {color: '#f0f0f0'}},
     axisLabel: {fontSize: 10, color: '#888'}, axisLine: {show: false}},
    {gridIndex: 1, min: 0, max: 1, splitNumber: 2, name: '仓位',
     nameTextStyle: {fontSize: 10, color: '#888'},
     axisLabel: {fontSize: 10, color: '#888',
                 formatter: function (v) { return Math.round(v * 100) + '%'; }},
     splitLine: {lineStyle: {color: '#f0f0f0'}}, axisLine: {show: false}}
  ],
  dataZoom: [
    {type: 'inside', xAxisIndex: [0, 1], start: S, end: 100},
    {type: 'slider', xAxisIndex: [0, 1], bottom: 24, height: 18,
     start: S, end: 100, labelFormatter: function (v) { return v; }}
  ],
  series: [
    {name: '日线', type: 'candlestick', xAxisIndex: 0, yAxisIndex: 0, data: D.ohlc,
     itemStyle: {color: '#D0021B', color0: '#1D9E75',
                 borderColor: '#D0021B', borderColor0: '#1D9E75'}, z: 2},
    {name: 'MA5', type: 'line', data: D.ma5, showSymbol: false, connectNulls: false,
     lineStyle: {width: 1, color: '#BA7517'}, z: 3},
    {name: 'MA10', type: 'line', data: D.ma10, showSymbol: false, connectNulls: false,
     lineStyle: {width: 1, color: '#378ADD'}, z: 3},
    {name: 'MA20', type: 'line', data: D.ma20, showSymbol: false, connectNulls: false,
     lineStyle: {width: 1.6, color: '#534AB7'}, z: 3},
    mk('建仓', D.buys, '#D0021B', 13, 0),
    mk('回补', D.reentries, '#E8A33D', 10, 0),
    mk('减半', D.reduces, '#1D9E75', 11, 180),
    mk('清仓', D.stops, '#0F6E56', 11, 180),
    {name: '共振未采用', type: 'scatter', xAxisIndex: 0, yAxisIndex: 0,
     data: D.skipped, symbol: 'circle', symbolSize: 5,
     itemStyle: {color: '#B4B2A9', opacity: 0.9},
     tooltip: {formatter: function (p) {
       return '<b>共振未采用</b><br>成交日 ' + p.data.dt
              + '<br>被 MA20 过滤挡掉，或当时已持仓'; }}, z: 6},
    {name: '仓位', type: 'line', xAxisIndex: 1, yAxisIndex: 1, data: D.pos,
     step: 'end', showSymbol: false, connectNulls: true,
     lineStyle: {width: 1.2, color: '#D0021B'},
     areaStyle: {color: '#D0021B', opacity: 0.18}, z: 2}
  ]
};

var chart = echarts.init(document.getElementById('chart'));
chart.setOption(option);
window.addEventListener('resize', function () { chart.resize(); });
</script>
</body>
</html>
"""


def render(inst: dict, title: str, foot: str) -> str:
    html = TEMPLATE.replace("__TITLE__", title)
    html = html.replace("__FOOT__", foot)
    html = html.replace("__DATA__", json.dumps(inst, ensure_ascii=False))
    html = html.replace("__ECHARTS__", ECHARTS.read_text(encoding="utf-8")
                        if ECHARTS.exists() else "")
    return html


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="六脉神剑买卖点图（单文件离线 HTML）")
    p.add_argument("codes", nargs="*", help="股票代码，如 sh600519")
    p.add_argument("--pool", default=str(Path(__file__).with_name("pool_random500.txt")))
    p.add_argument("--random", type=int, default=0, help="从池里随机抽 N 只")
    p.add_argument("--seed", type=int, default=0, help="随机种子（0 = 每次不同）")
    p.add_argument("--days", type=int, default=1500)
    p.add_argument("--start", default="2025-01-01",
                   help="显示起点（此前数据仅预热指标），传空字符串 = 全历史")
    p.add_argument("--entry-ma", type=int, default=20, help="买点过滤均线，0 = 关闭")
    p.add_argument("--cache-days", type=int, default=0)
    p.add_argument("--out-dir", default="outputs")
    args = p.parse_args(argv)

    names: dict[str, str] = {}
    if args.codes:
        codes = [c.strip() for c in args.codes if c.strip()]
        pool = load_pool(Path(args.pool))
        names = {c: n for c, n in pool}
    else:
        pool = load_pool(Path(args.pool))
        if args.seed:
            random.seed(args.seed)
        pick = random.sample(pool, min(args.random or 3, len(pool)))
        codes = [c for c, _ in pick]
        names = {c: n for c, n in pick}

    start = args.start or None
    strat = six_pulse.SixPulseStrategy(entry_ma=args.entry_ma or None,
                                       trade_from=start)
    out_dir = ROOT / args.out_dir if not Path(args.out_dir).is_absolute() else Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    made: list[Path] = []
    for code in codes:
        try:
            daily, _ = load_daily(code, args.days, cache_dir=DAILY_CACHE,
                                  cache_days=args.cache_days, polite_sleep=0.0)
        except Exception as exc:                              # noqa: BLE001
            print(f"{code} 取数失败：{exc}")
            continue
        inst = collect(daily, strat, start)
        nm = names.get(code, "")
        sub = (f"{args.entry_ma} 日线过滤 · 分级出口" if args.entry_ma
               else "无过滤 · 分级出口")
        title = f"{code} {nm}　六脉神剑（{sub}）"
        foot = (f"建仓 {len(inst['buys'])} · 减半 {len(inst['reduces'])} · "
                f"回补 {len(inst['reentries'])} · 清仓 {len(inst['stops'])} · "
                f"共振未采用 {len(inst['skipped'])}　"
                f"（标记落在成交日；悬停可看触发日与原因。T 日收盘确认 → T+1 开盘成交，"
                f"所以卖出标记天然比 MA5/MA10/MA20 的交点晚一根 K 线）")
        html = render(inst, title, foot)
        f = out_dir / f"six_pulse_{code}.html"
        f.write_text(html, encoding="utf-8")
        made.append(f)
        avg_pos = float(np.mean(inst["pos"])) if inst["pos"] else 0.0
        print(f"{code} {nm}　K线 {len(inst['dates'])} 根　"
              f"建仓 {len(inst['buys'])} 减半 {len(inst['reduces'])} "
              f"回补 {len(inst['reentries'])} 清仓 {len(inst['stops'])}　"
              f"平均仓位 {avg_pos:.1%} → {f.name}")

    if not made:
        print("没有生成任何图")
        return 1
    print("\n".join(str(x) for x in made))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
