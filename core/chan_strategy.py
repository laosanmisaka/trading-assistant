"""缠论多周期共振策略 —— 日线一买 → 日线二买 → 30 分钟二买，均线卖出

======================================================================
策略定义（老三 2026-09-18 重定，窗口按实测重标定）
======================================================================

**买点**：三重条件，有时间窗

1. 日线出现**一买**（几何判定，见 `core/chan_points.py`）
2. 一买之后 **40 个交易日内**出现**日线二买**
3. 日线二买**前后各 10 个交易日内**出现 **30 分钟二买**

三条**全部确认**的那一刻即为买点，成交再顺延 `entry_delay_bars` 根
30 分钟 bar（等 bar 收盘）。

**卖点**：买入后以日线均线作移动止损

默认 MA5；若收盘价跌破 MA5 但 MA10 仍完好，则改用 MA10 继续持有；
MA5、MA10 **双双被跌破**后卖出。
即"智能选取那条一直沿着上涨、没有被跌破的 5 日或 10 日均线"。

----------------------------------------------------------------------
窗口为什么是 40 / ±10（2026-09-18 实测重标定，改之前先看这组数字）
----------------------------------------------------------------------

原口径是「一买后 15 个交易日内出二买 → 二买后 2 个交易日内出 30 分钟二买」，
那是为 czsc `cxt_*` 的**密集 bar 状态**设计的（一次"日线二买"状态连续覆盖
几十上百根 bar，随便一个窗口都能捞到）。换成**稀疏的点事件**后原参数失效 ——
8 只标的在原口径下 **0 笔**。实测重标定：

- **一买→二买**：实测间隔 **6 / 17 / 27 / 33 / 34 / 34 / 50+ 个交易日**
  （中位数 ≈ 33），15 日卡掉 7/8 → 取 **40**（中位数 + 余量）。
- **二买→30 分钟二买**：原参数「之后 2 日内」**方向是反的** —— 几何法的
  30 分钟买点常**早于**日线二买（次级别先转势；紫金矿业早 5 个交易日）。
  改为**双向 ±10 个交易日**。

⚠️ 允许 30 分钟买点早于日线二买，**不等于**可以在日线二买确认前买入。
成交时点取「所有条件都确认之后」—— 若触发 bar 早于日线二买，成交推迟到
日线二买所在交易日的首根 bar 之后（见 `scan_from_frame` 的 `confirm_pos`），
否则就是在用还没发生的信息交易（1 个交易日尺度的未来函数）。

----------------------------------------------------------------------
信号源：默认几何判定，`cxt_*` 信号保留为备选
----------------------------------------------------------------------

`scan(source=...)` 两个取点方式（下游匹配/卖出两条纯逻辑完全共用）：

- ``SOURCE_GEOMETRY``（**默认**）：`points_from_chan()` —— 用 `core.chan_points`
  的几何判定在 czsc 的笔/中枢上推买卖点。老三 2026-09-18 定的口径：
  「策略也用图上那套几何买卖点，弃用 `cxt_*`」。
- ``SOURCE_SIGNAL``：原实现，用 czsc 的 `cxt_*` 择时信号（状态跃迁）。
  保留用于对比与回归，见 `docs/PLAN_BUYSELL_GEOMETRY.md`。

两者的**笔与中枢都来自 czsc**，差别只在「哪里算买点」这一层。

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

**代码硬门槛**：`scan(source="signal")` 要求 30 分钟 bar 数 > `init_n + 60`
（默认 560 根，按一天 8 根折合约 70 个交易日）。低于此直接返回空结果并记
一条日志。这个门槛只防"无意义的计算"，本身很低 —— 560 根里 czsc 前 500
根不产信号，实际只有 60 根可用（≈7.5 个交易日）。

`scan(source="geometry")` 的门槛是 `GEO_MIN_BARS` 根（480 ≈ 60 个交易日）。
几何法**没有 `init_n` 预热**，但日线要能形成中枢（= 至少 3 笔）才谈得上
一买，太短的数据算不出结构，所以门槛反而更高。

**有效下限**：两个源都远高于代码门槛。历史 cxt_* 版的实测 —— 20 只流动性
标的、每只 1970 根 / 247 个交易日，其中 16 只零买点，命中的 4 只也只有
1~2 笔。门槛低不等于能出结果。
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Iterable, Optional

import pandas as pd

from core.chan import klines_to_df, load_czsc
from data.models import KLineData
from utils.logger import get_logger

logger = get_logger(__name__)

# 一买 → 二买 的最大间隔（交易日）
# 2026-09-18 由 15 改为 40：实测 8 只标的的间隔为 6/17/27/33/34/34/50+，
# 中位数 ≈ 33，原值卡掉 7/8。详见模块 docstring「窗口为什么是 40 / ±10」。
DEFAULT_MAX_GAP_1TO2 = 40
# 日线二买 → 30 分钟二买 的**双向**窗口（前后各这么多交易日）
# 2026-09-18 由「之后 2 日内」改为双向 ±10：几何法的 30 分钟买点常早于
# 日线二买（次级别先转势），原窗口方向是反的。
DEFAULT_MAX_GAP_2TOM = 10
# 次级别周期
# ⚠️ 两个不同的字符串，别混：
#   BASE_FREQ   —— czsc **信号列名**里的频段（"30分钟_D1#SMA#21_BS2辅助V230320"）
#   BASE_PERIOD —— `core.chan.build()` 认的**项目周期**字符串（"30min"）
# 实测把 BASE_FREQ 传给 build() 会抛 `ValueError: 不支持的周期 '30分钟'`。
BASE_FREQ = "30分钟"
BASE_PERIOD = "30min"
# czsc 预热根数：前 init_n 根不产出信号（仅 signal 源使用）
DEFAULT_INIT_N = 500

# 取点来源
SOURCE_SIGNAL = "signal"        # czsc cxt_* 择时信号（状态跃迁）
SOURCE_GEOMETRY = "geometry"    # chan_points 几何判定（默认）

# geometry 源的硬门槛：480 根 ≈ 60 个交易日。
# 几何法没有 init_n 预热，但日线要形成中枢（≥3 笔）才谈得上一买 ——
# 实测日线中枢平均 49 个交易日一个，数据太短根本算不出结构，早退避免
# 无意义计算（避免"跑通了但永远 0 笔"这种更难查的症状）。
GEO_MIN_BARS = 480

# 一买 / 一卖是否要求 MACD 面积背驰确认（`core.chan_points.buy_sell_points`
# 的 require_divergence）。默认开：几何判定放宽了「≥2 中枢」的要求，
# 背驰过滤是补偿 —— 把「力度未衰竭的下跌中继」从一买里剔掉。
# 无法判定（bar 数据缺失）的候选保留，不误杀。
DEFAULT_REQUIRE_DIVERGENCE = True

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
    dt: str                 # 成交时刻（所有条件确认后第 entry_delay_bars 根 bar）
    price: float            # 成交价（该 bar 收盘价）
    d1: str                 # 日线一买生效日
    d2: str                 # 日线二买生效日
    gap_1to2: int           # 一买 → 二买 间隔交易日（恒为正）
    gap_2tom: int           # 30 分钟二买相对日线二买的交易日偏移
                            #   **有符号**：<0 表示 30 分钟买点早于日线二买
                            #   （次级别先转势，成交会推迟到 d2 之后）
    signal_dt: str = ""     # 信号确认时刻（触发用的那根 30 分钟 bar）


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
    source: str = ""                # 取点来源（SOURCE_SIGNAL / SOURCE_GEOMETRY）
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
    m30_items    : ``[(交易日, bar 标识), ...]``，30 分钟二买点，
                   **按时间升序**、允许同一天多次（一天内可有多个 30 分钟 bar）

    规则
    ----
    1. 对每个日线二买日 d2，向前找最近的日线一买日 d1，要求
       ``0 < pos(d2) - pos(d1) <= max_gap_1to2``
    2. 对每个 (d1, d2) 配对，在 ``|pos(d3) - pos(d2)| <= max_gap_2tom``
       的**双向**窗口内取**离 d2 最近**的、尚未被占用的 30 分钟买点作为
       触发点（同距离时取 d2 之后那根 —— 确认顺序上它可立即成交）
    3. 每个 30 分钟买点 bar 只触发一次（先到先得，不重复配对）

    ⚠️ ``max_gap_2tom`` 是**双向**窗口的一半，且 ``gap_2tom`` 返回**有符号**
    偏移（可为负）。2026-09-18 改：几何法的 30 分钟买点常早于日线二买
    （次级别先转势），原「只能在其后」的窗口方向是反的。
    触发 bar 早于 d2 时，**成交时点由调用方推迟**到 d2 之后 —— 本函数只管
    配对，不管成交时点（见 `scan_from_frame` 的 ``confirm_pos``）。

    返回：[{d1, d2, d3, bar, gap_1to2, gap_2tom}, ...]，按触发时间升序
    """
    pos = {d: i for i, d in enumerate(trading_days)}
    d1_list = sorted({d for d in d1_days if d in pos}, key=pos.get)
    d2_list = sorted({d for d in d2_days if d in pos}, key=pos.get)
    # 保留 bar 顺序与重复：一天内可能有多个 30 分钟买点 bar
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

        # 在双向窗口内找离 d2 最近的未占用 bar
        best = None
        for i3, d3, bar in m30_list:
            if i3 < i2 - max_gap_2tom:
                continue
            if i3 > i2 + max_gap_2tom:
                break
            if bar in used_bars:
                continue
            # 距离优先；同距离时 d2 之后的那根优先（可立即成交）
            key = (abs(i3 - i2), 0 if i3 >= i2 else 1)
            if best is None or key < best[0]:
                best = (key, i3, d3, bar)
        if best is None:
            continue

        _, i3, d3, bar = best
        all_matches.append({
            "d1": d1, "d2": d2, "d3": d3, "bar": bar,
            "gap_1to2": i2 - pos[d1],
            "gap_2tom": i3 - i2,          # 有符号：<0 = 30 分钟买点早于日线二买
        })
        used_bars.add(bar)

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
# 几何点事件（geometry 源）—— 用 chan_points 的判定替代 cxt_* 信号
# ======================================================================

