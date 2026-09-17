"""回测引擎 — 执行策略信号，模拟资金/持仓/手续费，计算绩效指标"""
from dataclasses import dataclass, field

from data.models import KLineData
from core.backtest.strategy import Strategy, Action
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class Trade:
    """一笔已平仓交易"""
    entry_date: str = ""
    entry_price: float = 0.0
    exit_date: str = ""
    exit_price: float = 0.0
    quantity: int = 0
    profit: float = 0.0
    profit_pct: float = 0.0
    reason: str = ""


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

    def run_on_data(self, strategy: Strategy, code: str, daily: list[KLineData]) -> BacktestReport:
        """对给定日线数据回测（不触网，便于测试）"""
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
        entry_price = 0.0
        entry_date = ""
        buy_fee = 0.0
        trades: list[Trade] = []
        equity_curve: list[float] = []

        for k in daily:
            for s in sig_by_date.get(k.date, []):
                if s.action == Action.BUY and position_qty == 0:
                    # 全仓买入，预留佣金（A 股一手 100 股）
                    qty = int(cash / (s.price * 100 * (1 + self.commission_rate))) * 100
                    if qty > 0:
                        cost = qty * s.price
                        buy_fee = max(cost * self.commission_rate, self.min_commission)
                        cash -= cost + buy_fee
                        position_qty = qty
                        entry_price = s.price
                        entry_date = s.date
                elif s.action == Action.SELL and position_qty > 0:
                    proceeds = position_qty * s.price
                    sell_fee = (
                        max(proceeds * self.commission_rate, self.min_commission)
                        + proceeds * self.stamp_tax_rate
                    )
                    cash += proceeds - sell_fee
                    profit = (s.price - entry_price) * position_qty - buy_fee - sell_fee
                    entry_cost = entry_price * position_qty + buy_fee
                    profit_pct = profit / entry_cost if entry_cost > 0 else 0.0
                    trades.append(Trade(
                        entry_date=entry_date, entry_price=entry_price,
                        exit_date=s.date, exit_price=s.price,
                        quantity=position_qty, profit=round(profit, 2),
                        profit_pct=profit_pct, reason=s.reason,
                    ))
                    position_qty = 0
                    buy_fee = 0.0

            equity_curve.append(cash + position_qty * k.close)

        return self._build_report(strategy, code, trades, equity_curve, position_qty > 0)

    def _build_report(self, strategy, code, trades, equity_curve, has_open) -> BacktestReport:
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
        )
