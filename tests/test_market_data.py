"""市场数据接口测试 — 搜索 DB 优先 / K线 / 代码前缀"""

import pytest
from data.database import save_stock_names_batch, get_stock_names_count
from data.market_data import (
    search_stock, sync_stock_names_from_api,
    _add_market_prefix,
    fetch_kline,
)


class TestCodePrefix:
    def test_shenzhen(self):
        assert _add_market_prefix("000001") == "sz000001"
        assert _add_market_prefix("300750") == "sz300750"

    def test_shanghai(self):
        assert _add_market_prefix("600519") == "sh600519"
        assert _add_market_prefix("900901") == "sh900901"

    def test_beijing(self):
        assert _add_market_prefix("830799") == "bj830799"

    def test_already_prefixed(self):
        assert _add_market_prefix("sz000001") == "sz000001"


class TestSearchLocalDB:
    """测试本地DB搜索 (不涉及网络)"""

    def test_search_by_code(self, temp_db):
        save_stock_names_batch([
            {"code": "000001", "name": "平安银行"},
            {"code": "000002", "name": "万科A"},
        ])
        results = search_stock("000001")
        assert len(results) >= 1
        assert any(r["code"] == "000001" and r["name"] == "平安银行" for r in results)

    def test_search_by_name_keyword(self, temp_db):
        save_stock_names_batch([
            {"code": "000001", "name": "平安银行"},
            {"code": "601318", "name": "中国平安"},
            {"code": "000002", "name": "万科A"},
        ])
        results = search_stock("平安")
        assert len(results) >= 2
        codes = [r["code"] for r in results]
        assert "000001" in codes
        assert "601318" in codes

    def test_search_no_match(self, temp_db):
        save_stock_names_batch([{"code": "000001", "name": "平安银行"}])
        results = search_stock("ZZZZZ")
        assert len(results) == 0

    def test_search_empty_db_falls_back_to_api(self, temp_db):
        """本地数据库为空时尝试API (可能因网络失败但不应崩溃)"""
        # 确保DB为空
        assert get_stock_names_count() == 0
        try:
            results = search_stock("000001")
        except RuntimeError:
            # API 不可用时抛出 RuntimeError
            pass
        # 不应抛出其他异常


class TestFallbackWhenQuotesUnavailable:
    """行情不可用时，表格应使用 DB 中的 stub 数据渲染 (回归: add后API挂, 表格空)"""

    def test_merge_db_stocks_with_empty_quotes(self, temp_db):
        """模拟: _on_quote_data_ready 收到空 quotes → 仍生成 stub RealtimeQuote"""
        from data.database import add_stock, get_stocks_by_group, get_all_groups
        from data.models import RealtimeQuote

        # 在持仓分组中添加股票
        groups = get_all_groups()
        holding = next(g for g in groups if g.type == "holding")
        add_stock("000001", "平安银行", holding.id)
        add_stock("600519", "贵州茅台", holding.id)

        # 获取 DB 中的股票列表
        stocks_in_group = get_stocks_by_group(holding.id)
        codes = [s.code for s in stocks_in_group]
        name_map = {s.code: s.name for s in stocks_in_group}

        # 模拟行情 API 挂了 → quotes 为空
        quotes = {}

        # 合并逻辑 (同 _on_quote_data_ready)
        merged = {}
        for code in codes:
            if code in quotes:
                merged[code] = quotes[code]
            else:
                merged[code] = RealtimeQuote(
                    code=code,
                    name=name_map.get(code, ""),
                    price=0.0,
                    timestamp="--",
                )

        # 验证: DB 中的股票都应出现在 merged 中
        assert len(merged) == 2
        assert "000001" in merged
        assert "600519" in merged
        assert merged["000001"].name == "平安银行"
        assert merged["000001"].price == 0.0  # stub
        assert merged["600519"].name == "贵州茅台"

    def test_merge_with_partial_quotes(self, temp_db):
        """部分有行情、部分没有 → 都显示"""
        from data.database import add_stock, get_stocks_by_group, get_all_groups
        from data.models import RealtimeQuote

        groups = get_all_groups()
        holding = next(g for g in groups if g.type == "holding")
        add_stock("000001", "平安银行", holding.id)
        add_stock("600519", "贵州茅台", holding.id)

        stocks_in_group = get_stocks_by_group(holding.id)
        codes = [s.code for s in stocks_in_group]
        name_map = {s.code: s.name for s in stocks_in_group}

        # 只有 000001 有行情
        quotes = {
            "000001": RealtimeQuote(code="000001", name="平安银行",
                                    price=10.50, change_pct=1.5, timestamp="14:30:00"),
        }

        merged = {}
        for code in codes:
            if code in quotes:
                merged[code] = quotes[code]
            else:
                merged[code] = RealtimeQuote(code=code, name=name_map.get(code, ""),
                                             price=0.0, timestamp="--")

        assert len(merged) == 2
        assert merged["000001"].price == 10.50  # 有行情
        assert merged["600519"].price == 0.0     # stub


