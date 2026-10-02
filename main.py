"""A股交易辅助系统 — 入口文件"""

import sys
import os
import traceback

# pythonw.exe 下 sys.stdout/stderr 为 None，
# 第三方库（AKShare/tqdm 等）内部 print 会抛 'NoneType' object has no attribute 'write'
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5.QtWidgets import QApplication, QMessageBox
from PyQt5.QtCore import Qt, qInstallMessageHandler, QtMsgType

from utils.logger import get_logger

logger = get_logger(__name__)


def _global_exception_hook(exc_type, exc_value, exc_tb):
    """全局未捕获异常处理器 — 打印堆栈到日志+stderr"""
    tb_str = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    logger.critical(f"未捕获异常:\n{tb_str}")
    print(f"[FATAL] {tb_str}", file=sys.stderr)

    # 尝试弹窗通知用户
    try:
        QMessageBox.critical(
            None, "程序异常",
            f"发生未捕获的异常:\n\n{exc_value}\n\n详情已记录到日志文件。"
        )
    except Exception:
        pass

    # 调用默认处理
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def _qt_message_handler(msg_type, context, message):
    """Qt 内部消息处理器 — 将 Qt 警告/错误写入日志"""
    type_map = {
        QtMsgType.QtDebugMsg: "DEBUG",
        QtMsgType.QtInfoMsg: "INFO",
        QtMsgType.QtWarningMsg: "WARNING",
        QtMsgType.QtCriticalMsg: "CRITICAL",
        QtMsgType.QtFatalMsg: "FATAL",
    }
    level = type_map.get(msg_type, "UNKNOWN")
    logger.warning(
        f"Qt {level}: {message} "
        f"(file={context.file}, line={context.line}, func={context.function})"
    )


def main():
    # 注册全局异常钩子 (在任何可能出错的代码之前)
    sys.excepthook = _global_exception_hook
    qInstallMessageHandler(_qt_message_handler)

    app = QApplication(sys.argv)
    app.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app.setApplicationName("A股交易辅助系统")
    app.setOrganizationName("TradingAssistant")

    app.setQuitOnLastWindowClosed(False)

    from ui.main_window import MainWindow
    window = MainWindow()
    window.show()

    logger.info("A股交易辅助系统已启动")
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
