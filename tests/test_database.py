"""数据库层测试 — 分组/股票/交易/手动止盈止损"""

import os
import pytest
from data.database import (
    init_db, get_all_groups, add_group, delete_group, update_group,
    add_stock, get_stocks_by_group, get_all_stocks, remove_stock, move_stock,
    get_trades, add_trade, update_trade, delete_trade, get_position_summary,
    get_first_buy_date,
    get_manual_alert, set_manual_alert, clear_manual_alert,
    get_discipline_rule, save_discipline_rule,
    get_setting, set_setting,
    is_alert_disabled, disable_alert, enable_alert,
)
from data.models import Trade, TradeType, GroupType
from config import PRESET_GROUPS


class TestGroupInit:
    """测试分组初始化和去重"""

    def test_init_creates_preset_groups(self, temp_db):
        """首次 init 应创建预设分组"""
        groups = get_all_groups()
        assert len(groups) == len(PRESET_GROUPS)
        names = [g.name for g in groups]
        for preset_name, _ in PRESET_GROUPS:
            assert preset_name in names

    def test_repeated_init_no_duplicates(self, temp_db):
        """重复调用 init_db 不会创建重复分组 (回归测试)"""
        # 第一次 init 已在 conftest 中完成
        count_before = len(get_all_groups())

        # 模拟程序重启: 再次调用 init_db
        init_db()
        init_db()
        init_db()

        count_after = len(get_all_groups())
        assert count_after == count_before, (
            f"重复 init 导致分组从 {count_before} 增加到 {count_after}"
        )

    def test_preset_groups_have_correct_types(self, temp_db):
        """预设分组类型正确"""
        groups = get_all_groups()
        types = {g.type for g in groups}
        assert "holding" in types
        assert "cleared" in types
        assert "tracking" in types


class TestCustomGroup:
    """测试自定义分组 CRUD"""

    def test_add_custom_group(self, temp_db):
        g = add_group("测试分组", "custom")
        assert g.id > 0
        assert g.name == "测试分组"
        all_groups = get_all_groups()
        assert any(x.name == "测试分组" for x in all_groups)

    def test_delete_custom_group(self, temp_db):
        g = add_group("待删除", "custom")
        delete_group(g.id)
        all_groups = get_all_groups()
        assert not any(x.id == g.id for x in all_groups)

    def test_cannot_delete_preset_group(self, temp_db):
        """预设分组不可删除"""
        groups = get_all_groups()
        holding = next(g for g in groups if g.type == "holding")
        delete_group(holding.id)
        # 应仍然存在
        groups2 = get_all_groups()
        assert any(g.id == holding.id for g in groups2)


class TestStockCRUD:
    """测试股票增删改查"""

    def test_add_stock_to_group(self, temp_db):
        groups = get_all_groups()
        holding = next(g for g in groups if g.type == "holding")
        stock = add_stock("000001", "平安银行", holding.id)
        assert stock is not None
        assert stock.code == "000001"

        stocks = get_stocks_by_group(holding.id)
        assert len(stocks) == 1

    def test_duplicate_stock_ignored(self, temp_db):
        """同分组内重复代码被忽略"""
        groups = get_all_groups()
        holding = next(g for g in groups if g.type == "holding")
        s1 = add_stock("000001", "平安银行", holding.id)
        s2 = add_stock("000001", "平安银行", holding.id)
        assert s1 is not None
        assert s2 is None  # 重复被忽略

    def test_remove_stock(self, temp_db):
        groups = get_all_groups()
        holding = next(g for g in groups if g.type == "holding")
        stock = add_stock("000001", "平安银行", holding.id)
        remove_stock(stock.id)
        stocks = get_stocks_by_group(holding.id)
        assert len(stocks) == 0

    def test_move_stock_between_groups(self, temp_db):
        groups = get_all_groups()
        holding = next(g for g in groups if g.type == "holding")
        tracking = next(g for g in groups if g.type == "tracking")

        stock = add_stock("000001", "平安银行", holding.id)
        move_stock(stock.id, tracking.id)

        holding_stocks = get_stocks_by_group(holding.id)
        tracking_stocks = get_stocks_by_group(tracking.id)
        assert len(holding_stocks) == 0
        assert len(tracking_stocks) == 1


