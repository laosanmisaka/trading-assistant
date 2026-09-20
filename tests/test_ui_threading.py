"""UI 线程契约回归测试 — 覆盖两项「改了代码但断言未落地」的修复

对应 `docs/KNOWN_ISSUES.md` 附表「仍缺测试保护的两项」：

  1. UI 线程同步网络请求   `ui/main_window.py::_try_add_by_code`
     原实现在 UI 线程直接调用 `sync_stock_names_from_api()`
     （3 次 AKShare 请求 + 递增 sleep，最坏 6 秒冻结界面）。
     已改为启动 `StockNameSyncWorker`。本文件把「必须异步」钉成契约。

  2. 标注计算线程泄漏      `ui/main_window.py::_request_chan_marks`
     原型为买点扫描：每只股票各起一个 QThread，无并发上限、无 deleteLater
     回收，且靠主线程计数归零复位（任一 worker 异常退出即永久停摆）。
     2026-09-18 买点扫描链路（`core/buy_point_scanner.py`）已整体删除，
     同一位置换成缠论买卖点标注的 `ui.chan_worker.ChanMarkWorker` ——
     **线程契约一字未改**：单飞 + deleteLater。本文件继续钉住它。

这两处一旦被无意改回同步调用、或改回循环建线程，本文件的断言必须失败。
"""

import sys
import types

import pytest


@pytest.fixture(scope="module")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


# ============================================================
# 替身 —— 不真起线程，只记录创建 / 启动 / 回收
# ============================================================

class _SignalStub:
    """QThread 信号的替身，只记录连接关系"""

    def __init__(self, name=""):
        self.name = name
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)


class _FakeNameSyncWorker:
    """StockNameSyncWorker 替身（构造签名与真实现一致）"""

    created: list = []

    def __init__(self, code, parent=None):
        self.code = code
        self.sync_done = _SignalStub("sync_done")
        self.sync_failed = _SignalStub("sync_failed")
        self.finished = _SignalStub("finished")
        self.started = False
        self.deleted = False
        self._running = False
        type(self).created.append(self)

    def start(self):
        self.started = True
        self._running = True

    def isRunning(self):
        return self._running

    def deleteLater(self):
        self.deleted = True


class _FakeMarkWorker:
    """ChanMarkWorker 替身（构造签名与真实现一致）"""

    created: list = []

    def __init__(self, code, parent=None):
        self.code = code
        self.marks_ready = _SignalStub("marks_ready")
        self.failed = _SignalStub("failed")
        self.finished = _SignalStub("finished")
        self.started = False
        self.deleted = False
        self._running = False
        type(self).created.append(self)

    def start(self):
        self.started = True
        self._running = True

    def isRunning(self):
        return self._running

    def deleteLater(self):
        self.deleted = True


class _DestroyedWorker:
    """底层 C++ 对象已随 deleteLater 销毁 —— isRunning() 抛 RuntimeError"""

    def isRunning(self):
        raise RuntimeError("wrapped C/C++ object of type QThread has been deleted")


class _FakeChart:
    """ChartWidget 替身 —— 只记录 set_chan_marks 的调用"""

    def __init__(self):
        self.calls: list = []

    def set_chan_marks(self, code, marks):
        self.calls.append((code, marks))


# ============================================================
# fixtures
# ============================================================

@pytest.fixture
def name_sync_workers(monkeypatch):
    """替换 StockNameSyncWorker，返回本轮创建实例列表"""
    _FakeNameSyncWorker.created = []
    monkeypatch.setattr("ui.main_window.StockNameSyncWorker", _FakeNameSyncWorker)
    return _FakeNameSyncWorker.created


@pytest.fixture
def mark_workers(monkeypatch):
    """替换 ChanMarkWorker，返回本轮创建实例列表"""
    _FakeMarkWorker.created = []
    monkeypatch.setattr("ui.main_window.ChanMarkWorker", _FakeMarkWorker)
    return _FakeMarkWorker.created


