"""缠论多周期共振策略测试

分两层（见 docs/STRATEGY_CHAN_MULTIFREQ.md）：
- 纯逻辑层：窗口匹配、状态跃迁、均线止损 —— 不依赖 czsc，覆盖全部边界
- 集成层：用合成 30 分钟数据跑完整 scan（czsc 未安装时跳过）
"""

from datetime import date, timedelta

import pandas as pd
import pytest

from core.chan_strategy import (
    BuySignal, ScanResult, SellSignal, Trade,
    CONTAINS_BUY1, CONTAINS_BUY2,
    SOURCE_GEOMETRY, SOURCE_SIGNAL,
    _effective_day,
    _entry_positions,
    daily_state_from_bars,
    match_buy_points,
    match_sell_points,
    points_from_chan,
    position_overlap_stats,
    scan_from_frame,
    signal_columns,
    build_signals_config,
)

COLS = signal_columns()
BUY1_COL = COLS["daily_buy1"]
BUY2_COL = COLS["daily_buy2"]
M30_COL = COLS["m30_buy2"]

OTHER = "其他_任意_任意_0"
D1_HIT = "一买_19笔_任意_0"
D2_HIT = "二买_任意_任意_0"
D2_SELL = "二卖_任意_任意_0"
# 30 分钟二买列的信号文本与日线二买相同，仅列名不同
M30_HIT = "二买_任意_任意_0"


# ----------------------------------------------------------------------
# 构造工具
# ----------------------------------------------------------------------

def build_out(day_plan, bars_per_day=4, base_close=10.0):
    """把"每天的信号取值"展开成逐 bar 的信号表

    day_plan: list of dict
        day   : 交易日字符串
        d1/d2/m30 : 全天统一取值，或 list 给出逐根取值
        closes: list 逐根收盘价（缺省 base_close 恒定）
    """
    rows = []
    for spec in day_plan:
        day = spec["day"]
        closes = spec.get("closes") or [base_close] * bars_per_day
        for j in range(bars_per_day):
            def val(key):
                v = spec.get(key, OTHER)
                if not isinstance(v, list):
                    return v
                # 列表比 bar 数短时，尾部复用最后一个值
                return v[j] if j < len(v) else v[-1]
            minutes = 30 + 10 * j          # 09:30 / 09:40 / 09:50 / 10:00 ...
            rows.append({
                "dt": f"{day} {9 + minutes // 60:02d}:{minutes % 60:02d}:00",
                "day": day,
                "close": closes[j],
                BUY1_COL: val("d1"),
                BUY2_COL: val("d2"),
                M30_COL: val("m30"),
            })
    return pd.DataFrame(rows)


def days_from(start, n):
    """从 start 起生成 n 个连续"交易日"（跳过周末，便于断言）"""
    out = []
    d = date.fromisoformat(start) if isinstance(start, str) else start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def seq(start, n):
    """n 个交易日字符串"""
    return [d.isoformat() for d in days_from(start, n)]


# ----------------------------------------------------------------------
# _entry_positions：状态跃迁
# ----------------------------------------------------------------------

def test_entry_positions_counts_transition_not_state():
    """持续状态只算一次跃迁 —— 这是策略的核心前提"""
    hit = pd.Series([False, False, True, True, True, False, True])
    assert _entry_positions(hit) == [2, 6]


def test_entry_positions_first_bar_hit():
    """首根即为真也算一次跃迁"""
    assert _entry_positions(pd.Series([True, True, False])) == [0]


def test_entry_positions_all_false():
    assert _entry_positions(pd.Series([False, False])) == []


def test_entry_positions_nan_treated_as_false():
    """NaN 视为未触发，不抛异常"""
    hit = pd.Series([None, True, True])
    assert _entry_positions(hit) == [1]


# ----------------------------------------------------------------------
# daily_state_from_bars：日线信号取当日首根
# ----------------------------------------------------------------------

def test_daily_state_takes_first_bar_of_day():
    """关键因果性断言：当日首根取得的值 == 开盘可读的值

    实测 czsc 的日线信号会在日内翻转（早盘"其他"、尾盘"二买"），
    策略必须取首根，否则回测会用到盘中才出现的信息。
    """
    out = build_out([
        {"day": "2024-01-02", "d2": OTHER},
        # 当日首根是"其他"，后续翻成"二买" —— 必须取"其他"
        {"day": "2024-01-03", "d2": [OTHER, OTHER, D2_HIT, D2_HIT]},
    ])
    state = daily_state_from_bars(out, BUY2_COL)
    assert state.loc["2024-01-03", BUY2_COL] == OTHER