@dataclass
class GeoPoints:
    """几何法产出的三级点事件（`points_from_chan` 的返回）

    d1_days / d2_days : 日线一买 / 二买**生效交易日**（= 笔终点所在交易日的
                        次一交易日，见 `points_from_chan`）
    m30_items         : ``[(交易日, bar 下标), ...]``，30 分钟二买点，
                        bar 下标基于调用方传入的 30 分钟帧
    daily_bars        : 合成出的日线根数（诊断用）
    counts            : 各类点数量（诊断用，含未采用的三买/卖点）
    """

    d1_days: list = field(default_factory=list)
    d2_days: list = field(default_factory=list)
    m30_items: list = field(default_factory=list)
    daily_bars: int = 0
    counts: dict = field(default_factory=dict)


def _effective_day(dt, day_list: list, offset: int = 1):
    """笔终点 dt → 生效交易日（``offset=1`` 即次一交易日）

    日线笔的终点落在交易日 T 的收盘价上，而"它成立"这件事要等 T 收盘才
    知道 —— 直接把 T 当生效日就是 1 个交易日的未来函数。T+1 才是可下单的
    第一天。

    返回 None 表示超出现有数据范围（尾部笔尚未确认）—— 宁可丢这个点，
    也不能拿"还不知道成不成立"的笔去交易。
    """
    d = pd.Timestamp(str(dt)).date()
    i = bisect.bisect_left(day_list, d)
    if i < len(day_list) and day_list[i] == d:
        base = i
    elif i > 0:
        base = i - 1        # dt 落在两个交易日之间 → 归到前一个交易日
    else:
        return None
    j = base + offset
    return day_list[j] if j < len(day_list) else None


