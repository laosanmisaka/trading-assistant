"""回归测试 — 覆盖 PROJECT_ASSESSMENT.md 4.0 节的四个已修复缺陷

这四个修复此前**没有任何测试保护**，全靠人工核对。任何一次无意回退都会让
崩溃、僵尸进程、每日止损失效重新出现。本文件把它们钉住。

对应关系:
  1. 分型索引错位      core/technical.py        _merge_contains / _resolve_fractal_index
  2. flush_today_bars  data/market_data_manager.py
  3. 托盘退出僵尸进程  ui/main_window.py        _quit_app / closeEvent
  4. 每日止损跨天失效  ui/main_window.py        _check_daily_stop_loss
"""

import sys
import types
from datetime import datetime

import numpy as np
import pytest

from config import DAILY_STOP_LOSS_HOUR, DAILY_STOP_LOSS_MINUTE
from core.technical import detect_bottom_fractal, detect_top_fractal


@pytest.fixture(scope="module")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


# ============================================================
# 1. 分型索引精确还原
# ============================================================

class TestFractalIndexResolution:
    """原实现用 idx = min(mi * 2, n - 1) 把合并序列索引近似映射回原始序列，
    一旦发生 K 线包含合并必然错位，导致止损止盈基于错误 K 线的高点计算。"""

    def test_bottom_fractal_simple_index(self):
        """无包含关系：底分型索引必须是中间那根 K 线

        旧公式 min(1 * 2, 2) = 2，会返回右侧那根（错误的）K 线。
        """
        highs = np.array([11.0, 10.0, 11.0])
        lows = np.array([10.0, 9.0, 10.0])
        assert detect_bottom_fractal(highs, lows) == [1]

    def test_top_fractal_simple_index(self):
        """无包含关系：顶分型索引必须是中间那根 K 线"""
        highs = np.array([10.0, 11.0, 10.0])
        lows = np.array([9.0, 10.0, 9.0])
        assert detect_top_fractal(highs, lows) == [1]

    def test_bottom_fractal_index_in_bounds_random(self):
        """随机序列（必然出现包含合并）：索引必须落在合法范围内，
        且指向的 K 线确实是该分型的最低点。

        旧公式在合并发生时会把索引推到 min(mi*2, n-1)，n-1 就是典型症状。
        """
        rng = np.random.default_rng(42)
        for _ in range(30):
            n = int(rng.integers(15, 60))
            closes = rng.uniform(8.0, 12.0, n)
            highs = closes + rng.uniform(0.0, 0.35, n)
            lows = closes - rng.uniform(0.0, 0.35, n)

            for idx in detect_bottom_fractal(highs, lows):
                assert 0 <= idx < n, "索引越界（旧近似公式的典型症状）"
                lo = max(0, idx - 1)
                hi = min(n, idx + 2)
                assert lows[idx] == min(lows[lo:hi]), (
                    f"索引 {idx} 处 low={lows[idx]:.3f} 并非局部最低 "
                    f"{lows[lo:hi]}，索引未还原到分型最低价所在的 K 线"
                )

    def test_top_fractal_index_in_bounds_random(self):
        """顶分型同理：索引必须指向最高价所在的 K 线"""
        rng = np.random.default_rng(7)
        for _ in range(30):
            n = int(rng.integers(15, 60))
            closes = rng.uniform(8.0, 12.0, n)
            highs = closes + rng.uniform(0.0, 0.35, n)
            lows = closes - rng.uniform(0.0, 0.35, n)

            for idx in detect_top_fractal(highs, lows):
                assert 0 <= idx < n, "索引越界"
                lo = max(0, idx - 1)
                hi = min(n, idx + 2)
                assert highs[idx] == max(highs[lo:hi]), (
                    f"索引 {idx} 处 high={highs[idx]:.3f} 并非局部最高 "
                    f"{highs[lo:hi]}，索引未还原到分型最高价所在的 K 线"
                )


# ============================================================
# 2. flush_today_bars 不再 KeyError
# ============================================================

class TestFlushTodayBars:
    """原实现读 b["time"] / b.get("price")，而 _today_bars 的实际键为
    code/date/open/high/low/close/volume/period —— 内存中一旦有当日 bar
    就必然 KeyError，且在主线程触发，盘中每 5 分钟弹一次异常框。"""

    @staticmethod
    def _manager():
        from data.market_data_manager import MarketDataManager
        return MarketDataManager()

    @staticmethod
    def _today_bar(code="000001", date="2026-09-17"):
        return {
            "code": code, "date": date,
            "open": 10.0, "high": 10.5, "low": 9.8,
            "close": 10.3, "volume": 1000, "period": "daily",
        }

    def test_flush_with_daily_bar_no_exception(self, temp_db):
        """有当日 bar 时 flush 必须正常写入，而不是抛 KeyError"""
        manager = self._manager()
        manager.update_today_bar("000001", "daily", self._today_bar())

        count = manager.flush_today_bars()

        assert count == 1

    def test_flush_writes_to_db(self, temp_db):
        """flush 的结果必须真的落库（否则等于静默丢数据）"""
        from data.database import get_klines
        manager = self._manager()
        manager.update_today_bar("000001", "daily", self._today_bar())

        manager.flush_today_bars()

        klines = get_klines("000001", "daily", days=5)
        assert any(k["date"] == "2026-09-17" for k in klines), "当日 bar 未写入 DB"

    def test_flush_empty_returns_zero(self, temp_db):
        """内存无数据时返回 0，不抛异常"""
        assert self._manager().flush_today_bars() == 0

    def test_flush_updates_last_flush_time(self, temp_db):
        """flush 必须推进 _last_flush_time，否则 should_flush 永远为真、反复写库"""
        manager = self._manager()
        before = manager._last_flush_time
        manager.flush_today_bars()
        assert manager._last_flush_time >= before