def test_daily_state_one_row_per_day():
    out = build_out([{"day": f"2024-01-{d:02d}"} for d in (2, 3, 4)])
    state = daily_state_from_bars(out, BUY2_COL)
    assert list(state.index) == ["2024-01-02", "2024-01-03", "2024-01-04"]
    assert len(state) == 3


def test_daily_state_ignores_duplicate_day_order():
    """同一交易日的多根 bar 只贡献一行首根值"""
    out = build_out([
        {"day": "2024-01-02", "d2": [OTHER, D2_HIT]},
    ])
    state = daily_state_from_bars(out, BUY2_COL)
    assert len(state) == 1
    assert state.iloc[0][BUY2_COL] == OTHER


# ----------------------------------------------------------------------
# match_buy_points：三重条件的时序窗口
# ----------------------------------------------------------------------

def test_match_triggers_within_windows():
    """一买 → 15 日内二买 → 2 日内 30 分钟二买 ⇒ 触发"""
    days = seq("2024-01-01", 30)
    res = match_buy_points(
        days,
        d1_days=[days[0]],
        d2_days=[days[10]],
        m30_items=[(days[11], "bar-A")],
    )
    assert len(res) == 1
    assert res[0]["d1"] == days[0]
    assert res[0]["d2"] == days[10]
    assert res[0]["gap_1to2"] == 10
    assert res[0]["gap_2tom"] == 1


def test_match_gap_1to2_boundary_40_ok_41_rejected():
    """一买→二买：第 40 个交易日 OK，第 41 个算超窗

    2026-09-18 由 15 改为 40：实测 8 只标的的间隔为 6/17/27/33/34/34/50+，
    中位数 ≈ 33，原值卡掉 7/8。
    """
    days = seq("2024-01-01", 90)
    ok = match_buy_points(days, [days[0]], [days[40]], [(days[41], "b1")])
    assert len(ok) == 1 and ok[0]["gap_1to2"] == 40

    over = match_buy_points(days, [days[0]], [days[41]], [(days[42], "b2")])
    assert over == []


def test_match_gap_2tom_window_is_symmetric_10_ok_11_rejected():
    """30 分钟买点窗口是双向 ±10（2026-09-18 由「之后 2 日」改）

    几何法的 30 分钟买点常早于日线二买（次级别先转势），原实现只允许
    「其后」，方向是反的 → 这类点全被丢掉，8 只标的 0 笔。
    """
    days = seq("2024-01-01", 90)
    after = match_buy_points(days, [days[0]], [days[20]], [(days[30], "b1")])
    assert len(after) == 1 and after[0]["gap_2tom"] == 10

    before = match_buy_points(days, [days[0]], [days[20]], [(days[10], "b2")])
    assert len(before) == 1 and before[0]["gap_2tom"] == -10

    # 两侧第 11 个交易日都超窗
    assert match_buy_points(days, [days[0]], [days[20]], [(days[31], "b3")]) == []
    assert match_buy_points(days, [days[0]], [days[20]], [(days[9], "b4")]) == []


def test_match_picks_m30_nearest_to_d2():
    """窗口内有多个 30 分钟买点时取**离 d2 最近**的，而不是最早的那个

    取最早的那个会捞到窗口边缘（±10 日）的陈旧触发点，然后一直等到 d2
    才成交 —— 该点已经失效。
    """
    days = seq("2024-01-01", 90)
    res = match_buy_points(days, [days[0]], [days[20]],
                           [(days[12], "far"), (days[19], "near")])
    assert len(res) == 1
    assert res[0]["bar"] == "near"
    assert res[0]["gap_2tom"] == -1


def test_match_same_day_1to2_not_counted():
    """同日既是一买又是二买不算"一买之后" —— 窗口要求严格大于 0"""
    days = seq("2024-01-01", 20)
    res = match_buy_points(days, [days[3]], [days[3]], [(days[4], "b")])
    assert res == []


def test_match_m30_before_d2_allowed_within_window():
    """30 分钟买点早于日线二买 —— 双向窗口内允许配对，gap_2tom 为负

    ⚠️ 「允许配对」≠「可以在 d2 之前成交」。成交时点由 `scan_from_frame`
    取 ``max(触发 bar, d2 当日首根 bar)``，见 `test_buy_not_before_d2_confirm`。
    """
    days = seq("2024-01-01", 30)
    res = match_buy_points(days, [days[0]], [days[10]], [(days[5], "b")])
    assert len(res) == 1
    assert res[0]["gap_2tom"] == -5


def test_match_same_day_m30_allowed():
    """30 分钟二买与日线二买同日 —— gap=0，允许"""
    days = seq("2024-01-01", 30)
    res = match_buy_points(days, [days[0]], [days[10]], [(days[10], "b")])
    assert len(res) == 1 and res[0]["gap_2tom"] == 0