def _bare_window(**attrs):
    """绕过 __init__ 构造窗口，只保留被测方法需要的属性

    注意：QObject 子类未走 __init__ 时，读取**不存在的**属性会抛
    RuntimeError（"super-class __init__() was never called"），而不是
    AttributeError —— 因此代码里的 getattr(self, '_chan_mark_worker', None)
    兜不住。必须先显式置空。
    """
    from ui.main_window import MainWindow

    win = MainWindow.__new__(MainWindow)
    win.status_bar = types.SimpleNamespace(showMessage=lambda *a, **k: None)
    win._chan_mark_worker = None
    win._name_sync_worker = None
    win._chan_marks = {}
    win._pending_chan_code = ""
    win._current_stock_code = ""
    win.chart_widget = _FakeChart()
    for key, value in attrs.items():
        setattr(win, key, value)
    return win


def _silence_sync_api(monkeypatch):
    """装哨兵：记录 sync_stock_names_from_api 是否被调用"""
    import data.market_data as md

    calls = []
    monkeypatch.setattr(
        md, "sync_stock_names_from_api",
        lambda *a, **k: (calls.append(True), 0)[1])
    return calls


# ============================================================
# 1. _try_add_by_code 必须异步
# ============================================================

class TestAddByCodeIsAsync:
    """纯代码添加股票时，名称库同步不得跑在 UI 线程上"""

    def test_db_miss_starts_worker_not_sync_call(
            self, qapp, monkeypatch, name_sync_workers):
        """DB 未命中 → 启动 worker，且**不**同步调用名称库同步

        这是本次修复的核心契约。原实现直接调 sync_stock_names_from_api()，
        在 UI 线程里跑 3 次 AKShare 请求 + 递增 sleep（最坏 6 秒）。
        """
        sync_calls = _silence_sync_api(monkeypatch)
        monkeypatch.setattr("data.database.get_stock_name", lambda code: "")

        win = _bare_window()
        win._try_add_by_code("600519")

        assert sync_calls == [], (
            "不得在 UI 线程里同步调用 sync_stock_names_from_api —— "
            "必须改为启动 StockNameSyncWorker")
        assert len(name_sync_workers) == 1, "应恰好启动一个名称同步 worker"
        assert name_sync_workers[0].code == "600519"
        assert name_sync_workers[0].started is True, "worker 必须被 start()"

    def test_db_hit_skips_sync_entirely(
            self, qapp, monkeypatch, name_sync_workers):
        """DB 已命中 → 弹窗确认即可，不应触发名称库同步"""
        sync_calls = _silence_sync_api(monkeypatch)
        monkeypatch.setattr("data.database.get_stock_name", lambda code: "贵州茅台")

        from PyQt5.QtWidgets import QMessageBox
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)

        added = []
        win = _bare_window()
        win._do_add_stock = lambda code, name: added.append((code, name))

        win._try_add_by_code("600519")

        assert name_sync_workers == [], "DB 命中时不应启动名称同步 worker"
        assert sync_calls == [], "DB 命中时不应触发网络同步"
        assert added == [("600519", "贵州茅台")]

    def test_skips_when_previous_sync_still_running(
            self, qapp, monkeypatch, name_sync_workers):
        """上一轮同步未结束 → 不新建 worker（防重入）"""
        monkeypatch.setattr("data.database.get_stock_name", lambda code: "")

        win = _bare_window()
        busy = _FakeNameSyncWorker("000001")
        busy.start()
        name_sync_workers.clear()

        win._name_sync_worker = busy
        win._try_add_by_code("600519")

        assert name_sync_workers == [], "上一轮仍在运行时不应用新建 worker"
        assert win._name_sync_worker is busy

    def test_starts_new_worker_after_previous_finished(
            self, qapp, monkeypatch, name_sync_workers):
        """上一轮已结束 → 允许开新一轮（不能被永久卡死）"""
        monkeypatch.setattr("data.database.get_stock_name", lambda code: "")

        win = _bare_window()
        done = _FakeNameSyncWorker("000001")   # 未 start，isRunning() 为 False
        name_sync_workers.clear()
        win._name_sync_worker = done

        win._try_add_by_code("600519")

        assert len(name_sync_workers) == 1
        assert name_sync_workers[0].code == "600519"

    def test_worker_signals_all_wired(
            self, qapp, monkeypatch, name_sync_workers):
        """三个信号都必须接上 —— 尤其 sync_failed，原实现失败时是静默的"""
        monkeypatch.setattr("data.database.get_stock_name", lambda code: "")

        win = _bare_window()
        win._try_add_by_code("600519")
        worker = name_sync_workers[0]

        assert worker.sync_done.slots == [win._on_name_sync_done]
        assert worker.sync_failed.slots == [win._on_name_sync_failed], (
            "sync_failed 必须接上，否则同步失败又变成静默")
        assert worker.finished.slots == [worker.deleteLater], (
            "finished 必须接 deleteLater，否则 worker 不回收")