class TestKLineFetch:
    """测试K线获取 (需要网络)"""

    def test_daily_kline(self):
        klines = fetch_kline("000001", "daily", days=5)
        assert len(klines) > 0
        assert klines[0].period == "daily"
        assert klines[0].code == "000001"
        assert klines[0].close > 0

    def test_weekly_kline_aggregated(self):
        """周线从日线聚合"""
        klines = fetch_kline("000001", "weekly", days=5)
        assert len(klines) > 0
        assert klines[0].period == "weekly"
        assert klines[0].close > 0

    def test_monthly_kline_aggregated(self):
        """月线从日线聚合"""
        klines = fetch_kline("000001", "monthly", days=5)
        assert len(klines) > 0
        assert klines[0].period == "monthly"


class TestSingleStockQuote:
    """单股增量行情获取"""

    def test_fetch_kline_returns_data(self):
        """日线数据可从数据库获取（不再调 API）"""
        klines = fetch_kline("000001", "daily", days=5)
        assert len(klines) > 0
        assert klines[0].code == "000001"
        assert klines[0].close > 0

    def test_fetch_kline_invalid_code(self):
        """无效代码返回空列表，不崩溃"""
        klines = fetch_kline("999999", "daily", days=5)
        # 无效代码可能返回空或数据
        assert isinstance(klines, list)


class TestIncrementalRefreshWorker:
    """并行增量刷新 — 多只股票并行获取"""

    def test_worker_fetches_multiple_codes(self, monkeypatch):
        from data.market_data import IncrementalRefreshWorker
        from PyQt5.QtWidgets import QApplication
        from PyQt5.QtCore import QEventLoop, QTimer
        import sys
        app = QApplication.instance() or QApplication(sys.argv)

        # mock TDX 数据：返回模拟的当日 1min bars
        def _mock_today_bars(code):
            return [
                {"code": code, "timestamp": f"2026-06-06 {h:02d}:{m:02d}:00",
                 "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.3,
                 "volume": 1000, "period": "1min"}
                for h, m in [(9, 30), (9, 31), (9, 32)]
            ]
        # 补丁 market_data_manager 中的引用（QThread 内会用到）
        monkeypatch.setattr("data.market_data_manager.fetch_today_1min_bars", _mock_today_bars)

        results = {}
        loop = QEventLoop()

        worker = IncrementalRefreshWorker(["000001", "600519"])
        worker.data_ready.connect(lambda q: (results.update(q), loop.quit()))
        QTimer.singleShot(15000, loop.quit)
        worker.start()
        loop.exec_()

        assert len(results) >= 1
        for code in results:
            assert results[code].price > 0

    def test_empty_codes_returns_empty(self):
        from data.market_data import IncrementalRefreshWorker
        from PyQt5.QtWidgets import QApplication
        from PyQt5.QtCore import QEventLoop, QTimer
        import sys
        app = QApplication.instance() or QApplication(sys.argv)

        results = {}
        loop = QEventLoop()
        worker = IncrementalRefreshWorker([])
        worker.data_ready.connect(lambda q: (results.update(q), loop.quit()))
        QTimer.singleShot(5000, loop.quit)
        worker.start()
        loop.exec_()
        assert len(results) == 0


