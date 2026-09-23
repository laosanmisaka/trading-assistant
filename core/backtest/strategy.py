"""回测策略接口（抽象层）

⚠️ 2026-09-20：**原有内置策略 `BuyPointStrategy` 已整体删除** —— 它属于已取缔的
伪缠论买点体系（周线底分型 + 日线 MACD 金叉 + 自算缩量回踩，与桌面端
`core/chan_points.py` 的几何判定是两套口径）。同时删除的还有：
`_resample_weekly`（周线前缀重采样）与 `WeeklyAggregator`（为前者做的增量优化）。
删掉的连带影响：`config.GOLDEN_CROSS_LOOKBACK_DAYS`、`core/technical.py` 的
`get_latest_bottom_fractal` / `detect_*_cross` 也随之失去唯一调用方并一并删除。

本模块现在只保留**策略接口**：要跑回测，自己实现 `Strategy.generate_signals()`
交给 `core.backtest.engine.BacktestEngine` 即可（`Strategy` 是 ABC）。

真正的缠论策略在 `core.chan_strategy`，它自带逐笔回测（`scan()`），但**不做资金
约束**（允许重叠持仓、按笔统计）。若要做带资金/仓位的组合回测，两条路：
  - 用本模块的 `BacktestEngine`，写一个把 `chan_strategy` 输出转成 `Signal` 的适配器
  - 或按 `docs/PLAN_BUYSELL_GEOMETRY.md` §8 的说明，在 `scan_from_frame` 组装
    trades 的循环里加仓位分配
两条路的取舍见该文档；截至 2026-09-20 没有实施任何一条。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class Action(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass
class Signal:
    """单条交易信号。date 为成交日，price 为成交价。

    `weight` 是**本次委托量占「本轮满仓股数」的比例**（满仓股数 = 本轮开仓时
    按可用资金能买满的整手数，一轮之内固定）：
      - `BUY  weight=0.5` → 再买入满仓的 1/2（不会越过满仓）
      - `SELL weight=0.5` → 卖出满仓的 1/2；`SELL weight=1.0` → 清仓
    默认 1.0 即「全进全出」，与加入该字段之前的行为完全一致。
    委托量按整手（100 股）四舍五入 —— 「卖一半」与「买回一半」用的是同一个数，
    所以卖完再买回能精确回到原仓位，不会因取整慢慢偏离。
    """
    date: str
    action: Action
    price: float = 0.0
    reason: str = ""
    weight: float = 1.0
    kind: str = ""
    """信号分类标签（如缠论的 "一买"/"三卖"），引擎原样记到
    `Trade.signal_kind`，供 `BacktestReport.win_rate_by_kind()` 分组统计。
    与 `reason` 的分工：reason 是给人看的完整描述，kind 是可枚举的分类键。"""


class Strategy(ABC):
    """策略抽象基类 — 换策略只需继承并实现 generate_signals，引擎不动。"""

    name = "base"

    @abstractmethod
    def generate_signals(self, daily) -> list[Signal]:
        """输入升序日线，输出逐日信号。第 i 日信号必须只用 daily[0..i] 计算。"""
