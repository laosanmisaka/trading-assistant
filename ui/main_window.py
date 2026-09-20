"""主窗口 — 布局、菜单、系统托盘、实时数据轮询、止损止盈检查、缠论买卖点标注"""

import os
from datetime import datetime

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QSplitter, QVBoxLayout, QHBoxLayout,
    QListWidget, QListWidgetItem, QTableView, QTabWidget,
    QStatusBar, QLabel, QMenuBar, QAction, QMessageBox,
    QSystemTrayIcon, QMenu, QApplication, QHeaderView,
    QAbstractItemView,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QIcon, QColor, QFont

from config import (
    WINDOW_TITLE, WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT,
    SIDEBAR_WIDTH, REALTIME_REFRESH_MS,
    KLINE_REFRESH_MS, KLINE_FLUSH_INTERVAL_SEC,
    DAILY_STOP_LOSS_HOUR, DAILY_STOP_LOSS_MINUTE,
    STOCK_TABLE_COLUMNS, PRESET_GROUPS, CHART_COLORS,
)
from data.database import (
    init_db, get_all_groups, get_stocks_by_group, get_all_stocks,
    add_stock, remove_stock, move_stock, get_all_trades, get_position_summary,
    is_alert_disabled, disable_alert, enable_alert,
)
from data.market_data import (
    StockSearchWorker, StockNameSyncWorker,
    IncrementalRefreshWorker, InitialFetchWorker,
)
from data.market_data_manager import get_data_manager
from data.models import RealtimeQuote, Group, Stock
from core.alert_engine import AlertEngine
from ui.chan_worker import ChanMarkWorker
import traceback

from utils import is_trading_time
from utils.logger import get_logger

logger = get_logger(__name__)


