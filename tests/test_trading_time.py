"""交易时段判断测试

钉住的契约：`utils.is_trading_time()` 的交易时段必须来自 `config`，
而不是硬编码字面量。

原实现（修复前）:
    morning_start = time(9, 30)
    morning_end = time(11, 30)
    ...
改 `config.TRADING_START_*` / `TRADING_END_*` 完全不生效 —— 配置项形同虚设，
静默产生「以为改了、其实没改」的行为差异。

对应 `docs/KNOWN_ISSUES.md` 中「`utils.is_trading_time()` 存在硬编码」一项。
"""

import types
from datetime import datetime

import pytest

import config
import utils
from utils import is_trading_time, trading_sessions


def _dt(*args) -> datetime:
    """2026-09-17 是周四，2026-09-19 是周六"""
    return datetime(*args)


# ============================================================
# 配置驱动 —— 这是本次修复的核心
# ============================================================

class TestDrivenByConfig:
    """时段必须来自 config，改配置立即生效"""

    def test_afternoon_end_comes_from_config(self, monkeypatch):
        """把下午收盘从 15:00 改到 16:00，15:30 应当变成交易时段

        原实现硬编码 time(15, 0)，此断言必失败。
        """
        monkeypatch.setattr(config, "TRADING_END_AFTERNOON", "16:00")

        assert is_trading_time(_dt(2026, 9, 17, 15, 30)) is True

    def test_afternoon_start_comes_from_config(self, monkeypatch):
        """把下午开盘从 13:00 提前到 12:30，12:45 应当算交易时段"""
        monkeypatch.setattr(config, "TRADING_START_AFTERNOON", "12:30")

        assert is_trading_time(_dt(2026, 9, 17, 12, 45)) is True

    def test_morning_start_comes_from_config(self, monkeypatch):
        """把上午开盘从 09:30 推迟到 10:00，09:45 应变为非交易时段"""
        monkeypatch.setattr(config, "TRADING_START_MORNING", "10:00")

        assert is_trading_time(_dt(2026, 9, 17, 9, 45)) is False

    def test_morning_end_comes_from_config(self, monkeypatch):
        """把上午收盘从 11:30 提前到 11:00，11:15 应变为非交易时段"""
        monkeypatch.setattr(config, "TRADING_END_MORNING", "11:00")

        assert is_trading_time(_dt(2026, 9, 17, 11, 15)) is False

    def test_sessions_reflect_current_config(self, monkeypatch):
        """trading_sessions() 每次调用都重读 config，不在导入期缓存"""
        before = trading_sessions()
        monkeypatch.setattr(config, "TRADING_START_MORNING", "01:23")

        after = trading_sessions()

        assert before != after, "改 config 后 trading_sessions() 必须随之变化"
        assert after[0][0].hour == 1 and after[0][0].minute == 23

    def test_sessions_parsed_from_default_config(self):
        """默认配置解析结果与 config 常量一致"""
        sessions = trading_sessions()

        assert len(sessions) == 2
        assert sessions[0][0] == _time_of(config.TRADING_START_MORNING)
        assert sessions[0][1] == _time_of(config.TRADING_END_MORNING)
        assert sessions[1][0] == _time_of(config.TRADING_START_AFTERNOON)
        assert sessions[1][1] == _time_of(config.TRADING_END_AFTERNOON)


def _time_of(hhmm: str):
    hour, minute = hhmm.split(":")
    from datetime import time
    return time(int(hour), int(minute))


# ============================================================
# 基本时段判断
# ============================================================

class TestSessionBoundaries:
    """上午 / 下午两段的边界（含端点）"""

    @pytest.mark.parametrize("hour, minute, expected", [
        (9, 29, False),     # 开盘前
        (9, 30, True),      # 上午开盘（含）
        (10, 15, True),
        (11, 30, True),     # 上午收盘（含）
        (11, 31, False),    # 午休
        (12, 30, False),    # 午休
        (12, 59, False),    # 午休
        (13, 0, True),      # 下午开盘（含）
        (14, 30, True),
        (15, 0, True),      # 下午收盘（含）
        (15, 1, False),     # 收盘后
        (20, 0, False),     # 夜间
    ])
    def test_boundaries(self, hour, minute, expected):
        assert is_trading_time(_dt(2026, 9, 17, hour, minute)) is expected


class TestTradingWeekdays:
    """周末判定 —— 不含节假日日历，此处只覆盖周末"""

    @pytest.mark.parametrize("day", [14, 15, 16, 17, 18])   # 周一 ~ 周五
    def test_weekdays_during_session(self, day):
        assert is_trading_time(_dt(2026, 9, day, 10, 0)) is True

    @pytest.mark.parametrize("day", [19, 20])               # 周六 / 周日
    def test_weekend_is_never_trading(self, day):
        assert is_trading_time(_dt(2026, 9, day, 10, 0)) is False
        assert is_trading_time(_dt(2026, 9, day, 14, 0)) is False


class TestDefaultNow:
    """不传参时取当前时间（只验证取值路径，不断言具体结果）"""

    def test_uses_current_time_when_omitted(self, monkeypatch):
        frozen = _dt(2026, 9, 17, 10, 0)
        monkeypatch.setattr(
            utils, "datetime", types.SimpleNamespace(now=lambda: frozen))

        assert is_trading_time() is True
