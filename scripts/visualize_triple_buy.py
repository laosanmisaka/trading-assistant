# -*- coding: utf-8 -*-
"""日线+5min 三买策略的买点可视化 —— 可拖动缩放的单文件离线 HTML

    python scripts/visualize_triple_buy.py                    # 随机抽 4 只有缓存的
    python scripts/visualize_triple_buy.py --codes sh601899 sh600519
    python scripts/visualize_triple_buy.py --n 6 --seed 7 --min-mode one_div

图上画什么
----------
- 日线 K 线（全量缓存，默认视野落在最近 250 根）+ 日线中枢（灰框）
- **红色 ▲** = 日线三买点（回抽低点，事后标注位）
- **蓝色阴影** = 回调窗口（突破笔终点 → 日线确认时刻）
- **橙色 ★** = 5min 最早买点（一买/二买，落在其所在交易日）
- **绿色 ▼** = 日线确认后次日开盘价（不提前入场的对照入场位）

交互：鼠标拖动平移、滚轮缩放、底部滑块选区间（与 `core/chan_viz` 的
HTML 同一套 dataZoom 配置）。数据走 `outputs/cache_min` 本地缓存，不联网。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from core import chan as chan_mod                     # noqa: E402
from eval_daily_5min import analyze_one, load_cached, DEFAULT_CACHE  # noqa: E402

_ECHARTS = ROOT / "resources" / "echarts.min.js"

UP, DOWN = "#DC143C", "#008000"     # 红涨绿跌，与 config.CHART_COLORS 一致

_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>__TITLE__</title>
<style>
  html,body{margin:0;padding:0;background:#f4f5f7;color:#222;
    font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;}
  #wrap{max-width:1720px;margin:0 auto;padding:16px 20px 32px;}
  h1{font-size:19px;margin:0 0 6px;font-weight:600;}
  .meta{font-size:12px;color:#555;line-height:1.8;margin-bottom:8px;}
  #chart{width:100%;height:78vh;background:#fff;border:1px solid #e3e6ea;
    border-radius:6px;}
</style>
</head>
<body>
<div id="wrap">
  <h1>__TITLE__</h1>
  <div class="meta">__META__</div>
  <div id="chart"></div>
</div>
<script>__ECHARTS__</script>
<script>
var CFG = __PAYLOAD__;
var dates = CFG.dates;
var chart = echarts.init(document.getElementById('chart'));

function scatter(name, pts, color, symbol, size, offset) {
  return {name: name, type: 'scatter', xAxisIndex: 0, yAxisIndex: 0,
    data: pts, symbol: symbol, symbolSize: size,
    symbolOffset: offset || [0, 0],
    itemStyle: {color: color, borderColor: '#fff', borderWidth: 0.8},
    z: 9, animation: false, emphasis: {scale: 1.6}};
}

chart.setOption({
  animation: false,
  legend: {top: 6, textStyle: {fontSize: 12}},
  tooltip: {trigger: 'axis', axisPointer: {type: 'line'},
    formatter: function (ps) {
      var i = ps[0].dataIndex, out = [dates[i]];
      ps.forEach(function (p) {
        if (p.seriesType === 'candlestick') {
          var d = p.data;
          out.push('开 ' + d[1] + '  收 ' + d[2] + '  低 ' + d[3] + '  高 ' + d[4]);
        } else {
          out.push(p.marker + p.seriesName + ' @' + p.data[1]);
        }
      });
      return out.join('<br/>');
    }},
  axisPointer: {link: [{xAxisIndex: 'all'}]},
  xAxis: {type: 'category', data: dates, boundaryGap: true,
    axisLine: {lineStyle: {color: '#888'}}, splitLine: {show: false}},
  yAxis: {scale: true, position: 'right',
    splitLine: {lineStyle: {color: '#eee'}},
    axisLabel: {formatter: function (v) { return v.toFixed(2); }}},
  dataZoom: [
    {type: 'inside', xAxisIndex: [0],
     startValue: CFG.zoom[0], endValue: CFG.zoom[1],
     zoomOnMouseWheel: true, moveOnMouseMove: true},
    {type: 'slider', xAxisIndex: [0], bottom: 14, height: 30,
     startValue: CFG.zoom[0], endValue: CFG.zoom[1],
     borderColor: '#ddd', fillerColor: 'rgba(100,150,220,0.15)',
     handleStyle: {color: '#78909c'},
     labelFormatter: function (v) { return dates[v] ? dates[v].slice(0, 10) : ''; }}
  ],
  series: [
    {name: 'K线', type: 'candlestick', xAxisIndex: 0, yAxisIndex: 0,
     data: CFG.kline, z: 4,
     itemStyle: {color: '__UP__', color0: '__DOWN__',
                 borderColor: '__UP__', borderColor0: '__DOWN__'},
     // 中枢灰框 + 回调窗口蓝影都挂在 K 线的 markArea 上（silent，不参与 hover）
     markArea: {silent: true, animation: false, data: CFG.areas}},
    scatter('日线三买点', CFG.p3, '__UP__', 'triangle', 13, [0, 6]),
    scatter('5min最早买点', CFG.p5, '#FF8C00', 'pin', 16, [0, -8]),
    scatter('日线确认入场', CFG.pd, '__DOWN__', 'triangle', 11, [0, -6])
  ]
});
window.addEventListener('resize', function () { chart.resize(); });
</script>
</body>
</html>
"""


def cached_codes(cache_dir: Path) -> list[str]:
    """缓存里同时有 daily 和 5min 的标的"""
    codes = {p.stem.split("_", 1)[1] for p in cache_dir.glob("5min_*.csv")}
    return sorted(c for c in codes
                  if (cache_dir / f"daily_{c}.csv").exists())


