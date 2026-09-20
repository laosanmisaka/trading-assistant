# -*- coding: utf-8 -*-
"""标的池扫描脚本（`scripts/scan_pool.py`）的纯逻辑测试

不联网：只测标的池解析、窗口诊断统计、汇总与报告渲染。
取数（akshare）与 czsc 计算不在本文件覆盖范围内 —— 那部分按
`scripts/scan_pool.py` 的实际运行验证（带磁盘缓存，可重复跑）。

为什么值得单独测：**窗口标定**是拿这个脚本的输出做决策的，
统计口径写错会直接把参数带偏（例如把「没有对应次级别买点」的二买
也算进偏移分布，就会把窗口越推越大）。
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "scan_pool.py"


@pytest.fixture(scope="module")
def sp():
    """按路径加载脚本模块（scripts/ 不是包）"""
    spec = importlib.util.spec_from_file_location("scan_pool", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scan_pool"] = mod
    spec.loader.exec_module(mod)
    return mod


# ----------------------------------------------------------------------
# 标的池解析
# ----------------------------------------------------------------------

def test_load_pool_skips_comments_and_dedupes(sp, tmp_path):
    f = tmp_path / "pool.txt"
    f.write_text(
        "# 注释行\n"
        "\n"
        "600519 贵州茅台\n"
        "sh600519 重复的茅台\n"
        "000001\n"
        "  \n"
        "300750 宁德时代  # 行尾注释\n",
        encoding="utf-8")

    pool = sp.load_pool(f)

    assert pool == [("sh600519", "贵州茅台"),
                    ("sz000001", ""),
                    ("sz300750", "宁德时代")], (
        "裸 6 位代码要补前缀、重复代码要丢掉、行尾注释要去掉")


def test_select_pool_by_codes(sp):
    pool = [("sh600519", "贵州茅台"), ("sz000001", "平安银行")]

    assert sp.select_pool(pool, codes="600519") == [("sh600519", "贵州茅台")]
    assert sp.select_pool(pool, codes="sz000001,sh600519") == [
        ("sh600519", "贵州茅台"), ("sz000001", "平安银行")]
    assert sp.select_pool(pool, limit=1) == [("sh600519", "贵州茅台")]
    assert sp.select_pool(pool) == pool


# ----------------------------------------------------------------------
# 统计
# ----------------------------------------------------------------------

def test_offset_stats_empty(sp):
    st = sp.offset_stats([])
    assert st["样本数"] == 0 and st["中位"] is None


def test_offset_stats_distribution(sp):
    st = sp.offset_stats([-3, 0, 1, 5])
    assert st["样本数"] == 4
    assert st["最小"] == -3 and st["最大"] == 5
    assert st["中位"] == 0.5
    assert st["绝对值中位"] == 2.0, "有符号样本的中位可能骗人，绝对值中位才是窗口要用的"


def test_coverage_uses_absolute_offset(sp):
    cov = sp.coverage([-9, -1, 0, 2, 11], [2, 10, 20])
    assert cov[2] == 0.6
    assert cov[10] == 0.8
    assert cov[20] == 1.0
    assert sp.coverage([], [2]) == {2: None}


# ----------------------------------------------------------------------
# 窗口诊断（标定的核心）
# ----------------------------------------------------------------------

def _days(*dates):
    return list(dates)


def test_diagnose_gap_and_signed_offset(sp):
    days = _days(*[f"2026-01-{d:02d}" for d in range(1, 29)])
    d1 = ["2026-01-02"]
    d2 = ["2026-01-20"]                       # 间隔 18 个交易日
    m30 = ["2026-01-15"]                      # 早于 d2 5 个交易日 → 负偏移

    diag = sp.diagnose(days, d1, d2, m30)

    assert diag["一买到二买间隔"] == [18]
    assert diag["二买到30分钟二买偏移"] == [-5], "次级别买点更早时必须给负数"
    assert diag["无对应30分钟买点的二买数"] == 0


def test_diagnose_picks_nearest_m30(sp):
    days = _days(*[f"2026-01-{d:02d}" for d in range(1, 29)])
    diag = sp.diagnose(days, ["2026-01-02"], ["2026-01-20"],
                       ["2026-01-05", "2026-01-19", "2026-01-26"])
    assert diag["二买到30分钟二买偏移"] == [-1], "要取离 d2 最近的那个"


def test_diagnose_ignores_d2_without_d1(sp):
    """没有一买配对的二买不进统计 —— 否则窗口会被推大"""
    days = _days(*[f"2026-01-{d:02d}" for d in range(1, 29)])
    # d1 在 d2 之后（或超出 60 日窗口）都不算配对
    diag = sp.diagnose(days, ["2026-01-25"], ["2026-01-20"], ["2026-01-21"])
    assert diag["一买到二买间隔"] == [] and diag["二买到30分钟二买偏移"] == []


def test_diagnose_counts_d2_without_any_m30(sp):
    days = _days(*[f"2026-01-{d:02d}" for d in range(1, 29)])
    diag = sp.diagnose(days, ["2026-01-02"], ["2026-01-20"], [])
    assert diag["无对应30分钟买点的二买数"] == 1
    assert diag["二买到30分钟二买偏移"] == []


def test_diagnose_counts_m30_outside_wide_window(sp):
    """最近的次级别买点在 ±60 交易日之外 → 等于没有"""
    days = _days(*[f"2026-{m:02d}-{d:02d}"
                   for m in range(1, 7) for d in range(1, 29)])
    diag = sp.diagnose(days, ["2026-01-02"], ["2026-01-05"], ["2026-06-20"])
    assert diag["无对应30分钟买点的二买数"] == 1


# ----------------------------------------------------------------------
# 汇总与报告
# ----------------------------------------------------------------------

def _result(code, rets):
    from core.chan_strategy import BuySignal, ScanResult, SellSignal, Trade

    res = ScanResult(code=code, source="geometry")
    for i, r in enumerate(rets):
        buy = BuySignal(dt=f"2026-01-{i + 2:02d} 10:30", price=10.0,
                        d1="2026-01-02", d2="2026-01-05",
                        gap_1to2=3, gap_2tom=0)
        if r is None:
            res.trades.append(Trade(buy=buy))
        else:
            res.trades.append(Trade(buy=buy, sell=SellSignal(
                dt=f"2026-02-{i + 2:02d}", price=10.0 * (1 + r / 100),
                ma="MA10", ma_value=9.0, hold_days=20)))
    return res


def test_aggregate_counts_all_trades_exactly(sp):
    rows = [{"代码": "sh600519", "买点": 2, "已平仓": 2, "最大同时持仓": 1},
            {"代码": "sz000001", "买点": 1, "已平仓": 0, "最大同时持仓": 1}]
    results = [_result("sh600519", [10.0, -5.0]), _result("sz000001", [None])]

    agg = sp.aggregate(rows, results)

    assert agg["总买点"] == 3 and agg["已平仓"] == 2 and agg["未平仓"] == 1
    assert agg["胜率%"] == 50.0
    assert agg["平均收益%"] == 2.5
    assert agg["最佳%"] == 10.0 and agg["最差%"] == -5.0


def test_aggregate_with_no_trades(sp):
    agg = sp.aggregate([], [])
    assert agg["总买点"] == 0 and agg["胜率%"] is None


def test_render_report_contains_calibration_sections(sp, tmp_path):
    rows = [{"代码": "sh600519", "名称": "贵州茅台", "30分钟根数": 1970,
             "交易日": 247, "一买": 1, "二买": 1, "30分二买": 8, "买点": 1,
             "已平仓": 1, "胜率%": 100.0, "平均收益%": 5.0,
             "最大同时持仓": 1, "重叠笔数": 0, "缓存": "是", "耗时秒": 2.0}]
    diags = [{"一买到二买间隔": [18], "二买到30分钟二买偏移": [-5],
              "无对应30分钟买点的二买数": 3}]

    report = sp.render_report(rows, diags, [_result("sh600519", [5.0])],
                              source="geometry", max_gap_1to2=40, max_gap_2tom=10,
                              pool_path=tmp_path / "pool.txt",
                              started=datetime(2026, 9, 18, 17, 0))

    assert "窗口 40 / ±10" in report
    assert "## 3. 窗口标定诊断" in report
    assert "无对应 30 分钟买点的日线二买" in report and "**3**" in report
    assert "覆盖率" in report
    assert "非资金曲线" in report, "按笔统计的口径必须写在报告里，否则数字会被误读"


# ----------------------------------------------------------------------
# 缓存日期门控（--cache-days）
# ----------------------------------------------------------------------

def _write_cache(sp, cache_dir, code, *, days_ago):
    """造一份缓存文件并把 mtime 拨到 N 天前"""
    import os
    import time as _time

    sym = sp.chan_viz.normalize_code(code)
    f = sp._cache_file(cache_dir, sym)
    cache_dir.mkdir(parents=True, exist_ok=True)
    f.write_text("dt,open,high,low,close,volume\n"
                 "2026-09-18 10:00:00,10.0,10.5,9.8,10.2,1000\n", encoding="utf-8")
    ts = _time.time() - days_ago * 86400
    os.utime(f, (ts, ts))
    return f


def _no_network(sp, monkeypatch):
    """把取数封死 —— 一旦真去联网，测试会以 RuntimeError 暴露出来

    `time` 只在脚本命名空间里替换，不污染全局 `time.sleep`。
    """
    import types

    def _boom(*a, **kw):
        raise AssertionError("不应触发联网取数")

    monkeypatch.setattr(sp.chan_viz, "fetch_klines", _boom)
    monkeypatch.setattr(sp, "time", types.SimpleNamespace(sleep=lambda *_: None))


def test_cache_hit_only_when_same_day(sp, tmp_path, monkeypatch):
    """cache_days=0（默认）只认今天的缓存"""
    cache = tmp_path / "cache"
    _write_cache(sp, cache, "sh600519", days_ago=0)
    _no_network(sp, monkeypatch)

    klines, cached = sp.load_klines("sh600519", cache_dir=cache)
    assert cached is True and len(klines) == 1


def test_cache_days_accepts_stale_cache(sp, tmp_path, monkeypatch):
    """跨日复查：cache_days 放宽后旧缓存仍可用（周末重跑不必整轮取数）"""
    cache = tmp_path / "cache"
    _write_cache(sp, cache, "sh600519", days_ago=3)
    _no_network(sp, monkeypatch)

    klines, cached = sp.load_klines("sh600519", cache_dir=cache, cache_days=3)
    assert cached is True and len(klines) == 1

    # 同一份文件，不放宽就必须真的去取数（这里被 _no_network 拦下）
    with pytest.raises(RuntimeError, match="取数失败"):
        sp.load_klines("sh600519", cache_dir=cache, cache_days=0, attempts=2)


def test_no_cache_bypasses_fresh_cache(sp, tmp_path, monkeypatch):
    """use_cache=False 时即使是当天的缓存也不读"""
    cache = tmp_path / "cache"
    _write_cache(sp, cache, "sh600519", days_ago=0)
    _no_network(sp, monkeypatch)

    with pytest.raises(RuntimeError, match="取数失败"):
        sp.load_klines("sh600519", cache_dir=cache, use_cache=False, attempts=2)


# ----------------------------------------------------------------------
# 窗口阶梯（一次宽窗口取数 → 算出所有 W2 取值的等效成绩）
# ----------------------------------------------------------------------

def _result_with_offsets(code, pairs):
    """pairs = [(gap_2tom, 收益率%或 None)]；None 表示未平仓"""
    from core.chan_strategy import BuySignal, ScanResult, SellSignal, Trade

    res = ScanResult(code=code, source="geometry")
    for i, (gap, r) in enumerate(pairs):
        buy = BuySignal(dt=f"2026-01-{i + 2:02d} 10:30", price=10.0,
                        d1="2026-01-02", d2="2026-01-05",
                        gap_1to2=3, gap_2tom=gap)
        if r is None:
            res.trades.append(Trade(buy=buy))
        else:
            res.trades.append(Trade(buy=buy, sell=SellSignal(
                dt=f"2026-02-{i + 2:02d}", price=10.0 * (1 + r / 100),
                ma="MA10", ma_value=9.0, hold_days=20)))
    return res


def test_window_ladder_accumulates_by_absolute_offset(sp):
    """阶梯按 |偏移| 累加 —— 每个 N 的行等于直接跑该窗口的结果"""
    results = [_result_with_offsets("sh600519", [
        (0, 10.0), (1, -10.0), (-3, 20.0), (-8, 30.0), (15, 40.0),
    ])]

    ladder = {r["W2"]: r for r in sp.window_ladder(results, thresholds=(0, 2, 5, 10))}

    assert ladder["±0"]["笔数"] == 1                     # 只有偏移 0
    assert ladder["±2"]["笔数"] == 2                     # +0, ±1
    assert ladder["±5"]["笔数"] == 3                     # +|−3|
    assert ladder["±10"]["笔数"] == 4                    # +|−8|，不含 15
    assert ladder["±10"]["平均收益%"] == 12.5             # (10-10+20+30)/4
    assert ladder["±2"]["胜率%"] == 50.0                  # +10 与 −10 各一


def test_window_ladder_win_rate_and_open_trades(sp):
    """未平仓的不计收益率；胜率按已平仓算"""
    results = [_result_with_offsets("sh600519", [
        (1, 10.0), (2, -5.0), (3, None),
    ])]

    row = [r for r in sp.window_ladder(results, thresholds=(5,))][0]

    assert row["笔数"] == 2, "未平仓的不该进阶梯"
    assert row["胜率%"] == 50.0
    assert row["平均收益%"] == 2.5
    assert row["最佳%"] == 10.0 and row["最差%"] == -5.0


def test_window_ladder_is_monotone_in_trade_count(sp):
    """笔数必须随 N 单调不减 —— 这是"阶梯等价于分别跑"的前提"""
    results = [_result_with_offsets("sh600519", [(g, 1.0) for g in (0, 1, 4, 9, 19)])]

    counts = [r["笔数"] for r in sp.window_ladder(results, (0, 2, 5, 10, 20))]

    assert counts == sorted(counts)


def test_offset_return_table_buckets_are_disjoint(sp):
    """分桶互不重叠，且桶外样本不进任何桶"""
    results = [_result_with_offsets("sh600519", [
        (0, 1.0), (2, 2.0), (5, 3.0), (10, 4.0), (999, 5.0),
    ])]

    tbl = {r["偏移区间"]: r for r in sp.offset_return_table(
        results, buckets=[(0, 0), (1, 2), (3, 10)])}

    assert tbl["0"]["笔数"] == 1
    assert tbl["1~2"]["笔数"] == 1
    assert tbl["3~10"]["笔数"] == 2      # 3 与 10 都落进来，且不含 999
    assert sum(r["笔数"] for r in tbl.values()) == 4


def test_trade_rows_carries_offset_for_audit(sp):
    """逐笔明细必须带上有符号偏移，便于自行分桶复核"""
    results = [_result_with_offsets("sh600519", [(-8, 12.5), (3, None)])]

    rows = sp.trade_rows(results)

    assert len(rows) == 2
    assert rows[0]["偏移2tom"] == -8 and rows[0]["收益率%"] == 12.5
    assert rows[1]["偏移2tom"] == 3 and rows[1]["收益率%"] == "", "未平仓的收益留空"


def test_window_ladder_handles_empty(sp):
    """空输入不抛异常（全池取数失败时报告仍要能生成）"""
    assert sp.window_ladder([], (0, 5))[0]["笔数"] == 0
    assert sp.offset_return_table([])[0]["笔数"] == 0
    assert sp.trade_rows([]) == []
