# -*- coding: utf-8 -*-
"""标的池批量扫描 —— 缠论多周期共振策略的实测与**窗口标定**工具

    python scripts/scan_pool.py                          # 默认 50 只池
    python scripts/scan_pool.py --source signal          # 对比旧 cxt_* 源
    python scripts/scan_pool.py --max-gap-1to2 60 --max-gap-2tom 20
    python scripts/scan_pool.py --no-cache               # 强制重新取数
    python scripts/scan_pool.py --cache-days 3           # 跨日复查：吃 3 天内的旧缓存
    python scripts/scan_pool.py --out outputs/pool_40_10.md

做什么
------
1. 读标的池（默认 `scripts/pool_liquid50.txt`，一行一只）
2. 取每只的 30 分钟行情（新浪源，约 1970 根 / 247 个交易日），**落盘缓存**
3. 跑 `core.chan_strategy.scan()`，汇总笔数 / 平均收益 / 胜率
4. 输出**窗口偏移诊断** —— 这是本脚本存在的主要理由：
   - 「一买 → 二买」间隔分布 ⇒ `--max-gap-1to2` 该取多少
   - 「日线二买 → 最近 30 分钟二买」的有符号偏移分布 ⇒ `--max-gap-2tom`
     该取多少、以及有多少日线二买**根本没有**对应的次级别买点
5. 报告写 markdown（默认 `outputs/` 下，已被 .gitignore 忽略）

为什么必须带缓存
----------------
取数是网络请求（50 只 ≈ 3~5 分钟），而调窗口参数要反复跑很多轮。
30 分钟行情当天不会变，所以按「文件 mtime 是今天」判缓存有效；
换参数重跑时取数走本地，单只计算 2~3 秒。

⚠️ 数字怎么读
-------------
- 策略**允许重叠持仓**，胜率/平均收益是**按笔统计不是资金曲线**，
  同一时刻多笔时收益不可相加（口径见 `core/chan_strategy.py` docstring）。
- 单只标的出点约 0.2 次/年，50 只 × 1 年只有 10 笔量级 ——
  **够看量级，不够定参数**。标定要 100~300 只，且应看诊断分布而不是总笔数。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:          # 允许 `python scripts/scan_pool.py` 直接跑
    sys.path.insert(0, str(ROOT))

from core import chan_strategy, chan_viz            # noqa: E402
from data.models import KLineData                   # noqa: E402

DEFAULT_POOL = Path(__file__).with_name("pool_liquid50.txt")
DEFAULT_CACHE = ROOT / "outputs" / "cache"
PERIOD = "30min"


# ======================================================================
# 标的池
# ======================================================================

def load_pool(path: Path) -> list[tuple[str, str]]:
    """读标的池文件 → ``[(代码, 名称), ...]``

    一行一只，格式 ``代码 名称``；`#` 开头与空行忽略，名称可省。
    代码用新浪格式（sh/sz/bj + 6 位），也接受纯 6 位数字（自动补前缀）。
    """
    pool: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        code = chan_viz.normalize_code(parts[0])
        if code in seen:
            continue
        seen.add(code)
        pool.append((code, parts[1] if len(parts) > 1 else ""))
    return pool


# ======================================================================
# 取数与缓存
# ======================================================================

def _cache_file(cache_dir: Path, sym: str) -> Path:
    return Path(cache_dir) / f"{PERIOD}_{sym}.csv"


def load_klines(
    code: str,
    *,
    cache_dir: Path = DEFAULT_CACHE,
    use_cache: bool = True,
    cache_days: int = 0,
    attempts: int = 3,
    polite_sleep: float = 1.5,
) -> tuple[list[KLineData], bool]:
    """取一只标的的 30 分钟 K 线 → ``(klines, 是否走了缓存)``

    缓存判据：文件存在、非空、且 **mtime 距今不超过 ``cache_days`` 天**。
    ``cache_days=0``（默认）等价于「mtime 就是今天」。30 分钟行情当天不会变，
    盘中最后一根 bar 还会走完，但代价可接受（要最新就 `--no-cache`）。

    ⚠️ 跨日重跑（比如周五取完、周一再看）时缓存会因日期不匹配而整轮重取，
    既慢又容易撞新浪限流。周末/隔日复查用 ``--cache-days 3`` 直接吃旧缓存
    —— 只要中间没有新交易日，旧数据就是最新的那一份。

    ⚠️ 新浪接口**会限流** —— 实测连续取到第 43 只开始整片丢包
    （`IndexError: list index out of range`，连之前成功的代码也一起失败，
    等一会儿才恢复）。所以：失败重试 3 次（递增退避）+ 每次成功取数后
    间隔 `polite_sleep` 秒。缓存命中时不 sleep。
    """
    sym = chan_viz.normalize_code(code)

    if use_cache:
        f = _cache_file(cache_dir, sym)
        if f.exists() and f.stat().st_size > 0:
            age = (date.today() - datetime.fromtimestamp(f.stat().st_mtime).date()).days
            if age <= cache_days:
                df = pd.read_csv(f)
                return [KLineData(code=sym, date=str(r.dt), open=float(r.open),
                                  high=float(r.high), low=float(r.low),
                                  close=float(r.close), volume=int(r.volume),
                                  period=PERIOD)
                        for r in df.itertuples()], True

    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            klines = chan_viz.fetch_klines(sym, PERIOD)
            break
        except Exception as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(5.0 * (attempt + 1))
    else:
        raise RuntimeError(
            f"取数失败（已重试 {attempts} 次，最后一次：{last_exc}）") from last_exc

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    f = _cache_file(cache_dir, sym)
    pd.DataFrame({
        "dt": [k.date for k in klines],
        "open": [k.open for k in klines],
        "high": [k.high for k in klines],
        "low": [k.low for k in klines],
        "close": [k.close for k in klines],
        "volume": [k.volume for k in klines],
    }).to_csv(f, index=False)

    if polite_sleep > 0:
        time.sleep(polite_sleep)
    return klines, False


# ======================================================================
# 窗口诊断
# ======================================================================

def offset_stats(values: list[int]) -> dict:
    """整数样本的分布摘要（空样本返回 None 值，不抛异常）"""
    if not values:
        return {"样本数": 0, "最小": None, "P25": None, "中位": None,
                "P75": None, "P90": None, "最大": None,
                "绝对值中位": None, "绝对值P90": None}
    s = pd.Series(sorted(values))
    a = s.abs()
    return {
        "样本数": len(values),
        "最小": int(s.min()), "P25": float(s.quantile(0.25)),
        "中位": float(s.median()), "P75": float(s.quantile(0.75)),
        "P90": float(s.quantile(0.90)), "最大": int(s.max()),
        "绝对值中位": float(a.median()), "绝对值P90": float(a.quantile(0.90)),
    }


def coverage(values: list[int], thresholds: list[int]) -> dict:
    """``|偏移| <= t`` 的覆盖率 —— 用来挑窗口：t 到多少就基本不漏"""
    n = len(values)
    if not n:
        return {t: None for t in thresholds}
    a = pd.Series(values).abs()
    return {t: round(float((a <= t).mean()), 3) for t in thresholds}


def diagnose(
    trading_days: list[str],
    d1_days: list[str],
    d2_days: list[str],
    m30_days: list[str],
    *,
    wide_gap_1to2: int = 60,
) -> dict:
    """窗口标定诊断（纯逻辑，不碰行情）

    参数都是**日期字符串**（升序交易日列表 + 三级的点日期）。

    返回
    ----
    ``{"一买到二买间隔": [int, ...],
       "二买到30分钟二买偏移": [int, ...],
       "无对应30分钟买点的二买数": int}``

    - 间隔 = 二买位置 − 其前最近一买位置（要求一买更早且不超过 wide_gap）
    - 偏移 = 30 分钟二买**交易日**位置 − 二买位置（**有符号**，
      负数表示 30 分钟买点早于日线二买 —— 几何法里这种情况很常见）
    - 只统计「有一买在 wide_gap 内」的二买，否则那些二买本来就不会触发，
      把它们的偏移算进来会把窗口推大
    """
    pos = {d: i for i, d in enumerate(trading_days)}
    d1_sorted = sorted({d for d in d1_days if d in pos}, key=pos.get)
    d2_sorted = sorted({d for d in d2_days if d in pos}, key=pos.get)
    m30 = sorted({d for d in m30_days if d in pos}, key=pos.get)

    gaps: list[int] = []
    offsets: list[int] = []
    no_m30 = 0

    for d2 in d2_sorted:
        i2 = pos[d2]
        prev = [d for d in d1_sorted if 0 < i2 - pos[d] <= wide_gap_1to2]
        if not prev:
            continue
        gaps.append(i2 - pos[max(prev, key=pos.get)])
        if not m30:
            no_m30 += 1
            continue
        i3 = min((pos[d] for d in m30), key=lambda i: (abs(i - i2), i < i2))
        if abs(i3 - i2) > wide_gap_1to2:
            no_m30 += 1                      # 最近的也在窗外 ⇒ 等于没有
            continue
        offsets.append(i3 - i2)

    return {
        "一买到二买间隔": gaps,
        "二买到30分钟二买偏移": offsets,
        "无对应30分钟买点的二买数": no_m30,
    }


# ======================================================================
# 单只扫描
# ======================================================================

def scan_one(
    code: str,
    name: str,
    *,
    source: str,
    max_gap_1to2: int,
    max_gap_2tom: int,
    cache_dir: Path,
    use_cache: bool,
    cache_days: int = 0,
    polite_sleep: float = 1.5,
    attempts: int = 3,
) -> tuple[dict, dict, object]:
    """扫一只 → ``(结果行, 诊断块, ScanResult)``；取数/计算失败时返回错误行"""
    t0 = time.time()
    try:
        klines, cached = load_klines(code, cache_dir=cache_dir, use_cache=use_cache,
                                     cache_days=cache_days,
                                     attempts=attempts, polite_sleep=polite_sleep)
    except Exception as exc:                 # 网络/接口问题都不该中断整轮
        return {"代码": code, "名称": name, "错误": f"取数失败: {exc}"}, {}, None

    try:
        result = chan_strategy.scan(klines, code=code, source=source,
                                   max_gap_1to2=max_gap_1to2,
                                   max_gap_2tom=max_gap_2tom)
    except Exception as exc:
        return {"代码": code, "名称": name, "错误": f"计算失败: {exc}"}, {}, None

    days = sorted({k.date[:10] for k in klines})
    diag = diagnose(days, result.buy1_days, result.buy2_days,
                    [b[:10] for b in result.m30_buy2_bars])
    stats = result.summary()
    row = {
        "代码": code,
        "名称": name,
        "30分钟根数": len(klines),
        "交易日": len(days),
        "一买": len(result.buy1_days),
        "二买": len(result.buy2_days),
        "30分二买": len(result.m30_buy2_bars),
        "买点": stats["买点数"],
        "已平仓": stats["已平仓"],
        "胜率%": stats["胜率%"],
        "平均收益%": stats["平均收益%"],
        "最大同时持仓": stats["最大同时持仓"],
        "重叠笔数": stats["重叠笔数"],
        "缓存": "是" if cached else "否",
        "耗时秒": round(time.time() - t0, 1),
    }
    return row, diag, result


# ======================================================================
# 汇总与报告
# ======================================================================

def aggregate(rows: list[dict], results: list) -> dict:
    """全池汇总 —— 收益仍按**笔**统计（允许重叠持仓，见模块 docstring）

    收益直接取自 `ScanResult.trades`（未平仓的不计入），不用逐只统计的
    四舍五入值反算 —— 那样 50 只的舍入误差会累到汇总里。
    """
    ok = [r for r in rows if "错误" not in r]
    trades = sum(r["买点"] for r in ok)
    closed = sum(r["已平仓"] for r in ok)
    rets = [t.return_pct for res in results for t in res.trades
            if t.return_pct is not None]
    wins = [r for r in rets if r > 0]
    return {
        "标的数": len(rows),
        "出错": len(rows) - len(ok),
        "有买点的标的": sum(1 for r in ok if r["买点"] > 0),
        "总买点": trades,
        "已平仓": closed,
        "未平仓": trades - closed,
        "胜率%": round(len(wins) / len(rets) * 100, 1) if rets else None,
        "平均收益%": round(sum(rets) / len(rets), 2) if rets else None,
        "最佳%": round(max(rets), 2) if rets else None,
        "最差%": round(min(rets), 2) if rets else None,
        "最大同时持仓": max((r["最大同时持仓"] for r in ok), default=0),
    }


def _fmt(v, nd=2) -> str:
    if v is None:
        return "--"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


# ----------------------------------------------------------------------
# 窗口阶梯：一次宽窗口取数 → 一次算出所有 W2 取值的等效成绩
# ----------------------------------------------------------------------

def offset_return_table(results: list, buckets=None) -> list[dict]:
    """按 ``|gap_2tom|`` 分桶统计收益

    每个桶是**独立**的一段（不累加），用来看"错开多远的那批笔长得怎么样"。
    """
    if buckets is None:
        buckets = [(0, 0), (1, 2), (3, 5), (6, 10), (11, 20), (21, 10 ** 6)]

    pairs: list[tuple[int, float]] = []
    for res in results:
        for t in res.trades:
            if t.return_pct is None or t.buy.gap_2tom is None:
                continue
            pairs.append((t.buy.gap_2tom, t.return_pct))

    out: list[dict] = []
    for lo, hi in buckets:
        sel = [r for g, r in pairs if lo <= abs(g) <= hi]
        label = f"{lo}" if lo == hi else (f"{lo}~{hi}" if hi < 10 ** 6 else f">{lo - 1}")
        out.append({
            "偏移区间": label,
            "笔数": len(sel),
            "胜率%": round(len([r for r in sel if r > 0]) / len(sel) * 100, 1) if sel else None,
            "平均收益%": round(sum(sel) / len(sel), 2) if sel else None,
            "最佳%": round(max(sel), 2) if sel else None,
            "最差%": round(min(sel), 2) if sel else None,
        })
    return out


def window_ladder(results: list, thresholds=(0, 2, 5, 10, 15, 20, 30)) -> list[dict]:
    """把同一批笔按 ``|gap_2tom| <= N`` **累加**，给出每个 W2 取值的等效成绩

    为什么一次宽窗口的取数就能算出全部 W2 取值：
    策略在窗口内取的是**最近**的那个 30 分钟买点，所以放宽窗口只会**新增**
    原先配不上的日线二买，不会改变已有配对的选择。⇒ W2 小的结果集是 W2 大的
    **子集**，单调嵌套。于是 `--max-gap-2tom 30` 跑一轮，把结果按 N 过滤累加，
    就等于分别用每个 N 跑一轮。

    ⚠️ 这条等价性依赖 `match_buy_points` "取最近" 的选取规则。改那个规则
    （比如改成"取最远"或"取第一个"）会让本节失效 —— `tests/test_scan_pool.py`
    用「阶梯 N=当前窗口 必须等于直接跑该窗口」把这条钉住。
    """
    pairs: list[tuple[int, float]] = []
    for res in results:
        for t in res.trades:
            if t.return_pct is None or t.buy.gap_2tom is None:
                continue
            pairs.append((abs(t.buy.gap_2tom), t.return_pct))

    out: list[dict] = []
    for n in thresholds:
        sel = [r for g, r in pairs if g <= n]
        out.append({
            "W2": f"±{n}",
            "笔数": len(sel),
            "胜率%": round(len([r for r in sel if r > 0]) / len(sel) * 100, 1) if sel else None,
            "平均收益%": round(sum(sel) / len(sel), 2) if sel else None,
            "最佳%": round(max(sel), 2) if sel else None,
            "最差%": round(min(sel), 2) if sel else None,
        })
    return out


def trade_rows(results: list) -> list[dict]:
    """逐笔明细（供 CSV 审计：每个买点的偏移与收益，可自行分桶复核）"""
    rows: list[dict] = []
    for res in results:
        for t in res.trades:
            rows.append({
                "代码": res.code,
                "一买": t.buy.d1,
                "二买": t.buy.d2,
                "成交日": t.buy.dt,
                "成交价": t.buy.price,
                "间隔1to2": t.buy.gap_1to2,
                "偏移2tom": t.buy.gap_2tom,
                "卖点日": t.sell.dt if t.sell else "",
                "收益率%": round(t.return_pct, 2) if t.return_pct is not None else "",
            })
    return rows


def _dist_block(title: str, values: list[int], thresholds: list[int]) -> list[str]:
    """分布段落 —— 结论（覆盖率）在前，样本摘要在后"""
    st = offset_stats(values)
    cov = coverage(values, thresholds)
    out = [f"**{title}**", ""]
    if not st["样本数"]:
        out += ["样本数 0（本池没有可统计的配对）", ""]
        return out
    out.append(f"- 样本 {st['样本数']} 个；范围 {st['最小']} ~ {st['最大']} 交易日，"
               f"中位 {_fmt(st['中位'], 1)}，P25/P75 {_fmt(st['P25'], 1)}/{_fmt(st['P75'], 1)}，"
               f"P90 {_fmt(st['P90'], 1)}")
    out.append("- 覆盖率（窗口取到该值时能捞到的比例）："
               + "，".join(f"±{t}: {cov[t]:.0%}" for t in thresholds))
    out.append("")
    return out


def render_report(rows: list[dict], diags: list[dict], results: list, *,
                  source: str,
                  max_gap_1to2: int, max_gap_2tom: int, pool_path: Path,
                  started: datetime) -> str:
    agg = aggregate(rows, results)
    gaps = [g for d in diags for g in d.get("一买到二买间隔", [])]
    offs = [o for d in diags for o in d.get("二买到30分钟二买偏移", [])]
    no_m30 = sum(d.get("无对应30分钟买点的二买数", 0) for d in diags)

    lines: list[str] = []
    lines.append(f"# 标的池扫描报告（{source} 源，窗口 {max_gap_1to2} / ±{max_gap_2tom}）")
    lines.append("")
    lines.append(f"- 生成时间：{started:%Y-%m-%d %H:%M}")
    lines.append(f"- 标的池：`{pool_path.name}`（{agg['标的数']} 只，出错 {agg['出错']} 只）")
    lines.append(f"- 数据：30 分钟 K 线（新浪源，前复权）")
    lines.append("")
    lines.append("## 1. 汇总（按**笔**统计，非资金曲线）")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("| --- | --- |")
    for k in ("有买点的标的", "总买点", "已平仓", "未平仓", "胜率%", "平均收益%",
              "最佳%", "最差%", "最大同时持仓"):
        lines.append(f"| {k} | {_fmt(agg[k])} |")
    lines.append("")
    lines.append("## 2. 逐标的")
    lines.append("")
    cols = ["代码", "名称", "30分钟根数", "交易日", "一买", "二买", "30分二买",
            "买点", "已平仓", "胜率%", "平均收益%", "最大同时持仓", "缓存"]
    lines.append("| " + " | ".join(cols) + " |")
    lines.append("| " + " | ".join("---" for _ in cols) + " |")
    for r in rows:
        if "错误" in r:
            lines.append(f"| {r['代码']} | {r.get('名称','')} | ⚠️ {r['错误']} |"
                         + " |" * (len(cols) - 3))
            continue
        lines.append("| " + " | ".join(_fmt(r.get(c)) for c in cols) + " |")
    lines.append("")
    lines.append("## 3. 窗口标定诊断")
    lines.append("")
    lines.append(f"「无对应 30 分钟买点的日线二买」共 **{no_m30}** 个"
                 f"（最近一个次级别买点也在 ±60 交易日之外）。")
    lines.append("")
    lines += _dist_block("一买 → 二买 间隔（交易日，恒正）", gaps, [15, 20, 30, 40, 60])
    lines += _dist_block("日线二买 → 最近 30 分钟二买 偏移（交易日，**有符号**，"
                         "负 = 次级别买点更早）", offs, [2, 5, 10, 15, 20])
    lines.append("覆盖率读法（**仅供参考，不要据此定窗口**）：覆盖率高的档位表示"
                 "「窗口取到该值时能捞到多少比例的日线二买」。但**捞得多 ≠ 赚得多** —— "
                 "真正该看的是下面两节。")
    lines.append("")

    # ---- 偏移分桶：错开多远的那批笔各自长什么样 ----
    obt = offset_return_table(results)
    lines.append("### 3.1 偏移 → 收益（按 `|偏移|` 分段，不累加）")
    lines.append("")
    lines.append("| |偏移| 区间 | 笔数 | 胜率% | 平均收益% | 最佳% | 最差% |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for r in obt:
        lines.append(f"| {r['偏移区间']} | {r['笔数']} | {_fmt(r['胜率%'])} | "
                     f"{_fmt(r['平均收益%'])} | {_fmt(r['最佳%'])} | {_fmt(r['最差%'])} |")
    lines.append("")

    # ---- 窗口阶梯：每个 W2 取值的等效成绩 ----
    ladder = window_ladder(results)
    lines.append("### 3.2 窗口阶梯（按 `|偏移| <= N` **累加** = 每个 W2 取值的等效成绩）")
    lines.append("")
    lines.append("| W2 | 笔数 | 胜率% | 平均收益% | 最佳% | 最差% |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for r in ladder:
        lines.append(f"| {r['W2']} | {r['笔数']} | {_fmt(r['胜率%'])} | "
                     f"{_fmt(r['平均收益%'])} | {_fmt(r['最佳%'])} | {_fmt(r['最差%'])} |")
    lines.append("")
    lines.append("等价性：本表由**一次宽窗口取数**（`--max-gap-2tom` 取到最大档）"
                 "按 N 过滤累加得出。成立的前提是策略取窗口内**最近**的次级别买点 ⇒ "
                 "W2 小的结果集是 W2 大的子集。改选取规则会让本表失效。")
    lines.append("读法：**别只看笔数**。若某档收益显著更差，说明那个距离段的配对是噪声，"
                 "窗口就该收在它之前；若各档差不多，说明窗口大小对质量不敏感，"
                 "取小的（少而精）即可。")
    lines.append("")
    return "\n".join(lines)


# ======================================================================
# 入口
# ======================================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="标的池批量扫描（缠论多周期共振策略实测 / 窗口标定）")
    p.add_argument("--pool", type=Path, default=DEFAULT_POOL, help="标的池文件")
    p.add_argument("--source", default=chan_strategy.SOURCE_GEOMETRY,
                   choices=[chan_strategy.SOURCE_GEOMETRY, chan_strategy.SOURCE_SIGNAL],
                   help="取点来源（默认 geometry）")
    p.add_argument("--max-gap-1to2", type=int,
                   default=chan_strategy.DEFAULT_MAX_GAP_1TO2,
                   help="一买 → 二买 最大间隔（交易日）")
    p.add_argument("--max-gap-2tom", type=int,
                   default=chan_strategy.DEFAULT_MAX_GAP_2TOM,
                   help="二买 ↔ 30 分钟二买 双向窗口半径（交易日）")
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE, help="行情缓存目录")
    p.add_argument("--no-cache", action="store_true", help="忽略缓存，强制重新取数")
    p.add_argument("--cache-days", type=int, default=0,
                   help="缓存容忍天数（0=只认今天；跨日复查可用 3，避免整轮重取撞限流）")
    p.add_argument("--sleep", type=float, default=1.5,
                   help="每次成功取数后的间隔秒数（新浪会限流，别调太小）")
    p.add_argument("--attempts", type=int, default=3, help="取数失败重试次数")
    p.add_argument("--out", type=Path, default=None,
                   help="报告输出路径（默认 outputs/pool_<source>_<g1>_<g2>.md）")
    p.add_argument("--limit", type=int, default=0, help="只扫前 N 只（调试用）")
    p.add_argument("--codes", default="",
                   help="只扫指定代码（逗号分隔，名称从池里取）—— "
                        "用于定向补跑被限流打掉的那几只")
    p.add_argument("--dump-trades", type=Path, default=None,
                   help="逐笔明细 CSV（含每个买点的有符号偏移与收益），便于自行分桶复核")
    return p


def select_pool(pool: list[tuple[str, str]], *, codes: str = "",
                limit: int = 0) -> list[tuple[str, str]]:
    """按 `--codes` / `--limit` 裁剪标的池（都不给就全量）"""
    if codes:
        want = {chan_viz.normalize_code(c) for c in
                (x.strip() for x in codes.split(",")) if c}
        name_of = dict(pool)
        return [(c, name_of.get(c, "")) for c in sorted(want)]
    return pool[:limit] if limit else pool


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    started = datetime.now()

    pool = load_pool(args.pool)
    pool = select_pool(pool, codes=args.codes, limit=args.limit)
    if not pool:
        print(f"标的池为空：{args.pool}", file=sys.stderr)
        return 2

    print(f"标的池 {len(pool)} 只 | 源 {args.source} | "
          f"窗口 {args.max_gap_1to2} / ±{args.max_gap_2tom}")
    rows: list[dict] = []
    diags: list[dict] = []
    results: list = []
    for i, (code, name) in enumerate(pool, 1):
        row, diag, result = scan_one(code, name, source=args.source,
                                    max_gap_1to2=args.max_gap_1to2,
                                    max_gap_2tom=args.max_gap_2tom,
                                    cache_dir=args.cache_dir,
                                    use_cache=not args.no_cache,
                                    cache_days=args.cache_days,
                                    polite_sleep=args.sleep,
                                    attempts=args.attempts)
        rows.append(row)
        if diag:
            diags.append(diag)
        if result is not None:
            results.append(result)
        flag = row.get("错误") or (f"买点 {row['买点']} 笔"
                                  + (f" 均收益 {row['平均收益%']:+.2f}%"
                                     if row.get("平均收益%") is not None else ""))
        print(f"[{i}/{len(pool)}] {code} {name}: {flag}", flush=True)

    report = render_report(rows, diags, results, source=args.source,
                           max_gap_1to2=args.max_gap_1to2,
                           max_gap_2tom=args.max_gap_2tom,
                           pool_path=args.pool, started=started)
    out = args.out or (ROOT / "outputs" /
                       f"pool_{args.source}_{args.max_gap_1to2}_{args.max_gap_2tom}.md")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")

    if args.dump_trades:
        detail = pd.DataFrame(trade_rows(results))
        dump = Path(args.dump_trades)
        dump.parent.mkdir(parents=True, exist_ok=True)
        detail.to_csv(dump, index=False, encoding="utf-8-sig")
        print(f"逐笔明细：{dump}（{len(detail)} 行）")

    agg = aggregate(rows, results)
    print("\n" + "=" * 56)
    print(f"汇总：有买点 {agg['有买点的标的']}/{agg['标的数']} 只 | "
          f"总买点 {agg['总买点']} | 已平仓 {agg['已平仓']} | "
          f"胜率 {agg['胜率%']} | 平均收益 {agg['平均收益%']}%")
    print(f"报告：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
