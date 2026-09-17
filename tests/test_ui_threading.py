"""UI 线程契约回归测试 — 覆盖第一批两项「改了代码但断言未落地」的修复

对应 `docs/KNOWN_ISSUES.md` 附表「仍缺测试保护的两项」：

  1. UI 线程同步网络请求   `ui/main_window.py::_try_add_by_code`
     原实现在 UI 线程直接调用 `sync_stock_names_from_api()`
     （3 次 AKShare 请求 + 递增 sleep，最坏 6 秒冻结界面）。
     已改为启动 `StockNameSyncWorker`。本文件把「必须异步」钉成契约。

  2. 买点扫描线程泄漏      `ui/main_window.py::_scan_buy_points`
     原实现为每只股票各起一个 QThread，无并发上限、无 deleteLater 回收，
     且靠主线程计数归零复位（任一 worker 异常退出即永久停摆）。
     已改为单 worker 串行 + `batch_finished` 复位。
     本文件把「单轮单 worker」钉成契约。

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


class _FakeScanWorker:
    """BuyPointScanWorker 替身（构造签名与真实现一致）"""

    created: list = []

    def __init__(self, codes, parent=None):
        self.codes = list(codes)
        self.scan_done = _SignalStub("scan_done")
        self.batch_finished = _SignalStub("batch_finished")
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
def scan_workers(monkeypatch):
    """替换 BuyPointScanWorker，返回本轮创建实例列表"""
    _FakeScanWorker.created = []
    monkeypatch.setattr("ui.main_window.BuyPointScanWorker", _FakeScanWorker)
    return _FakeScanWorker.created


def _bare_window(**attrs):
    """绕过 __init__ 构造窗口，只保留被测方法需要的属性

    注意：QObject 子类未走 __init__ 时，读取**不存在的**属性会抛
    RuntimeError（"super-class __init__() was never called"），而不是
    AttributeError —— 因此代码里的 getattr(self, '_bp_worker', None)
    兜不住。必须先显式置空。
    """
    from ui.main_window import MainWindow

    win = MainWindow.__new__(MainWindow)
    win.status_bar = types.SimpleNamespace(showMessage=lambda *a, **k: None)
    win._bp_worker = None
    win._name_sync_worker = None
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
# 2. 买点扫描单轮单 worker
# ============================================================

CODES = ["000001", "000002", "600519", "600036", "601318"]


class TestBuyPointScanSingleWorker:
    """一轮买点扫描只能创建一个 worker —— 不允许每只股票各起一个 QThread"""

    @pytest.fixture(autouse=True)
    def _in_trading_time(self, monkeypatch):
        monkeypatch.setattr("ui.main_window.is_trading_time", lambda: True)

    @staticmethod
    def _window(codes=None):
        if codes is None:
            codes = CODES
        return _bare_window(_get_all_tracked_codes=lambda: list(codes))

    def test_one_worker_for_all_codes(self, qapp, scan_workers):
        """5 只股票 → 1 个 worker（原实现是 5 个）"""
        win = self._window()
        win._scan_buy_points()

        assert len(scan_workers) == 1, (
            f"应只创建一个 worker，实际 {len(scan_workers)} 个 —— "
            "原实现为每只股票各起一个 QThread")
        assert scan_workers[0].codes == CODES, "worker 必须持有全部待扫代码"
        assert scan_workers[0].started is True

    def test_skips_while_previous_round_running(self, qapp, scan_workers):
        """上一轮仍在运行 → 跳过，不新建"""
        win = self._window()
        win._scan_buy_points()      # 第 1 个，running=True
        win._scan_buy_points()
        win._scan_buy_points()

        assert len(scan_workers) == 1, "上一轮未结束时不应新建 worker"

    def test_rounds_do_not_accumulate_references(self, qapp, scan_workers):
        """连续 4 轮：每轮新建 1 个、旧引用被覆盖，且每个都接了 deleteLater"""
        win = self._window()
        for _ in range(4):
            win._scan_buy_points()
            win._bp_worker._running = False     # 模拟该轮已结束

        assert len(scan_workers) == 4, "每轮创建 1 个，不应随股票数放大"
        assert win._bp_worker is scan_workers[-1], "只应保留最新一轮的引用"
        for worker in scan_workers:
            assert worker.finished.slots == [worker.deleteLater], (
                "每个 worker 都必须接 deleteLater 回收，否则又是泄漏")

    def test_tolerates_destroyed_worker(self, qapp, scan_workers):
        """上一轮 worker 已随 deleteLater 销毁（isRunning 抛 RuntimeError）
        → 必须能开新一轮，而不是永久停摆"""
        win = self._window()
        win._bp_worker = _DestroyedWorker()

        win._scan_buy_points()

        assert len(scan_workers) == 1, (
            "底层对象已销毁时必须放行，否则买点扫描永久停摆")

    def test_batch_finished_signal_wired(self, qapp, scan_workers):
        """batch_finished 必须接上 —— 它是复位判据，不依赖计数归零"""
        win = self._window()
        win._scan_buy_points()

        assert scan_workers[0].batch_finished.slots == [
            win._on_buy_point_scan_finished], (
            "batch_finished 必须接上，否则一轮结束后没有收尾回调")

    def test_no_scan_without_codes(self, qapp, scan_workers):
        """无跟踪股票 → 不创建 worker"""
        win = self._window(codes=[])
        win._scan_buy_points()
        assert scan_workers == []

    def test_no_scan_outside_trading_time(self, qapp, scan_workers, monkeypatch):
        """非交易时段 → 不创建 worker"""
        monkeypatch.setattr("ui.main_window.is_trading_time", lambda: False)
        win = self._window()
        win._scan_buy_points()
        assert scan_workers == []
