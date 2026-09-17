"""缠论多周期共振策略 —— 日线一买 → 日线二买 → 30 分钟二买，均线卖出

======================================================================
策略定义（老三 2026-09-17 确认）
======================================================================

**买点**：三重条件，有顺序、有时间窗

1. 日线出现**一买**
2. 一买之后 **15 个交易日内**出现**日线二买**
3. 日线二买之后 **2 个交易日内**出现 **30 分钟二买**

三条依次满足的那一刻（30 分钟 bar 收盘）即为买点。

**卖点**：买入后以日线均线作移动止损

默认 MA5；若收盘价跌破 MA5 但 MA10 仍完好，则改用 MA10 继续持有；
MA5、MA10 **双双被跌破**后卖出。
即"智能选取那条一直沿着上涨、没有被跌破的 5 日或 10 日均线"。

======================================================================
两条硬约束（都有实测依据，改代码前先读）
======================================================================

**1. 信号是「持续状态」，不是「点事件」**

czsc 的 `日线_D1B_BUY1` / `..._BS2辅助V230320` 输出的不是 0/1，
而是持续状态字符串（如 `二买_任意_任意_0`）。实测日线二买状态连续
覆盖 352 根 30 分钟 bar（约 44 个交易日），只要状态没结束就一直为真。

因此"出现二买"一律定义为**状态跃迁**（首次进入该状态的那一根 bar），
否则 15 日 / 2 日的时间窗约束形同虚设。

**2. 日线信号在日内会翻转 → 取当日首根 bar 的值**

实测 418 个交易日中有 45 天，日线二买在**同一交易日内发生翻转**
（早盘读到"其他"、尾盘变"二买"，或反之）—— 因为当天的日线 bar
尚未走完，czsc 会拿当天已发生的部分 30 分钟合成日线参与计算。

本模块因此对日线信号**按交易日取首根 bar 的值**，它等价于
「上一交易日收盘确认」的日线状态：
- 实盘在开盘时就能读到该值，**不存在未来函数**
- 回测与实盘看到的是同一个值，结论可比
- 代价：日线信号滞后 1 个交易日才生效（15 日窗口仍够用）

30 分钟信号无此问题（30 分钟 bar 收盘即确定），按 bar 级跃迁处理。

======================================================================
成交价约定（含"确认延迟"）
======================================================================

不需要盘中即时性 —— 缠论买卖点都要等 K 线走完才能确认，早一根 bar 提醒
没有意义。因此一律**等确认后再成交**：

- **买点**：30 分钟二买 bar 收盘确认 → **下一根 30 分钟 bar 收盘价**成交
  （`entry_delay_bars=1`）
- **卖点**：日线收盘确认跌破均线 → **次一交易日收盘价**成交
  （`exit_delay_days=1`，约半天到一天的延迟）

回测与实盘看到的是同一时点的信息，不存在"用当天收盘价在自己确认的那一刻
成交"这种做不到的假设。

======================================================================
数据量要求
======================================================================

czsc 的 `init_n=500` 意味着前 500 根 30 分钟 bar 不产出信号（约 63 个
交易日）。要让策略真正跑出结果，至少需要 **150 个交易日以上**的
30 分钟数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

import pandas as pd

from core.chan import klines_to_df, load_czsc
from data.models import KLineData
from utils.logger import get_logger

logger = get_logger(__name__)

# 一买 → 二买 的最大间隔（交易日）
DEFAULT_MAX_GAP_1TO2 = 15
# 日线二买 → 30 分钟二买 的最大间隔（交易日）
DEFAULT_MAX_GAP_2TOM = 2
# 次级别周期
BASE_FREQ = "30分钟"
# czsc 预热根数：前 init_n 根不产出信号
DEFAULT_INIT_N = 500

_FIRST_BUY_SIGNAL = "cxt_first_buy_V221126"
_SECOND_BS_SIGNAL = "cxt_second_bs_V230320"


def signal_columns(ma_type: str = "SMA", timeperiod: int = 21) -> dict[str, str]:
    """信号输出列名（随信号参数变化，不要硬编码到调用处）"""
    return {
        "daily_buy1": "日线_D1B_BUY1",
        "daily_buy2": f"日线_D1#{ma_type}#{timeperiod}_BS2辅助V230320",
        "m30_buy2": f"{BASE_FREQ}_D1#{ma_type}#{timeperiod}_BS2辅助V230320",
    }


def build_signals_config(ma_type: str = "SMA", timeperiod: int = 21) -> list[dict]:
    """czsc 信号配置：一份 30 分钟数据，同时产出日线 + 30 分钟三级信号"""
    return [
        {"name": _FIRST_BUY_SIGNAL, "freq": "日线", "di": 1},
        {"name": _SECOND_BS_SIGNAL, "freq": "日线", "di": 1,
         "ma_type": ma_type, "timeperiod": timeperiod},
        {"name": _SECOND_BS_SIGNAL, "freq": BASE_FREQ, "di": 1,
         "ma_type": ma_type, "timeperiod": timeperiod},
    ]


# ======================================================================
# 结果模型
# ======================================================================

@dataclass
class BuySignal:
    """一次买点触发"""
    dt: str                 # 成交时刻（信号 bar 之后第 entry_delay_bars 根）
    price: float            # 成交价（该 bar 收盘价）
    d1: str                 # 日线一买生效日
    d2: str                 # 日线二买生效日
    gap_1to2: int           # 一买 → 二买 间隔交易日
    gap_2tom: int           # 二买 → 30 分钟二买 间隔交易日
    signal_dt: str = ""     # 信号确认时刻（30 分钟二买跃迁 bar）


@dataclass
class SellSignal:
    """一次卖点触发"""
    dt: str                 # 成交日（确认日之后第 exit_delay_days 个交易日）
    price: float            # 成交价（当日收盘价）
    ma: str                 # 最终生效的均线（MA5 / MA10）
    ma_value: float         # 触发时该均线值
    hold_days: int          # 持有交易日数（成交日 - 买入日）
    confirm_dt: str = ""    # 均线跌破的确认日


@dataclass
class Trade:
    """一笔完整交易"""
    buy: BuySignal
    sell: Optional[SellSignal] = None   # None = 数据末尾仍未平仓

    @property
    def return_pct(self) -> Optional[float]:
        if self.sell is None or self.buy.price <= 0:
            return None
        return (self.sell.price / self.buy.price - 1) * 100


@dataclass
class ScanResult:
    """一次扫描的完整结果"""
    code: str = ""
    trades: list[Trade] = field(default_factory=list)
    # 诊断用：各级信号的原始跃迁点（便于排查"为什么没触发"）
    buy1_days: list[str] = field(default_factory=list)
    buy2_days: list[str] = field(default_factory=list)
    m30_buy2_bars: list[str] = field(default_factory=list)
    trading_days: int = 0

    @property
    def buy_count(self) -> int:
        return len(self.trades)

    def summary(self) -> dict:
        """汇总统计（未平仓的交易不计入收益率）"""
        closed = [t for t in self.trades if t.sell is not None]
        rets = [t.return_pct for t in closed if t.return_pct is not None]
        wins = [r for r in rets if r > 0]
        return {
            "code": self.code,
            "买点数": self.buy_count,
            "已平仓": len(closed),
            "未平仓": self.buy_count - len(closed),
            "胜率%": round(len(wins) / len(rets) * 100, 1) if rets else None,
            "平均收益%": round(sum(rets) / len(rets), 2) if rets else None,
            "最佳%": round(max(rets), 2) if rets else None,
            "最差%": round(min(rets), 2) if rets else None,
            "平均持有交易日": (round(sum(t.sell.hold_days for t in closed) / len(closed), 1)
                        if closed else None),
        }


# ======================================================================
# 信号提取（纯函数，便于单测）
# ======================================================================

CONTAINS_BUY1 = "一买"
CONTAINS_BUY2 = "二买"


def _entry_positions(hit: pd.Series) -> list[int]:
    """状态跃迁点：False → True 的位置（下标列表）

    注意"出现"必须是跃迁，不能用"值为 True" —— 信号在设计上是持续状态。
    """
    hit = hit.fillna(False).astype(bool)
    prev = hit.shift(1, fill_value=False).astype(bool)
    return [i for i, flag in enumerate((hit & ~prev).tolist()) if flag]


def daily_state_from_bars(out: pd.DataFrame, column: str) -> pd.DataFrame:
    """把 30 分钟 bar 序列上的日线信号，压缩为「每个交易日一个可用值」

    每个交易日取其**首根 bar** 上的日线信号 —— 见模块 docstring 硬约束 2：
    该值等价于上一交易日收盘确认的状态，实盘开盘可读、无未来函数。

    返回 DataFrame(index=交易日, columns=[column, 'first_bar_pos'])
    """
    days = out["day"].tolist()
    first_pos: dict = {}
    for i, d in enumerate(days):
        if d not in first_pos:
            first_pos[d] = i
    idx = sorted(first_pos)
    return pd.DataFrame(
        {column: [out[column].iloc[first_pos[d]] for d in idx],
         "first_bar_pos": [first_pos[d] for d in idx]},
        index=pd.Index(idx, name="day"),
    )


def match_buy_points(
    trading_days: list,
    d1_days: Iterable,
    d2_days: Iterable,
    m30_items: Iterable[tuple],
    max_gap_1to2: int = DEFAULT_MAX_GAP_1TO2,
    max_gap_2tom: int = DEFAULT_MAX_GAP_2TOM,
) -> list[dict]:
    """三条件时序匹配（纯逻辑，与 czsc 解耦）

    参数
    ----
    trading_days : 交易日序列（升序），窗口按"第几个交易日"计算
    d1_days      : 日线一买生效的交易日
    d2_days      : 日线二买生效的交易日
    m30_items    : ``[(交易日, bar 标识), ...]``，30 分钟二买跃迁点，
                   **按时间升序**、允许同一天多次（一天内可有多个 30 分钟 bar）

    规则
    ----
    1. 对每个日线二买日 d2，向前找最近的日线一买日 d1，要求
       ``0 < pos(d2) - pos(d1) <= max_gap_1to2``
    2. 对每个 (d1, d2) 配对，取 ``pos(d2) <= pos(d3) <= pos(d2) + max_gap_2tom``
       区间内**第一个未被占用**的 30 分钟二买 bar 作为触发点
    3. 每个 30 分钟二买 bar 只触发一次（先到先得，不重复配对）

    返回：[{d1, d2, d3, bar, gap_1to2, gap_2tom}, ...]，按触发时间升序
    """
    pos = {d: i for i, d in enumerate(trading_days)}
    d1_list = sorted({d for d in d1_days if d in pos}, key=pos.get)
    d2_list = sorted({d for d in d2_days if d in pos}, key=pos.get)
    # 保留 bar 顺序与重复：一天内可能有多个 30 分钟二买跃迁 bar
    m30_list = [(pos[d], d, bar) for d, bar in m30_items if d in pos]
    m30_list.sort(key=lambda x: (x[0], str(x[2])))

    all_matches: list[dict] = []
    used_bars: set = set()

    for d2 in d2_list:
        i2 = pos[d2]
        # 向前找最近的、落在窗口内的日线一买
        candidates = [d1 for d1 in d1_list if 0 < i2 - pos[d1] <= max_gap_1to2]
        if not candidates:
            continue
        d1 = max(candidates, key=pos.get)

        # 向后找窗口内第一个未被占用的 30 分钟二买 bar
        for i3, d3, bar in m30_list:
            if i3 < i2:
                continue
            if i3 > i2 + max_gap_2tom:
                break
            if bar in used_bars:
                continue
            all_matches.append({
                "d1": d1, "d2": d2, "d3": d3, "bar": bar,
                "gap_1to2": i2 - pos[d1],
                "gap_2tom": i3 - i2,
            })
            used_bars.add(bar)
            break

    return sorted(all_matches, key=lambda m: (pos[m["d3"]], str(m["bar"])))


def match_sell_points(
    daily: pd.DataFrame,
    buy_day,
    ma_short: int = 5,
    ma_long: int = 10,
    exit_delay_days: int = 1,
) -> Optional[dict]:
    """均线移动止损（纯逻辑）

    规则：从买入日**次一交易日**起逐日检查
    - 收盘价跌破 MA{short}      → 提示短均线失效，切到 MA{long}（未破则继续持有）
    - 短、长两条均线**都被跌破** → 卖出

    均线跌破要等**收盘**才能确认，故实际成交顺延 ``exit_delay_days`` 个交易日
    （默认 1 日，对应"等数据确认后再提醒"）。

    参数 daily 需含 'close' / 'sma_short' / 'sma_long' 三列，index 为交易日。

    返回：{dt, price, ma, ma_value, hold_days, confirm_dt}；
          数据末尾仍未确认、或确认后无次日可成交 → None（视为未平仓）
    """
    try:
        start = daily.index.get_loc(buy_day)
    except KeyError:
        return None

    broken_short = False
    broken_long = False
    for i in range(start + 1, len(daily)):
        row = daily.iloc[i]
        close = row["close"]
        s_ma, l_ma = row["sma_short"], row["sma_long"]
        if pd.isna(s_ma) or pd.isna(l_ma):
            continue
        if not broken_short and close < s_ma:
            broken_short = True
        if not broken_long and close < l_ma:
            broken_long = True
        if broken_short and broken_long:
            exit_i = i + exit_delay_days
            if exit_i >= len(daily):
                return None         # 数据末尾确认，但已无次日可成交
            return {
                "dt": str(daily.index[exit_i]),
                "price": float(daily.iloc[exit_i]["close"]),
                "ma": f"MA{ma_long}",
                "ma_value": float(l_ma),
                "hold_days": exit_i - start,
                "confirm_dt": str(daily.index[i]),
            }
    return None


# ======================================================================
# 主入口
# ======================================================================

def scan(
    klines: Iterable[KLineData],
    code: str = "",
    *,
    max_gap_1to2: int = DEFAULT_MAX_GAP_1TO2,
    max_gap_2tom: int = DEFAULT_MAX_GAP_2TOM,
    ma_short: int = 5,
    ma_long: int = 10,
    ma_type: str = "SMA",
    timeperiod: int = 21,
    init_n: int = DEFAULT_INIT_N,
    entry_delay_bars: int = 1,
    exit_delay_days: int = 1,
) -> ScanResult:
    """对一只股票的 30 分钟 K 线跑完整策略，返回买点/卖点与诊断信息

    需要 czsc；未安装时抛 ImportError（由调用方决定是否降级）。
    """
    czsc = load_czsc()
    df = klines_to_df(klines)
    result = ScanResult(code=code)

    if len(df) < init_n + 60:
        logger.info(
            f"缠论多周期策略跳过 {code}：30 分钟 K 线 {len(df)} 根，"
            f"不足（需 > {init_n + 60} 根，czsc 前 {init_n} 根不产信号）")
        return result

    out = generate_signal_frame(df, czsc, ma_type=ma_type,
                                timeperiod=timeperiod, init_n=init_n)
    cols = signal_columns(ma_type, timeperiod)
    return scan_from_frame(out, code=code, ma_short=ma_short, ma_long=ma_long,
                           max_gap_1to2=max_gap_1to2, max_gap_2tom=max_gap_2tom,
                           columns=cols,
                           entry_delay_bars=entry_delay_bars,
                           exit_delay_days=exit_delay_days)


def generate_signal_frame(df: pd.DataFrame, czsc, *, ma_type="SMA",
                          timeperiod=21, init_n=DEFAULT_INIT_N) -> pd.DataFrame:
    """跑 czsc 产出信号，并补上 day / bar_of_day 辅助列"""
    try:
        from czsc import generate_czsc_signals
    except ImportError:  # pragma: no cover - 视版本而定
        from czsc.signals.signals import generate_czsc_signals

    freq = getattr(czsc.Freq, "F30")
    bars = czsc.format_standard_kline(df, freq=freq)
    out = generate_czsc_signals(
        bars, build_signals_config(ma_type, timeperiod),
        sdt=str(df["dt"].iloc[0])[:10], init_n=init_n, df=True,
    ).reset_index(drop=True)

    out["dt"] = pd.to_datetime(out["dt"])
    out["day"] = out["dt"].dt.date

    # czsc 输出的价格/量列是字符串（实测），不转数值会在均线比较时抛
    # "ufunc 'greater' did not contain a loop ... StrDType"。
    for col in ("open", "close", "high", "low", "vol", "amount"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def scan_from_frame(
    out: pd.DataFrame,
    code: str = "",
    *,
    ma_short: int = 5,
    ma_long: int = 10,
    max_gap_1to2: int = DEFAULT_MAX_GAP_1TO2,
    max_gap_2tom: int = DEFAULT_MAX_GAP_2TOM,
    columns: Optional[dict] = None,
    entry_delay_bars: int = 1,
    exit_delay_days: int = 1,
) -> ScanResult:
    """从已算好的信号表执行策略（信号表含 day 列与三个信号列）"""
    columns = columns or signal_columns()
    result = ScanResult(code=code)

    # czsc 输出的数值列是字符串，统一转数值后再用（见 generate_signal_frame）
    out = out.copy()
    if "close" in out.columns:
        out["close"] = pd.to_numeric(out["close"], errors="coerce")

    trading_days = sorted(set(out["day"]))
    result.trading_days = len(trading_days)

    # ---- 日线信号：按交易日取首根 bar 的值（见模块 docstring 硬约束 2） ----
    d1_state = daily_state_from_bars(out, columns["daily_buy1"])
    d2_state = daily_state_from_bars(out, columns["daily_buy2"])

    d1_hit = d1_state[columns["daily_buy1"]].astype(str).str.contains(CONTAINS_BUY1, na=False)
    d2_hit = d2_state[columns["daily_buy2"]].astype(str).str.contains(CONTAINS_BUY2, na=False)

    all_days = list(d2_state.index)
    d1_days = [all_days[i] for i in _entry_positions(d1_hit)]
    d2_days = [all_days[i] for i in _entry_positions(d2_hit)]
    result.buy1_days = [str(d) for d in d1_days]
    result.buy2_days = [str(d) for d in d2_days]

    # ---- 30 分钟二买：bar 级跃迁（30 分钟信号无日内翻转问题） ----
    m30_hit = out[columns["m30_buy2"]].astype(str).str.contains(CONTAINS_BUY2, na=False)
    m30_positions = _entry_positions(m30_hit)
    m30_items = [(out["day"].iloc[i], i) for i in m30_positions]
    result.m30_buy2_bars = [str(out["dt"].iloc[i]) for i in m30_positions]

    # ---- 三条件时序匹配 ----
    matches = match_buy_points(all_days, d1_days, d2_days, m30_items,
                               max_gap_1to2=max_gap_1to2,
                               max_gap_2tom=max_gap_2tom)
    if not matches:
        logger.info(
            f"缠论多周期策略 {code}：无买点（一买 {len(d1_days)} 次、"
            f"二买 {len(d2_days)} 次、30 分钟二买 {len(m30_positions)} 次，未形成共振）")
        return result

    # ---- 日线收盘价与均线（卖点用） ----
    daily = (out.groupby("day", sort=True)
                .agg(close=("close", "last"))
                .rename_axis("day"))
    # 双保险：即使调用方自建 out（如测试）时 close 是字符串，也要能算
    daily["close"] = pd.to_numeric(daily["close"], errors="coerce")
    daily["sma_short"] = daily["close"].rolling(ma_short).mean()
    daily["sma_long"] = daily["close"].rolling(ma_long).mean()

    # ---- 组装交易 ----
    for m in matches:
        # 信号要等 30 分钟 bar 收盘才确认，实际成交顺延 entry_delay_bars 根
        trade_pos = m["bar"] + entry_delay_bars
        if trade_pos >= len(out):
            logger.info(
                f"缠论多周期策略 {code}：{m['d3']} 的二买信号落在数据末尾，"
                "无后续 bar 可成交，已跳过")
            continue
        trade_bar = out.iloc[trade_pos]
        buy = BuySignal(
            dt=str(trade_bar["dt"]),
            price=float(trade_bar["close"]),
            d1=str(m["d1"]), d2=str(m["d2"]),
            gap_1to2=m["gap_1to2"], gap_2tom=m["gap_2tom"],
            signal_dt=str(out.iloc[m["bar"]]["dt"]),
        )
        sell = match_sell_points(daily, m["d3"], ma_short=ma_short,
                                 ma_long=ma_long, exit_delay_days=exit_delay_days)
        trade = Trade(buy=buy, sell=SellSignal(**sell) if sell else None)
        result.trades.append(trade)
        logger.info(
            f"缠论多周期策略 {code} 买点 {buy.dt} @{buy.price:.2f} "
            f"(一买 {buy.d1} → 二买 {buy.d2} 间隔{buy.gap_1to2}日 → "
            f"30分钟二买 {buy.gap_2tom}日内)"
            + (f"，卖点 {trade.sell.dt} @{trade.sell.price:.2f} "
               f"({trade.sell.ma}, {trade.return_pct:+.2f}%)" if trade.sell else "，未平仓")
        )

    return result