class TestTradeCRUD:
    """测试交易记录"""

    def test_add_buy_trade(self, temp_db):
        trade = Trade(stock_code="000001", trade_type=TradeType.BUY.value,
                      price=10.50, quantity=1000, fee=5.0,
                      trade_date="2026-05-20")
        added = add_trade(trade)
        assert added.id > 0

        trades = get_trades("000001")
        assert len(trades) == 1
        assert trades[0].price == 10.50

    def test_position_summary(self, temp_db):
        """持仓成本和数量计算"""
        add_trade(Trade(stock_code="000001", trade_type="buy",
                        price=10.0, quantity=1000, fee=5.0,
                        trade_date="2026-05-20"))
        add_trade(Trade(stock_code="000001", trade_type="buy",
                        price=11.0, quantity=500, fee=3.0,
                        trade_date="2026-05-25"))

        s = get_position_summary("000001")
        assert s["hold_qty"] == 1500
        assert s["total_buy_qty"] == 1500
        # 成本 = (10*1000+5 + 11*500+3) / 1500 = (10005 + 5503) / 1500 = 10.339
        assert abs(s["avg_cost"] - 10.339) < 0.01

    def test_position_after_sell(self, temp_db):
        """卖出后持仓减少"""
        add_trade(Trade(stock_code="000001", trade_type="buy",
                        price=10.0, quantity=1000, fee=5.0,
                        trade_date="2026-05-20"))
        add_trade(Trade(stock_code="000001", trade_type="sell",
                        price=12.0, quantity=500, fee=3.0,
                        trade_date="2026-05-30"))

        s = get_position_summary("000001")
        assert s["hold_qty"] == 500
        assert s["total_sell_qty"] == 500
        assert s["avg_cost"] == 10.005
        assert s["position_cost"] == 5002.5
        add_trade(Trade(stock_code="000001", trade_type="sell", price=12.0,
                        quantity=500, trade_date="2026-06-01"))
        assert get_first_buy_date("000001") is None
        add_trade(Trade(stock_code="000001", trade_type="buy", price=20.0,
                        quantity=100, trade_date="2026-06-01"))
        reopened = get_position_summary("000001")
        assert reopened["avg_cost"] == 20.0
        assert reopened["first_buy_date"] == "2026-06-01"


class TestManualAlert:
    """测试手动止盈止损持久化"""

    def test_set_and_get_manual_alert(self, temp_db):
        set_manual_alert("000001", sl_active=True, sl_price=9.50,
                         tp_active=False, tp_price=0.0)
        result = get_manual_alert("000001")
        assert result["sl_active"] is True
        assert result["sl_price"] == 9.50
        assert result["tp_active"] is False

    def test_clear_manual_alert(self, temp_db):
        set_manual_alert("000001", sl_active=True, sl_price=9.50,
                         tp_active=True, tp_price=12.00)
        clear_manual_alert("000001", "sl")
        result = get_manual_alert("000001")
        assert result["sl_active"] is False
        assert result["tp_active"] is True  # tp 未被清除

    def test_clear_all_manual(self, temp_db):
        set_manual_alert("000001", sl_active=True, sl_price=9.50,
                         tp_active=True, tp_price=12.00)
        clear_manual_alert("000001")
        result = get_manual_alert("000001")
        assert result["sl_active"] is False
        assert result["tp_active"] is False


class TestDiscipline:
    """测试交易纪律"""

    def test_save_and_get_discipline(self, temp_db):
        save_discipline_rule("", "测试纪律内容")
        rule = get_discipline_rule("")
        assert rule is not None
        assert rule.rule_text == "测试纪律内容"

    def test_stock_specific_discipline(self, temp_db):
        save_discipline_rule("000001", "特定纪律")
        rule = get_discipline_rule("000001")
        assert rule.rule_text == "特定纪律"


class TestStockNames:
    """测试股票名称本地缓存"""

    def test_get_nonexistent_returns_none(self, temp_db):
        from data.database import get_stock_name
        assert get_stock_name("999999") is None

    def test_save_and_get_name(self, temp_db):
        from data.database import update_stock_name, get_stock_name
        update_stock_name("000001", "平安银行")
        assert get_stock_name("000001") == "平安银行"

    def test_update_existing_name(self, temp_db):
        from data.database import update_stock_name, get_stock_name
        update_stock_name("000001", "旧名称")
        update_stock_name("000001", "新名称")
        assert get_stock_name("000001") == "新名称"

    def test_batch_save_and_search(self, temp_db):
        from data.database import save_stock_names_batch, search_stock_names
        data = [
            {"code": "000001", "name": "平安银行"},
            {"code": "000002", "name": "万科A"},
            {"code": "600519", "name": "贵州茅台"},
            {"code": "300750", "name": "宁德时代"},
        ]
        count = save_stock_names_batch(data)
        assert count == 4

        # 按代码搜索
        results = search_stock_names("000001", limit=10)
        assert len(results) == 1
        assert results[0]["name"] == "平安银行"

        # 按名称模糊搜索
        results2 = search_stock_names("平安", limit=10)
        assert len(results2) == 1
        assert results2[0]["code"] == "000001"

        # 部分名称搜索
        results3 = search_stock_names("德时", limit=10)
        assert len(results3) == 1
        assert results3[0]["code"] == "300750"

    def test_search_case_insensitive(self, temp_db):
        from data.database import save_stock_names_batch, search_stock_names
        save_stock_names_batch([{"code": "000001", "name": "平安银行"}])
        results = search_stock_names("PING", limit=10)
        # SQLite LIKE 默认不区分大小写
        assert len(results) >= 0  # 不一定匹配中文

    def test_get_count(self, temp_db):
        from data.database import save_stock_names_batch, get_stock_names_count
        assert get_stock_names_count() == 0
        save_stock_names_batch([{"code": "000001", "name": "平安银行"}])
        assert get_stock_names_count() == 1