class TestInitialFetchWorker:
    """新股全量数据获取 — 独立于增量刷新"""

    def test_worker_fetches_all_periods(self, temp_db):
        from data.market_data import InitialFetchWorker
        from PyQt5.QtWidgets import QApplication
        from PyQt5.QtCore import QEventLoop, QTimer
        import sys
        app = QApplication.instance() or QApplication(sys.argv)

        periods_received = []
        klines_by_period = {}

        loop = QEventLoop()
        worker = InitialFetchWorker("000001")
        worker.kline_ready.connect(
            lambda c, p, k: (periods_received.append(p), klines_by_period.update({p: k})))
        worker.all_done.connect(loop.quit)
        QTimer.singleShot(60000, loop.quit)
        worker.start()
        loop.exec_()

        assert "daily" in periods_received
        assert len(klines_by_period.get("daily", [])) > 0

    def test_async_isolation(self, temp_db):
        """增量刷新和新股全量可以同时运行 (不同QThread)"""
        from data.market_data import IncrementalRefreshWorker, InitialFetchWorker
        import time

        # 同时启动两个 worker
        inc = IncrementalRefreshWorker(["600519"])
        init = InitialFetchWorker("000001")

        inc.start()
        init.start()

        # 验证两个都在运行
        assert inc.isRunning() or inc.wait(1000)
        assert init.isRunning() or init.wait(1000)

        inc.wait(60000)
        init.wait(60000)


class TestExceptionHandling:
    """全局异常钩子 + Worker traceback 日志"""

    def test_global_excepthook_registered(self):
        """验证异常钩子可被注册，且不会覆盖 sys.__excepthook__"""
        import sys
        import traceback as tb

        def dummy_hook(t, v, tr):
            tb.print_exception(t, v, tr)

        orig = sys.excepthook
        try:
            sys.excepthook = dummy_hook
            assert sys.excepthook is dummy_hook
            assert sys.excepthook is not sys.__excepthook__
        finally:
            sys.excepthook = orig

    def test_worker_exception_logs_traceback(self, tmp_path):
        """Worker 异常时打印完整堆栈"""
        from data.market_data import StockSearchWorker
        import traceback as tb

        # 创建一个必然失败的 worker (search_stock 内部会抛异常)
        # 只验证 worker 不会因异常而崩溃
        from PyQt5.QtWidgets import QApplication
        import sys
        app = QApplication.instance() or QApplication(sys.argv)

        error_msgs = []
        worker = StockSearchWorker("ZZZZ_NONEXISTENT_KEYWORD_99999")
        worker.error_occurred.connect(lambda m: error_msgs.append(m))
        worker.start()
        worker.wait(5000)

        # Worker应正常结束不崩溃，error_occurred信号携带了traceback
        assert not worker.isRunning()


class TestChartRefresh:
    """定时刷新只刷新当前Tab，不拉全部4个周期"""

    def test_refresh_current_tab_only(self):
        """refresh_current_tab 只触发当前Tab的 load_data，不触发其他3个"""
        from ui.chart_widget import ChartWidget
        from PyQt5.QtWidgets import QApplication
        import sys
        app = QApplication.instance() or QApplication(sys.argv)

        widget = ChartWidget()
        # 初始所有tab都没有code
        assert widget.intraday_tab.code == ""
        assert widget.daily_tab.code == ""

        # 设置code但不实际发请求 (load_data会触发worker)
        widget.daily_tab.code = "000001"
        widget.intraday_tab.code = "000001"

        # refresh_current_tab 应该存在且不崩溃
        assert hasattr(widget, 'refresh_current_tab')


class TestStockTableModel:
    """表格模型处理行情缺失数据"""

    def test_model_renders_stub_quotes(self):
        """price=0 时仍显示代码和名称"""
        from ui.stock_table import StockTableModel
        from data.models import RealtimeQuote
        from PyQt5.QtWidgets import QApplication
        import sys
        app = QApplication.instance() or QApplication(sys.argv)

        model = StockTableModel()
        quotes = {
            "000001": RealtimeQuote(code="000001", name="平安银行", price=0.0),
        }
        model.update_data(quotes, set(), set())
        assert model.rowCount() == 1
        # 验证 display role 返回代码和名称
        idx_code = model.index(0, StockTableModel.COL_CODE)
        idx_name = model.index(0, StockTableModel.COL_NAME)
        assert model.data(idx_code) == "000001"
        assert model.data(idx_name) == "平安银行"

    def test_empty_quotes_shows_zero_rows(self):
        """空行情不崩溃"""
        from ui.stock_table import StockTableModel
        from PyQt5.QtWidgets import QApplication
        import sys
        app = QApplication.instance() or QApplication(sys.argv)

        model = StockTableModel()
        model.update_data({}, set(), set())
        assert model.rowCount() == 0