# ============================================================
# 2. 缠论买卖点标注：单飞 + 必回收
# ============================================================

CODE = "600519"


class TestChanMarkSingleFlight:
    """一次标注计算只能创建一个 worker —— 不允许每只股票各起一个 QThread"""

    @staticmethod
    def _window(**attrs):
        return _bare_window(**attrs)

    def test_one_worker_per_request(self, qapp, mark_workers):
        """一次请求 → 1 个 worker（原实现是每只股票 1 个）"""
        win = self._window()
        win._request_chan_marks(CODE)

        assert len(mark_workers) == 1, (
            f"应只创建一个 worker，实际 {len(mark_workers)} 个")
        assert mark_workers[0].code == CODE
        assert mark_workers[0].started is True

    def test_skips_while_previous_round_running(self, qapp, mark_workers):
        """上一轮仍在运行 → 跳过，不新建（连点不能叠线程）"""
        win = self._window()
        win._request_chan_marks(CODE)      # 第 1 个，running=True
        win._request_chan_marks(CODE)
        win._request_chan_marks(CODE)

        assert len(mark_workers) == 1, "上一轮未结束时不应新建 worker"

    def test_skips_when_cached(self, qapp, mark_workers):
        """已有缓存 → 直接上屏，不再起线程"""
        win = self._window(_chan_marks={CODE: {"geometry": [], "trades": []}},
                           _current_stock_code=CODE)
        win._request_chan_marks(CODE)

        assert mark_workers == [], "命中缓存时不应新建 worker"
        assert win.chart_widget.calls == [
            (CODE, {"geometry": [], "trades": []})]

    def test_force_refresh_bypasses_cache(self, qapp, mark_workers):
        """force=True → 忽略缓存重算（右键「刷新缠论买卖点」用）"""
        win = self._window(_chan_marks={CODE: {"geometry": [], "trades": []}})
        win._request_chan_marks(CODE, force=True)

        assert len(mark_workers) == 1
        assert win._chan_marks == {}, "force 时应先清掉旧缓存"

    def test_rounds_do_not_accumulate_references(self, qapp, mark_workers):
        """连续 4 轮：每轮新建 1 个、旧引用被覆盖，且每个都接了 deleteLater"""
        win = self._window()
        for _ in range(4):
            win._request_chan_marks(CODE)
            win._chan_mark_worker._running = False     # 模拟该轮已结束

        assert len(mark_workers) == 4, "每轮创建 1 个，不应随股票数放大"
        assert win._chan_mark_worker is mark_workers[-1], "只应保留最新一轮的引用"
        for worker in mark_workers:
            assert worker.finished.slots == [
                win._on_chan_worker_finished, worker.deleteLater], (
                "每个 worker 都必须接 deleteLater 回收（否则又是泄漏），"
                "并先接补算回调（否则排队请求永不执行）")

    def test_tolerates_destroyed_worker(self, qapp, mark_workers):
        """上一轮 worker 已随 deleteLater 销毁（isRunning 抛 RuntimeError）
        → 必须能开新一轮，而不是永久停摆"""
        win = self._window()
        win._chan_mark_worker = _DestroyedWorker()

        win._request_chan_marks(CODE)

        assert len(mark_workers) == 1, (
            "底层对象已销毁时必须放行，否则标注永久停摆")

    def test_signals_all_wired(self, qapp, mark_workers):
        """三个信号都必须接上 —— failed 不接就会静默失败"""
        win = self._window()
        win._request_chan_marks(CODE)
        worker = mark_workers[0]

        assert worker.marks_ready.slots == [win._on_chan_marks_ready]
        assert worker.failed.slots == [win._on_chan_marks_failed], (
            "failed 必须接上，否则取数失败时界面毫无反应")
        assert worker.finished.slots == [
            win._on_chan_worker_finished, worker.deleteLater]
        assert win._pending_chan_code == "", "正常启动一轮不应留下排队记录"