def test_match_picks_nearest_d1():
    """向前找最近的、窗口内的一买，不是最早的"""
    days = seq("2024-01-01", 40)
    res = match_buy_points(
        days,
        d1_days=[days[0], days[8]],   # 都为候选（8-0=8 > 15? 否；最近的是 days[8]）
        d2_days=[days[20]],
        m30_items=[(days[21], "b")],
    )
    assert len(res) == 1
    assert res[0]["d1"] == days[8]      # 最近的那个
    assert res[0]["gap_1to2"] == 12


def test_match_ignores_stale_d1():
    """一买太早（超出 40 日窗口）不能作为配对起点"""
    days = seq("2024-01-01", 90)
    res = match_buy_points(days, [days[0]], [days[45]], [(days[46], "b")])
    assert res == []


def test_match_one_bar_used_once():
    """同一个 30 分钟二买 bar 不能被两个配对重复消耗"""
    days = seq("2024-01-01", 40)
    res = match_buy_points(
        days,
        d1_days=[days[0]],
        d2_days=[days[10], days[11]],   # 两个二买日
        m30_items=[(days[12], "only-bar")],
        max_gap_2tom=2,
    )
    assert len(res) == 1                # 只触发一次
    assert res[0]["d2"] == days[10]     # 先到先得


def test_match_multiple_bars_same_day():
    """同一天有多根 30 分钟二买 bar 时，可分别支撑不同配对"""
    days = seq("2024-01-01", 40)
    res = match_buy_points(
        days,
        d1_days=[days[0], days[2]],
        d2_days=[days[6], days[14]],
        m30_items=[(days[7], "bar-1"), (days[15], "bar-2")],
    )
    assert len(res) == 2
    assert [m["bar"] for m in res] == ["bar-1", "bar-2"]
    # 各自配到**最近的**、窗口内的一买
    assert res[0]["gap_1to2"] == 4      # days[6] - days[2]
    assert res[1]["gap_1to2"] == 12     # days[14] - days[2]


def test_match_results_sorted_by_time():
    days = seq("2024-01-01", 60)
    res = match_buy_points(
        days,
        d1_days=[days[0], days[20]],
        d2_days=[days[3], days[23]],
        m30_items=[(days[4], "late-first-in-list"), (days[24], "earlier-in-list")],
    )
    assert [m["d3"] for m in res] == [days[4], days[24]]


def test_match_unknown_days_ignored():
    """不在交易日序列里的日期直接忽略，不抛异常"""
    days = seq("2024-01-01", 20)
    res = match_buy_points(days, ["1999-01-01"], [days[5]], [(days[6], "b")])
    assert res == []


def test_match_empty_inputs():
    assert match_buy_points([], [], [], []) == []


# ----------------------------------------------------------------------
# match_sell_points：均线移动止损
# ----------------------------------------------------------------------

def make_daily(days, closes, sma_short, sma_long):
    return pd.DataFrame(
        {"close": closes, "sma_short": sma_short, "sma_long": sma_long},
        index=days,
    )


def test_sell_when_both_mas_broken():
    """MA5、MA10 双双被跌破 → 卖出（确认日次一交易日成交）"""
    days = seq("2024-01-01", 6)
    daily = make_daily(
        days,
        closes=[10, 10, 10, 9.5, 9.0, 8.0],
        sma_short=[10, 10, 10, 10, 10, 10],
        sma_long=[10, 10, 10, 10, 10, 10],
    )
    res = match_sell_points(daily, days[0])
    assert res is not None
    assert res["confirm_dt"] == days[3]   # 两条都破的确认日
    assert res["dt"] == days[4]           # 实际成交日 = 确认日 + 1
    assert res["price"] == 9.0            # 用成交日收盘价，不是确认日
    assert res["ma"] == "MA10"
    assert res["hold_days"] == 4


def test_hold_when_short_ma_broken_but_long_ma_intact():
    """只破 MA5、MA10 完好 → 改用 MA10 继续持有"""
    days = seq("2024-01-01", 6)
    daily = make_daily(
        days,
        closes=[10, 10, 9.5, 9.5, 9.5, 9.5],
        sma_short=[10, 10, 10, 10, 10, 10],
        sma_long=[9.0, 9.0, 9.0, 9.0, 9.0, 9.0],   # 一直未破
    )
    assert match_sell_points(daily, days[0]) is None


def test_no_sell_when_price_above_all_mas():
    """价格始终在均线上方 → 未平仓"""
    days = seq("2024-01-01", 6)
    daily = make_daily(
        days, closes=[10, 11, 12, 13, 14, 15],
        sma_short=[9] * 6, sma_long=[8] * 6,
    )
    assert match_sell_points(daily, days[0]) is None


