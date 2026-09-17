"""命令行回测入口：python -m core.backtest 000001 --days 500"""
import argparse

from core.backtest.strategy import BuyPointStrategy
from core.backtest.engine import BacktestEngine
from data.market_data import DataSourceError


def main() -> int:
    parser = argparse.ArgumentParser(description="回测单只股票")
    parser.add_argument("code", help="6位股票代码，如 000001")
    parser.add_argument("--days", type=int, default=500, help="回测历史天数")
    parser.add_argument("--capital", type=float, default=100000.0, help="初始资金")
    parser.add_argument("--strategy", default="buypoint", help="策略名（当前仅 buypoint）")
    args = parser.parse_args()

    if args.strategy != "buypoint":
        print(f"未知策略: {args.strategy}，当前仅支持 buypoint")
        return 2

    engine = BacktestEngine(initial_capital=args.capital)
    try:
        report = engine.run(BuyPointStrategy(), args.code, days=args.days)
    except DataSourceError as e:
        # 数据源故障 → 明确报错并非零退出，而不是打印一份空报告
        print(f"数据源错误：{e}")
        return 1

    print(report.format())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