def build_payload(code: str, cache_dir: Path, *, min_mode: str,
                  zoom_bars: int) -> dict | None:
    """一个标的 → 图表 payload（缠论结构 + 三买策略点，全走 analyze_one）"""
    kd = load_cached(code, "daily", cache_dir)
    if not kd:
        print(f"{code}: 无 daily 缓存，跳过")
        return None
    res = analyze_one(code, cache_dir, holds=[20], min_mode=min_mode)
    if res is None or not res["明细"]:
        print(f"{code}: 无可用三买样本，跳过")
        return None

    # 日线中枢（analyze_one 不返回结构，为画中枢框重建一次）
    rd = chan_mod.build(kd, "daily")
    zs_d = chan_mod.centers(rd) if rd else []

    dates = [str(k.date)[:10] for k in kd]
    idx_of = {d: i for i, d in enumerate(dates)}
    n = len(dates)

    def _x(t) -> int | None:
        return idx_of.get(str(t)[:10])

    kline = [[round(float(k.open), 3), round(float(k.close), 3),
              round(float(k.low), 3), round(float(k.high), 3)] for k in kd]

    # markArea：中枢灰框 + 回调窗口蓝影
    areas = []
    for z in zs_d:
        x0, x1 = _x(z["sdt"]), _x(z["edt"])
        if x0 is None or x1 is None:
            continue
        areas.append([{"xAxis": dates[x0], "yAxis": round(float(z["low"]), 3),
                       "itemStyle": {"color": "rgba(120,120,120,0.12)"}},
                      {"xAxis": dates[x1], "yAxis": round(float(z["high"]), 3)}])
    for r in res["明细"]:
        x0, x1 = _x(r["窗口起"]), _x(r["日线确认"])
        if x0 is None or x1 is None:
            continue
        areas.append([{"xAxis": dates[x0],
                       "itemStyle": {"color": "rgba(31,119,180,0.10)"}},
                      {"xAxis": dates[x1]}])

    def _points(key, price_of) -> list:
        out = []
        for r in res["明细"]:
            if r.get(key) is None:
                continue
            x = _x(r[key])
            if x is None:
                continue
            out.append([x, price_of(r, x)])
        return out

    p3 = _points("三买点", lambda r, x: round(float(kd[x].low), 3))
    p5 = _points("最早买点", lambda r, x: round(float(kd[x].low), 3))
    pd_ = []
    for r in res["明细"]:
        i = r.get("_entry_d")
        if i is None or not (0 <= i < n):
            continue
        pd_.append([i, round(float(kd[i].open), 3)])

    zoom = [max(0, n - zoom_bars), n - 1]
    return {
        "dates": dates, "kline": kline, "areas": areas,
        "p3": p3, "p5": p5, "pd": pd_, "zoom": zoom,
        "summary": {
            "三买点": res["三买点"], "5min覆盖": res["5min覆盖点数"],
            "有候选": res["有候选"], "提前中位": res["提前中位"],
            "价优中位": res["价优中位"],
        },
    }


def render_html(payload: dict, code: str, min_mode: str, out: Path) -> Path:
    s = payload["summary"]
    title = f"{code}  日线+5min 三买策略（{min_mode}）"
    meta = (f"日线三买 <b>{s['三买点']}</b> 个 / 5min 覆盖 <b>{s['5min覆盖']}</b> 个 / "
            f"有候选 <b>{s['有候选']}</b> 个 / 提前中位 <b>{s['提前中位']}</b> 交易日 / "
            f"价优中位 <b>{s['价优中位']}</b>%　　"
            f"图例：红▲=日线三买点　橙★=5min最早买点　绿▼=日线确认入场　"
            f"灰框=日线中枢　蓝影=回调窗口　　拖动平移 / 滚轮缩放")
    html = (_HTML
            .replace("__TITLE__", title)
            .replace("__META__", meta)
            .replace("__ECHARTS__", _ECHARTS.read_text(encoding="utf-8"))
            .replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
            .replace("__UP__", UP)
            .replace("__DOWN__", DOWN))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="日线+5min 三买策略买点可视化（可拖动 HTML）")
    ap.add_argument("--codes", nargs="*", default=None,
                    help="标的代码；不给则从缓存里随机抽 --n 只")
    ap.add_argument("--n", type=int, default=4)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--min-mode", default="any",
                    choices=["any", "one", "one_div"])
    ap.add_argument("--zoom-bars", type=int, default=250,
                    help="默认视野显示最近多少根日线（全量可拖动查看）")
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--out", type=Path,
                    default=ROOT / "outputs" / "triple_buy_charts")
    args = ap.parse_args(argv)

    if not _ECHARTS.exists():
        print(f"找不到 ECharts 库：{_ECHARTS}")
        return 1

    if args.codes:
        codes = args.codes
    else:
        pool = cached_codes(args.cache)
        if not pool:
            print(f"缓存为空：{args.cache}（先跑 scripts/fetch_min.py）")
            return 1
        rng = random.Random(args.seed)
        codes = rng.sample(pool, min(args.n, len(pool)))

    ok = 0
    for code in codes:
        payload = build_payload(code, args.cache, min_mode=args.min_mode,
                                zoom_bars=args.zoom_bars)
        if payload is None:
            continue
        out = render_html(payload, code, args.min_mode,
                          args.out / f"triple_buy_{code}_{args.min_mode}.html")
        print(f"{code} → {out}")
        ok += 1
    print(f"完成 {ok}/{len(codes)} 张，输出目录：{args.out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