def test_sell_skips_buy_day():
    """买入当日不检查（避免用买入当天的收盘反手判定）"""
    days = seq("2024-01-01", 3)
    daily = make_daily(
        days, closes=[10, 10, 10],
        sma_short=[99, 10, 10],      # 买入日"跌破"但不该触发
        sma_long=[99, 10, 10],
    )
    assert match_sell_points(daily, days[0]) is None


def test_sell_returns_none_for_unknown_buy_day():
    days = seq("2024-01-01", 3)
    daily = make_daily(days, [10, 10, 10], [9, 9, 9], [8, 8, 8])
    assert match_sell_points(daily, "1999-01-01") is None


def test_sell_skips_nan_ma_rows():
    """均线尚未成型（NaN）的日子不参与判定"""
    days = seq("2024-01-01", 4)
    daily = make_daily(
        days, closes=[10, 10, 8, 8],
        sma_short=[float("nan"), 10, 10, 10],
        sma_long=[float("nan"), 10, 10, 10],
    )
    res = match_sell_points(daily, days[0])
    assert res is not None
    assert res["confirm_dt"] == days[2]
    assert res["dt"] == days[3]


def test_sell_returns_none_when_confirmed_on_last_day():
    """最后一天才确认跌破 → 无次日可成交，视为未平仓"""
    days = seq("2024-01-01", 4)
    daily = make_daily(
        days, closes=[10, 10, 10, 9.0],
        sma_short=[10, 10, 10, 10], sma_long=[10, 10, 10, 10],
    )
    assert match_sell_points(daily, days[0]) is None


def test_sell_delay_zero_executes_on_confirm_day():
    """exit_delay_days=0 时在确认日当天成交（对照用）"""
    days = seq("2024-01-01", 6)
    daily = make_daily(
        days, closes=[10, 10, 10, 9.5, 9.0, 8.0],
        sma_short=[10] * 6, sma_long=[10] * 6,
    )
    res = match_sell_points(daily, days[0], exit_delay_days=0)
    assert res["dt"] == days[3] == res["confirm_dt"]


# ----------------------------------------------------------------------
# 结果模型
# ----------------------------------------------------------------------

def test_trade_return_pct():
    buy = BuySignal(dt="2024-01-02 10:00", price=10.0, d1="2024-01-01",
                    d2="2024-01-05", gap_1to2=4, gap_2tom=1)
    sell = SellSignal(dt="2024-01-20", price=11.0, ma="MA5",
                      ma_value=10.5, hold_days=10)
    assert Trade(buy=buy, sell=sell).return_pct == pytest.approx(10.0)
    assert Trade(buy=buy, sell=None).return_pct is None


def test_scan_result_summary_counts():
    buy = BuySignal(dt="2024-01-02 10:00", price=10.0, d1="2024-01-01",
                    d2="2024-01-05", gap_1to2=4, gap_2tom=1)
    r = ScanResult(code="600000", trades=[
        Trade(buy=buy, sell=SellSignal("2024-02-01", 11.0, "MA5", 10.5, 5)),
        Trade(buy=buy, sell=SellSignal("2024-02-02", 9.0, "MA10", 9.5, 7)),
        Trade(buy=buy, sell=None),
    ])
    s = r.summary()
    assert s["买点数"] == 3
    assert s["已平仓"] == 2
    assert s["未平仓"] == 1
    assert s["胜率%"] == 50.0
    assert s["最佳%"] == pytest.approx(10.0)
    assert s["最差%"] == pytest.approx(-10.0)


def test_scan_result_summary_empty():
    s = ScanResult(code="600000").summary()
    assert s["买点数"] == 0
    assert s["胜率%"] is None


# ----------------------------------------------------------------------
# 同时持仓统计（2026-09-18 定案：每笔独立、允许重叠）
# ----------------------------------------------------------------------

def _trade(buy_dt, sell_dt=None, buy_px=10.0, sell_px=11.0, hold=5):
    """构造一笔交易；sell_dt=None 表示数据末尾仍未平仓"""
    buy = BuySignal(dt=buy_dt, price=buy_px, d1="2024-01-01", d2="2024-01-05",
                    gap_1to2=4, gap_2tom=1)
    sell = (SellSignal(dt=sell_dt, price=sell_px, ma="MA5",
                       ma_value=10.5, hold_days=hold)
            if sell_dt else None)
    return Trade(buy=buy, sell=sell)


def test_position_overlap_sequential_trades():
    """首尾相接、互不重叠 → 最多同时只持一笔"""
    stats = position_overlap_stats([
        _trade("2024-01-02", "2024-02-01"),
        _trade("2024-02-01", "2024-03-01"),   # 卖在买当天：换仓，不是重叠
        _trade("2024-03-05", "2024-04-01"),
    ])
    assert stats["最大同时持仓"] == 1
    assert stats["重叠笔数"] == 0
    assert stats["重叠对数"] == 0