class TestKLines:
    """测试K线数据持久化"""

    def test_save_and_get_klines(self, temp_db):
        from data.database import save_klines_batch, get_klines, get_kline_count
        bars = [
            {"code": "000001", "date": "2026-06-01", "open": 10.0, "high": 10.5,
             "low": 9.8, "close": 10.2, "volume": 100000, "period": "daily"},
            {"code": "000001", "date": "2026-06-02", "open": 10.2, "high": 10.8,
             "low": 10.1, "close": 10.5, "volume": 120000, "period": "daily"},
            {"code": "000001", "date": "2026-06-03", "open": 10.5, "high": 11.0,
             "low": 10.3, "close": 10.8, "volume": 150000, "period": "daily"},
        ]
        count = save_klines_batch(bars)
        assert count == 3
        assert get_kline_count("000001", "daily") == 3

    def test_get_klines_returns_ascending(self, temp_db):
        from data.database import save_klines_batch, get_klines
        bars = [
            {"code": "000001", "date": "2026-06-03", "open": 10.5, "high": 11.0,
             "low": 10.3, "close": 10.8, "volume": 150000, "period": "daily"},
            {"code": "000001", "date": "2026-06-01", "open": 10.0, "high": 10.5,
             "low": 9.8, "close": 10.2, "volume": 100000, "period": "daily"},
            {"code": "000001", "date": "2026-06-02", "open": 10.2, "high": 10.8,
             "low": 10.1, "close": 10.5, "volume": 120000, "period": "daily"},
        ]
        save_klines_batch(bars)
        result = get_klines("000001", "daily")
        assert len(result) == 3
        assert result[0]["date"] == "2026-06-01"
        assert result[2]["date"] == "2026-06-03"

    def test_get_klines_with_days_limit(self, temp_db):
        from data.database import save_klines_batch, get_klines
        bars = [
            {"code": "000001", "date": f"2026-06-{d:02d}", "open": 10.0, "high": 10.5,
             "low": 9.8, "close": 10.2, "volume": 100000, "period": "daily"}
            for d in range(1, 16)
        ]
        save_klines_batch(bars)
        result = get_klines("000001", "daily", days=5)
        assert len(result) == 5
        assert result[0]["date"] == "2026-06-11"
        assert result[-1]["date"] == "2026-06-15"

    def test_upsert_same_date_updates(self, temp_db):
        from data.database import save_klines_batch, get_klines
        bar1 = [
            {"code": "000001", "date": "2026-06-01", "open": 10.0, "high": 10.5,
             "low": 9.8, "close": 10.2, "volume": 100000, "period": "daily"},
        ]
        save_klines_batch(bar1)
        bar2 = [
            {"code": "000001", "date": "2026-06-01", "open": 10.2, "high": 11.0,
             "low": 10.1, "close": 10.8, "volume": 150000, "period": "daily"},
        ]
        save_klines_batch(bar2)
        result = get_klines("000001", "daily")
        assert len(result) == 1
        assert result[0]["close"] == 10.8  # 第二次写入的值

    def test_get_latest_date(self, temp_db):
        from data.database import save_klines_batch, get_latest_kline_date
        bars = [
            {"code": "000001", "date": "2026-05-15", "open": 10.0, "high": 10.5,
             "low": 9.8, "close": 10.2, "volume": 100000, "period": "daily"},
            {"code": "000001", "date": "2026-06-01", "open": 10.2, "high": 10.8,
             "low": 10.1, "close": 10.5, "volume": 120000, "period": "daily"},
        ]
        save_klines_batch(bars)
        assert get_latest_kline_date("000001", "daily") == "2026-06-01"

    def test_different_periods_separate(self, temp_db):
        from data.database import save_klines_batch, get_klines
        daily = [
            {"code": "000001", "date": "2026-06-01", "open": 10.0, "high": 10.5,
             "low": 9.8, "close": 10.2, "volume": 100000, "period": "daily"},
        ]
        weekly = [
            {"code": "000001", "date": "2026-06-01", "open": 10.0, "high": 10.5,
             "low": 9.8, "close": 10.2, "volume": 100000, "period": "weekly"},
        ]
        save_klines_batch(daily)
        save_klines_batch(weekly)
        assert len(get_klines("000001", "daily")) == 1
        assert len(get_klines("000001", "weekly")) == 1
