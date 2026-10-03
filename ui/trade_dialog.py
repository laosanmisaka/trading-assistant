"""交易记录对话框 — 查看/添加/编辑/删除买卖记录，计算持仓成本"""

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QMessageBox, QHeaderView, QAbstractItemView,
    QFormLayout, QLineEdit, QComboBox, QDateEdit, QDialogButtonBox,
    QGroupBox,
)
from PyQt5.QtCore import Qt, QDate

from data.database import (
    get_trades, add_trade, update_trade, delete_trade, get_position_summary,
)
from data.models import Trade, TradeType


class TradeDialog(QDialog):
    """交易记录管理对话框"""

    def __init__(self, stock_code: str, parent=None):
        super().__init__(parent)
        self.stock_code = stock_code
        self.setWindowTitle(f"交易记录 - {stock_code}")
        self.setMinimumSize(750, 550)

        self._setup_ui()
        self._load_trades()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # ---- 交易记录表格 ----
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels([
            "日期", "类型", "价格", "数量(股)", "手续费", "金额", "备注"
        ])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 100)  # 日期
        self.table.setColumnWidth(1, 50)   # 类型
        self.table.setColumnWidth(3, 80)   # 数量
        layout.addWidget(self.table)

        # ---- 操作按钮 ----
        btn_layout = QHBoxLayout()
        self.btn_add = QPushButton("添加记录")
        self.btn_add.clicked.connect(self._on_add)
        self.btn_edit = QPushButton("编辑记录")
        self.btn_edit.clicked.connect(self._on_edit)
        self.btn_delete = QPushButton("删除记录")
        self.btn_delete.clicked.connect(self._on_delete)
        btn_layout.addWidget(self.btn_add)
        btn_layout.addWidget(self.btn_edit)
        btn_layout.addWidget(self.btn_delete)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # ---- 持仓摘要 ----
        summary_group = QGroupBox("持仓摘要")
        s_layout = QFormLayout(summary_group)
        self.lbl_hold_qty = QLabel("--")
        self.lbl_avg_cost = QLabel("--")
        self.lbl_total_buy = QLabel("--")
        self.lbl_total_sell = QLabel("--")
        self.lbl_pnl = QLabel("--")
        s_layout.addRow("持仓数量:", self.lbl_hold_qty)
        s_layout.addRow("持仓成本:", self.lbl_avg_cost)
        s_layout.addRow("总买入金额:", self.lbl_total_buy)
        s_layout.addRow("总卖出金额:", self.lbl_total_sell)
        s_layout.addRow("累计总盈亏:", self.lbl_pnl)
        layout.addWidget(summary_group)

        # ---- 关闭 ----
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close, alignment=Qt.AlignRight)

    # ================================================================
    # 数据加载
    # ================================================================

    def _load_trades(self):
        """加载交易记录到表格"""
        trades = get_trades(self.stock_code)
        self.table.setRowCount(len(trades))

        for i, t in enumerate(trades):
            self.table.setItem(i, 0, QTableWidgetItem(t.trade_date))
            type_text = "买入" if t.trade_type == TradeType.BUY.value else "卖出"
            type_item = QTableWidgetItem(type_text)
            type_item.setForeground(
                Qt.red if t.trade_type == TradeType.BUY.value else Qt.darkGreen)
            self.table.setItem(i, 1, type_item)
            self.table.setItem(i, 2, QTableWidgetItem(f"{t.price:.3f}"))
            self.table.setItem(i, 3, QTableWidgetItem(str(t.quantity)))
            self.table.setItem(i, 4, QTableWidgetItem(f"{t.fee:.2f}"))
            amount = t.price * t.quantity
            self.table.setItem(i, 5, QTableWidgetItem(f"{amount:.2f}"))
            self.table.setItem(i, 6, QTableWidgetItem(t.notes))

            # 存储trade id
            self.table.item(i, 0).setData(Qt.UserRole, t.id)

        self._update_summary()

    def _update_summary(self):
        """更新持仓摘要"""
        s = get_position_summary(self.stock_code)
        self.lbl_hold_qty.setText(f"{s['hold_qty']} 股")

        if s["hold_qty"] > 0:
            self.lbl_avg_cost.setText(f"¥{s['avg_cost']:.3f}")
            self.lbl_avg_cost.setStyleSheet("color: #333;")
        else:
            self.lbl_avg_cost.setText("已清仓")
            self.lbl_avg_cost.setStyleSheet("color: gray;")

        self.lbl_total_buy.setText(f"¥{s['total_buy_amt']:,.2f}")
        self.lbl_total_sell.setText(f"¥{s['total_sell_amt']:,.2f}")

        # 总盈亏 = 累计卖出净收入 + 当前市值 - 累计买入支出。
        if not s.get("valid", True):
            self.lbl_pnl.setText("交易记录存在卖超，请核对")
            return
        profit = s["total_sell_amt"] - s["total_buy_amt"]
        if s["hold_qty"] > 0:
            # 尝试获取实时价格
            main_win = self._get_main_window()
            quote = main_win.data_manager.get_quote(self.stock_code) if main_win else None
            if quote is None or quote.price <= 0:
                self.lbl_pnl.setText("待有效行情（含未平仓市值）")
                return
            profit += quote.price * s["hold_qty"]
        pct = profit / s["total_buy_amt"] * 100 if s["total_buy_amt"] > 0 else 0
        color = "red" if profit >= 0 else "green"
        self.lbl_pnl.setText(
            f"<span style='color:{color}'>{profit:+,.2f} ({pct:+.2f}% 累计买入额)</span>")
        self.lbl_pnl.setTextFormat(Qt.RichText)

    def _get_main_window(self):
        """获取主窗口实例"""
        w = self.parent()
        while w:
            from ui.main_window import MainWindow
            if isinstance(w, MainWindow):
                return w
            w = w.parent()
        return None

    # ================================================================
    # 操作
    # ================================================================

    def _on_add(self):
        """添加交易记录"""
        dlg = TradeEditDialog(self.stock_code, self)
        if dlg.exec_() == QDialog.Accepted:
            trade = dlg.get_trade()
            add_trade(trade)

            # 如果卖出后全部清仓 → 自动移到已清仓分组
            if trade.trade_type == TradeType.SELL.value:
                s = get_position_summary(self.stock_code)
                if s["hold_qty"] <= 0:
                    self._auto_move_to_cleared(trade.stock_code)

            self._load_trades()

    def _auto_move_to_cleared(self, code: str):
        """自动将股票从持仓中移到已清仓"""
        from data.database import get_all_groups, get_stock_by_code_group, move_stock
        from utils.logger import get_logger
        logger = get_logger(__name__)

        target_group = None
        for g in get_all_groups():
            if g.type == "cleared":
                target_group = g
                break

        if target_group is None:
            return

        # 在持仓组中找到该股票并移动
        for hg in get_all_groups():
            if hg.type == "holding":
                hs = get_stock_by_code_group(code, hg.id)
                if hs:
                    move_stock(hs.id, target_group.id)
                    logger.info(f"{code} 清仓后自动移至已清仓分组")
                    return

    def _on_edit(self):
        """编辑选中记录"""
        row = self.table.currentRow()
        if row < 0:
            return
        trade_id = self.table.item(row, 0).data(Qt.UserRole)
        trades = get_trades(self.stock_code)
        trade = next((t for t in trades if t.id == trade_id), None)
        if trade is None:
            return

        dlg = TradeEditDialog(self.stock_code, self, trade)
        if dlg.exec_() == QDialog.Accepted:
            updated = dlg.get_trade()
            updated.id = trade_id
            update_trade(updated)
            self._load_trades()

    def _on_delete(self):
        """删除选中记录"""
        row = self.table.currentRow()
        if row < 0:
            return
        trade_id = self.table.item(row, 0).data(Qt.UserRole)

        reply = QMessageBox.question(
            self, "确认删除", "确定删除此交易记录?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            delete_trade(trade_id)
            self._load_trades()


class TradeEditDialog(QDialog):
    """交易记录编辑对话框"""

    def __init__(self, stock_code: str, parent=None, trade: Trade = None):
        super().__init__(parent)
        self.stock_code = stock_code
        self._trade = trade
        title = "编辑交易记录" if trade else "添加交易记录"
        self.setWindowTitle(f"{title} - {stock_code}")
        self.setMinimumWidth(350)

        layout = QFormLayout(self)

        # 日期
        self.date_edit = QDateEdit(QDate.currentDate())
        self.date_edit.setCalendarPopup(True)
        if trade:
            self.date_edit.setDate(QDate.fromString(trade.trade_date, "yyyy-MM-dd"))
        layout.addRow("日期:", self.date_edit)

        # 类型
        self.type_combo = QComboBox()
        self.type_combo.addItem("买入", TradeType.BUY.value)
        self.type_combo.addItem("卖出", TradeType.SELL.value)
        if trade and trade.trade_type == TradeType.SELL.value:
            self.type_combo.setCurrentIndex(1)
        layout.addRow("类型:", self.type_combo)

        # 价格
        self.price_edit = QLineEdit(str(trade.price) if trade else "")
        self.price_edit.setPlaceholderText("成交价格")
        layout.addRow("价格:", self.price_edit)

        # 数量
        self.qty_edit = QLineEdit(str(trade.quantity) if trade else "")
        self.qty_edit.setPlaceholderText("股数 (100的倍数)")
        layout.addRow("数量(股):", self.qty_edit)

        # 手续费
        self.fee_edit = QLineEdit(str(trade.fee) if trade else "0")
        layout.addRow("手续费:", self.fee_edit)

        # 备注
        self.notes_edit = QLineEdit(trade.notes if trade else "")
        layout.addRow("备注:", self.notes_edit)

        # 按钮
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _validate_and_accept(self):
        """验证输入"""
        try:
            price = float(self.price_edit.text().strip())
            qty = int(self.qty_edit.text().strip())
            fee = float(self.fee_edit.text().strip() or "0")

            if price <= 0:
                raise ValueError("价格必须大于0")
            if qty <= 0:
                raise ValueError("数量必须大于0")
            if fee < 0:
                raise ValueError("手续费不能为负")
        except ValueError as e:
            QMessageBox.warning(self, "输入错误", str(e))
            return

        self.accept()

    def get_trade(self) -> Trade:
        return Trade(
            stock_code=self.stock_code,
            trade_type=self.type_combo.currentData(),
            price=float(self.price_edit.text().strip()),
            quantity=int(self.qty_edit.text().strip()),
            fee=float(self.fee_edit.text().strip() or "0"),
            trade_date=self.date_edit.date().toString("yyyy-MM-dd"),
            notes=self.notes_edit.text().strip(),
        )
