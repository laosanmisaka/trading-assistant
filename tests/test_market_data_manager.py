"""MarketDataManager 测试 — flush_today_bars / refresh_minute_bars

数据一律构造假数据注入，不打真实网络。
"""

from data.market_data_manager import MarketDataManager
from data.database import get_klines, get_klines_minute, get_kline_count


def _fake_minute_bars(code: str, date: str = "2026-06-15") -> list[dict]:
    """构造与 refresh_minute_bars 写入内存格式一致的假 1min 数据"""
    return [
        {"code": code, "timestamp": f"{date} 09:31:00",
         "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1,
         "volume": 50000, "period": "1min"},
        {"code": code, "timestamp": f"{date} 09:32:00",
         "open": 10.1, "high": 10.4, "low": 10.0, "close": 10.3,
         "volume": 60000, "period": "1min"},
        {"code": code, "timestamp": f"{date} 09:33:00",
         "open": 10.3, "high": 10.5, "low": 10.2, "close": 10.4,
         "volume": 70000, "period": "1min"},
    ]


class TestFlushTodayBars:
    """flush_today_bars 回归测试"""

    def test_flush_with_minute_bars_in_memory_no_exception(self, temp_db):
        """内存中有分钟数据时 flush 不抛异常（回归：旧代码读取错误的键名会 KeyError）"""
        manager = MarketDataManager()
        # 模拟 refresh_minute_bars 写入内存的分钟数据（键: timestamp/open/high/low/close）
        manager._minute_bars["000001"] = _fake_minute_bars("000001")

        count = manager.flush_today_bars()  # 不应抛 KeyError
        assert count == 0  # 无今日日线bar

    def test_flush_persists_today_daily_bar(self, temp_db):
        """今日日线bar正确落库"""
        manager = MarketDataManager()
        manager.update_today_bar("000001", "daily", {
            "code": "000001", "date": "2026-06-15",
            "open": 10.0, "high": 10.5, "low": 9.9, "close": 10.4,
            "volume": 180000, "period": "daily",
        })
        manager._minute_bars["000001"] = _fake_minute_bars("000001")

        count = manager.flush_today_bars()
        assert count == 1

        bars = get_klines("000001", "daily")
        assert len(bars) == 1
        assert bars[0]["date"] == "2026-06-15"
        assert bars[0]["close"] == 10.4

    def test_flush_does_not_duplicate_minute_rows(self, temp_db):
        """flush 不重复写分钟数据（分钟数据由 refresh_minute_bars 实时落库）"""
        from data.database import save_klines_minute_batch

        manager = MarketDataManager()
        minute_bars = _fake_minute_bars("000001")
        saved = save_klines_minute_batch(minute_bars)
        assert saved == 3
        manager._minute_bars["000001"] = minute_bars

        manager.flush_today_bars()

        # DB 中仍只有 3 条，flush 未追加重复写入
        db_minutes = get_klines_minute("000001", "1min")
        assert len(db_minutes) == 3
        # 且保留原始 OHLC（旧 flush 会把 OHLC 全写成 price 单值）
        assert db_minutes[0]["open"] == 10.0
        assert db_minutes[0]["high"] == 10.2
        assert db_minutes[0]["low"] == 9.9


class TestRefreshMinuteBars:
    """refresh_minute_bars 使用假数据注入验证写库链路"""

    def test_refresh_writes_minute_bars_to_db(self, temp_db, monkeypatch):
        """refresh_minute_bars 拉取后即写库，flush 无需兜底"""
        fake_bars = _fake_minute_bars("000001")
        monkeypatch.setattr(
            "data.market_data_manager.fetch_today_1min_bars",
            lambda code: fake_bars,
        )

        manager = MarketDataManager()
        saved = manager.refresh_minute_bars("000001")
        assert saved == 3

        # 分钟数据已在 DB
        db_minutes = get_klines_minute("000001", "1min")
        assert len(db_minutes) == 3
        assert db_minutes[-1]["timestamp"] == "2026-06-15 09:33:00"
        assert db_minutes[-1]["close"] == 10.4

        # 内存缓存也已更新
        assert len(manager.get_minute_bars("000001")) == 3

        # 聚合出的今日日线bar已生成，随后 flush 落库不抛异常
        today = manager.get_today_bar("000001", "daily")
        assert today is not None
        assert today["open"] == 10.0
        assert today["high"] == 10.5
        assert today["close"] == 10.4
        assert today["volume"] == 180000

        count = manager.flush_today_bars()
        assert count == 1
        assert get_kline_count("000001", "daily") == 1


def test_history_backfill_uses_latest_session_and_refreshes_all_periods(temp_db, monkeypatch):
    import data.market_data_manager as module
    from data.models import KLineData
    from data.database import save_klines_batch
    from core.trading_calendar import local_now
    daily = [KLineData(code="000001", date=day, open=10, high=12, low=9, close=price,
                       volume=100, period="daily") for day, price in
             [("2026-06-12", 9.5), ("2026-06-15", 10.4)]]
    minute = _fake_minute_bars("000001", "2026-06-12") + _fake_minute_bars("000001")
    hourly = [{**minute[-1], "period": "60min"}]
    monkeypatch.setattr(module, "fetch_kline", lambda *a: daily)
    monkeypatch.setattr(module, "fetch_1min_kline_history", lambda *a: list(reversed(minute)))
    monkeypatch.setattr(module, "fetch_60min_kline_history", lambda *a: hourly)
    save_klines_batch([{"code": "000001", "date": "2026-06-12", "open": 1, "high": 1,
                       "low": 1, "close": 1, "volume": 1, "period": "daily"}])
    manager = MarketDataManager()
    assert manager.needs_history_refresh("000001")
    manager.fetch_and_store_initial("000001")
    quote = manager.get_quote("000001")
    assert (quote.open, quote.high, quote.low, quote.price, quote.volume) == (10, 10.5, 9.9, 10.4, 180000)
    assert quote.pre_close == 9.5
    assert [b["close"] for b in get_klines("000001", "daily")] == [9.5, 10.4]
    assert get_klines("000001", "weekly")[-1]["date"] == "2026-06-15"
    assert get_klines("000001", "monthly")[-1]["close"] == 10.4
    assert get_klines_minute("000001", "60min")[-1]["close"] == 10.4
    assert not manager.needs_history_refresh("000001")
    manager._history_checked["000001"] = None
    calls = []
    real_fetch = manager.fetch_and_store_initial
    monkeypatch.setattr(manager, "fetch_and_store_initial", lambda code: (calls.append(code), real_fetch(code))[1])
    manager.refresh_history_if_needed("000001")
    assert calls == ["000001"]


def test_sina_fallback_keeps_real_ohlc(monkeypatch):
    import data.market_data as module
    from datetime import datetime
    day = datetime.now().strftime("%Y-%m-%d")
    monkeypatch.setattr(module, "fetch_intraday_data", lambda code: [
        {"time": day + " 09:31:00", "open": 10, "high": 12, "low": 9, "price": 11, "volume": 5},
        {"time": "2000-01-01 09:31:00", "open": 1, "high": 2, "low": 1, "price": 1, "volume": 50}])
    bars = module._fetch_today_1min_sina_fallback("000001")
    assert len(bars) == 1
    assert [bars[0][key] for key in ("open", "high", "low", "close", "volume")] == [10, 12, 9, 11, 5]
