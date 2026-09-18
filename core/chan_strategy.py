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
三条硬约束（都有实测依据，改代码前先读）
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

本模块因此对日线信号**按交易日取首根 bar 的值**（当日 10:00 那根）：
- 该值在 **10:00 时点即可读到**，不必等当天收盘，**不构成未来函数**
- 回测与实盘看到的是同一个值，结论可比

⚠️ 措辞更正：它**不等于「上一交易日收盘确认」**。这根 bar 上算出的日线
信号已经含了当天 9:30–10:00 的数据，是「上一交易日收盘 + 今日早盘半小时」
的合成状态，只是时点上 10:00 可读。此前写作"等价于上一交易日收盘确认、
滞后 1 个交易日"不准确。

30 分钟信号**没有日内翻转**问题（30 分钟 bar 收盘即确定，不涉及未走完的
更大周期 bar）。但这只说明"同一根 bar 的值不会自己变"，**不等于信号稳定**。

**3. 30 分钟二买用的是同一个 `cxt_*` 信号 —— 它稳不稳？（已专项实测）**

`core/chan_points.py` 用实测论证过 `cxt_*` 不可靠（同一段行情里「一买」
和「二卖」逐根 bar 交替闪断、笔序号在 9 → 5 → 11 之间漂移），因此那边
**弃用** `cxt_*` 改用几何判定。本模块的 30 分钟二买触发用的却是同一个
信号，看起来自相矛盾，所以专门测了一遍：

**实测（sh688981，1970 根 / 248 个交易日 30 分钟数据）**

| 项 | 结果 |
| --- | --- |
| 30 分钟二买状态跃迁点 | 51 个 |
| 相邻跃迁点间隔 | 最小 6 根、中位 55 根、最大 1256 根 |
| 间隔 ≤ 2 根 bar 的相邻对 | **0 / 50** |

**没有观察到逐根闪断** —— 真闪断会产生大量间隔 1~2 根的相邻对。

原因（推测，未逐条验证）：`cxt_second_bs_V230320` 输出
`二买_任意_任意_0`，**不带笔序号**；而 chan_points 举的闪断例子
（一买 26 次 vs 定义上的 21 个）恰恰是**带笔序号**的信号 —— 漂移的是
笔的计数基准，二买不吃这一项。

⚠️ 结论的边界，**不要外推**：
- 样本只有 1 只标的。要下"二买跃迁稳定"的结论应再扩样本。
- 跃迁 51 个 ≠ 几何二买 4 个，两者口径不同（状态可反复结束-重入），
  数量不可直接比较。
- 两侧口径**尚未正式统一**。若老三决定统一到几何判定，本模块的触发源
  要跟着换 —— 届时改 `_SECOND_BS_SIGNAL` 的取值方式即可。

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
持仓与统计口径（2026-09-18 定案，改之前先读）
======================================================================

**每个买点独立成一笔交易，前一笔未平仓时新买点照开 —— 允许重叠持仓。**

定案理由：当前目的是**验证买卖点是否成立**，不是做组合回测。重叠持仓把
「信号质量」与「资金约束」解耦 —— 每笔交易独立可查、互不干扰，不会被
「没资金了所以没买」这类组合层面的事盖住信号本身的问题。

由此产生的口径（读统计数字前必须知道）：

- `ScanResult.summary()` 的胜率 / 平均收益 / 平均持有是**按笔统计**，
  **不是资金曲线**。同一时刻若有 N 笔持仓，这些收益不可直接相加成组合收益。
- `summary()` 额外给出 `最大同时持仓` / `重叠笔数`（`position_overlap_stats`），
  用来自查这批结果有多大比例是重叠的。**重叠笔数占比高时，按笔统计的结论
  要打折看**；若只想看互不干扰的信号质量，可先把重叠的那几笔摘掉再谈胜率。
- 未平仓的那笔（`sell is None`）视为一直持有到数据末尾，与之后所有买点重叠。

**什么时候要改**：接组合回测 / 资金曲线 / 仓位管理时。届时二选一 ——
「持仓中忽略新买点」（单持仓）或按仓位分配。改的位置是 `scan_from_frame`
组装 `trades` 的那段循环；`position_overlap_stats` 可直接当回归判据
（改成单持仓后，`最大同时持仓` 必须为 1）。

======================================================================
数据量要求（两个门槛，别混为一谈）
======================================================================

**代码硬门槛**：`scan()` 要求 30 分钟 bar 数 > `init_n + 60`（默认 560 根，
按一天 8 根折合约 70 个交易日）。低于此直接返回空结果并记一条日志。
这个门槛只防"无意义的计算"，本身很低 —— 560 根里 czsc 前 500 根不产
信号，实际只有 60 根可用（≈7.5 个交易日）。