def test_position_overlap_counts_concurrent_trades():
    """两笔交叠 → 最大同时持仓 2"""
    stats = position_overlap_stats([
        _trade("2024-01-02", "2024-03-01"),
        _trade("2024-02-01", "2024-04-01"),
    ])
    assert stats["最大同时持仓"] == 2
    assert stats["重叠笔数"] == 2
    assert stats["重叠对数"] == 1


def test_position_overlap_open_trade_spans_to_end():
    """未平仓的那笔视为持有到数据末尾，与后面所有买点重叠"""
    stats = position_overlap_stats([
        _trade("2024-01-02", None),
        _trade("2024-05-01", "2024-06-01"),
    ])
    assert stats["最大同时持仓"] == 2
    assert stats["重叠笔数"] == 2


def test_position_overlap_empty_and_single():
    assert position_overlap_stats([]) == {
        "最大同时持仓": 0, "重叠笔数": 0, "重叠对数": 0}
    assert position_overlap_stats([_trade("2024-01-02", "2024-02-01")]) == {
        "最大同时持仓": 1, "重叠笔数": 0, "重叠对数": 0}


def test_summary_exposes_overlap_metrics():
    """summary() 必须显式给出重叠指标

    允许重叠持仓是老三 2026-09-18 的定案（先验证买卖点，不做资金约束），
    但胜率/平均收益因此在口径上是「按笔统计」而非组合收益 —— 重叠笔数
    必须能让读者看到，否则数字会被读成资金曲线收益。
    """
    s = ScanResult(code="600000", trades=[
        _trade("2024-01-02", "2024-03-01"),
        _trade("2024-02-01", "2024-04-01"),
    ]).summary()
    assert s["最大同时持仓"] == 2
    assert s["重叠笔数"] == 2


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------

def test_signal_columns_follow_params():
    cols = signal_columns("EMA", 30)
    assert cols["daily_buy1"] == "日线_D1B_BUY1"
    assert "EMA#30" in cols["daily_buy2"]
    assert cols["m30_buy2"].startswith("30分钟")


def test_signals_config_covers_three_levels():
    cfg = build_signals_config()
    freqs = [c["freq"] for c in cfg]
    assert freqs.count("日线") == 2
    assert "30分钟" in freqs
    assert all("name" in c for c in cfg)


# ----------------------------------------------------------------------
# 集成：scan_from_frame 的端到端链路与成交延迟（不依赖 czsc）
# ----------------------------------------------------------------------

def chain_plan(days, d1_day, d2_day, m30_specs):
    """构造一条完整的三级信号链（closes 逐根递增，便于识别成交价）"""
    plan = []
    for d in days:
        spec = {"day": d, "closes": [10.0, 10.1, 10.2, 10.3]}
        if d == d1_day:
            spec["d1"] = D1_HIT
        if d == d2_day:
            spec["d2"] = D2_HIT
        if d in m30_specs:
            spec["m30"] = m30_specs[d]
        plan.append(spec)
    return plan


def test_buy_executes_on_next_bar_after_signal():
    """买点成交价取信号 bar 的**下一根** —— 要等 30 分钟 bar 收盘确认"""
    days = seq("2024-01-01", 20)
    out = build_out(chain_plan(
        days, days[0], days[10],
        {days[11]: [OTHER, OTHER, M30_HIT, M30_HIT]},
    ))
    res = scan_from_frame(out, code="T")
    assert len(res.trades) == 1
    b = res.trades[0].buy
    assert b.signal_dt == f"{days[11]} 09:50:00"   # 第 3 根 bar 确认
    assert b.dt == f"{days[11]} 10:00:00"          # 第 4 根 bar 成交
    assert b.price == 10.3
    assert b.gap_1to2 == 10
    assert b.gap_2tom == 1


def test_buy_skipped_when_signal_on_last_bar():
    """信号落在最后一根 bar → 无下一根可成交，不作为交易"""
    days = seq("2024-01-01", 20)
    out = build_out(chain_plan(
        days, days[0], days[18],
        {days[19]: [OTHER, OTHER, OTHER, M30_HIT]},
    ))
    res = scan_from_frame(out, code="T")
    assert res.trades == []
    assert len(res.m30_buy2_bars) == 1      # 信号本身仍被记录


def test_buy_chain_end_to_end_windows():
    """端到端：窗口全满足则成交，且 gap 记录正确"""
    days = seq("2024-01-01", 25)
    out = build_out(chain_plan(
        days, days[2], days[17],                 # gap_1to2 = 15（边界）
        {days[19]: [OTHER, M30_HIT, OTHER, OTHER]},   # gap_2tom = 2（边界）
    ))
    res = scan_from_frame(out, code="T")
    assert len(res.trades) == 1
    assert res.trades[0].buy.gap_1to2 == 15
    assert res.trades[0].buy.gap_2tom == 2


