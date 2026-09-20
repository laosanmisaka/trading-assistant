"""股票列表表格 — 自定义QTableView + Model，支持高亮、排序、右键菜单

2026-09-18：删除「买点信号」列与买点高亮 —— 买点提醒已取消，改为在
K 线图上标注（`ui/chart_widget.py` 的 `set_chan_marks`）。表格只保留
止损止盈提醒这一条提醒链路。
"""

from PyQt5.QtWidgets import (
    QTableView, QHeaderView, QAbstractItemView, QMenu, QAction,
)
from PyQt5.QtCore import (
    Qt, QAbstractTableModel, QModelIndex, pyqtSignal, QTimer,
)
from PyQt5.QtGui import QColor, QBrush, QFont

from config import STOCK_TABLE_COLUMNS
from data.models import RealtimeQuote
from utils.logger import get_logger

logger = get_logger(__name__)

# 提醒高亮色（红底）
ALERT_COLOR = "#FF4444"


class StockTableModel(QAbstractTableModel):
    """股票数据模型"""

    COL_CODE = 0
    COL_NAME = 1
    COL_PRICE = 2
    COL_CHANGE_PCT = 3
    COL_CHANGE_AMT = 4
    COL_VOLUME = 5
    COL_STOP_LOSS = 6
    COL_TAKE_PROFIT = 7
    COL_ALERT = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self._quotes: dict[str, RealtimeQuote] = {}
        self._codes: list[str] = []
        self._alert_codes: set[str] = set()      # 止损止盈触发代码
        self._highlight_rows: set[str] = set()    # 当前高亮行
        self._highlight_colors: dict[str, QColor] = {}

    def update_data(
        self,
        quotes: dict[str, RealtimeQuote],
        alert_codes: set[str],
    ):
        """更新数据"""
        self.beginResetModel()
        self._quotes = quotes
        self._codes = list(quotes.keys())
        self._alert_codes = alert_codes
        # 保持高亮与触发状态同步
        self._highlight_rows = set(alert_codes)
        self.endResetModel()

    def set_highlights(self, codes: set[str], color: QColor):
        """设置高亮行"""
        self._highlight_rows = codes
        for c in codes:
            self._highlight_colors[c] = color

    def clear_highlights(self):
        self._highlight_rows = set()
        self._highlight_colors.clear()

    def rowCount(self, parent=QModelIndex()):
        return len(self._codes)

    def columnCount(self, parent=QModelIndex()):
        return len(STOCK_TABLE_COLUMNS)

    def headerData(self, section, orientation, role):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return STOCK_TABLE_COLUMNS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None

        code = self._codes[index.row()]
        quote = self._quotes.get(code)
        if quote is None:
            return None

        col = index.column()
        is_alert = code in self._alert_codes

        # 背景色
        if role == Qt.BackgroundRole:
            if is_alert and code in self._highlight_rows:
                # 止损止盈触发 → 红色背景
                return QBrush(QColor(ALERT_COLOR))
            return None

        # 前景色
        if role == Qt.ForegroundRole:
            if col == self.COL_CHANGE_PCT:
                val = quote.change_pct
                return QBrush(QColor("red") if val >= 0 else QColor("green"))
            if col == self.COL_PRICE:
                return QBrush(QColor("red") if quote.change_pct >= 0 else QColor("green"))
            return None

        # 文字对齐
        if role == Qt.TextAlignmentRole:
            if col in (self.COL_CODE, self.COL_NAME, self.COL_ALERT):
                return Qt.AlignCenter
            return Qt.AlignRight | Qt.AlignVCenter

        # 高亮行加粗
        if role == Qt.FontRole:
            if is_alert and code in self._highlight_rows:
                font = QFont()
                font.setBold(True)
                return font
            return None

        if role != Qt.DisplayRole:
            return None

        # 显示值 — 行情缺失时显示 "--"
        if col == self.COL_CODE:
            return code
        elif col == self.COL_NAME:
            return quote.name or "--"
        elif col == self.COL_PRICE:
            return f"{quote.price:.2f}" if quote.price > 0 else "--"
        elif col == self.COL_CHANGE_PCT:
            return f"{quote.change_pct:+.2f}%" if quote.price > 0 else "--"
        elif col == self.COL_CHANGE_AMT:
            return f"{quote.change_amt:+.2f}" if quote.price > 0 else "--"
        elif col == self.COL_VOLUME:
            return f"{quote.volume:,}"
        elif col == self.COL_STOP_LOSS:
            return "--"
        elif col == self.COL_TAKE_PROFIT:
            return "--"
        elif col == self.COL_ALERT:
            if is_alert:
                return "⚠ 触发"
            return "正常"

        return None

    def get_code_at(self, row: int) -> str:
        if 0 <= row < len(self._codes):
            return self._codes[row]
        return ""