**有效下限**：要真正跑出买点，实测需要 **150 个交易日以上**（≈1200 根）。
依据：20 只流动性标的、每只 1970 根 / 247 个交易日，其中 16 只零买点，
命中的 4 只也只有 1~2 笔。门槛低不等于能出结果。
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
        """汇总统计（未平仓的交易不计入收益率）

        ⚠️ 收益按**笔**统计，不是资金曲线 —— 本策略允许重叠持仓（见模块
        docstring「持仓与统计口径」），同一时刻多笔时收益不可相加。
        `最大同时持仓` / `重叠笔数` 同时返回，用于自查重叠程度。
        """
        closed = [t for t in self.trades if t.sell is not None]
        rets = [t.return_pct for t in closed if t.return_pct is not None]
        wins = [r for r in rets if r > 0]
        stats = {
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
        stats.update(position_overlap_stats(self.trades))
        return stats


# ======================================================================
# 同时持仓统计（校验「每笔独立、允许重叠」的口径）
# ======================================================================

def _as_naive(dt) -> pd.Timestamp:
    """转成不带时区的 Timestamp

    买点 dt 来自 czsc 输出（**带 UTC 时区**，墙钟仍是北京时间），卖点 dt
    来自日线索引（朴素日期）—— 两者直接比较会抛
    `TypeError: Cannot compare tz-naive and tz-aware timestamps`。
    统一去时区后再比（`chan_viz._naive` 同口径）。
    """
    ts = pd.Timestamp(str(dt))
    return ts.tz_localize(None) if ts.tzinfo is not None else ts


def _spans_overlap(a: tuple, b: tuple) -> bool:
    """两个时间区间是否有**正长度**的交叠

    端点相接（卖在买当天）不算重叠 —— 那是换仓，不是同时在手。
    `end is None` 表示仍未平仓，视作延伸到无穷。
    """
    far = pd.Timestamp.max
    a_end = a[1] if a[1] is not None else far
    b_end = b[1] if b[1] is not None else far
    return a[0] < b_end and b[0] < a_end


def position_overlap_stats(trades: Iterable[Trade]) -> dict:
    """同时持仓统计 —— 每个买点独立成笔的副产物（见模块 docstring）

    每笔交易占用 ``[买入成交, 卖出成交)`` 这段区间；未平仓的占用到数据末尾之后。

    返回
    ----
    ``{"最大同时持仓": int, "重叠笔数": int, "重叠对数": int}``

    - 最大同时持仓：任意时刻同时在手的最大笔数（1 = 全程最多只持一笔）
    - 重叠笔数：与至少另一笔有交叠的交易笔数（未平仓那笔会与之后所有买点重叠）
    - 重叠对数：有交叠的交易两两配对数量

    注意这是**按笔独立**口径下的自查指标。若重叠笔数占比高，`summary()`
    里的胜率 / 平均收益属于「信号统计」而非「组合收益」。
    """
    spans = []
    for t in trades:
        start = _as_naive(t.buy.dt)
        end = _as_naive(t.sell.dt) if t.sell is not None else None
        spans.append((start, end))
    if not spans:
        return {"最大同时持仓": 0, "重叠笔数": 0, "重叠对数": 0}

    # 扫描线：同一时刻**先平后开**（-1 排在 +1 前），
    # 否则「卖在买当天」的换仓会被算成两笔同时在手。
    events: list[tuple] = []
    for start, end in spans:
        events.append((start, 1))
        if end is not None:
            events.append((end, -1))
    events.sort(key=lambda e: (e[0], e[1]))
    running = max_conc = 0
    for _, delta in events:
        running += delta
        max_conc = max(max_conc, running)

    overlapped: set = set()
    pairs = 0
    for i in range(len(spans)):
        for j in range(i + 1, len(spans)):
            if _spans_overlap(spans[i], spans[j]):
                pairs += 1
                overlapped.update((i, j))

    return {"最大同时持仓": max_conc,
            "重叠笔数": len(overlapped),
            "重叠对数": pairs}


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
    m30_list.sort(key=lambda x: (x[0], x[2]))

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

    return sorted(all_matches, key=lambda m: (pos[m["d3"]], m["bar"]))


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
    df = klines_to_df(klines)
    result = ScanResult(code=code)

    # 长度检查必须排在 load_czsc() 前面：这条早退路径不需要 czsc
    # （klines_to_df 只用 pandas），否则没装 czsc 的环境连「数据不足」
    # 都走不通，会直接抛 ImportError。
    if len(df) < init_n + 60:
        logger.info(
            f"缠论多周期策略跳过 {code}：30 分钟 K 线 {len(df)} 根，"
            f"不足（需 > {init_n + 60} 根，czsc 前 {init_n} 根不产信号）")
        return result

    czsc = load_czsc()
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
