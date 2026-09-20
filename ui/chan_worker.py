# -*- coding: utf-8 -*-
"""缠论买卖点标注的后台计算 worker

为什么单独起线程：算一只标的要 ① 联网取 30 分钟行情（新浪，约 1970 根）
② 建两套 czsc 对象（30 分钟 + 日线）。实测单只 2~3 秒，放在 UI 线程里就是
双击股票卡 2~3 秒。

2026-09-18 起替代原 `core/buy_point_scanner.py::BuyPointScanWorker`
（伪缠论买点扫描，已删除）。**同一套线程契约照旧**：
单飞（上一轮在跑就跳过）+ `finished` 接 `deleteLater`，
`tests/test_ui_threading.py` 把这套契约钉成回归用例。
"""

from __future__ import annotations

from PyQt5.QtCore import QThread, pyqtSignal

from core import chan_viz
from utils.logger import get_logger

logger = get_logger(__name__)


class ChanMarkWorker(QThread):
    """取 30 分钟行情 → 算缠论买卖点 → 回传图上标注

    signals
    -------
    ``marks_ready(code, marks)``
        `chan_viz.marks_from_klines()` 的返回，见其 docstring
    ``failed(code, message)``
        取数或计算失败。**不抛出** —— 网络问题不该让 UI 崩，
        main_window 只把消息显示到状态栏。
    """

    marks_ready = pyqtSignal(str, dict)
    failed = pyqtSignal(str, str)

    def __init__(self, code: str, klines=None, parent=None):
        super().__init__(parent)
        self.code = code
        # 注入行情（离线测试/复用已取好的数据）；None 表示自己联网取
        self._klines = klines
        self.period = "30min"

    def _compute(self) -> dict:
        """取数 + 计算（纯同步逻辑，便于离线测试直接调用）"""
        klines = self._klines
        if klines is None:
            klines = chan_viz.fetch_klines(self.code, self.period)
        return chan_viz.marks_from_klines(klines, code=self.code)

    def run(self):  # pragma: no cover - 线程体，逻辑都在 _compute
        try:
            marks = self._compute()
        except Exception as exc:
            logger.error(f"缠论买卖点计算失败 {self.code}: {exc}")
            self.failed.emit(self.code, str(exc))
            return
        self.marks_ready.emit(self.code, marks)