class StockTableWidget(QTableView):
    """股票表格组件"""

    stock_double_clicked = pyqtSignal(str)  # code
    stock_right_clicked = pyqtSignal(str, str)  # (code, action)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._model = StockTableModel(self)
        self.setModel(self._model)

        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setAlternatingRowColors(True)
        self.setSortingEnabled(False)
        self.setShowGrid(True)
        self.verticalHeader().setVisible(False)

        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        self.setColumnWidth(0, 80)
        self.setColumnWidth(1, 80)

        self.doubleClicked.connect(self._on_double_click)

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

        # 闪烁动画定时器
        self._flash_timer = QTimer(self)
        self._flash_timer.timeout.connect(self._toggle_flash)
        self._flash_on = False
        self._flash_codes: set[str] = set()

    def update_quotes(
        self,
        quotes: dict[str, RealtimeQuote],
        alert_codes: set[str],
    ):
        """更新行情数据"""
        self._model.update_data(quotes, alert_codes)

    def highlight_rows(self, codes: list[str]):
        """高亮指定股票行（止损止盈触发）"""
        self._flash_codes = set(codes)
        self._model.set_highlights(set(codes), QColor(ALERT_COLOR))
        self.viewport().update()

        if not self._flash_timer.isActive():
            self._flash_timer.start(600)

    def clear_highlights(self):
        self._flash_timer.stop()
        self._flash_codes.clear()
        self._model.clear_highlights()
        self.viewport().update()

    def clear(self):
        self._model.update_data({}, set())

    def _toggle_flash(self):
        """切换高亮闪烁"""
        if self._flash_on:
            self._model.clear_highlights()
        else:
            self._model.set_highlights(self._flash_codes, QColor(ALERT_COLOR))
        self._flash_on = not self._flash_on
        self.viewport().update()

    def _on_double_click(self, index: QModelIndex):
        code = self._model.get_code_at(index.row())
        if code:
            self.stock_double_clicked.emit(code)

    def _on_context_menu(self, pos):
        index = self.indexAt(pos)
        code = self._model.get_code_at(index.row()) if index.isValid() else ""

        menu = QMenu(self)

        if code:
            act_view = QAction("查看图表", self)
            act_view.triggered.connect(lambda: self.stock_double_clicked.emit(code))
            menu.addAction(act_view)

            act_marks = QAction("刷新缠论买卖点", self)
            act_marks.triggered.connect(
                lambda: self.stock_right_clicked.emit(code, "refresh_chan_marks"))
            menu.addAction(act_marks)

            menu.addSeparator()

            act_trade = QAction("交易记录", self)
            act_trade.triggered.connect(lambda: self.stock_right_clicked.emit(code, "add_trade"))
            menu.addAction(act_trade)

            act_discipline = QAction("交易纪律...", self)
            act_discipline.triggered.connect(
                lambda: self.stock_right_clicked.emit(code, "discipline"))
            menu.addAction(act_discipline)

            menu.addSeparator()

            act_manual = QAction("手动止盈止损设置...", self)
            act_manual.triggered.connect(lambda: self.stock_right_clicked.emit(code, "manual_alert"))
            menu.addAction(act_manual)

            menu.addSeparator()

            act_disable = QAction("关闭提醒", self)
            act_disable.triggered.connect(
                lambda: self.stock_right_clicked.emit(code, "disable_alert"))
            menu.addAction(act_disable)

            act_enable = QAction("开启提醒", self)
            act_enable.triggered.connect(
                lambda: self.stock_right_clicked.emit(code, "enable_alert"))
            menu.addAction(act_enable)

            menu.addSeparator()

            act_move = QAction("移至已清仓", self)
            act_move.triggered.connect(
                lambda: self.stock_right_clicked.emit(code, "move_to_cleared"))
            menu.addAction(act_move)

            act_remove = QAction("从分组移除", self)
            act_remove.triggered.connect(
                lambda: self.stock_right_clicked.emit(code, "remove_stock"))
            menu.addAction(act_remove)

        menu.exec_(self.viewport().mapToGlobal(pos))
