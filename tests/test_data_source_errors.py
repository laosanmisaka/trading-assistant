"""数据源异常语义测试 —— 「源故障」与「无数据」必须可区分

修复前的契约：`data/market_data.py` 里所有取数函数都是
`except Exception: logger.error(...); return []`。于是

    数据源挂了（网络断了 / 接口报错 / 依赖没装）  →  []
    该股确实没数据（停牌 / 代码不存在）          →  []

调用方拿到的都是空列表，图表都渲染成空白，使用者**看不出是「没数据」还是「坏了」**。

修复后：`DataSourceError` 只表示「数据源不可用」；返回空列表只表示
「源正常应答，但此刻确实没有数据」。

本文件用注入假 akshare 模块的方式离线覆盖这两个分支（不依赖网络）。
"""

import sys
import types

import pandas as pd
import pytest

from data.market_data import (
    DataSourceError,
    _fetch_1min_kline_em_fallback,
    fetch_kline,
)


# ============================================================
# 假的 akshare —— 注入 sys.modules，让函数内的 import akshare 生效
# ============================================================

@pytest.fixture
def fake_akshare(monkeypatch):
    """安装假 akshare 模块；传入要生效的接口函数"""

    def _install(**funcs):
        module = types.ModuleType("akshare")
        for name, fn in funcs.items():
            setattr(module, name, fn)
        monkeypatch.setitem(sys.modules, "akshare", module)
        return module

    return _install


@pytest.fixture
def no_sleep(monkeypatch):
    """跳过重试等待，避免测试被 sleep 拖慢"""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    return None


def _ok_daily_df() -> pd.DataFrame:
    return pd.DataFrame({
        "date": ["2026-01-05", "2026-01-06"],
        "open": [10.0, 10.2],
        "high": [10.5, 10.4],
        "low": [9.9, 10.0],
        "close": [10.3, 10.1],
        "volume": [1000, 1200],
    })


def _boom(**kwargs):
    raise ConnectionError("网络断了")


# ============================================================
# 1. 核心契约：两种「空」必须可区分
# ============================================================

class TestEmptyVersusFailure:
    """同一个入口，两种原因必须给出两种结果"""

    def test_source_ok_but_no_data_returns_empty_list(self, fake_akshare):
        """源正常应答但无数据 → 空列表，不抛异常"""
        fake_akshare(stock_zh_a_daily=lambda **kw: pd.DataFrame())

        assert fetch_kline("T100001", "daily", days=5) == []

    def test_source_failure_raises(self, fake_akshare):
        """源故障 → 抛 DataSourceError，绝不返回空列表"""
        fake_akshare(stock_zh_a_daily=_boom)

        with pytest.raises(DataSourceError):
            fetch_kline("T100002", "daily", days=5)

    def test_the_two_outcomes_differ(self, fake_akshare, monkeypatch):
        """同一函数、同一参数形状，两种原因必须给出可区分的两种结果

        这是本次修复的契约本身 —— 修复前两者都是 []。
        """
        fake_akshare(stock_zh_a_daily=lambda **kw: pd.DataFrame())
        empty = fetch_kline("T100003", "daily", days=5)

        fake_akshare(stock_zh_a_daily=_boom)
        with pytest.raises(DataSourceError) as excinfo:
            fetch_kline("T100004", "daily", days=5)

        assert empty == []
        assert isinstance(excinfo.value, DataSourceError)


# ============================================================
# 2. 异常信息必须带够排查线索
# ============================================================

class TestErrorCarriesContext:

    def test_message_contains_code_period_and_cause(self, fake_akshare):
        fake_akshare(stock_zh_a_daily=_boom)

        with pytest.raises(DataSourceError) as excinfo:
            fetch_kline("T200001", "daily", days=5)

        message = str(excinfo.value)
        assert "T200001" in message, "错误信息必须含股票代码"
        assert "daily" in message, "错误信息必须含周期"
        assert "网络断了" in message, "错误信息必须含原始原因"

    def test_original_exception_chained(self, fake_akshare):
        """必须保留原始异常链，否则排查时只能看到包装后的信息"""
        fake_akshare(stock_zh_a_daily=_boom)

        with pytest.raises(DataSourceError) as excinfo:
            fetch_kline("T200002", "daily", days=5)

        assert isinstance(excinfo.value.__cause__, ConnectionError)

    def test_missing_dependency_is_a_source_failure(self, monkeypatch):
        """依赖缺失（akshare 未安装）属于「数据源不可用」，不是「无数据」

        修复前会被 `except Exception` 吞成空列表，干净环境里表现为图表空白
        且没有任何提示。
        """
        monkeypatch.setitem(sys.modules, "akshare", None)   # import 时抛 ImportError

        with pytest.raises(DataSourceError) as excinfo:
            fetch_kline("T200003", "daily", days=5)

        assert "akshare" in str(excinfo.value)


# ============================================================
# 3. 非故障路径不得被误伤
# ============================================================

class TestNonFailurePathsUnchanged:

    def test_normal_data_still_parsed(self, fake_akshare):
        """正常取数路径不受影响"""
        fake_akshare(stock_zh_a_daily=lambda **kw: _ok_daily_df())

        klines = fetch_kline("T300001", "daily", days=5)

        assert len(klines) == 2
        assert klines[0].date == "2026-01-05"
        assert klines[0].close == 10.3
        assert klines[0].period == "daily"

    def test_weekly_aggregation_still_works(self, fake_akshare):
        fake_akshare(stock_zh_a_daily=lambda **kw: _ok_daily_df())

        klines = fetch_kline("T300002", "weekly", days=5)

        assert len(klines) >= 1
        assert klines[0].period == "weekly"

    def test_unsupported_period_returns_empty_not_error(self, fake_akshare):
        """不支持的周期是调用方参数问题，不是数据源故障 —— 保持返回空列表"""
        fake_akshare(stock_zh_a_daily=lambda **kw: _ok_daily_df())

        assert fetch_kline("T300003", "hourly", days=5) == []


# ============================================================
# 4. 分钟线回退链：重试耗尽才算故障
# ============================================================

class TestMinuteFallbackRetries:

    def test_1min_retries_then_raises(self, fake_akshare, no_sleep):
        """东方财富 1min 回退：重试 2 次仍失败 → DataSourceError"""
        attempts = []

        def _failing(**kwargs):
            attempts.append(1)
            raise ConnectionError("EM 挂了")

        fake_akshare(stock_zh_a_hist_min_em=_failing)

        with pytest.raises(DataSourceError) as excinfo:
            _fetch_1min_kline_em_fallback("T400001")

        assert len(attempts) == 2, "应重试 2 次后才判定为数据源故障"
        assert "EM 挂了" in str(excinfo.value)

    def test_1min_empty_response_is_not_a_failure(self, fake_akshare):
        """源应答了、只是没数据 —— 一次都不该重试"""
        attempts = []

        def _empty(**kwargs):
            attempts.append(1)
            return pd.DataFrame()

        fake_akshare(stock_zh_a_hist_min_em=_empty)

        assert _fetch_1min_kline_em_fallback("T400002") == []
        assert len(attempts) == 1, "空应答属正常结果，不应触发重试"
