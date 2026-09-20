# -*- coding: utf-8 -*-
"""「六脉神剑」六指标共振策略 —— MACD / KDJ / RSI / LWR / BBI / MTM

来源
----
移植自通达信副图公式（原文见仓库根 `lmsj.txt`）。原公式给六个常用技术指标
各记一个多头布尔量 `B1`~`B6`，**六者同时为真的第一天**即「涨买入」信号
（原文 `涨买入` 一行的 `REF(...)=0` 判断）。

指标口径（照原公式，未调参）
---------------------------
| 记号 | 定义 | 多头条件 |
| --- | --- | --- |
| B1 | `DIFF=EMA(C,5)-EMA(C,10)`，`DEA=EMA(DIFF,3)` | `DIFF > DEA` |
| B2 | `RSV=(C-LLV(L,8))/(HHV(H,8)-LLV(L,8))*100`，`K=SMA(RSV,3,1)`，`D=SMA(K,3,1)` | `K > D` |
| B3 | `RSI1=SMA(MAX(C-REF(C,1),0),5,1)/SMA(ABS(C-REF(C,1)),5,1)*100`，`RSI2` 同式取 13 | `RSI1 > RSI2` |
| B4 | `RSV'=-(HHV(H,13)-C)/(HHV(H,13)-LLV(L,13))*100`，`LWR1=SMA(RSV',3,1)`，`LWR2=SMA(LWR1,3,1)` | `LWR1 > LWR2` |
| B5 | `BBI=(MA(C,3)+MA(C,6)+MA(C,12)+MA(C,24))/4` | `C > BBI` |
| B6 | `MTM=C-REF(C,1)`，`MMS=100*EMA(EMA(MTM,5),3)/EMA(EMA(ABS(MTM),5),3)`，`MMM` 同式取 13/8 | `MMS > MMM` |

关于 B4 的方向（原文作者存疑，这里已核对）
------------------------------------------
LWR 在多数软件里是**反向**指标（值越高越弱）。但原公式写的是
`RSV' = -(HHV(H,13)-C)/(HHV(H,13)-LLV(L,13))*100`，**多了个负号**，
值域变成 `[-100, 0]` —— 价格越靠近 13 日高点越接近 0（越大）。
所以 `LWR1 > LWR2` 表示短期相对位置在抬升，与其它五项**同向**，
方向本身没有问题。

真正的隐忧不在方向，而在**六项高度共线**：B1/B5/B6 是趋势与动量、
B2/B3/B4 是同一价格区间的不同刻度，周期都挤在 5~24 根内。
实测（茅台 1500 根）共振天数占比 **28%**，远高于"六项独立"时的 1%
⇒ 共振并不稀有，"共振首日"更接近短线择时而非稀缺信号。
量化诊断见 `scripts/eval_six_pulse.py` 产出的报告。

`SMA` 是通达信的加权平均 `Y=(M*X+(N-M)*Y')/N`（**不是**简单均线），
等价于 `ewm(alpha=M/N, adjust=False)`；`EMA` 等价于 `ewm(span=N, adjust=False)`。

买卖点
------
- **买点**：`B1&…&B6` 全真且**前一日不全真**（共振首日）。
- **卖点**：收盘价跌破 MA10（`C < MA10`）—— 本轮口径，老三指定「卖点也取 10 日均线」。
- **成交延迟**：信号在 T 日**收盘**才能确认，成交一律放到 **T+1 开盘**。
  两端对称，无未来函数（`tests/test_six_pulse.py` 有回归保护）。

持仓状态机：空仓才认买点、持仓才认卖点 ⇒ 输出天然买卖交替，不会出现 T+0。

`hold_days` 参数是**给对照实验用的备用出口**（买点不变、把卖点换成"固定持有
N 个交易日"），默认 `None` 即仍走 MA 卖点。它的用途是定位亏损来源 ——
到底是买点选错，还是 MA 出口太紧。生产口径不要用。

与 `core.chan_strategy` 的关系
------------------------------
两者互相独立，共用 `core.backtest.engine` 的执行/绩效口径（资金、佣金、
印花税、回撤）。`SixPulseStrategy` 实现 `core.backtest.strategy.Strategy`，
可直接交给 `BacktestEngine.run_on_data()`。

可视化
------
`core/chan_viz`（`python visualize_chan.py <代码>`）把本策略的买卖点以
**菱形**标在缠论 K 线图的同一张图上（与缠论策略的圆点区分开），供两者
对照买点位置。注意图上用的是**同一份合成日线**（约 247 个交易日），
不是评估用的 1500 根日线 —— 样本少得多，且前 60 根是预热区、不出信号。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.backtest.strategy import Action, Signal, Strategy
from data.models import KLineData

#: 预热根数。BBI 需 24 根，MTM 是双重 EMA(13→8)，60 根后初值影响可忽略。
WARMUP = 60

#: 六项指标名，顺序与原文 B1..B6 一致
PULSE_NAMES = ("B1", "B2", "B3", "B4", "B5", "B6")


# ======================================================================
# 通达信函数等价实现
# ======================================================================

def ema(s: pd.Series, n: int) -> pd.Series:
    """通达信 `EMA(X,N)` —— 等价 `ewm(span=N, adjust=False)`"""
    return s.ewm(span=n, adjust=False).mean()


def sma_tdx(s: pd.Series, n: int, m: int = 1) -> pd.Series:
    """通达信 `SMA(X,N,M)` —— `Y=(M*X+(N-M)*Y')/N`，等价 `ewm(alpha=M/N)`

    ⚠️ 与 `pandas.rolling().mean()`（简单均线）**不是**一回事，
    也与 `ema()` 不同：这里 alpha=M/N，M 默认为 1。
    """
    return s.ewm(alpha=m / n, adjust=False).mean()


def _ma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def _hhv(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).max()


def _llv(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).min()


# ======================================================================
# 指标
# ======================================================================

def to_frame(daily: list[KLineData]) -> pd.DataFrame:
    """`list[KLineData]` → DataFrame（按输入顺序，假定已升序）"""
    return pd.DataFrame({
        "date": [k.date for k in daily],
        "open": [float(k.open) for k in daily],
        "high": [float(k.high) for k in daily],
        "low": [float(k.low) for k in daily],
        "close": [float(k.close) for k in daily],
        "volume": [int(k.volume) for k in daily],
    })


def compute_indicators(df: pd.DataFrame, ma_exit: int = 10) -> pd.DataFrame:
    """按原公式算 B1~B6，并派生 `all6` / `buy_signal` / `sell_signal`

    `sell_signal` = 收盘跌破 `ma_exit` 日均线（默认 10）。

    除零（连续一字板导致 `HHV==LLV`、`REF` 差为 0）产生 NaN/inf 的地方，
    布尔比较结果为 False —— 即该 bar 不算多头，不会炸。
    """
    c, h, l = df["close"], df["high"], df["low"]
    out = pd.DataFrame(index=df.index)

    diff = ema(c, 5) - ema(c, 10)
    dea = ema(diff, 3)
    out["B1"] = diff > dea

    rsv1 = (c - _llv(l, 8)) / (_hhv(h, 8) - _llv(l, 8)) * 100
    k = sma_tdx(rsv1, 3, 1)
    d = sma_tdx(k, 3, 1)
    out["B2"] = k > d

    lc = c.shift(1)
    up = (c - lc).clip(lower=0)
    absd = (c - lc).abs()
    rsi1 = sma_tdx(up, 5, 1) / sma_tdx(absd, 5, 1) * 100
    rsi2 = sma_tdx(up, 13, 1) / sma_tdx(absd, 13, 1) * 100
    out["B3"] = rsi1 > rsi2

    rsv = -(_hhv(h, 13) - c) / (_hhv(h, 13) - _llv(l, 13)) * 100
    lwr1 = sma_tdx(rsv, 3, 1)
    lwr2 = sma_tdx(lwr1, 3, 1)
    out["B4"] = lwr1 > lwr2

    bbi = (_ma(c, 3) + _ma(c, 6) + _ma(c, 12) + _ma(c, 24)) / 4
    out["B5"] = c > bbi

    mtm = c - c.shift(1)
    mms = 100 * ema(ema(mtm, 5), 3) / ema(ema(absd, 5), 3)
    mmm = 100 * ema(ema(mtm, 13), 8) / ema(ema(absd, 13), 8)
    out["B6"] = mms > mmm

    for name in PULSE_NAMES:
        out[name] = out[name].fillna(False).astype(bool)

    all6 = out["B1"] & out["B2"] & out["B3"] & out["B4"] & out["B5"] & out["B6"]
    out["all6"] = all6
    # 共振首日：今天全真、昨天不全真
    out["buy_signal"] = all6 & ~all6.shift(1).fillna(False).astype(bool)
    out["sell_signal"] = c < _ma(c, ma_exit)
    # 连同 OHLC 一起返回 —— 下游（诊断/报告）需要 close/open 而不必再取一次
    result = df.copy()
    for name in list(PULSE_NAMES) + ["all6", "buy_signal", "sell_signal"]:
        result[name] = out[name]
    return result


# ======================================================================
# 策略
# ======================================================================

class SixPulseStrategy(Strategy):
    """六指标共振首日买入、跌破 MA10 卖出

    参数
    ----
    warmup : 前 N 根不产生信号（指标预热，避免初值干扰）
    ma_exit : 卖出均线周期，默认 10
    hold_days : **仅供对照实验**。不为 None 时改用「固定持有 N 个交易日」出口，
        用来区分「买点选错」与「MA 出口太紧」。生产口径保持 None。
    """

    name = "six_pulse"

    def __init__(self, warmup: int = WARMUP, ma_exit: int = 10,
                 hold_days: int | None = None):
        self.warmup = int(warmup)
        self.ma_exit = int(ma_exit)
        self.hold_days = None if hold_days is None else int(hold_days)

    def generate_signals(self, daily: list[KLineData]) -> list[Signal]:
        """逐日信号；成交日 = 信号次日，成交价 = 次日开盘价"""
        if len(daily) < self.warmup + 2:
            return []

        df = to_frame(daily)
        ind = compute_indicators(df, ma_exit=self.ma_exit)
        dates = df["date"].tolist()
        opens = df["open"].to_numpy(dtype=float)
        buy = ind["buy_signal"].to_numpy()
        sell = ind["sell_signal"].to_numpy()
        n = len(dates)

        signals: list[Signal] = []
        holding = False
        entry_i = -1
        for i in range(self.warmup, n):
            if not holding:
                if buy[i] and i + 1 < n:
                    signals.append(Signal(
                        date=dates[i + 1], action=Action.BUY,
                        price=float(opens[i + 1]), reason="六脉共振首日",
                    ))
                    holding = True
                    entry_i = i + 1
            else:
                if self.hold_days is not None:
                    # 卖出日 = 买入日 + hold_days ⇒ 持有 hold_days 个交易日（含两端）
                    trigger = (i - entry_i) >= self.hold_days - 1
                    reason = f"持有 {self.hold_days} 日到期"
                else:
                    trigger = bool(sell[i])
                    reason = f"破 MA{self.ma_exit}"
                if trigger and i + 1 < n:
                    signals.append(Signal(
                        date=dates[i + 1], action=Action.SELL,
                        price=float(opens[i + 1]), reason=reason,
                    ))
                    holding = False
        return signals