def _bar_lookup(dt_list: list):
    """返回 ``dt → bar 下标`` 的查找函数

    先精确匹配（笔端点必然落在某根 bar 上），匹配不上再取时间最近的 ——
    错位会静默指向错误的 K 线，所以宁可慢一点也不能瞎猜。
    """
    keys = [t.value for t in dt_list]
    pos = {t: i for i, t in enumerate(dt_list)}

    def lookup(dt) -> int:
        t = pd.Timestamp(str(dt))
        if t.tzinfo is not None:
            t = t.tz_localize(None)
        i = pos.get(t)
        if i is not None:
            return i
        k = bisect.bisect_left(keys, t.value)
        if k <= 0:
            return 0
        if k >= len(keys):
            return len(keys) - 1
        return k - 1 if (t.value - keys[k - 1]) <= (keys[k] - t.value) else k

    return lookup


def points_from_structures(
    bis_m30,
    centers_m30,
    bis_daily,
    centers_daily,
    days,
    day_list,
    dt_list,
    *,
    confirm_offset: int = 1,
    bars_m30=None,
    bars_daily=None,
    require_divergence: bool = DEFAULT_REQUIRE_DIVERGENCE,
) -> GeoPoints:
    """从**已算好的**缠论结构推三级点事件（纯函数，不碰 czsc）

    与 `points_from_chan` 的分工：后者负责「建 czsc 对象」，本函数只做
    「结构 → 点事件」的换算。可视化层已经为画图建好了两套结构，直接用
    本函数可以省掉一次重复计算（建 czsc 对象是全链路最慢的一步）。

    参数
    ----
    bis_m30 / centers_m30     : `core.chan.bis()/centers()` 在 30 分钟上的输出
    bis_daily / centers_daily : 同上，日线级别
    days     : 与 30 分钟帧**等长**的交易日（date 对象，允许重复）
    day_list : 去重升序的交易日（date 对象）
    dt_list  : 与 30 分钟帧**等长**的 Timestamp（用于把笔终点映射回 bar 下标）
    bars_m30 / bars_daily : 两级各自的 bar 行情（DataFrame，含 dt/close），
               供一买/一卖的 MACD 面积背驰判定；不传则背驰退化为「无法判定」
    require_divergence : 一买/一卖是否要求背驰确认（不过滤二买/三买）
    """
    from core import chan_points

    pts = GeoPoints()
    bar_of = _bar_lookup(dt_list)

    def tally(prefix: str, p: dict) -> None:
        key = f"{prefix}{p['kind']}"
        pts.counts[key] = pts.counts.get(key, 0) + 1

    # 日线级：一买 / 二买，延后到次一交易日生效
    for p in chan_points.buy_sell_points(bis_daily, centers_daily,
                                         bars=bars_daily,
                                         require_divergence=require_divergence):
        tally("日线", p)
        if p["kind"] not in ("一买", "二买"):
            continue
        d = _effective_day(p["dt"], day_list, confirm_offset)
        if d is None:
            continue
        if p["kind"] == "一买":
            pts.d1_days.append(d)
        else:
            pts.d2_days.append(d)

    # 30 分钟级：二买，按 bar 时刻直接生效
    for p in chan_points.buy_sell_points(bis_m30, centers_m30,
                                         bars=bars_m30,
                                         require_divergence=require_divergence):
        tally("30分", p)
        if p["kind"] != "二买":
            continue
        i = bar_of(p["dt"])
        pts.m30_items.append((days[i], i))

    pts.m30_items.sort(key=lambda x: x[1])
    pts.d1_days = sorted(set(pts.d1_days))
    pts.d2_days = sorted(set(pts.d2_days))
    return pts