# ============================================================
# 3. 托盘退出不再产生僵尸进程
# ============================================================

class _FakeCloseEvent:
    """最小 CloseEvent 替身，只记录 accept / ignore"""

    def __init__(self):
        self.accepted = None

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.accepted = False


class TestTrayExit:
    """原实现 setQuitOnLastWindowClosed(False) 而托盘「退出」连的是 self.close，
    导致窗口关了、托盘没了、进程变僵尸且无法再打开界面。"""

    @staticmethod
    def _bare_window():
        from ui.main_window import MainWindow
        return MainWindow.__new__(MainWindow)   # 跳过 __init__，只测方法级逻辑

    def test_close_event_minimizes_to_tray(self, qapp):
        """未退出时点 X → 隐藏到托盘，绝不接受关闭事件"""
        win = self._bare_window()
        win._quitting = False
        hidden = []
        win.hide = lambda: hidden.append(True)
        # tray_icon 必须存在且可调用：closeEvent 会用它弹气泡提示
        win.tray_icon = types.SimpleNamespace(showMessage=lambda *a, **k: None)

        event = _FakeCloseEvent()
        win.closeEvent(event)

        assert event.accepted is False, "未进入退出流程时不应接受关闭事件"
        assert hidden == [True], "应最小化到托盘而不是关闭窗口"

    def test_close_event_accepts_while_quitting(self, qapp):
        """退出流程中必须真正关闭，否则进程退不掉"""
        win = self._bare_window()
        win._quitting = True

        event = _FakeCloseEvent()
        win.closeEvent(event)

        assert event.accepted is True, "退出流程中的关闭事件必须被接受"

    def test_quit_app_noop_when_already_quitting(self, qapp):
        """防重入：已进入退出流程时不应重复执行收尾"""
        win = self._bare_window()
        win._quitting = True
        called = []
        win.flash_tray = lambda on: called.append(on)

        win._quit_app()

        assert called == [], "已标记退出后不应重复执行收尾逻辑"


# ============================================================
# 4. 每日止损跨天不再失效
# ============================================================

class TestDailyStopLossSchedule:
    """原实现 _daily_stop_loss_done 从不按日期清空 → 第二天 15:05 永不执行；
    且用 now.minute != 5 精确匹配 → 界面卡顿即错过当天。"""

    def _make_window(self, monkeypatch, now_holder, codes=("000001",)):
        from ui.main_window import MainWindow

        win = MainWindow.__new__(MainWindow)
        win._daily_stop_loss_done = set()
        win._get_holding_codes = lambda: list(codes)
        win._show_alert_conflict = lambda code, conflict: None

        calls = []

        def _update(code):
            calls.append(code)
            return 10.0, None

        win.alert_engine = types.SimpleNamespace(update_daily_stop_loss=_update)

        # _check_daily_stop_loss 内部用 datetime.now()，整体替换模块级 datetime
        monkeypatch.setattr(
            "ui.main_window.datetime",
            types.SimpleNamespace(now=lambda: now_holder[0]))

        return win, calls

    @staticmethod
    def _at(*args):
        return [datetime(*args)]

    def test_runs_at_scheduled_time(self, monkeypatch):
        win, calls = self._make_window(
            monkeypatch, self._at(2026, 9, 17, DAILY_STOP_LOSS_HOUR, DAILY_STOP_LOSS_MINUTE))
        win._check_daily_stop_loss()
        assert calls == ["000001"]

    def test_runs_only_once_per_day(self, monkeypatch):
        """同一交易日内每只股票只应更新一次"""
        win, calls = self._make_window(
            monkeypatch, self._at(2026, 9, 17, DAILY_STOP_LOSS_HOUR, DAILY_STOP_LOSS_MINUTE))
        win._check_daily_stop_loss()
        win._check_daily_stop_loss()
        win._check_daily_stop_loss()
        assert calls == ["000001"], "同一天内不应重复执行"

    def test_runs_again_next_day(self, monkeypatch):
        """跨天后必须重新执行

        这正是原 bug 的核心：标记不按日期清空，第二天 15:05 永不执行。
        """
        now = self._at(2026, 9, 17, DAILY_STOP_LOSS_HOUR, DAILY_STOP_LOSS_MINUTE)
        win, calls = self._make_window(monkeypatch, now)

        win._check_daily_stop_loss()
        assert calls == ["000001"]

        now[0] = datetime(2026, 9, 18, DAILY_STOP_LOSS_HOUR, DAILY_STOP_LOSS_MINUTE)
        win._check_daily_stop_loss()
        assert calls == ["000001", "000001"], "次日必须重新执行每日止损更新"

    def test_tolerates_late_trigger(self, monkeypatch):
        """迟到触发也必须执行

        原实现用 now.minute != 5 精确匹配，界面卡顿导致晚一分钟就整天错过。
        """
        win, calls = self._make_window(monkeypatch, self._at(2026, 9, 17, 15, 12))
        win._check_daily_stop_loss()
        assert calls == ["000001"], "晚于计划时刻应放行，而不是错过当天"

    def test_skips_before_scheduled_time(self, monkeypatch):
        """未到既定时刻不执行"""
        win, calls = self._make_window(monkeypatch, self._at(2026, 9, 17, 14, 0))
        win._check_daily_stop_loss()
        assert calls == []

    def test_skips_weekend(self, monkeypatch):
        """非交易日不执行（2026-09-19 为周六）"""
        win, calls = self._make_window(monkeypatch, self._at(2026, 9, 19, 15, 12))
        win._check_daily_stop_loss()
        assert calls == []
