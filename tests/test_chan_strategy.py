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
    _entry_positions,
    daily_state_from_bars,
    match_buy_points,
    match_sell_points,
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


def test_match_gap_1to2_boundary_15_ok_16_rejected():
    """一买→二买：第 15 个交易日 OK，第 16 个算超窗"""
    days = seq("2024-01-01", 40)
    ok = match_buy_points(days, [days[0]], [days[15]], [(days[16], "b1")])
    assert len(ok) == 1 and ok[0]["gap_1to2"] == 15

    over = match_buy_points(days, [days[0]], [days[16]], [(days[17], "b2")])
    assert over == []


def test_match_gap_2tom_boundary_2_ok_3_rejected():
    """二买→30 分钟二买：第 2 个交易日 OK，第 3 个算超窗"""
    days = seq("2024-01-01", 40)
    ok = match_buy_points(days, [days[0]], [days[5]], [(days[7], "b1")])
    assert len(ok) == 1 and ok[0]["gap_2tom"] == 2

    over = match_buy_points(days, [days[0]], [days[5]], [(days[8], "b2")])
    assert over == []


def test_match_same_day_1to2_not_counted():
    """同日既是一买又是二买不算"一买之后" —— 窗口要求严格大于 0"""
    days = seq("2024-01-01", 20)
    res = match_buy_points(days, [days[3]], [days[3]], [(days[4], "b")])
    assert res == []


def test_match_m30_before_d2_not_used():
    """30 分钟二买出现在日线二买之前 → 不能配对"""
    days = seq("2024-01-01", 30)
    res = match_buy_points(days, [days[0]], [days[10]], [(days[5], "b")])
    assert res == []


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
    """一买太早（超出 15 日）不能作为配对起点"""
    days = seq("2024-01-01", 40)
    res = match_buy_points(days, [days[0]], [days[20]], [(days[21], "b")])
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
        Trade(buy=buy, sell=SellSignal("d", 11.0, "MA5", 10.5, 5)),
        Trade(buy=buy, sell=SellSignal("d", 9.0, "MA10", 9.5, 7)),
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
    """30 分钟二买超出 2 日窗口 → 不成交"""
    days = seq("2024-01-01", 25)
    out = build_out(chain_plan(
        days, days[0], days[10],
        {days[13]: [M30_HIT, OTHER, OTHER, OTHER]},   # gap_2tom = 3
    ))
    res = scan_from_frame(out, code="T")
    assert res.trades == []


def test_scan_from_frame_records_diagnostics():
    """诊断信息必须齐备（用于排查"为什么没触发"）"""
    days = seq("2024-01-01", 25)
    out = build_out(chain_plan(
        days, days[0], days[16],
        {days[20]: [M30_HIT, OTHER, OTHER, OTHER]},   # 超窗，不成交
    ))
    res = scan_from_frame(out, code="T")
    assert res.trades == []
    assert res.buy1_days == [days[0]]
    assert res.buy2_days == [days[16]]
    assert len(res.m30_buy2_bars) == 1
    assert res.trading_days == 25


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
    """完整链路能跑通，返回结构正确"""
    pytest.importorskip("czsc")
    from core.chan_strategy import scan

    res = scan(synth_30min, code="TEST")
    assert isinstance(res, ScanResult)
    assert res.code == "TEST"
    # czsc 的 bars_raw 会从头截断（见 core/chan.py 约束 3），所以少于自然交易日数
    assert res.trading_days > 200
    # 三级信号都要被记录（即使最终没共振，诊断信息必须在）
    assert isinstance(res.buy1_days, list)
    assert isinstance(res.buy2_days, list)
    assert isinstance(res.m30_buy2_bars, list)
    for t in res.trades:
        assert t.buy.price > 0
        assert 0 < t.buy.gap_1to2 <= 15
        assert 0 <= t.buy.gap_2tom <= 2
        assert t.buy.dt > t.buy.signal_dt      # 成交必须晚于信号确认
        if t.sell:
            assert t.sell.hold_days >= 1
            assert t.sell.ma in ("MA5", "MA10")
            assert t.sell.dt > t.sell.confirm_dt


def test_scan_respects_windows_on_real_signals(synth_30min):
    """真实信号上，所有触发的窗口约束都成立"""
    pytest.importorskip("czsc")
    from core.chan_strategy import scan

    res = scan(synth_30min, code="TEST", max_gap_1to2=15, max_gap_2tom=2)
    for t in res.trades:
        assert t.buy.gap_1to2 <= 15
        assert t.buy.gap_2tom <= 2


def test_scan_tighter_window_reduces_or_keeps_signals(synth_30min):
    """收窄窗口不应产生更多买点（单调性）"""
    pytest.importorskip("czsc")
    from core.chan_strategy import scan

    wide = scan(synth_30min, code="T", max_gap_1to2=15, max_gap_2tom=2)
    narrow = scan(synth_30min, code="T", max_gap_1to2=3, max_gap_2tom=1)
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
