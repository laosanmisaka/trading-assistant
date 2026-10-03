"""Offline evidence for REVIEW_REPORT_2026-10-03.md, baseline 2edb3fb.

Run with the project environment. These checks reproduce defects; an assertion
failure after a fix means the corresponding review evidence needs updating.
No market requests, user database writes, or process termination are performed.
Only temporary CSV/JSON/SQLite files and the application's normal logs are used.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np
import pandas as pd

from core import buy_points, chan, chan_points, triple_buy
from core.alert_engine import AlertEngine
from core.backtest.engine import BacktestEngine
from core.backtest.strategy import Action, Signal, Strategy
from data import database, market_data_manager
from data.models import KLineData, Trade
from scripts import daily_routine, eval_daily_5min, monitor_buy


def synthetic_bars(n=600):
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0, .5, n))
    op = close + rng.normal(0, .2, n)
    hi = np.maximum(op, close) + rng.uniform(0, .5, n)
    lo = np.minimum(op, close) - rng.uniform(0, .5, n)
    vol = rng.integers(10_000, 1_000_000, n)
    # Synthetic weekday sessions; not a claim about an exchange holiday calendar.
    dts = []
    for day in pd.bdate_range("2024-01-02", periods=(n + 47) // 48):
        dts.extend(pd.date_range(day + pd.Timedelta(hours=9, minutes=35),
                                 periods=24, freq="5min"))
        dts.extend(pd.date_range(day + pd.Timedelta(hours=13, minutes=5),
                                 periods=24, freq="5min"))
    return [KLineData(code="TEST", date=str(dts[i]), open=float(op[i]),
                      high=float(hi[i]), low=float(lo[i]), close=float(close[i]),
                      volume=int(vol[i]), period="5min") for i in range(n)]


def signal(**kwargs):
    return {"code": "sh600000", "window_start": "2026-09-01",
            "conf": "2026-09-30 10:00:00", "entry_price": 10.0,
            "bi_idx": 5, "strategy_entry": True,
            "signal_date": "2026-09-29", "amp": 10, "zg": 9, **kwargs}


class FixedStrategy(Strategy):
    def __init__(self, signals):
        self.signals = signals

    def generate_signals(self, daily):
        return self.signals


def main():
    out = {}
    with tempfile.TemporaryDirectory(prefix="ta-review-") as temp:
        tmp = Path(temp)

        # Actual czsc prefix replay, including an actual first-buy point.
        bars = synthetic_bars()
        full = chan.build(bars, "5min")
        bis = chan.bis(full)
        points = chan_points.buy_sell_points(bis, chan.centers(full))
        for p in points:
            if p["kind"] != "一买" or p["bi_idx"] + 1 >= len(bis):
                continue
            conf = triple_buy._confirm_time(bis, p["bi_idx"])
            prefix = [b for b in bars if pd.Timestamp(b.date) <= conf]
            prefix_bis = chan.bis(chan.build(prefix, "5min"))
            following = bis[p["bi_idx"] + 1]
            present = any((b["sdt"], b["edt"]) ==
                          (following["sdt"], following["edt"]) for b in prefix_bis)
            if not present:
                observed = None
                for end in range(len(prefix) + 1, len(bars) + 1):
                    later_bis = chan.bis(chan.build(bars[:end], "5min"))
                    if any((b["sdt"], b["edt"]) ==
                           (following["sdt"], following["edt"]) for b in later_bis):
                        observed = bars[end - 1].date
                        break
                assert observed is not None and pd.Timestamp(observed) > conf
                out["R01_confirmation"] = {
                    "first_buy_dt": str(p["dt"]), "claimed_conf": str(conf),
                    "following_bi_present_at_conf": present,
                    "first_observed_following_bi": observed}
                break
        assert "R01_confirmation" in out

        # A rolling input window renumbers surviving historical strokes.
        trimmed = chan.bis(chan.build(bars[100:], "5min"))
        old = {(str(b["sdt"]), str(b["edt"])): i for i, b in enumerate(bis)}
        changes = [(old[key], i, key) for i, b in enumerate(trimmed)
                   if (key := (str(b["sdt"]), str(b["edt"]))) in old
                   and old[key] != i]
        assert changes
        old_i, new_i, endpoints = changes[-1]
        out["R04_rolling_identity"] = {
            "old_index": old_i, "new_index": new_i, "same_endpoints": endpoints}

        (tmp / "daily_sh600519.csv").write_text(
            "dt\n2026-09-28\n2026-09-29\n", encoding="utf-8")
        days = buy_points.recent_trading_days(
            cache_dir=tmp, today=date(2026, 9, 30))
        sig = tmp / "signals.jsonl"
        sig.write_text(json.dumps(signal()) + "\n", encoding="utf-8")
        pts = buy_points.recent_buy_points(sig, keep_dates=set(days))
        assert not pts and "2026-09-30" not in days
        out["R03_intraday_filter"] = {"keep_dates": days, "visible_today": pts}

        sig.write_text("\n".join(map(json.dumps, [
            signal(), signal(conf="2026-09-30 09:55:00", entry_price=9.5, revision=1)
        ])), encoding="utf-8")
        selected = buy_points.recent_buy_points(sig, keep_dates={"2026-09-30"})
        assert selected["600000"]["entry_price"] == 10.0
        out["R05_revision"] = {"latest_revision_price": 9.5,
                                "displayed_price": selected["600000"]["entry_price"]}

        # All database functions resolve to an isolated temporary database.
        with patch.object(database, "_get_path", return_value=str(tmp / "review.db")):
            database.init_db()
            engine = AlertEngine()
            engine.set_manual_sl("TEST", 9)
            engine.set_manual_tp("TEST", 12)
            saved = database.get_manual_alert("TEST")
            assert saved["sl_active"] is False and engine.get_state("TEST").sl_manual
            out["R06_manual_persistence"] = saved

            for typ, px, day in [("buy", 10, "2026-01-05"),
                                  ("sell", 11, "2026-01-06"),
                                  ("buy", 20, "2026-01-07")]:
                database.add_trade(Trade(stock_code="RESET", trade_type=typ,
                                         price=px, quantity=100, trade_date=day))
            pos = database.get_position_summary("RESET")
            assert pos["avg_cost"] == 15 and pos["hold_qty"] == 100
            out["R08_position_cost"] = {"expected_current_cost": 20, "actual": pos}

            from ui.trade_dialog import TradeDialog
            class Label:
                def setText(self, value):
                    self.text = value
                def setStyleSheet(self, value):
                    pass
                def setTextFormat(self, value):
                    pass
            database.add_trade(Trade(stock_code="PNL", price=10, quantity=100,
                                     trade_date="2026-01-05"))
            obj = SimpleNamespace(stock_code="PNL", **{
                key: Label() for key in ["lbl_hold_qty", "lbl_avg_cost", "lbl_total_buy",
                                         "lbl_total_sell", "lbl_pnl"]})
            obj._get_main_window = lambda: SimpleNamespace(data_manager=SimpleNamespace(
                get_quote=lambda c: SimpleNamespace(price=10)))
            TradeDialog._update_summary(obj)
            assert "-1,000.00" in obj.lbl_pnl.text
            out["R07_pnl"] = {"expected": 0, "display": obj.lbl_pnl.text}

        history = [dict(code="TEST", timestamp=f"2026-09-{d} 10:00:00",
                        open=p, high=p, low=p, close=p, volume=100, period="1min")
                   for d, p in [(28, 10), (29, 20), (30, 30)]]
        manager = market_data_manager.MarketDataManager()
        with patch.object(market_data_manager, "fetch_kline", return_value=[]), \
             patch.object(market_data_manager, "fetch_1min_kline_history", return_value=history), \
             patch.object(market_data_manager, "fetch_60min_kline_history", return_value=[]), \
             patch.object(market_data_manager, "save_klines_minute_batch", return_value=3):
            manager.fetch_and_store_initial("TEST")
        q = manager.get_quote("TEST")
        assert q.open == 10 and q.volume == 300
        out["R09_multiday_quote"] = {"expected_open": 30, "actual_open": q.open,
                                     "expected_volume": 100, "actual_volume": q.volume}

        daily = [KLineData(date="2026-01-05", close=10)]
        cheap = BacktestEngine(initial_capital=1001)
        qty = cheap._affordable_lots(1001, 10)
        cash = 1001 - qty * 10 - max(qty * 10 * cheap.commission_rate, cheap.min_commission)
        zero = cheap.run_on_data(FixedStrategy([
            Signal("2026-01-05", Action.BUY, price=10, weight=0)]), "TEST", daily)
        dd = BacktestEngine(initial_capital=1000)._build_report(
            FixedStrategy([]), "TEST", [], [900, 950], False)
        assert cash == -4 and zero.open_position and dd.max_drawdown == 0
        out["R12_execution"] = {"remaining_cash": cash, "zero_weight_opened": zero.open_position,
                                "expected_drawdown": 0.1, "reported_drawdown": dd.max_drawdown}

        old_frame = pd.DataFrame({"dt": ["2026-09-01", "2026-09-02"], "close": [10, 10]})
        new_frame = pd.DataFrame({"dt": ["2026-09-28"], "close": [5]})
        merged, status = daily_routine.merge_incremental(old_frame, new_frame)
        assert status == "appended" and len(merged) == 3
        out["R13_unverified_adjustment"] = {"overlap_rows": 0, "status": status,
                                           "mixed_closes": merged.close.tolist()}

        # Numeric conversion occurs outside the request/JSON exception boundary.
        malformed = {"day": "2026-09-30 09:35:00", "open": "bad",
                     "high": "10", "low": "9", "close": "10", "volume": "100"}
        with patch.object(monitor_buy.requests, "get", return_value=SimpleNamespace(
                text=json.dumps([malformed]))):
            try:
                monitor_buy.fetch_sina_5min("sh600000")
            except ValueError:
                out["R15_invalid_bar"] = "ValueError escapes per-stock fetch"
        assert "R15_invalid_bar" in out
        assert monitor_buy._in_session(datetime(2026, 10, 3, 10, 0)) is True
        out["R16_weekend"] = "2026-10-03 Saturday 10:00 accepted"

        # Real chart renderer with a forced, platform-independent fallback path.
        from core.backtest import viz
        with patch.object(viz, "_chinese_font", return_value="sans-serif"):
            try:
                viz.render_report_figure(zero, daily)
            except ValueError as exc:
                assert "sans-serif" in str(exc)
                out["R17_font"] = "sans-serif fallback raises ValueError"
        assert "R17_font" in out

        # Every opportunity equals its own stock's baseline, yet ex_pt is +4 pp.
        returns = [{"code": "A", "ret": 10.0}] * 9 + [{"code": "B", "ret": 0.0}]
        stats = eval_daily_5min.metrics(returns, {"A": 10.0, "B": 0.0})
        assert stats["ex_pt"] == 4.0 and stats["ex_eqw"] == 0.0
        out["R18_baseline_weights"] = {"expected_paired_excess": 0, "actual": stats}

        # Inspect generated source only; no browser execution or network request.
        from core import chan_viz
        marker = "</script><script>/* review marker */</script>"
        payload = {"meta": {"name": marker, "code": "TEST", "period_label": "5分钟",
                            "counts": {}, "daily_points": {}, "m30_points": {},
                            "bars": 0, "trading_days": 0, "first_dt": "", "last_dt": "",
                            "strategy_summary": {}}}
        with patch.object(chan_viz, "_echarts_source", return_value=""):
            html = chan_viz.render_html(payload)
        assert marker in html.split("var CFG = ", 1)[1]
        out["R19_html_embedding"] = "literal closing script tag preserved inside CFG payload"

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