def points_from_chan(
    klines_30m: Iterable[KLineData],
    df_30m: Optional[pd.DataFrame] = None,
    code: str = "",
    *,
    confirm_offset: int = 1,
    require_divergence: bool = DEFAULT_REQUIRE_DIVERGENCE,
) -> GeoPoints:
    """用几何判定（`core.chan_points`）产出三级点事件 —— 替代 `cxt_*` 信号

    参数
    ----
    klines_30m : 30 分钟 K 线（原样传给 `core.chan.build`）
    df_30m     : ``klines_to_df(klines_30m)`` 的结果。不传则内部现算；
                 **传进来的必须与 klines_30m 同源** —— 返回的 bar 下标
                 是这份 DataFrame 的整数位置，错位会静默指向错误的 K 线。
    confirm_offset : 日线点延后的交易日数（默认 1 = 次一交易日）

    三条口径
    --------
    1. **日线点延后到次一交易日生效**，理由见 `_effective_day`。
    2. **30 分钟点直接用 bar 时刻** —— 30 分钟 bar 收盘即确定，不涉及
       未走完的更大周期 bar，无需延后。
    3. 只取**买点**里的「日线一买 / 日线二买 / 30 分钟二买」三级
       （对应策略定义）；其余类别只计入 `counts` 供诊断。

    与 signal 源的差别：czsc 的 `cxt_*` 是**逐 bar 持续状态**，"出现"要靠
    状态跃迁定义；几何点是**离散事件**，没有这个问题（也就没有"一次二买
    状态覆盖几十个交易日"的歧义）。见 `docs/PLAN_BUYSELL_GEOMETRY.md`。
    """
    from core import chan as chan_mod

    klines_30m = list(klines_30m)
    if df_30m is None:
        df_30m = chan_mod.klines_to_df(klines_30m)

    df = df_30m.copy()
    df["dt"] = pd.to_datetime(df["dt"])
    if getattr(df["dt"].dtype, "tz", None) is not None:
        df["dt"] = df["dt"].dt.tz_localize(None)
    days = [t.date() for t in df["dt"]]
    day_list = sorted(set(days))
    dt_list = list(df["dt"])

    daily_df = chan_mod.resample_daily(df)
    cr = chan_mod.build(klines_30m, period=BASE_PERIOD)
    cr_d = None
    if len(daily_df) >= 3:
        cr_d = chan_mod.build(
            chan_mod.df_to_klines(daily_df, period="daily", code=code),
            period="daily")

    pts = points_from_structures(
        chan_mod.bis(cr) if cr else [],
        chan_mod.centers(cr) if cr else [],
        chan_mod.bis(cr_d) if cr_d else [],
        chan_mod.centers(cr_d) if cr_d else [],
        days, day_list, dt_list, confirm_offset=confirm_offset,
        bars_m30=df, bars_daily=daily_df,
        require_divergence=require_divergence)
    pts.daily_bars = len(daily_df)
    return pts


