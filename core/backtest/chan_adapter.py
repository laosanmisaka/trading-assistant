# -*- coding: utf-8 -*-
"""缠论几何买卖点 → `BacktestEngine` 的信号适配器

把 `core/chan_points.buy_sell_points()` 的六类买卖点包装成 `Strategy`
接口的逐日 `Signal`：买点（一/二/三买）→ BUY，卖点（一/二/三卖）→ SELL，
`Signal.kind` 携带买卖点分类，引擎记账后可用
`BacktestReport.win_rate_by_kind()` 得到**按买卖点类型分组的胜率**。

⚠️ 未来函数口径（KI-009，必读）
--------------------------------
几何买卖点的位置是**笔的终点**，而笔要等后续反向笔成型才锁定 ——
「笔终点当成交时刻」天然带滞后（日线级实测中位 ≈6.5 个交易日）。
本适配器的口径与 `core.chan_strategy` 一致：点事件确认后**延后
``confirm_offset`` 个交易日**、以当日收盘价成交（默认 1 = 次一交易日）。
这缓解的是「信号当日 bar 未走完」的问题，**并不能消除笔确认滞后本身**；
要更保守的回测，把 ``confirm_offset`` 调到 6~7 再跑一遍对比。

也正因为这个口径，本策略的回测结果读作「这套买卖点体系的事后有效性
体检」，不是「实盘可复现的逐笔收益」。严格无未来函数的口径（突破瞬间
入场）见 `scripts/eval_triple_prob.py` 与 `docs/` 里的反向统计。
"""
from __future__ import annotations

import bisect
from typing import Iterable, Optional, Sequence

from core.backtest.strategy import Action, Signal, Strategy
from core.chan_strategy import DEFAULT_REQUIRE_DIVERGENCE
from data.models import KLineData


class ChanPointStrategy(Strategy):
    """缠论六类买卖点策略（几何判定，与桌面端标注图同源）

    参数
    ----
    points             : 注入的买卖点序列（`buy_sell_points()` 的输出）。
                         传入则 ``generate_signals`` 不再现算缠论结构 ——
                         测试与「点已算好」的调用方（如 UI 复用 payload）用这条路，
                         不依赖 czsc。None 则对输入日线现算（需要 czsc）。
    require_divergence : 现算时一买/一卖是否要求 MACD 面积背驰确认
    confirm_offset     : 点事件确认后延后几个交易日成交（KI-009 见模块 docstring）
    """

    name = "chan_points"

    BUY_KINDS = ("一买", "二买", "三买")
    SELL_KINDS = ("一卖", "二卖", "三卖")

    def __init__(
        self,
        points: Optional[Sequence[dict]] = None,
        *,
        require_divergence: bool = DEFAULT_REQUIRE_DIVERGENCE,
        confirm_offset: int = 1,
    ):
        self._points = list(points) if points is not None else None
        self.require_divergence = require_divergence
        self.confirm_offset = confirm_offset

    def compute_points(self, daily: Iterable[KLineData]) -> list[dict]:
        """对日线现算六类买卖点（需要 czsc）"""
        from core import chan as chan_mod
        from core import chan_points

        daily = list(daily)
        result = chan_mod.build(daily, period="daily")
        if result is None:
            return []
        return chan_points.buy_sell_points(
            chan_mod.bis(result), chan_mod.centers(result),
            bars=chan_mod.klines_to_df(daily),
            require_divergence=self.require_divergence)

    def generate_signals(self, daily) -> list[Signal]:
        daily = list(daily)
        if not daily:
            return []
        points = (self._points if self._points is not None
                  else self.compute_points(daily))

        dates = [k.date for k in daily]
        closes = {k.date: float(k.close) for k in daily}
        signals: list[Signal] = []
        for p in points:
            kind = str(p["kind"])
            if kind in self.BUY_KINDS:
                action = Action.BUY
            elif kind in self.SELL_KINDS:
                action = Action.SELL
            else:
                continue
            d = str(p["dt"])[:10]
            # 点的日期理论上就是某个交易日；对不上（分钟点/非交易日）时
            # 落到其后第一个交易日，再叠加确认延后
            i = bisect.bisect_left(dates, d)
            j = i + self.confirm_offset
            if j >= len(dates):
                continue                    # 数据末尾，点确认后已无可成交日
            date = dates[j]
            signals.append(Signal(
                date=date, action=action, price=closes[date],
                reason=f"缠论{kind}", kind=kind))
        signals.sort(key=lambda s: s.date)
        return signals
