"""K线/分时图组件 — matplotlib 嵌入 PyQt5"""

import numpy as np
import pandas as pd

from PyQt5.QtWidgets import QWidget, QVBoxLayout, QTabWidget

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qt5agg import NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec
import matplotlib.font_manager as fm

from config import MA_PERIODS, CHART_COLORS
from data.models import KLineData


# ---- 中文字体配置 ----
def _get_chinese_font():
    """探测可用的中文字体，返回字体名"""
    available = {f.name for f in fm.fontManager.ttflist}
    candidates = ["Microsoft YaHei", "SimHei", "WenQuanYi Micro Hei",
                  "Noto Sans CJK SC", "Source Han Sans SC", "FangSong",
                  "KaiTi", "SimSun", "AR PL UMing CN"]
    for name in candidates:
        if name in available:
            return name
    return "sans-serif"


_CHINESE_FONT = _get_chinese_font()
plt.rcParams["font.sans-serif"] = [_CHINESE_FONT, "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


class MplCanvas(FigureCanvas):
    """Matplotlib 画布 — 支持鼠标滚轮缩放"""

    def __init__(self, figsize=(10, 6), dpi=100):
        self.fig = Figure(figsize=figsize, dpi=dpi, tight_layout=True)
        super().__init__(self.fig)
        self.setFocusPolicy(3)  # Qt.StrongFocus

    def wheelEvent(self, event):
        """滚轮缩放 X 轴，以鼠标位置为中心；Y 轴根据可见数据自适应"""
        axes = self.fig.get_axes()
        if not axes:
            return

        ax = axes[0]  # 所有子图 sharex，改一个即可
        xlim = ax.get_xlim()
        x_center = xlim[0] + (xlim[1] - xlim[0]) / 2

        # 将 Qt 鼠标坐标转为 matplotlib data 坐标
        try:
            x_display = event.pos().x()
            y_display = self.height() - event.pos().y()
            x_data, _ = ax.transData.inverted().transform((x_display, y_display))
            if xlim[0] <= x_data <= xlim[1]:
                x_center = x_data
        except (TypeError, ValueError, AttributeError):
            pass

        scale = 1.15
        if event.angleDelta().y() > 0:
            new_half = (xlim[1] - xlim[0]) / (2 * scale)
        else:
            new_half = (xlim[1] - xlim[0]) * scale / 2

        new_x0 = x_center - new_half
        new_x1 = x_center + new_half

        # 钳制到数据范围（留 2% 边距防止缩小时无响应）
        data_lim = ax.dataLim
        if data_lim.x0 < data_lim.x1:
            margin = max(1, (data_lim.x1 - data_lim.x0) * 0.02)
            if new_x0 < data_lim.x0 - margin:
                new_x0 = data_lim.x0 - margin
            if new_x1 > data_lim.x1 + margin:
                new_x1 = data_lim.x1 + margin

        ax.set_xlim(new_x0, new_x1)

        # Y 轴根据可见数据范围自适应
        for a in axes:
            self._autoscale_y(a, new_x0, new_x1)

        self.draw_idle()

    @staticmethod
    def _autoscale_y(ax, x0, x1):
        """根据 X 范围内可见数据的 Y 值自动调整 Y 轴"""
        if not hasattr(ax, '_y_mode'):
            return

        x_arr = np.asarray(ax._x_data)
        mask = (x_arr >= x0) & (x_arr <= x1)
        if not mask.any():
            return

        if ax._y_mode == 'volume' and hasattr(ax, '_y_data'):
            y_visible = np.asarray(ax._y_data)[mask]
            ymax = y_visible.max()
            ax.set_ylim(0, ymax * 1.2 if ymax > 0 else 1)
        elif ax._y_mode == 'price' and hasattr(ax, '_y_highs') and hasattr(ax, '_y_lows'):
            y_highs = np.asarray(ax._y_highs)[mask]
            y_lows = np.asarray(ax._y_lows)[mask]
            ymin, ymax = y_lows.min(), y_highs.max()
            pad = max((ymax - ymin) * 0.05, 0.01)
            ax.set_ylim(ymin - pad, ymax + pad)


class ChartNavigationToolbar(NavigationToolbar):
    """自定义导航工具栏 — 移除 Subplots 按钮避免误触弹窗"""

    def __init__(self, canvas, parent=None):
        super().__init__(canvas, parent)
        # 移除 "Subplots" (configure_subplots) action，避免双击误触
        for action in self.actions():
            if action.text() == 'Subplots':
                self.removeAction(action)
                break


class ChartTabWidget(QWidget):
    """单个图表页 (K线图)"""

    def __init__(self, period: str = "daily", parent=None):
        super().__init__(parent)
        self.period = period  # 'intraday', 'daily', 'weekly', 'monthly'
        self.code = ""
        self.klines: list[KLineData] = []
        self.intraday_data: list[dict] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.canvas = MplCanvas()
        self.toolbar = ChartNavigationToolbar(self.canvas, self)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.canvas)

    def load_data(self, code: str, force_reload: bool = False):
        """加载股票图表数据 — 直接从 Manager(DB+内存) 同步读取，不调 API"""
        from data.market_data_manager import get_data_manager
        manager = get_data_manager()

        # 同股票且已有数据 → 直接重绘
        if code == self.code and not force_reload:
            if self.period == "intraday" and self.intraday_data:
                self._draw_intraday()
                return
            elif self.klines:
                self._draw_kline()
                return

        self.code = code
        self.klines = []
        self.intraday_data = []

        if self.period == "intraday":
            # 从 klines_minute 表读最近 5 天的 1min 数据
            rows = manager.get_minute_klines_from_db(code, "1min")
            if rows:
                self.intraday_data = [
                    {"time": r["timestamp"], "date": r["timestamp"][:10],
                     "price": r["close"], "volume": r["volume"],
                     "avg_price": r["close"]}
                    for r in rows
                ]
                self._draw_intraday()
        else:
            # 从 Manager 读 K 线 (DB + 今日 bar)
            days_map = {"daily": 250, "weekly": 100, "monthly": 60}
            days = days_map.get(self.period, 250)
            klines = manager.get_klines(code, self.period, days)
            if klines:
                self.klines = klines
                self._draw_kline()

    # ================================================================
    # 图表绘制
    # ================================================================

    def _draw_intraday(self):
        """绘制分时图 — 价格/成交量上下合体，共用X轴，同步缩放"""
        self.canvas.fig.clear()

        if not self.intraday_data:
            self.canvas.draw()
            return

        times = [d["time"] for d in self.intraday_data]
        dates = [d.get("date", t[:10]) for d, t in zip(self.intraday_data, times)]
        prices = [d["price"] for d in self.intraday_data]
        avg_prices = [d["avg_price"] for d in self.intraday_data]
        volumes = [d["volume"] for d in self.intraday_data]
        n = len(prices)

        # 找出日期切换点 (day transition indices)
        day_boundaries = [0]
        day_labels = [dates[0]] if dates else []
        for i in range(1, n):
            if dates[i] != dates[i - 1]:
                day_boundaries.append(i)
                day_labels.append(dates[i])
        day_boundaries.append(n)

        # 只保留最近 5 天
        if len(day_labels) > 5:
            day_labels = day_labels[-5:]
            day_boundaries = day_boundaries[-5:]
            # 重切片数据
            start_idx = day_boundaries[0]
            times = times[start_idx:]
            dates = dates[start_idx:]
            prices = prices[start_idx:]
            avg_prices = avg_prices[start_idx:]
            volumes = volumes[start_idx:]
            n = len(prices)
            # 平移索引到从 0 开始
            day_boundaries = [b - start_idx for b in day_boundaries]
            x_all = list(range(n))
        else:
            x_all = list(range(n))

        # GridSpec 统一布局 — 上下子图共用X轴，缩放/平移同步
        gs = GridSpec(2, 1, figure=self.canvas.fig, height_ratios=[3, 1], hspace=0.05)
        ax1 = self.canvas.fig.add_subplot(gs[0])
        ax2 = self.canvas.fig.add_subplot(gs[1], sharex=ax1)

        ax1.plot(x_all, prices, color="#333333", linewidth=1.0, label="价格")
        ax1.plot(x_all, avg_prices, color="#FFA500",
                 linewidth=0.8, linestyle="--", label="均价")

        # 日期分隔线和标签
        tick_positions = [0]
        tick_labels = [day_labels[0][5:]]
        for di in range(1, len(day_labels)):
            boundary = day_boundaries[di]
            tick_positions.append(boundary)
            tick_labels.append(day_labels[di][5:])
            ax1.axvline(x=boundary - 0.5, color="#CCCCCC", linewidth=0.5, linestyle=":")
            ax2.axvline(x=boundary - 0.5, color="#CCCCCC", linewidth=0.5, linestyle=":")

        # 昨收线
        pre_close = 0
        if self.klines:
            pre_close = self.klines[-1].close
        elif prices:
            pre_close = prices[0]
        if pre_close > 0:
            ax1.axhline(y=pre_close, color="#999999", linewidth=0.5, linestyle="-.")

        ax1.set_ylabel("价格", fontfamily=_CHINESE_FONT)
        handles, labels = ax1.get_legend_handles_labels()
        if handles:
            ax1.legend(loc="upper left", fontsize=8,
                      prop=fm.FontProperties(family=_CHINESE_FONT, size=8))
        ax1.grid(True, alpha=0.3)
        # 上栏隐藏X轴标签，统一下栏显示
        ax1.tick_params(axis="x", labelbottom=False)

        # 下栏: 成交量
        colors_vol = ["#DC143C" if i > 0 and prices[i] >= prices[i - 1]
                      else "#008000" for i in range(n)]
        ax2.bar(range(n), volumes, color=colors_vol, width=1.0)

        ax2.set_ylabel("成交量", fontfamily=_CHINESE_FONT)
        ax2.set_xticks(tick_positions)
        ax2.set_xticklabels(tick_labels, fontsize=8, fontfamily=_CHINESE_FONT)
        ax2.grid(True, alpha=0.3)

        ax1.set_title(f"{self.code} 分时图", fontsize=12, fontweight="bold",
                      fontfamily=_CHINESE_FONT)

        # 存储数据引用，供滚轮缩放时 Y 轴自适应使用
        x_data = np.arange(n)
        ax1._x_data = x_data
        ax1._y_highs = np.array(prices)
        ax1._y_lows = np.array(prices)  # 分时图 highs=lows=prices
        ax1._y_mode = "price"
        ax2._x_data = x_data
        ax2._y_data = np.array(volumes)
        ax2._y_mode = "volume"

        self.canvas.draw()

    def _draw_kline(self):
        """绘制K线图 (日线/周线/月线) — 纯手工绘制，无 mplfinance 依赖"""
        self.canvas.fig.clear()

        if not self.klines:
            self.canvas.draw()
            return

        # 转换为DataFrame
        data = {
            "Date": pd.to_datetime([k.date for k in self.klines]),
            "Open": [k.open for k in self.klines],
            "High": [k.high for k in self.klines],
            "Low": [k.low for k in self.klines],
            "Close": [k.close for k in self.klines],
            "Volume": [k.volume for k in self.klines],
        }
        df = pd.DataFrame(data)
        df.set_index("Date", inplace=True)

        period_names = {
            "daily": "日线", "weekly": "周线", "monthly": "月线",
            "60min": "60分钟线",
        }
        title = f"{self.code} {period_names.get(self.period, self.period)}K线图"

        self._draw_kline_manual(df, title)
        self.canvas.draw()

    def _draw_kline_manual(self, df: pd.DataFrame, title: str):
        """手工绘制K线图 — 价格/成交量上下合体，共用X轴同步缩放"""
        # GridSpec 统一布局 — 3:1 高度比，hspace=0 消除间隙
        gs = GridSpec(2, 1, figure=self.canvas.fig, height_ratios=[3, 1], hspace=0.05)
        ax1 = self.canvas.fig.add_subplot(gs[0])
        ax2 = self.canvas.fig.add_subplot(gs[1], sharex=ax1)

        n = len(df)
        # 自适应蜡烛宽度
        width = max(0.3, min(0.8, 200.0 / max(n, 1)))

        # --- 上栏: K线 + MA ---
        for i, (_idx, row) in enumerate(df.iterrows()):
            color = CHART_COLORS["up"] if row["Close"] >= row["Open"] else CHART_COLORS["down"]
            ax1.plot([i, i], [row["Low"], row["High"]], color=color, linewidth=0.8)
            body_bottom = min(row["Open"], row["Close"])
            body_height = abs(row["Close"] - row["Open"])
            if body_height < 0.0001:
                body_height = max(row["High"] - row["Low"], 0.001)
            ax1.bar(i, body_height, width=width, bottom=body_bottom, color=color, alpha=0.9)

        # MA线
        for period in MA_PERIODS:
            if len(df) >= period:
                ma = df["Close"].rolling(window=period).mean()
                color_idx = MA_PERIODS.index(period) % len(CHART_COLORS["ma_colors"])
                ax1.plot(range(len(df)), ma.values,
                        color=CHART_COLORS["ma_colors"][color_idx],
                        linewidth=1.0, label=f"MA{period}", alpha=0.8)

        # 止损止盈线
        if hasattr(self, 'stop_loss_price') and self.stop_loss_price > 0:
            ax1.axhline(y=self.stop_loss_price, color=CHART_COLORS["alert_stop_loss"],
                       linestyle="--", linewidth=1.0, label=f"止损 {self.stop_loss_price:.2f}")
        if hasattr(self, 'take_profit_price') and self.take_profit_price > 0:
            ax1.axhline(y=self.take_profit_price, color=CHART_COLORS["alert_take_profit"],
                       linestyle="--", linewidth=1.0, label=f"止盈 {self.take_profit_price:.2f}")

        # 底分型标注
        if hasattr(self, 'bottom_fractal_indices') and self.bottom_fractal_indices:
            for bf_idx in self.bottom_fractal_indices:
                if 0 <= bf_idx < len(df):
                    low = df.iloc[bf_idx]["Low"]
                    ax1.scatter(bf_idx, low * 0.98, marker="^", color="blue", s=80, zorder=5)

        ax1.set_title(title, fontsize=12, fontweight="bold", fontfamily=_CHINESE_FONT)
        ax1.set_ylabel("价格", fontfamily=_CHINESE_FONT)
        ax1.legend(loc="upper left", fontsize=7, ncol=2,
                  prop=fm.FontProperties(family=_CHINESE_FONT, size=7))
        ax1.grid(True, alpha=0.3)
        # 上栏隐藏X轴标签，统一下栏显示
        ax1.tick_params(axis="x", labelbottom=False)

        # x轴标签 — 根据周期选择日期格式
        if self.period == "monthly":
            date_fmt = "%Y-%m"
        elif self.period == "weekly":
            date_fmt = "%m-%d"
        else:
            date_fmt = "%Y-%m-%d"

        step = max(1, n // 10)
        tick_idx = list(range(0, n, step))
        tick_labels = []
        for i in tick_idx:
            idx_val = df.index[i]
            if hasattr(idx_val, 'strftime'):
                tick_labels.append(idx_val.strftime(date_fmt))
            else:
                tick_labels.append(str(idx_val)[:10])

        # --- 下栏: 成交量 ---
        colors_vol = [CHART_COLORS["volume_up"] if row["Close"] >= row["Open"]
                      else CHART_COLORS["volume_down"] for _, row in df.iterrows()]
        ax2.bar(range(n), df["Volume"].values, color=colors_vol, width=width, alpha=0.7)
        ax2.set_ylabel("成交量", fontfamily=_CHINESE_FONT)
        ax2.set_xticks(tick_idx)
        ax2.set_xticklabels(tick_labels, rotation=30, fontsize=7,
                           fontfamily=_CHINESE_FONT)
        ax2.grid(True, alpha=0.3)

        # 存储数据引用，供滚轮缩放时 Y 轴自适应使用
        x_data = np.arange(n)
        ax1._x_data = x_data
        ax1._y_highs = df["High"].values
        ax1._y_lows = df["Low"].values
        ax1._y_mode = "price"
        ax2._x_data = x_data
        ax2._y_data = df["Volume"].values
        ax2._y_mode = "volume"

    # 外部接口
    def set_alert_lines(self, stop_loss: float, take_profit: float):
        """设置止损止盈显示线"""
        self.stop_loss_price = stop_loss
        self.take_profit_price = take_profit

    def set_bottom_fractals(self, indices: list[int]):
        """设置底分型位置"""
        self.bottom_fractal_indices = indices


class ChartWidget(QWidget):
    """图表组件 (含周期切换标签)"""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.tabs = QTabWidget()
        self.intraday_tab = ChartTabWidget("intraday")
        self.daily_tab = ChartTabWidget("daily")
        self.weekly_tab = ChartTabWidget("weekly")
        self.monthly_tab = ChartTabWidget("monthly")

        self.tabs.addTab(self.intraday_tab, "分时")
        self.tabs.addTab(self.daily_tab, "日线")
        self.tabs.addTab(self.weekly_tab, "周线")
        self.tabs.addTab(self.monthly_tab, "月线")

        layout.addWidget(self.tabs)

    def load_stock(self, code: str):
        """加载股票全部周期数据 (首次打开时)"""
        self.intraday_tab.load_data(code)
        self.daily_tab.load_data(code)
        self.weekly_tab.load_data(code)
        self.monthly_tab.load_data(code)

    def refresh_current_tab(self, code: str):
        """定时刷新当前Tab — 日线/周线/月线从Manager内存取，分时重绘已有数据"""
        current = self.tabs.currentWidget()
        if not current or not hasattr(current, 'period'):
            return

        if current.period == "intraday":
            # 分时: 强制重拉(盘中数据实时变化)
            current.load_data(code, force_reload=True)
        elif current.klines:
            # K线: 从Manager读最新缓存(内存) → 重绘
            from data.market_data_manager import get_data_manager
            manager = get_data_manager()
            days_map = {"daily": 250, "weekly": 100, "monthly": 60}
            days = days_map.get(current.period, 250)
            updated = manager.get_klines(code, current.period, days)
            if updated:
                current.klines = updated
                current._draw_kline()

    def set_alert_lines(self, stop_loss: float, take_profit: float):
        """设置所有周期图表的止损止盈线"""
        for tab in [self.intraday_tab, self.daily_tab, self.weekly_tab, self.monthly_tab]:
            tab.set_alert_lines(stop_loss, take_profit)