def test_no_trade_when_m30_window_missed():
    """30 分钟买点超出 ±10 双向窗口 → 不成交"""
    days = seq("2024-01-01", 40)
    out = build_out(chain_plan(
        days, days[0], days[10],
        {days[25]: [M30_HIT, OTHER, OTHER, OTHER]},   # gap_2tom = 15
    ))
    res = scan_from_frame(out, code="T")
    assert res.trades == []


def test_buy_not_before_d2_confirm():
    """成交时点不得早于「最后一个被确认的条件」（未来函数回归）

    双向窗口允许 30 分钟买点早于日线二买。若照搬「触发 bar 的下一根成交」，
    成交会落在**日线二买确认之前** —— 那一刻还不知道二买会成立。
    成交必须推迟到 d2 当日首根 bar 之后。
    """
    days = seq("2024-01-01", 40)
    out = build_out(chain_plan(
        days, days[0], days[20],
        {days[15]: [OTHER, OTHER, M30_HIT, OTHER]},   # 30 分钟买点早 5 个交易日
    ))
    res = scan_from_frame(out, code="T")
    assert len(res.trades) == 1
    b = res.trades[0].buy
    assert b.gap_2tom == -5
    assert b.signal_dt == f"{days[15]} 09:50:00"      # 触发时点仍记在买点 bar 上
    assert b.dt.startswith(days[20])                  # 但成交不早于 d2
    assert b.dt > b.signal_dt


def test_scan_from_frame_records_diagnostics():
    """诊断信息必须齐备（用于排查"为什么没触发"）"""
    days = seq("2024-01-01", 40)
    out = build_out(chain_plan(
        days, days[0], days[16],
        {days[35]: [M30_HIT, OTHER, OTHER, OTHER]},   # gap_2tom = 19，超窗
    ))
    res = scan_from_frame(out, code="T")
    assert res.trades == []
    assert res.buy1_days == [days[0]]
    assert res.buy2_days == [days[16]]
    assert len(res.m30_buy2_bars) == 1
    assert res.trading_days == 40


def test_scan_from_frame_handles_string_prices():
    """czsc 输出的价格列是字符串 —— 均线比较必须能正常进行

    回归：曾因 daily['close'] 为字符串，在卖出比较时抛
    "ufunc 'greater' did not contain a loop ... StrDType"。
    """
    days = seq("2024-01-01", 25)
    plan = []
    for i, d in enumerate(days):
        # 买入后价格逐日走低，必然跌破两条均线
        closes = [10.0, 10.1, 10.2, 10.3] if i < 12 else [9.0, 9.0, 9.0, 9.0]
        spec = {"day": d, "closes": [str(c) for c in closes]}   # 故意用字符串
        if d == days[0]:
            spec["d1"] = D1_HIT
        if d == days[10]:
            spec["d2"] = D2_HIT
        if d == days[11]:
            spec["m30"] = [OTHER, OTHER, M30_HIT, M30_HIT]
        plan.append(spec)

    res = scan_from_frame(build_out(plan), code="T")
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.buy.price == 10.3
    assert t.sell is not None          # 跌破了均线，必须能卖出而不是抛异常
    assert t.sell.price == 9.0


# ----------------------------------------------------------------------
# 集成：完整 scan（需要 czsc）
# ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def synth_30min():
    """合成 30 分钟 K 线（随机游走），用于跑通完整链路"""
    np = pytest.importorskip("numpy")
    from data.models import KLineData

    rng = np.random.default_rng(7)
    kl, price = [], 10.0
    d = date(2023, 1, 3)
    for _ in range(440):
        if d.weekday() < 5:
            for j in range(8):
                ret = rng.normal(0, 0.004)
                o = price
                c = price * (1 + ret)
                h = max(o, c) * (1 + abs(rng.normal(0, 0.002)))
                l = min(o, c) * (1 - abs(rng.normal(0, 0.002)))
                kl.append(KLineData(
                    code="TEST", date=f"{d.isoformat()} {9 + (j * 30) // 60:02d}:"
                                      f"{(j * 30) % 60:02d}:00",
                    open=o, high=h, low=l, close=c,
                    volume=int(rng.integers(10000, 100000)), period="30min"))
                price = c
        d += timedelta(days=1)
    return kl