# ======================================================================
# 主入口
# ======================================================================

def scan(
    klines: Iterable[KLineData],
    code: str = "",
    *,
    source: str = SOURCE_GEOMETRY,
    max_gap_1to2: int = DEFAULT_MAX_GAP_1TO2,
    max_gap_2tom: int = DEFAULT_MAX_GAP_2TOM,
    ma_short: int = 5,
    ma_long: int = 10,
    ma_type: str = "SMA",
    timeperiod: int = 21,
    init_n: int = DEFAULT_INIT_N,
    entry_delay_bars: int = 1,
    exit_delay_days: int = 1,
    require_divergence: bool = DEFAULT_REQUIRE_DIVERGENCE,
) -> ScanResult:
    """对一只股票的 30 分钟 K 线跑完整策略，返回买点/卖点与诊断信息

    `source` **默认 geometry**（老三 2026-09-18 定：策略用图上那套几何买卖点、
    弃用 `cxt_*`）。传 ``source="signal"`` 可回到原 czsc 信号实现做对比。

    需要 czsc；未安装时抛 ImportError（由调用方决定是否降级）。
    """
    klines = list(klines)
    df = klines_to_df(klines)
    result = ScanResult(code=code, source=source)

    if source == SOURCE_GEOMETRY:
        # 几何源不需要 czsc 的 init_n 预热，但日线要能形成中枢才谈得上一买
        if len(df) < GEO_MIN_BARS:
            logger.info(
                f"缠论多周期策略跳过 {code}：30 分钟 K 线 {len(df)} 根，"
                f"不足（geometry 源需 ≥ {GEO_MIN_BARS} 根 ≈ "
                f"{GEO_MIN_BARS // 8} 个交易日，否则日线形不成中枢）")
            return result
        pts = points_from_chan(klines, df, code,
                               require_divergence=require_divergence)
        logger.info(
            f"缠论多周期策略 {code}：几何源，30 分钟 {len(df)} 根 / "
            f"日线 {pts.daily_bars} 根，点事件 {pts.counts}")
        return scan_from_frame(df, code=code, source=SOURCE_GEOMETRY, points=pts,
                               ma_short=ma_short, ma_long=ma_long,
                               max_gap_1to2=max_gap_1to2,
                               max_gap_2tom=max_gap_2tom,
                               entry_delay_bars=entry_delay_bars,
                               exit_delay_days=exit_delay_days)

    if source != SOURCE_SIGNAL:
        raise ValueError(
            f"未知的 source={source!r}，可选：{SOURCE_SIGNAL} / {SOURCE_GEOMETRY}")

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
    return scan_from_frame(out, code=code, source=SOURCE_SIGNAL, ma_short=ma_short,
                           ma_long=ma_long,
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
    source: str = SOURCE_SIGNAL,
    points: Optional[GeoPoints] = None,
    ma_short: int = 5,
    ma_long: int = 10,
    max_gap_1to2: int = DEFAULT_MAX_GAP_1TO2,
    max_gap_2tom: int = DEFAULT_MAX_GAP_2TOM,
    columns: Optional[dict] = None,
    entry_delay_bars: int = 1,
    exit_delay_days: int = 1,
) -> ScanResult:
    """从已算好的 30 分钟帧执行策略（匹配/卖出两条纯逻辑对两个源完全共用）

    `source` 决定「点事件」从哪来：

    - ``SOURCE_SIGNAL``（**本函数默认**）：从 `out` 的 czsc 信号列取
      （状态跃迁），需要 `columns`；与历史行为一致。
    - ``SOURCE_GEOMETRY``：用 `points`（`points_from_chan()` 的产出），
      `out` 只需含 dt / close（day 缺了会自动补）。

    ⚠️ 两个入口的默认源不同是**有意的**：`scan()` 是产品入口、默认 geometry；
    本函数是「手上已有一份信号帧」的底层入口，默认沿用 signal。
    新代码请走 `scan()`。

    `out` 的每一行是一根 30 分钟 bar，行序即时间序；行下标（整数位置）就是
    匹配结果里 ``bar`` 的含义。
    """
    columns = columns or signal_columns()
    result = ScanResult(code=code, source=source)

    # czsc 输出的数值列是字符串，统一转数值后再用（见 generate_signal_frame）
    out = out.copy()
    if "close" in out.columns:
        out["close"] = pd.to_numeric(out["close"], errors="coerce")
    if "day" not in out.columns:
        # geometry 源直接传 klines_to_df 的结果（没有 day 列）
        out["day"] = pd.to_datetime(out["dt"]).dt.date

    trading_days = sorted(set(out["day"]))
    result.trading_days = len(trading_days)

    # 每个交易日的**首根 bar** 下标 —— 成交时点不能早于「最后一个被确认的条件」
    first_bar: dict = {}
    for i, d in enumerate(out["day"]):
        first_bar.setdefault(d, i)

    if source == SOURCE_GEOMETRY:
        if points is None:
            raise ValueError(
                "source=geometry 需要传入 points_from_chan() 的结果")
        all_days = list(trading_days)
        d1_days = [d for d in points.d1_days if d in first_bar]
        d2_days = [d for d in points.d2_days if d in first_bar]
        m30_items = [(d, b) for d, b in points.m30_items if d in first_bar]
        m30_positions = [b for _, b in m30_items]
        result.buy1_days = [str(d) for d in d1_days]
        result.buy2_days = [str(d) for d in d2_days]
        result.m30_buy2_bars = [str(out["dt"].iloc[b]) for b in m30_positions]
    elif source == SOURCE_SIGNAL:
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
    else:
        raise ValueError(
            f"未知的 source={source!r}，可选：{SOURCE_SIGNAL} / {SOURCE_GEOMETRY}")

    # ---- 三条件时序匹配 ----
    matches = match_buy_points(all_days, d1_days, d2_days, m30_items,
                               max_gap_1to2=max_gap_1to2,
                               max_gap_2tom=max_gap_2tom)
    if not matches:
        logger.info(
            f"缠论多周期策略 {code}（{source}）：无买点（一买 {len(d1_days)} 次、"
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
        # 成交时点 = **所有条件都确认之后**，再等 entry_delay_bars 根 bar。
        # 双向窗口允许 30 分钟买点早于日线二买，那种情况下必须等到日线二买
        # 所在交易日的**首根 bar** 之后才能动手 —— 否则就是在用尚未发生的
        # 信息交易（1 个交易日尺度的未来函数）。
        confirm_pos = max(m["bar"], first_bar.get(m["d2"], m["bar"]))
        trade_pos = confirm_pos + entry_delay_bars
        if trade_pos >= len(out):
            logger.info(
                f"缠论多周期策略 {code}：{m['d3']} 的触发落在数据末尾，"
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
        # 卖点从**实际成交日**起算 —— 触发点可能早于 d2，用 m["d3"] 会从
        # 一个还没成交的日子开始扫均线。
        sell = match_sell_points(daily, trade_bar["day"], ma_short=ma_short,
                                 ma_long=ma_long, exit_delay_days=exit_delay_days)
        trade = Trade(buy=buy, sell=SellSignal(**sell) if sell else None)
        result.trades.append(trade)
        logger.info(
            f"缠论多周期策略 {code} 买点 {buy.dt} @{buy.price:.2f} "
            f"(一买 {buy.d1} → 二买 {buy.d2} 间隔{buy.gap_1to2}日 → "
            f"30分钟二买 偏移{buy.gap_2tom:+d}日)"
            + (f"，卖点 {trade.sell.dt} @{trade.sell.price:.2f} "
               f"({trade.sell.ma}, {trade.return_pct:+.2f}%)" if trade.sell else "，未平仓")
        )

    return result
