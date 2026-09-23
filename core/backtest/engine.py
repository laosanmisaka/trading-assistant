"""回测引擎 — 执行策略信号，模拟资金/持仓/手续费，计算绩效指标"""
from dataclasses import dataclass, field

from data.models import KLineData
from core.backtest.strategy import Strategy, Action
from utils.logger import get_logger

logger = get_logger(__name__)


def lots(qty: float) -> int:
    """四舍五入到整手（100 股）。A 股委托必须是 100 的整数倍。

    用**四舍五入**而不是向下取整：分级出口里「卖一半」与「买回一半」用的都是
    `lots(满仓股数 * 0.5)`，同一个数 ⇒ 卖了再买回能精确回到原仓位，不产生
    取整漂移（900 股卖 500 剩 400，买回 500 又是 900）。
    """
    return max(int(qty / 100.0 + 0.5) * 100, 0)


@dataclass
class Trade:
    """一笔已平仓交易

    `episode` 是**持仓轮次**编号（每只标的从 1 开始，每轮首次建仓 +1）。
    全进全出时一轮 = 一笔；分级减仓/回补时一轮会拆成多笔（卖半仓、回补后再卖…），
    此时「按笔」的胜率会被拆细，要按 `episode` 归并才是真正的「一轮赚没赚」。
    """
    entry_date: str = ""
    entry_price: float = 0.0
    exit_date: str = ""
    exit_price: float = 0.0
    quantity: int = 0
    profit: float = 0.0
    profit_pct: float = 0.0
    reason: str = ""
    episode: int = 0
    signal_kind: str = ""   # 本轮开仓信号的分类（如 "一买"），来自 Signal.kind


@dataclass
class BacktestReport:
    """回测绩效报告"""
    code: str = ""
    strategy: str = ""
    initial_capital: float = 0.0
    total_trades: int = 0
    win_trades: int = 0
    loss_trades: int = 0
    win_rate: float = 0.0
    avg_profit_pct: float = 0.0
    profit_factor: float = 0.0
    total_return: float = 0.0
    max_drawdown: float = 0.0
    open_position: bool = False
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)   # 逐 bar 权益（资金+持仓市值）
    equity_dates: list = field(default_factory=list)   # 与 equity_curve 等长的日期

    def win_rate_by_kind(self) -> dict:
        """按买入信号分类（`Trade.signal_kind`）分组统计胜率

        返回 ``{kind: {"trades", "wins", "win_rate", "avg_profit_pct"}}``，
        未标注分类的交易归入 ``"未标注"``。用于回答「缠论一买/二买/三买
        各自的胜率是多少」这类问题。注意是**按笔**统计 —— 分级减仓把一轮
        持仓拆成多笔时会拆细（缠论买卖点策略全进全出，一轮=一笔，不受影响）。
        """
        groups: dict[str, list] = {}
        for t in self.trades:
            groups.setdefault(t.signal_kind or "未标注", []).append(t)
        out = {}
        for kind, ts in groups.items():
            wins = sum(1 for t in ts if t.profit > 0)
            out[kind] = {
                "trades": len(ts),
                "wins": wins,
                "win_rate": wins / len(ts) if ts else 0.0,
                "avg_profit_pct": (sum(t.profit_pct for t in ts) / len(ts)
                                   if ts else 0.0),
            }
        return out

    def format(self) -> str:
        lines = [
            "=" * 46,
            f"回测报告  [{self.code}]  策略: {self.strategy}",
            f"初始资金: {self.initial_capital:,.0f}",
            "-" * 46,
            f"交易次数: {self.total_trades}",
            f"胜率: {self.win_rate:.2%}  ({self.win_trades}胜 / {self.loss_trades}负)",
            f"平均每笔盈亏: {self.avg_profit_pct:+.2%}",
            f"盈亏比: {self._pf_str()}",
            f"总收益率: {self.total_return:+.2%}",
            f"最大回撤: {self.max_drawdown:.2%}",
        ]
        by_kind = self.win_rate_by_kind()
        if len(by_kind) > 1 or "未标注" not in by_kind:
            lines.append("-" * 46)
            lines.append("按信号分类胜率:")
            for kind, g in by_kind.items():
                lines.append(
                    f"  {kind}: {g['win_rate']:.2%} "
                    f"({g['wins']}胜 / {g['trades'] - g['wins']}负)  "
                    f"平均 {g['avg_profit_pct']:+.2%}"
                )
        if self.open_position:
            lines.append("⚠ 仍有未平仓持仓（按最后收盘价计入收益）")
        lines.append("-" * 46)
        lines.append("逐笔明细:")
        for idx, t in enumerate(self.trades, 1):
            lines.append(
                f"[{idx}] {t.entry_date} @{t.entry_price:.2f} → "
                f"{t.exit_date} @{t.exit_price:.2f}  "
                f"{t.profit:+.2f} ({t.profit_pct:+.2%})  {t.reason}"
            )
        lines.append("=" * 46)
        return "\n".join(lines)

    def _pf_str(self) -> str:
        if self.profit_factor == float("inf"):
            return "∞"
        return f"{self.profit_factor:.2f}"


