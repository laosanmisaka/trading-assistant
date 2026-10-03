"""止损止盈引擎测试 — 手动覆写 / 冲突检测 / 状态管理"""

import pytest
from data.database import init_db, get_manual_alert, set_manual_alert, clear_manual_alert
from data.models import AlertState, RealtimeQuote
from core.alert_engine import AlertEngine


@pytest.fixture
def engine(temp_db):
    """提供已初始化的 AlertEngine"""
    return AlertEngine()


class TestAlertState:
    """测试状态管理"""

    def test_get_state_creates_default(self, engine):
        state = engine.get_state("000001")
        assert state.stock_code == "000001"
        assert state.stop_loss_price == 0.0
        assert state.sl_manual is False

    def test_get_state_returns_same_instance(self, engine):
        s1 = engine.get_state("000001")
        s2 = engine.get_state("000001")
        assert s1 is s2

    def test_different_codes_have_separate_states(self, engine):
        s1 = engine.get_state("000001")
        s2 = engine.get_state("000002")
        assert s1 is not s2
        s1.stop_loss_price = 10.0
        assert s2.stop_loss_price == 0.0


class TestManualOverride:
    """测试手动止盈止损覆写"""

    def test_set_manual_sl(self, engine, temp_db):
        engine.set_manual_sl("000001", 8.50)
        state = engine.get_state("000001")
        assert state.sl_manual is True
        assert state.stop_loss_price == 8.50

    def test_set_manual_tp(self, engine, temp_db):
        engine.set_manual_tp("000001", 13.00)
        state = engine.get_state("000001")
        assert state.tp_manual is True
        assert state.take_profit_price == 13.00

    def test_clear_manual_restores_auto(self, engine, temp_db):
        engine.set_manual_sl("000001", 8.50)
        engine.clear_manual("000001", "sl")
        state = engine.get_state("000001")
        assert state.sl_manual is False

    @pytest.mark.parametrize("order", [("sl", "tp"), ("tp", "sl")])
    def test_manual_persisted_across_engines(self, temp_db, order):
        """手动设置在数据库持久化，新引擎自动加载"""
        engine1 = AlertEngine()
        for field in order:
            getattr(engine1, f"set_manual_{field}")("000001", 7.77 if field == "sl" else 12.0)

        engine2 = AlertEngine()
        state = engine2.get_state("000001")
        assert state.sl_manual is True
        assert state.stop_loss_price == 7.77
        assert state.tp_manual and state.take_profit_price == 12.0

        engine2.clear_manual("000001", "all")

    def test_calc_stop_loss_skips_when_manual(self, engine, temp_db):
        """手动模式下 calc_stop_loss 不更新值"""
        engine.set_manual_sl("000001", 9.00)
        new_sl, conflict = engine.calc_stop_loss("000001", 11.00)
        # 自动值本应是 11.00，但手动模式返回手动值
        assert new_sl == 9.00
        assert conflict is None  # 手动模式不产生冲突，直接静默返回

        engine.clear_manual("000001", "all")

    def test_calc_take_profit_skips_when_manual(self, engine, temp_db):
        """手动止盈模式下不自动更新"""
        engine.set_manual_tp("000001", 15.00)
        new_tp, conflict = engine.calc_take_profit("000001", 14.00)
        assert new_tp == 15.00


class TestAlertCheck:
    """测试止损止盈触发检查"""

    def test_stop_loss_triggered(self, engine, temp_db):
        state = engine.get_state("000001")
        state.stop_loss_price = 10.00
        quote = RealtimeQuote(code="000001", price=9.50)
        result = engine.check_alerts("000001", quote)
        assert result["triggered"] is True
        assert result["type"] == "stop_loss"

    def test_no_alert_when_price_above_stop(self, engine, temp_db):
        state = engine.get_state("000001")
        state.stop_loss_price = 10.00
        quote = RealtimeQuote(code="000001", price=10.50)
        result = engine.check_alerts("000001", quote)
        assert result["triggered"] is False

    def test_take_profit_limit_up_triggered(self, engine, temp_db):
        """涨停模式下止盈：价格 >= 止盈价触发"""
        state = engine.get_state("000001")
        state.take_profit_price = 12.00
        state.top_fractal_detected = False  # 涨停模式
        quote = RealtimeQuote(code="000001", price=12.05)
        result = engine.check_alerts("000001", quote)
        assert result["triggered"] is True
        assert result["type"] == "take_profit"

    def test_take_profit_fractal_triggered(self, engine, temp_db):
        """顶分型模式：价格 <= 止盈价触发"""
        state = engine.get_state("000001")
        state.take_profit_price = 11.00
        state.top_fractal_detected = True
        quote = RealtimeQuote(code="000001", price=10.80)
        result = engine.check_alerts("000001", quote)
        assert result["triggered"] is True
        assert result["type"] == "take_profit"

    def test_alert_disabled_skips(self, engine, temp_db):
        """关闭提醒后不触发"""
        from data.database import disable_alert
        disable_alert("000001")
        state = engine.get_state("000001")
        state.stop_loss_price = 10.00
        quote = RealtimeQuote(code="000001", price=9.00)
        result = engine.check_alerts("000001", quote)
        assert result["triggered"] is False


class TestCalcTakeProfitThrottle:
    """calc_take_profit 的30min分型检测限流 (避免高频重复调用API)"""

    def test_first_call_checks_fractal(self, engine, temp_db, monkeypatch):
        """首次调用应该触发检测 (返回的新tp可能不同于初始值)"""
        from data.database import add_trade, get_all_groups
        from data.models import Trade, TradeType
        # 需要持仓才能设置止盈
        groups = get_all_groups()
        holding = next(g for g in groups if g.type == "holding")
        add_trade(Trade(stock_code="000001", trade_type="buy",
                        price=10.0, quantity=100, fee=1.0,
                        trade_date="2026-05-20"))
        state = engine.get_state("000001")
        state._last_fractal_check = 0  # 强制允许检查

        from types import SimpleNamespace
        rows = [{"timestamp": f"2026-05-20 {hour}:00:00", "open": 10, "high": high,
                 "low": low, "close": 10, "volume": 100} for hour, high, low in
                (("10", 12, 8), ("11", 15, 9), ("14", 12, 8))]
        manager = SimpleNamespace(get_minute_klines_from_db=lambda *a: rows)
        monkeypatch.setattr("core.alert_engine.get_data_manager", lambda: manager)
        tp, conflict = engine.calc_take_profit("000001", 10.50)
        assert tp > 0 and not state.top_fractal_detected
        for row in rows:
            row["timestamp"] = row["timestamp"].replace("05-20", "05-21")
        state._last_fractal_check = 0
        tp, conflict = engine.calc_take_profit("000001", 10.50)
        assert tp == 15 and state.top_fractal_detected

    def test_second_call_within_60s_skips(self, engine, temp_db):
        """60秒内第二次调用不触发API (返回原值)"""
        state = engine.get_state("000001")
        state._last_fractal_check = 9999999999  # 未来时间，禁止检查
        state.take_profit_price = 11.00

        tp, conflict = engine.calc_take_profit("000001", 10.50)
        # 应该直接返回原值，不触发API
        assert tp == 11.00

    def test_top_fractal_detected_no_more_checks(self, engine, temp_db):
        """顶分型已检测到后不再调用API (即使超过60秒)"""
        state = engine.get_state("000001")
        state.top_fractal_detected = True
        state.take_profit_price = 11.00
        state._last_fractal_check = 0

        tp, _ = engine.calc_take_profit("000001", 10.50)
        assert tp == 11.00  # 锁定不变