def test_scan_runs_end_to_end(synth_30min):
    """完整链路能跑通（**默认几何源**），返回结构正确"""
    pytest.importorskip("czsc")
    from core.chan_strategy import SOURCE_GEOMETRY, scan

    res = scan(synth_30min, code="TEST")
    assert isinstance(res, ScanResult)
    assert res.code == "TEST"
    assert res.source == SOURCE_GEOMETRY
    # czsc 的 bars_raw 会从头截断（见 core/chan.py 约束 3），所以少于自然交易日数
    assert res.trading_days > 200
    # 三级点事件都要被记录（即使最终没共振，诊断信息必须在）
    assert isinstance(res.buy1_days, list)
    assert isinstance(res.buy2_days, list)
    assert isinstance(res.m30_buy2_bars, list)
    for t in res.trades:
        assert t.buy.price > 0
        assert 0 < t.buy.gap_1to2 <= 40
        assert abs(t.buy.gap_2tom) <= 10
        assert t.buy.dt > t.buy.signal_dt      # 成交必须晚于信号确认
        if t.sell:
            assert t.sell.hold_days >= 1
            assert t.sell.ma in ("MA5", "MA10")
            assert t.sell.dt > t.sell.confirm_dt


def test_scan_signal_source_still_available(synth_30min):
    """signal 源保留可跑（用于与几何源对比）"""
    pytest.importorskip("czsc")
    from core.chan_strategy import SOURCE_SIGNAL, scan

    res = scan(synth_30min, code="TEST", source=SOURCE_SIGNAL)
    assert res.source == SOURCE_SIGNAL
    assert isinstance(res.buy1_days, list)


def test_scan_rejects_unknown_source(synth_30min):
    pytest.importorskip("czsc")
    from core.chan_strategy import scan

    with pytest.raises(ValueError, match="未知的 source"):
        scan(synth_30min, code="T", source="nonsense")


def test_scan_respects_windows_on_real_signals(synth_30min):
    """真实信号上，所有触发的窗口约束都成立（两个源各自的口径）"""
    pytest.importorskip("czsc")
    from core.chan_strategy import SOURCE_SIGNAL, scan

    sig = scan(synth_30min, code="TEST", source=SOURCE_SIGNAL,
               max_gap_1to2=15, max_gap_2tom=2)
    for t in sig.trades:
        assert t.buy.gap_1to2 <= 15
        assert abs(t.buy.gap_2tom) <= 2

    geo = scan(synth_30min, code="TEST", max_gap_1to2=40, max_gap_2tom=10)
    for t in geo.trades:
        assert t.buy.gap_1to2 <= 40
        assert abs(t.buy.gap_2tom) <= 10


def test_scan_tighter_window_reduces_or_keeps_signals(synth_30min):
    """收窄窗口不应产生更多买点（单调性）—— 两个源都测"""
    pytest.importorskip("czsc")
    from core.chan_strategy import SOURCE_SIGNAL, scan

    for src in (SOURCE_SIGNAL, "geometry"):
        wide = scan(synth_30min, code="T", source=src,
                    max_gap_1to2=40, max_gap_2tom=10)
        narrow = scan(synth_30min, code="T", source=src,
                      max_gap_1to2=3, max_gap_2tom=1)
        assert narrow.buy_count <= wide.buy_count


def test_scan_insufficient_data_returns_empty():
    """数据不足时安全返回空结果（czsc 前 init_n 根不产信号）"""
    from data.models import KLineData
    from core.chan_strategy import scan

    kl = [KLineData(code="X", date=f"2024-01-01 10:{i:02d}:00",
                    open=1, high=1, low=1, close=1, volume=1) for i in range(50)]
    res = scan(kl, code="X")
    assert res.buy_count == 0
    assert res.trading_days == 0


def test_scan_insufficient_data_does_not_need_czsc(monkeypatch):
    """数据不足的早退路径不该碰 czsc

    回归：scan() 原先第一行就 load_czsc()、长度检查在后面 ——
    没装 czsc 的环境连「数据不足返回空」都走不通（直接 ImportError）。
    """
    from data.models import KLineData
    import core.chan_strategy as cs

    def _boom():
        raise ImportError("模拟：czsc 未安装")

    monkeypatch.setattr(cs, "load_czsc", _boom)
    kl = [KLineData(code="X", date=f"2024-01-01 10:{i:02d}:00",
                    open=1, high=1, low=1, close=1, volume=1) for i in range(50)]
    res = cs.scan(kl, code="X")
    assert res.buy_count == 0


def test_match_buy_points_picks_earliest_bar_in_day():
    """同一天内多个 30 分钟跃迁 bar，必须取**下标较小**的那根

    回归：排序键曾写作 str(bar)，而 '100' < '99' 为真 —— 一天内的 bar
    下标一旦跨过 10 的幂（99 → 100），会选中较晚的那根。
    """
    from core.chan_strategy import match_buy_points

    days = [f"2025-01-{d:02d}" for d in range(1, 21)]
    m = match_buy_points(days, [days[0]], [days[1]],
                         [(days[1], 100), (days[1], 99)],
                         max_gap_1to2=15, max_gap_2tom=2)
    assert len(m) == 1
    assert m[0]["bar"] == 99, "应取较早的 bar(99)，而不是 str 排序下的 100"