class TestChanMarkCallbacks:
    """回调只对**当前股票**上屏，并且失败不弹窗"""

    def test_ready_marks_are_cached_and_shown(self, qapp):
        win = _bare_window(_current_stock_code=CODE)
        marks = {"geometry": [{"date": "2026-01-05", "kind": "二买", "price": 10}],
                 "trades": [{"buy_date": "2026-01-05"}]}

        win._on_chan_marks_ready(CODE, marks)

        assert win._chan_marks[CODE] is marks
        assert win.chart_widget.calls == [(CODE, marks)]

    def test_ready_marks_not_shown_after_switching_stock(self, qapp):
        """算完时用户已切走 → 只缓存，不画到别人的图上"""
        win = _bare_window(_current_stock_code="000001")

        win._on_chan_marks_ready(CODE, {"geometry": [], "trades": []})

        assert CODE in win._chan_marks, "仍应缓存，回头切回来能直接用"
        assert win.chart_widget.calls == [], "不是当前股票就不该上屏"

    def test_failed_clears_marks_and_does_not_raise(self, qapp):
        """取数失败：清掉旧标注 + 状态栏提示，不抛异常"""
        win = _bare_window(_current_stock_code=CODE)

        win._on_chan_marks_failed(CODE, "网络错误")

        assert win.chart_widget.calls == [(CODE, None)], "失败时应清掉标注"

    def test_refresh_without_current_stock_is_noop(self, qapp, mark_workers):
        """没有当前股票时「刷新」不该起线程"""
        win = _bare_window(_current_stock_code="")
        win._refresh_chan_marks()
        assert mark_workers == []

    def test_refresh_uses_current_stock(self, qapp, mark_workers):
        win = _bare_window(_current_stock_code=CODE)
        win._refresh_chan_marks()
        assert len(mark_workers) == 1
        assert mark_workers[0].code == CODE


class TestChanMarkPendingQueue:
    """单飞期间被丢弃的请求必须排队补算（2026-09-20 加）

    原实现直接 `return` 丢弃：「A 还在算时双击 B」会让 B **永远**没有标注 ——
    B 的请求被丢，A 算完时 `code` 已不是当前股票（不上屏），而换股票时旧标注
    已被 `load_data` 清空，图上就一直是空的，用户只能再双击一次。
    """

    def test_dropped_request_is_requeued_and_retried(self, qapp, mark_workers):
        win = _bare_window(_current_stock_code=CODE)
        win._request_chan_marks(CODE)              # A 开始算
        win._request_chan_marks("000001")          # B 被单飞丢弃 → 排队

        assert len(mark_workers) == 1, "单飞：不得为 B 另起线程"
        assert win._pending_chan_code == "000001"

        win._current_stock_code = "000001"         # 用户确实停在 B 上
        mark_workers[0]._running = False           # A 这一轮结束
        win._on_chan_worker_finished()

        assert len(mark_workers) == 2, "排队中的请求必须被补算"
        assert mark_workers[1].code == "000001"
        assert win._pending_chan_code == "", "补算后必须清空排队，否则会无限重试"

    def test_pending_dropped_when_user_moved_on(self, qapp, mark_workers):
        """排队期间用户又切走 → 那只不必补算（省一次 2~3 秒的计算）"""
        win = _bare_window(_current_stock_code=CODE)
        win._request_chan_marks(CODE)
        win._request_chan_marks("000001")

        win._current_stock_code = "600000"         # 又切走了
        mark_workers[0]._running = False
        win._on_chan_worker_finished()

        assert len(mark_workers) == 1, "已不是当前股票的排队请求不该补算"
        assert win._pending_chan_code == ""

    def test_no_pending_is_noop(self, qapp, mark_workers):
        """没有排队记录时，worker 结束不该凭空起新线程"""
        win = _bare_window(_current_stock_code=CODE)
        win._request_chan_marks(CODE)
        mark_workers[0]._running = False

        win._on_chan_worker_finished()

        assert len(mark_workers) == 1