class BacktestEngine:
    """回测引擎 — 哑执行器：接收策略信号，模拟交易并算指标。"""

    def __init__(
        self,
        initial_capital: float = 100000.0,
        commission_rate: float = 0.00025,   # 佣金万 2.5（双边）
        stamp_tax_rate: float = 0.001,        # 印花税千 1（仅卖出）
        min_commission: float = 5.0,          # 最低佣金 5 元
    ):
        self.initial_capital = initial_capital
        self.commission_rate = commission_rate
        self.stamp_tax_rate = stamp_tax_rate
        self.min_commission = min_commission

    def run(self, strategy: Strategy, code: str, days: int = 500) -> BacktestReport:
        """拉取历史日线并回测

        两种"空"必须区分（`fetch_kline` 已改为按此契约）：
          - **数据源故障** → 向上抛 `DataSourceError`，调用方应提示/重试，
            绝不能拿一份空报告当做"回测跑完了"
          - **源正常但无数据** → 返回空报告，并在日志中写明是停牌/代码不存在
        """
        from data.market_data import fetch_kline
        daily = fetch_kline(code, "daily", days=days)
        if not daily:
            logger.warning(f"{code} 数据源正常应答但无日线数据（停牌或代码不存在）")
        return self.run_on_data(strategy, code, daily)

    def _affordable_lots(self, cash: float, price: float) -> int:
        """这笔现金按当前价最多能买几手（预留佣金）"""
        if price <= 0:
            return 0
        return int(cash / (price * 100 * (1 + self.commission_rate))) * 100

    def run_on_data(self, strategy: Strategy, code: str, daily: list[KLineData]) -> BacktestReport:
        """对给定日线数据回测（不触网，便于测试）

        支持**部分成交**（`Signal.weight`）：一轮持仓内可以减半仓、再回补，
        再全清。成本按**加权平均**计（含买入费用），每次减仓按卖出比例结转成本，
        因此每笔的 `profit_pct` 是「这一部分份额」的收益率，不是整轮的。
        """
        if not daily:
            return BacktestReport(
                code=code, strategy=strategy.name, initial_capital=self.initial_capital,
            )

        signals = strategy.generate_signals(daily)
        sig_by_date: dict[str, list] = {}
        for s in signals:
            sig_by_date.setdefault(s.date, []).append(s)

        cash = self.initial_capital
        position_qty = 0
        position_cost = 0.0          # 当前持仓成本（含买入费用），按卖出比例结转
        episode_full_qty = 0         # 本轮「满仓股数」，weight 的基准
        episode = 0
        episode_kind = ""            # 本轮开仓信号的分类（Signal.kind）
        entry_price = 0.0
        entry_date = ""
        trades: list[Trade] = []
        equity_curve: list[float] = []

        for k in daily:
            for s in sig_by_date.get(k.date, []):
                if s.price <= 0:
                    continue
                w = float(getattr(s, "weight", 1.0) or 1.0)

                if s.action == Action.BUY:
                    if position_qty == 0:
                        # 新一轮开仓：先按可用资金定出「满仓股数」
                        episode_full_qty = self._affordable_lots(cash, s.price)
                        if episode_full_qty <= 0:
                            continue
                        entry_price, entry_date = s.price, s.date
                        episode_kind = str(getattr(s, "kind", "") or "")
                        episode += 1
                    # 买入 `weight × 满仓股数`，但不越过满仓、也买不起更多
                    room = max(episode_full_qty - position_qty, 0)
                    delta = min(lots(episode_full_qty * w),
                                room, self._affordable_lots(cash, s.price))
                    if delta > 0:
                        cost = delta * s.price
                        fee = max(cost * self.commission_rate, self.min_commission)
                        cash -= cost + fee
                        position_qty += delta
                        position_cost += cost + fee

                elif s.action == Action.SELL and position_qty > 0:
                    if w >= 1.0:
                        delta = position_qty          # 清仓：不留碎股
                    else:
                        delta = min(lots(episode_full_qty * w), position_qty)
                    if delta <= 0:
                        continue
                    proceeds = delta * s.price
                    fee = (max(proceeds * self.commission_rate, self.min_commission)
                           + proceeds * self.stamp_tax_rate)
                    cash += proceeds - fee
                    cost_part = position_cost * (delta / position_qty)
                    profit = proceeds - cost_part - fee
                    trades.append(Trade(
                        entry_date=entry_date, entry_price=entry_price,
                        exit_date=s.date, exit_price=s.price, quantity=delta,
                        profit=round(profit, 2),
                        profit_pct=(profit / cost_part) if cost_part > 0 else 0.0,
                        reason=s.reason, episode=episode,
                        signal_kind=episode_kind,
                    ))
                    position_qty -= delta
                    position_cost -= cost_part
                    if position_qty <= 0:
                        position_qty = 0
                        position_cost = 0.0
                        entry_price = 0.0
                        entry_date = ""
                        episode_full_qty = 0

            equity_curve.append(cash + position_qty * k.close)

        return self._build_report(strategy, code, trades, equity_curve,
                                  position_qty > 0,
                                  dates=[k.date for k in daily])

    def _build_report(self, strategy, code, trades, equity_curve, has_open,
                      dates=None) -> BacktestReport:
        total = len(trades)
        wins = [t for t in trades if t.profit > 0]
        losses = [t for t in trades if t.profit <= 0]
        total_gain = sum(t.profit for t in wins)
        total_loss = abs(sum(t.profit for t in losses))

        final_equity = equity_curve[-1] if equity_curve else self.initial_capital
        total_return = (final_equity - self.initial_capital) / self.initial_capital

        peak = -float("inf")
        max_dd = 0.0
        for e in equity_curve:
            peak = max(peak, e)
            if peak > 0:
                max_dd = max(max_dd, (peak - e) / peak)

        profit_factor = (total_gain / total_loss) if total_loss > 0 else float("inf")

        return BacktestReport(
            code=code, strategy=strategy.name, initial_capital=self.initial_capital,
            total_trades=total, win_trades=len(wins), loss_trades=len(losses),
            win_rate=(len(wins) / total) if total else 0.0,
            avg_profit_pct=(sum(t.profit_pct for t in trades) / total) if total else 0.0,
            profit_factor=profit_factor,
            total_return=total_return, max_drawdown=max_dd,
            open_position=has_open, trades=trades,
            equity_curve=list(equity_curve),
            equity_dates=list(dates) if dates else [],
        )