def test_klines_to_df_drops_intraday_nan_placeholder():
    """盘中未走完的占位 bar（OHLC 全 NaN）必须在桥接层丢掉

    回归：新浪 stock_zh_a_minute 盘中会返回当日首根 bar 的占位行
    （OHLC 全 NaN、volume/amount 有值），czsc 的 BarGenerator 拒绝该输入，
    实测抛 ValueError: update_signals 失败 ... bar.open = NaN。
    """
    from core.chan import klines_to_df
    from data.models import KLineData

    kl = [KLineData(code="X", date="2026-09-17 15:00:00",
                    open=1.0, high=1.1, low=0.9, close=1.05, volume=100),
          KLineData(code="X", date="2026-09-18 10:00:00",
                    open=float("nan"), high=float("nan"), low=float("nan"),
                    close=float("nan"), volume=200)]
    df = klines_to_df(kl)
    assert len(df) == 1
    assert str(df["dt"].iloc[0]).startswith("2026-09-17")


# ----------------------------------------------------------------------
# 几何源：生效日（未来函数防线）
# ----------------------------------------------------------------------

def test_effective_day_shifts_to_next_trading_day():
    """日线笔终点落在 T 的收盘上，而 T 收盘才成立 → 生效日是 T+1

    直接把 T 当生效日就是 1 个交易日的未来函数。
    """
    days = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    assert _effective_day("2026-01-05 00:00:00", days) == date(2026, 1, 6)
    assert _effective_day("2026-01-06", days) == date(2026, 1, 7)


def test_effective_day_none_at_tail():
    """尾部笔尚未确认（次日不存在）→ 丢点

    宁可少一个点，也不能拿"还不知道成不成立"的笔去交易。
    """
    days = [date(2026, 1, 5), date(2026, 1, 6)]
    assert _effective_day("2026-01-06", days) is None


def test_effective_day_normalizes_time_and_tz():
    """带时区 / 带时分秒的 dt 都要归到正确交易日"""
    days = [date(2026, 1, 5), date(2026, 1, 6)]
    assert _effective_day("2026-01-05 15:00:00+00:00", days) == date(2026, 1, 6)


def test_effective_day_between_two_trading_days():
    """dt 落在两个交易日之间（如周末）→ 归到**前一个**交易日再顺延

    取前一个而非后一个是**偏保守**的选择：生效日更晚，不会提前交易。
    这条分支在正常数据里走不到（日线由 30 分钟合成，日期必然在交易日序列里），
    兜住它是为了不让上游异常静默变成"提前成交"。
    """
    days = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 9)]
    # 1/7 落在 1/6 与 1/9 之间 → 归到 1/6，再顺延一个交易日 → 1/9
    assert _effective_day("2026-01-07", days) == date(2026, 1, 9)
    # 落在最后一个交易日 → 顺延后的日期不存在 → 丢点
    assert _effective_day("2026-01-09", days) is None


def test_scan_from_frame_geometry_requires_points():
    """geometry 源必须给 points —— 静默返回空结果会掩盖调用错误"""
    out = build_out([{"day": "2024-01-02"}])
    with pytest.raises(ValueError, match="points_from_chan"):
        scan_from_frame(out, code="T", source=SOURCE_GEOMETRY)


def test_points_from_chan_structure(synth_30min):
    """几何源产出的点事件结构正确，且都能对上帧里的 bar / 交易日"""
    pytest.importorskip("czsc")
    from core.chan import klines_to_df

    df = klines_to_df(synth_30min)
    pts = points_from_chan(synth_30min, df, code="TEST")
    assert pts.daily_bars > 0
    assert isinstance(pts.counts, dict)

    days = set(pd.to_datetime(df["dt"]).dt.date)
    assert all(d in days for d in pts.d1_days)
    assert all(d in days for d in pts.d2_days)
    assert all(0 <= i < len(df) for _, i in pts.m30_items)
    # 30 分钟点按 bar 下标升序
    assert [i for _, i in pts.m30_items] == sorted(i for _, i in pts.m30_items)

    # 不带 day 列的帧也要能被接受（geometry 路径会自动补 day）
    res = scan_from_frame(df, code="TEST", source=SOURCE_GEOMETRY, points=pts)
    assert res.source == SOURCE_GEOMETRY
    assert res.trading_days > 200
    assert res.buy1_days == [str(d) for d in pts.d1_days]