class MainWindow(QMainWindow):
    """交易系统主窗口"""

    # 信号
    quote_updated = pyqtSignal(dict)  # 实时行情更新 {code: RealtimeQuote}

    def __init__(self):
        super().__init__()
        init_db()
        logger.info("A股交易辅助系统启动中...")

        self.setWindowTitle(WINDOW_TITLE)
        self.setMinimumSize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)

        # ---- 引擎 ----
        self.alert_engine = AlertEngine()

        # ---- 数据管理器 (内存+DB双层) ----
        self.data_manager = get_data_manager()

        # ---- 内部状态 ----
        self._current_group_id: int = -1
        self._current_stock_code: str = ""
        self._chan_marks: dict[str, dict] = {}   # code → 缠论买卖点标注（内存缓存）
        # 单飞期间被丢弃的请求（2026-09-20 加）—— 见 `_request_chan_marks`
        self._pending_chan_code: str = ""
        self._chan_mark_worker = None            # 单飞的标注计算 worker
        self._tray_flash_timer: QTimer = None
        self._tray_flash_on: bool = False
        self._alert_triggered_codes: set[str] = set()
        self._daily_stop_loss_done: set[tuple[str, str]] = set()  # 已执行每日止损更新的 (代码, 日期)
        self._quitting: bool = False  # 真正退出应用标志 (区分窗口关闭与退出)

        # 构建UI
        self._setup_menu()
        self._setup_ui()
        self._setup_tray()
        self._setup_timers()
        self._load_groups()

        # 启动初始化: 从DB加载现价，交易时段额外触发API刷新
        self._startup_initialize()

        logger.info("主窗口初始化完成")

    # ================================================================
    # 菜单
    # ================================================================

    def _setup_menu(self):
        menubar = self.menuBar()

        file_menu = menubar.addMenu("文件(&F)")
        act_add_stock = QAction("添加股票(&A)", self)
        act_add_stock.triggered.connect(self._on_add_stock)
        file_menu.addAction(act_add_stock)
        file_menu.addSeparator()
        act_exit = QAction("退出(&X)", self)
        act_exit.triggered.connect(self._quit_app)
        file_menu.addAction(act_exit)

        group_menu = menubar.addMenu("分组(&G)")
        act_new_group = QAction("新建自定义分组(&N)", self)
        act_new_group.triggered.connect(self._on_new_group)
        group_menu.addAction(act_new_group)
        act_del_group = QAction("删除当前分组(&D)", self)
        act_del_group.triggered.connect(self._on_delete_group)
        group_menu.addAction(act_del_group)

        view_menu = menubar.addMenu("视图(&V)")
        act_refresh = QAction("刷新数据(&R)\tF5", self)
        act_refresh.triggered.connect(self._refresh_current_group_data)
        view_menu.addAction(act_refresh)
        act_marks = QAction("刷新缠论买卖点(&C)", self)
        act_marks.triggered.connect(lambda: self._refresh_chan_marks())
        view_menu.addAction(act_marks)

        help_menu = menubar.addMenu("帮助(&H)")
        act_about = QAction("关于(&A)", self)
        act_about.triggered.connect(self._on_about)
        help_menu.addAction(act_about)

    # ================================================================
    # 主界面布局
    # ================================================================

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(4, 4, 4, 4)

        # ---- 左侧分组面板 ----
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)

        left_label = QLabel("分组列表")
        left_label.setFont(QFont("Microsoft YaHei", 10, QFont.Bold))
        left_layout.addWidget(left_label)

        self.group_list = QListWidget()
        self.group_list.setFixedWidth(SIDEBAR_WIDTH)
        self.group_list.currentItemChanged.connect(self._on_group_selected)
        left_layout.addWidget(self.group_list)

        # ---- 右侧面板 ----
        right_splitter = QSplitter(Qt.Vertical)

        from ui.stock_table import StockTableWidget
        self.stock_table = StockTableWidget()
        self.stock_table.stock_double_clicked.connect(self._on_stock_double_clicked)
        self.stock_table.stock_right_clicked.connect(self._on_stock_right_clicked)
        right_splitter.addWidget(self.stock_table)

        from ui.chart_widget import ChartWidget
        self.chart_widget = ChartWidget()
        right_splitter.addWidget(self.chart_widget)

        right_splitter.setStretchFactor(0, 3)
        right_splitter.setStretchFactor(1, 5)

        # ---- 分割器 ----
        main_splitter = QSplitter(Qt.Horizontal)
        main_splitter.addWidget(left_widget)
        main_splitter.addWidget(right_splitter)
        main_splitter.setStretchFactor(0, 0)
        main_splitter.setStretchFactor(1, 1)

        main_layout.addWidget(main_splitter)

        # ---- 状态栏 ----
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self._status_refresh_label = QLabel("上次刷新: --")
        self._status_profit_label = QLabel("持仓盈亏: --")
        self.status_bar.addWidget(self._status_refresh_label)
        self.status_bar.addPermanentWidget(self._status_profit_label)

    # ================================================================
    # 系统托盘
    # ================================================================

    def _setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            logger.warning("系统托盘不可用")
            return

        self.tray_icon = QSystemTrayIcon(self)
        icon_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "resources", "icons", "app.png"
        )
        if os.path.exists(icon_path):
            self.tray_icon.setIcon(QIcon(icon_path))
        else:
            self.tray_icon.setIcon(self.style().standardIcon(
                self.style().SP_ComputerIcon
            ))

        tray_menu = QMenu()
        act_show = QAction("显示主窗口", self)
        act_show.triggered.connect(self.showNormal)
        tray_menu.addAction(act_show)
        act_hide = QAction("最小化到托盘", self)
        act_hide.triggered.connect(self.hide)
        tray_menu.addAction(act_hide)
        tray_menu.addSeparator()
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self._quit_app)
        tray_menu.addAction(act_quit)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.show()

        self.tray_icon.messageClicked.connect(self.showNormal)
        self.tray_icon.activated.connect(self._on_tray_activated)

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.showNormal()
            self.activateWindow()
            # 点击托盘时停止闪烁（用户已注意到）
            self.flash_tray(False)
            self._clear_all_highlights()

    def flash_tray(self, enable: bool = True):
        """启动/停止托盘图标闪烁"""
        if not hasattr(self, 'tray_icon'):
            return

        if enable and self._alert_triggered_codes:
            if self._tray_flash_timer is None:
                self._tray_flash_timer = QTimer(self)
                self._tray_flash_timer.timeout.connect(self._toggle_tray_icon)
                self._tray_flash_timer.start(500)
        else:
            if self._tray_flash_timer:
                self._tray_flash_timer.stop()
                self._tray_flash_timer = None
            self.tray_icon.setIcon(self.style().standardIcon(
                self.style().SP_ComputerIcon
            ))
            self._tray_flash_on = False

    def _toggle_tray_icon(self):
        """切换托盘图标 (闪烁效果)"""
        if self._tray_flash_on:
            self.tray_icon.setIcon(self.style().standardIcon(
                self.style().SP_ComputerIcon
            ))
        else:
            self.tray_icon.setIcon(self.style().standardIcon(
                self.style().SP_MessageBoxWarning
            ))
        self._tray_flash_on = not self._tray_flash_on

    # ================================================================
    # 启动初始化
    # ================================================================

    def _startup_initialize(self):
        """启动时初始化所有跟踪股票的数据

        开盘:  DB加载现价 → 表格立即显示 → API实时刷新覆盖
        非开盘: DB加载现价 → 表格显示最后交易日收盘，不调API
        """
        codes = self._get_all_tracked_codes()
        if codes:
            loaded = self.data_manager.startup_load_quotes(codes)
            logger.info(
                f"启动加载完成: {loaded}/{len(codes)} 只股票, "
                f"{'交易时段，触发实时刷新' if is_trading_time() else '非交易时段，使用DB收盘数据'}"
            )

        self._refresh_table_display()

        if is_trading_time():
            self._refresh_current_group_data()

    # ================================================================
    # 定时器
    # ================================================================

    def _setup_timers(self):
        # 表格缓存刷新 (3秒，仅读缓存不调API，保证表格实时)
        self._table_display_timer = QTimer(self)
        self._table_display_timer.timeout.connect(self._refresh_table_display)
        self._table_display_timer.start(3000)

        # 实时行情API刷新 (60秒)
        self._realtime_timer = QTimer(self)
        self._realtime_timer.timeout.connect(self._refresh_current_group_data)
        self._realtime_timer.start(REALTIME_REFRESH_MS)

        # 买点**没有**常驻定时器了：2026-09-18 起买点不做提醒，只在 K 线图上
        # 标注，双击股票时按需异步计算（见 `_request_chan_marks`）。
        # 原定时扫描（自研伪缠论）连同 config 开关一并删除。

        # K线数据刷新 (60秒)
        self._kline_timer = QTimer(self)
        self._kline_timer.timeout.connect(self._refresh_kline_if_active)
        self._kline_timer.start(KLINE_REFRESH_MS)

        # 每日止损更新检查 (每分钟检查一次是否到15:05)
        self._daily_sl_timer = QTimer(self)
        self._daily_sl_timer.timeout.connect(self._check_daily_stop_loss)
        self._daily_sl_timer.start(60 * 1000)

    # ================================================================
    # 分组管理
    # ================================================================

    def _load_groups(self):
        """加载分组到左侧列表 (扁平单层)"""
        self.group_list.blockSignals(True)
        self.group_list.clear()

        groups = get_all_groups()
        type_order = {"holding": 0, "cleared": 1, "tracking": 2, "custom": 3}
        type_icons = {"holding": "📊", "cleared": "📋", "tracking": "👁", "custom": "📁"}

        groups.sort(key=lambda g: (type_order.get(g.type, 99), g.sort_order))

        for g in groups:
            icon = type_icons.get(g.type, "📁")
            item = QListWidgetItem(f"{icon}  {g.name}")
            item.setData(Qt.UserRole, g.id)
            item.setData(Qt.UserRole + 1, g.type)
            self.group_list.addItem(item)

        self.group_list.blockSignals(False)

        # 默认选中第一个
        if self.group_list.count() > 0:
            self.group_list.setCurrentRow(0)

    def _on_group_selected(self, current, previous):
        if current is None:
            return
        group_id = current.data(Qt.UserRole)
        if group_id is None:
            return
        self._current_group_id = group_id
        self._refresh_current_group_data()  # 始终刷新表格，交易时段额外拉API

    # ================================================================
    # 数据刷新
    # ================================================================

    def _refresh_current_group_data(self):
        """刷新当前分组：始终刷新表格显示，交易时段才拉API增量数据"""
        self._refresh_table_display()  # 切换分组/添加删除/F5 等始终生效

        if not is_trading_time():
            return

        codes = self._get_all_tracked_codes()
        if not codes:
            return

        # 跳过还在等待初始全量数据的新股
        pending = self.data_manager.get_pending_codes()
        refresh_codes = [c for c in codes if c not in pending]
        if not refresh_codes:
            return

        # 防止重复启动: 如果上一轮 worker 还在跑则跳过
        prev = getattr(self, '_inc_worker', None)
        if prev is not None and prev.isRunning():
            logger.debug("上一轮增量刷新尚未完成，跳过")
            return

        self._inc_worker = IncrementalRefreshWorker(refresh_codes)
        self._inc_worker.data_ready.connect(self._on_incremental_complete)
        # 安全网: 无论 data_ready 是否触发，finished 一定触发
        self._inc_worker.finished.connect(self._on_incremental_complete_safe)
        self._inc_worker.start()

    def _refresh_kline_if_active(self):
        """如果当前有正在查看的股票，刷新K线图表 — 仅交易时段"""
        if not is_trading_time():
            return
        if self._current_stock_code:
            self.chart_widget.refresh_current_tab(self._current_stock_code)

    def _get_all_tracked_codes(self) -> list[str]:
        """获取所有需要监控的股票代码"""
        stocks = get_all_stocks()
        return list(set(s.code for s in stocks))

    def _get_current_group_codes(self) -> list[str]:
        """获取当前选中分组的股票代码"""
        if self._current_group_id < 0:
            return []
        stocks = get_stocks_by_group(self._current_group_id)
        return [s.code for s in stocks]

    def _on_incremental_complete(self, quotes: dict[str, RealtimeQuote]):
        """本轮增量刷新全部完成"""
        # Manager 已自行更新内部缓存，此处只刷新UI
        self._refresh_table_display()
        self._on_incremental_finished(quotes)

    def _on_incremental_complete_safe(self):
        """安全网: worker 异常退出时确保表格至少刷新一次（从缓存读取）"""
        self._on_incremental_finished({})

    def _on_incremental_finished(self, quotes: dict[str, RealtimeQuote]):
        """增量刷新完成后的公共收尾逻辑"""
        now = datetime.now().strftime("%H:%M:%S")
        self._status_refresh_label.setText(f"上次刷新: {now}")

        # 定期 flush 今日bar到DB
        if self.data_manager.should_flush(KLINE_FLUSH_INTERVAL_SEC):
            flushed = self.data_manager.flush_today_bars()
            if flushed > 0:
                logger.debug(f"定时flush: {flushed} 条今日bar写入DB")

        if quotes:
            all_quotes = self.data_manager.get_all_quotes()
            self._check_alerts(all_quotes)
            self._update_profit_status(all_quotes)

    def _refresh_table_display(self):
        """根据DB+Manager缓存刷新表格 (行情缺失时stub填充)"""
        codes = self._get_current_group_codes()
        stocks_in_group = get_stocks_by_group(self._current_group_id)
        name_map = {s.code: s.name for s in stocks_in_group}
        quotes_cache = self.data_manager.get_all_quotes()

        merged = {}
        for code in codes:
            if code in quotes_cache and quotes_cache[code].price > 0:
                q = quotes_cache[code]
                name = q.name or name_map.get(code, "")
                # 创建新对象，避免修改缓存中的原对象
                merged[code] = RealtimeQuote(
                    code=code, name=name,
                    price=q.price, change_pct=q.change_pct,
                    change_amt=q.change_amt, volume=q.volume,
                    turnover=q.turnover, high=q.high, low=q.low,
                    open=q.open, pre_close=q.pre_close,
                    timestamp=q.timestamp,
                )
            else:
                merged[code] = RealtimeQuote(
                    code=code, name=name_map.get(code, ""),
                    price=0.0, timestamp="--",
                )

        self.stock_table.update_quotes(merged, self._alert_triggered_codes)

    def _update_profit_status(self, quotes: dict[str, RealtimeQuote]):
        """更新持仓盈亏状态栏"""
        holding_group = None
        for g in get_all_groups():
            if g.type == "holding":
                holding_group = g
                break
        if holding_group is None:
            return

        stocks = get_stocks_by_group(holding_group.id)
        total_profit = 0.0
        total_cost = 0.0
        for s in stocks:
            summary = get_position_summary(s.code)
            if summary["hold_qty"] > 0 and s.code in quotes:
                q = quotes[s.code]
                profit = (q.price - summary["avg_cost"]) * summary["hold_qty"]
                total_profit += profit
                total_cost += summary["avg_cost"] * summary["hold_qty"]

        if total_cost > 0:
            pct = total_profit / total_cost * 100
            color = "red" if total_profit >= 0 else "green"
            self._status_profit_label.setText(
                f"持仓盈亏: <span style='color:{color}'>{total_profit:+.2f} ({pct:+.2f}%)</span>"
            )
            self._status_profit_label.setTextFormat(Qt.RichText)

    # ================================================================
    # 每日止损更新
    # ================================================================

    def _check_daily_stop_loss(self):
        """检查是否到达每日止损更新时间 (15:05之后且今日尚未执行，容忍卡顿迟到)"""
        now = datetime.now()
        if (now.hour, now.minute) < (DAILY_STOP_LOSS_HOUR, DAILY_STOP_LOSS_MINUTE):
            return
        if now.weekday() >= 5:
            return

        today_str = now.strftime("%Y-%m-%d")
        holding_codes = self._get_holding_codes()
        for code in holding_codes:
            if (code, today_str) not in self._daily_stop_loss_done:
                new_stop, conflict = self.alert_engine.update_daily_stop_loss(code)
                if conflict:
                    # 手动止损与自动计算冲突 → 弹窗确认
                    # 同样标记今日已处理，避免每分钟重复弹窗
                    self._daily_stop_loss_done.add((code, today_str))
                    self._show_alert_conflict(code, conflict)
                else:
                    self._daily_stop_loss_done.add((code, today_str))
                    logger.info(f"[{today_str}] {code} 收盘止损更新: {new_stop:.2f}")

        # 跨天时旧日期记录自动失效，只保留今日
        self._daily_stop_loss_done = {
            (c, d) for c, d in self._daily_stop_loss_done
            if d == today_str
        }

    def _get_holding_codes(self) -> list[str]:
        """获取持仓中的代码列表"""
        for g in get_all_groups():
            if g.type == "holding":
                stocks = get_stocks_by_group(g.id)
                return [s.code for s in stocks]
        return []

    # ================================================================
    # 止损止盈检查 (使用AlertEngine)
    # ================================================================

    def _check_alerts(self, quotes: dict[str, RealtimeQuote]):
        """检查止损止盈触发 (AlertEngine) + 自动更新冲突检测"""
        holding_codes = self._get_holding_codes()
        triggered = []

        for code in holding_codes:
            if code not in quotes:
                continue
            if is_alert_disabled(code):
                self._alert_triggered_codes.discard(code)
                continue

            quote = quotes[code]

            # 计算止损线 (用今日最低价更新)，检查手动冲突
            _, sl_conflict = self.alert_engine.calc_stop_loss(
                code, quote.low if quote.low > 0 else quote.price
            )
            if sl_conflict:
                self._show_alert_conflict(code, sl_conflict)

            # 计算止盈线 (检查30min顶分型)，检查手动冲突
            _, tp_conflict = self.alert_engine.calc_take_profit(code, quote.price)
            if tp_conflict:
                self._show_alert_conflict(code, tp_conflict)

            # 检查触发
            result = self.alert_engine.check_alerts(code, quote)
            if result["triggered"]:
                triggered.append((
                    code, result["type"], result["trigger_price"], quote.price,
                    result.get("message", "")
                ))
                self._alert_triggered_codes.add(code)
            else:
                self._alert_triggered_codes.discard(code)

        if triggered:
            self._on_alerts_triggered(triggered)
        else:
            # 更新高亮（清除不再触发的）
            alert_codes = list(self._alert_triggered_codes)
            if alert_codes:
                self.stock_table.highlight_rows(alert_codes)
            else:
                self.stock_table.clear_highlights()
                self.flash_tray(False)

    def _on_alerts_triggered(self, triggered: list[tuple]):
        """提醒触发处理"""
        self.flash_tray(True)

        alert_codes = list(self._alert_triggered_codes)
        self.stock_table.highlight_rows(alert_codes)

        msgs = [f"{code} {reason}: 触发价={trigger:.2f} 现价={price:.2f}"
                for code, reason, trigger, price, _ in triggered]
        self.tray_icon.showMessage(
            "⚠ 交易提醒",
            "\n".join(msgs[:3]) + ("..." if len(msgs) > 3 else ""),
            QSystemTrayIcon.Warning,
            5000,
        )

    def _show_alert_conflict(self, code: str, conflict: dict):
        """
        手动设置与自动计算冲突 → 弹窗确认
        conflict: {field: 'sl'|'tp', auto_value: float, manual_value: float}
        """
        from PyQt5.QtWidgets import QMessageBox

        field_name = "止损" if conflict["field"] == "sl" else "止盈"
        auto_val = conflict["auto_value"]
        manual_val = conflict["manual_value"]

        reply = QMessageBox.question(
            self,
            f"止盈止损冲突 - {code}",
            f"系统自动计算的{field_name}价 (¥{auto_val:.2f})\n"
            f"与您手动设置的{field_name}价 (¥{manual_val:.2f}) 不一致。\n\n"
            f"选择「覆盖」: 放弃手动设置，使用系统自动值 ¥{auto_val:.2f}\n"
            f"选择「保留」: 继续使用手动设置 ¥{manual_val:.2f}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )

        if reply == QMessageBox.Yes:
            # 用户选择覆盖 → 使用系统自动值
            if conflict["field"] == "sl":
                self.alert_engine.clear_manual(code, "sl")
                self.alert_engine.get_state(code).stop_loss_price = auto_val
            else:
                self.alert_engine.clear_manual(code, "tp")
                self.alert_engine.get_state(code).take_profit_price = auto_val
            logger.info(f"{code} 用户选择覆盖手动{field_name}: {manual_val:.2f} → {auto_val:.2f}")
        else:
            logger.info(f"{code} 用户选择保留手动{field_name}: {manual_val:.2f}")

    # ================================================================
    # 缠论买卖点标注（异步，只画图不提醒）
    # ================================================================

    def _request_chan_marks(self, code: str, force: bool = False):
        """请求一只股票的缠论买卖点标注（命中缓存就直接上屏）

        2026-09-18 老三定：买点**不做提醒**，只在 K 线图上标注。
        计算要联网取 30 分钟行情 + 建两套 czsc 对象（实测单只 2~3 秒），
        所以放后台线程。**单飞**：上一轮没跑完就跳过，避免连点叠线程 ——
        这是原 `_scan_buy_points` 的线程契约，一字未改，`tests/test_ui_threading.py`
        继续把它钉住（单轮单 worker + `finished` 接 `deleteLater`）。
        """
        if force:
            self._chan_marks.pop(code, None)

        cached = self._chan_marks.get(code)
        if cached is not None:
            self.chart_widget.set_chan_marks(code, cached)
            return

        prev = getattr(self, '_chan_mark_worker', None)
        if prev is not None:
            try:
                if prev.isRunning():
                    # 静默丢弃会让新股票**永远**没有标注：换股票时旧标注已被
                    # `load_data` 清空，而这一轮的 ready 回调又因
                    # `code != 当前股票` 不上屏 —— 图上一直是空的，用户只能
                    # 再双击一次。所以记下来，等当前这轮结束再补算
                    # （见 `_on_chan_worker_finished`）。
                    logger.debug(f"上一轮缠论买卖点计算尚未完成，{code} 排队等待")
                    self._pending_chan_code = code
                    return
            except RuntimeError:
                pass  # 底层对象已随 deleteLater 销毁

        logger.info(f"开始异步计算 {code} 的缠论买卖点")
        self.status_bar.showMessage(f"正在计算 {code} 的缠论买卖点...")
        self._chan_mark_worker = ChanMarkWorker(code)
        self._chan_mark_worker.marks_ready.connect(self._on_chan_marks_ready)
        self._chan_mark_worker.failed.connect(self._on_chan_marks_failed)
        self._chan_mark_worker.finished.connect(self._on_chan_worker_finished)
        self._chan_mark_worker.finished.connect(self._chan_mark_worker.deleteLater)
        self._chan_mark_worker.start()

    def _on_chan_marks_ready(self, code: str, marks: dict):
        """标注算好了（主线程）—— 缓存；只在它还是当前股票时才上屏"""
        self._chan_marks[code] = marks
        trades = len(marks.get("trades") or [])
        geo = len(marks.get("geometry") or [])
        logger.info(f"{code} 缠论标注完成：日线几何点 {geo} 个、策略买卖点 {trades} 笔")
        if code == self._current_stock_code:
            self.chart_widget.set_chan_marks(code, marks)
            self.status_bar.showMessage(
                f"{code} 缠论买卖点：策略 {trades} 笔 / 日线几何点 {geo} 个", 5000)

    def _on_chan_marks_failed(self, code: str, message: str):
        """取数/计算失败 —— 状态栏提示即止，不弹窗、不影响其它功能"""
        logger.error(f"{code} 缠论买卖点计算失败: {message}")
        if code == self._current_stock_code:
            self.chart_widget.set_chan_marks(code, None)
            self.status_bar.showMessage(f"{code} 缠论买卖点计算失败：{message}", 8000)

    def _on_chan_worker_finished(self):
        """worker 结束 —— 补算单飞期间被丢弃的请求（2026-09-20 加）

        没有这一步，「A 还在算时双击 B」会让 B **永远**拿不到标注：B 的请求
        被丢弃，A 算完时 `code` 已不是当前股票（回调不上屏），而换股票时旧
        标注已被 `load_data` 清空 —— 图上就一直是空的，用户只能再双击一次。

        只在排队的那只**仍是当前股票**时才补算：中途又切走就没有必要算。
        """
        pending, self._pending_chan_code = self._pending_chan_code, ""
        if pending and pending == self._current_stock_code:
            logger.debug(f"补算排队中的缠论买卖点：{pending}")
            self._request_chan_marks(pending)

    def _refresh_chan_marks(self, code: str = ""):
        """强制重算（右键菜单 / 视图菜单）—— 盘中重取或换参数后用"""
        code = code or self._current_stock_code
        if not code:
            return
        self._request_chan_marks(code, force=True)

    # ================================================================
    # 股票操作
    # ================================================================

    def _on_stock_double_clicked(self, code: str):
        """双击股票行 → 切换图表 + 停止闪烁 + 标注缠论买卖点"""
        self._current_stock_code = code
        self.chart_widget.load_stock(code)

        # 用户点击了触发提醒的股票 → 停止托盘闪烁和表格高亮
        if code in self._alert_triggered_codes:
            self._alert_triggered_codes.discard(code)
            if not self._alert_triggered_codes:
                self.flash_tray(False)
                self.stock_table.clear_highlights()
            else:
                self.stock_table.highlight_rows(list(self._alert_triggered_codes))

        # 缠论买卖点：**只标注在 K 线图上**，不弹窗、不提醒（2026-09-18 老三定）
        self._request_chan_marks(code)

    def _on_stock_right_clicked(self, code: str, action: str):
        """股票右键菜单操作"""
        if action == "add_trade":
            from ui.trade_dialog import TradeDialog
            dlg = TradeDialog(code, self)
            dlg.exec_()
        elif action == "refresh_chan_marks":
            self._refresh_chan_marks(code)
        elif action == "discipline":
            # 交易纪律从「买点触发自动弹窗」改为手动入口 —— 自动弹窗属买点提示，已取消
            from ui.discipline_dialog import DisciplineDialog
            DisciplineDialog(code, self).exec_()
            self._refresh_table_display()
        elif action == "manual_alert":
            self._on_manual_alert_settings(code)
        elif action == "disable_alert":
            disable_alert(code)
            self._alert_triggered_codes.discard(code)
            if not self._alert_triggered_codes:
                self.flash_tray(False)
                self.stock_table.clear_highlights()
            logger.info(f"{code} 提醒已手动关闭")
        elif action == "enable_alert":
            enable_alert(code)
            logger.info(f"{code} 提醒已手动开启")
        elif action == "remove_stock":
            stocks = get_stocks_by_group(self._current_group_id)
            for s in stocks:
                if s.code == code:
                    remove_stock(s.id)
                    break
            self._refresh_current_group_data()
        elif action == "move_to_cleared":
            self._move_to_cleared(code)

    def _move_to_cleared(self, code: str):
        """将股票移到已清仓分组"""
        cleared_id = None
        for g in get_all_groups():
            if g.type == "cleared":
                cleared_id = g.id
                break
        if cleared_id:
            stocks = get_stocks_by_group(self._current_group_id)
            for s in stocks:
                if s.code == code:
                    move_stock(s.id, cleared_id)
                    break
            self._refresh_current_group_data()
            logger.info(f"{code} 已移至已清仓分组")

    def _on_manual_alert_settings(self, code: str):
        """打开手动止盈止损设置对话框"""
        from ui.alert_settings_dialog import AlertSettingsDialog
        quote = self.data_manager.get_quote(code)
        dlg = AlertSettingsDialog(code, quote, self)
        if dlg.exec_() == AlertSettingsDialog.Accepted:
            result = dlg.get_result()
            # 应用手动设置到 AlertEngine
            if result.get("sl_active"):
                self.alert_engine.set_manual_sl(code, result["sl_price"])
            else:
                self.alert_engine.clear_manual(code, "sl")

            if result.get("tp_active"):
                self.alert_engine.set_manual_tp(code, result["tp_price"])
            else:
                self.alert_engine.clear_manual(code, "tp")

            self._refresh_current_group_data()

    def _clear_all_highlights(self):
        """清除所有高亮"""
        self.stock_table.clear_highlights()

    def _on_add_stock(self):
        """添加股票 — 纯代码走本地确认，关键字走搜索"""
        from PyQt5.QtWidgets import QInputDialog
        import re

        # 如果没有选中分组，默认选"跟踪中"
        if self._current_group_id < 0:
            for g in get_all_groups():
                if g.type == "tracking":
                    self._current_group_id = g.id
                    break

        keyword, ok = QInputDialog.getText(self, "添加股票", "输入股票代码或名称:")
        if not ok or not keyword.strip():
            return

        keyword = keyword.strip()

        # 检测是否为纯6位数字代码
        if re.match(r"^\d{6}$", keyword):
            self._try_add_by_code(keyword)
        else:
            # 关键字搜索 — 异步
            self.status_bar.showMessage(f"正在搜索 '{keyword}' ...")
            self._search_worker = StockSearchWorker(keyword)
            self._search_worker.data_ready.connect(
                lambda results: self._on_search_result(results, keyword))
            self._search_worker.error_occurred.connect(self._on_search_error)
            self._search_worker.start()

    def _try_add_by_code(self, code: str):
        """纯代码添加: DB有→弹窗确认→添加; DB没有→异步同步名称库→回查后再添加"""
        from data.database import get_stock_name

        name = get_stock_name(code)
        if name:
            reply = QMessageBox.question(
                self, "确认添加",
                f"检测到股票:\n\n{code}  {name}\n\n确认添加到当前分组?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply == QMessageBox.Yes:
                self._do_add_stock(code, name)
            else:
                logger.info(f"用户取消添加 {code}")
            return

        # DB中没有 → 异步全量同步
        # (该同步含 3 次 AKShare 请求 + 递增 sleep，同步执行会冻结 UI 十秒以上)
        prev = getattr(self, '_name_sync_worker', None)
        if prev is not None and prev.isRunning():
            self.status_bar.showMessage("股票名称库正在同步中，请稍候...", 3000)
            logger.debug(f"{code} 添加请求跳过: 上一轮名称库同步尚未完成")
            return

        logger.info(f"本地无 {code}，触发异步全量名称同步...")
        self.status_bar.showMessage(
            f"本地无 {code}，正在同步股票名称库（可能需要数十秒）...")

        self._name_sync_worker = StockNameSyncWorker(code)
        self._name_sync_worker.sync_done.connect(self._on_name_sync_done)
        self._name_sync_worker.sync_failed.connect(self._on_name_sync_failed)
        self._name_sync_worker.finished.connect(self._name_sync_worker.deleteLater)
        self._name_sync_worker.start()

    def _on_name_sync_done(self, code: str, name: str):
        """名称库同步完成 → 按回查结果添加或提示"""
        if name:
            self.status_bar.showMessage(f"名称库已同步: {code} {name}", 5000)
            self._do_add_stock(code, name)
        else:
            self.status_bar.showMessage(f"未找到代码 {code}", 5000)
            QMessageBox.warning(self, "未找到", f"未找到代码 {code}，请检查后重试。")

    def _on_name_sync_failed(self, err: str):
        """名称库同步异常 → 明确告知调用方，不再静默"""
        logger.error(f"股票名称库同步失败: {err}")
        self.status_bar.showMessage("股票名称库同步失败", 5000)
        QMessageBox.warning(
            self, "同步失败",
            "股票名称库同步失败（通常是网络问题）。\n"
            "请稍后重试，或改用名称搜索添加。",
        )

    def _do_add_stock(self, code: str, name: str):
        """添加股票到DB，启动独立全量数据获取 (不阻塞增量刷新)"""
        stock = add_stock(code, name, self._current_group_id)
        if stock is None:
            QMessageBox.information(self, "提示", f"股票 {code} 已存在于此分组")
            return

        self.status_bar.showMessage(f"已添加: {code} {name}，正在获取全量数据...")
        logger.info(f"添加股票: {code} {name}")

        # 标记为等待初始全量数据 (增量刷新暂时跳过)
        self.data_manager.mark_pending(code)

        # 先刷新表格显示 (stub数据)
        self._refresh_table_display()

        # 启动独立的全量数据 Worker (与增量刷新互不阻塞)
        self._init_worker = InitialFetchWorker(code)
        self._init_worker.all_done.connect(self._on_new_stock_init_done)
        self._init_worker.error_occurred.connect(
            lambda e: logger.error(f"新股 {code} 全量数据获取失败: {e}"))
        self._init_worker.start()

    def _on_new_stock_init_done(self, code: str):
        """新股全量数据获取完成 → 刷新表格显示（现价已由Manager写入）"""
        self.data_manager.unmark_pending(code)
        self._refresh_table_display()  # 立即显示新股的现价数据
        self.status_bar.showMessage(f"{code} 数据初始化完成", 3000)
        logger.info(f"{code} 全量数据初始化完成")

    def _fallback_to_search(self, keyword: str):
        """回退到搜索模式"""
        self._search_worker = StockSearchWorker(keyword)
        self._search_worker.data_ready.connect(
            lambda results: self._on_search_result(results, keyword))
        self._search_worker.error_occurred.connect(self._on_search_error)
        self._search_worker.start()

    def _on_search_result(self, results: list[dict], keyword: str):
        """搜索结果回调"""
        from PyQt5.QtWidgets import QDialog, QListWidget, QVBoxLayout, QDialogButtonBox, QListWidgetItem

        if not results:
            QMessageBox.information(self, "搜索", f"未找到 '{keyword}'")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("选择股票")
        dlg.setMinimumSize(350, 400)
        layout = QVBoxLayout(dlg)

        list_widget = QListWidget()
        for r in results:
            item = QListWidgetItem(f"{r['code']}  {r['name']}  ¥{r['price']:.2f}")
            item.setData(Qt.UserRole, r)
            list_widget.addItem(item)
        layout.addWidget(list_widget)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        if dlg.exec_() != QDialog.Accepted:
            return

        selected = list_widget.currentItem()
        if selected is None:
            return

        r = selected.data(Qt.UserRole)
        if self._current_group_id < 0:
            for g in get_all_groups():
                if g.type == "tracking":
                    self._current_group_id = g.id
                    break

        stock = add_stock(r["code"], r["name"], self._current_group_id)
        if stock:
            self._refresh_current_group_data()
            logger.info(f"添加股票: {r['code']} {r['name']}")
        else:
            QMessageBox.information(self, "提示", f"股票 {r['code']} 已存在于此分组")

    def _on_search_error(self, error_msg: str):
        """搜索出错回调"""
        self.status_bar.showMessage("搜索失败", 5000)
        logger.error(f"搜索失败: {error_msg}")
        QMessageBox.warning(
            self, "搜索失败",
            f"无法搜索股票，请检查网络连接。\n\n{error_msg}"
        )

    def _on_new_group(self):
        """新建自定义分组"""
        from PyQt5.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "新建分组", "分组名称:")
        if ok and name.strip():
            from data.database import add_group
            add_group(name.strip(), "custom")
            self._load_groups()
            logger.info(f"新建自定义分组: {name.strip()}")

    def _on_delete_group(self):
        """删除当前自定义分组"""
        if self._current_group_id < 0:
            return
        for g in get_all_groups():
            if g.id == self._current_group_id:
                if g.type != "custom":
                    QMessageBox.warning(self, "提示", "不能删除系统分组")
                    return
                reply = QMessageBox.question(
                    self, "确认", f"确定删除分组 '{g.name}' 及其所有股票?",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if reply == QMessageBox.Yes:
                    from data.database import delete_group
                    delete_group(g.id)
                    self._current_group_id = -1
                    self._load_groups()
                    self.stock_table.clear()
                    logger.info(f"删除分组: {g.name}")
                return

    def _on_about(self):
        QMessageBox.about(
            self, "关于",
            "A股交易辅助系统 v1.0\n\n"
            "功能:\n"
            "• 实时股票数据与K线图表\n"
            "• 持仓/已清仓/跟踪分组管理\n"
            "• 止损止盈线自动计算与提醒\n"
            "• 缠论买卖点标注（日线几何点 + 多周期共振策略）\n"
            "• 交易纪律提醒\n\n"
            "数据来源: AKShare"
        )

    # ================================================================
    # 生命周期
    # ================================================================

    def closeEvent(self, event):
        """窗口X按钮 → 最小化到托盘，不退出应用 (真正退出走 _quit_app)"""
        if self._quitting:
            event.accept()
            return
        event.ignore()
        self.hide()
        if hasattr(self, 'tray_icon'):
            self.tray_icon.showMessage(
                "A股交易辅助系统",
                "程序已最小化到托盘，仍在后台运行。\n右键托盘图标选择「退出」可完全关闭。",
                QSystemTrayIcon.Information,
                3000,
            )
        logger.info("窗口关闭，最小化到托盘")

    def _quit_app(self):
        """托盘/菜单「退出」→ 清理后真正退出应用"""
        if self._quitting:
            return  # 防重入，收尾逻辑只执行一次
        self._quitting = True
        logger.info("系统退出")

        self.flash_tray(False)

        # 停止所有定时器
        for timer in (self._table_display_timer, self._realtime_timer,
                      self._kline_timer, self._daily_sl_timer):
            timer.stop()

        # 退出前最后flush一次今日bar到DB
        try:
            flushed = self.data_manager.flush_today_bars()
            if flushed > 0:
                logger.info(f"退出前flush: {flushed} 条今日bar写入DB")
        except Exception as e:
            logger.error(f"退出前flush失败: {e}")

        if hasattr(self, 'tray_icon'):
            self.tray_icon.hide()

        QApplication.instance().quit()
