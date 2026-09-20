"""全局配置常量

约定：凡标注 `[未接线]` 的常量，表示当前代码中**没有任何引用**
（2026-09-17 全量核实）。保留而不删除的原因：无法确定原作者是想预留
给未完成的功能，还是重构后的残留。删除是破坏信息的，故只做标注；
确认无用后再清理。
"""

# 数据刷新
REALTIME_REFRESH_MS = 60000         # 增量行情刷新间隔 (毫秒, 60秒)
KLINE_REFRESH_MS = 60000            # K线数据刷新间隔 (毫秒, 60秒, 仅当前查看的股票)
KLINE_FLUSH_INTERVAL_SEC = 300      # 内存K线 flush 到 DB 间隔 (秒, 5分钟)
KLINE_CACHE_TTL_SEC = 60            # 内存K线缓存TTL (秒) —— [未接线] 无引用
KLINE_INITIAL_MONTHS = 6            # 新股初始获取历史数据月数 —— [未接线] 无引用；
                                    #   实际取数窗口写死在 market_data_manager.py:271
                                    #   的 days_map = {"daily": 126, ...}（126 交易日 ≈ 6 个月）
DAILY_STOP_LOSS_HOUR = 15           # 每日止损更新时间 (15点收盘)
DAILY_STOP_LOSS_MINUTE = 5          # 收盘后5分钟触发

# 交易时间 (中国A股) —— 已接入 utils.is_trading_time()，改这里即生效
TRADING_START_MORNING = "09:30"
TRADING_END_MORNING = "11:30"
TRADING_START_AFTERNOON = "13:00"
TRADING_END_AFTERNOON = "15:00"

# 通达信行情服务器 (MOOTDX) —— 已接入，
# 见 data/market_data.py:182-187 与 :363-367
TDX_HOST = "58.251.33.227"
TDX_PORT = 443
TDX_TIMEOUT = 15

# 数据库
DB_PATH = "trading_assistant.db"

# 止损止盈
STOP_LOSS_DEFAULT = 0.0             # 默认止损价 (运行时计算) —— [未接线] 无引用
TAKE_PROFIT_LIMITUP_RATIO = 1.10    # 涨停止盈比例 (10%)
TOP_FRACTAL_LOOKBACK = 10           # 分型回溯K线数 —— [未接线] 无引用。
                                    #   原注释写「30分钟顶分型」，但分型检测实际读 60min 数据，
                                    #   且该常量未被使用。是否应接入 + 周期到底是哪个，
                                    #   已列入 docs/BUSINESS_RULES_CONFIRMATION.md Q1

# 买点扫描参数
# ⚠️ 2026-09-18 已删除伪缠论买点扫描链路（core/buy_point_scanner.py 及其
#    calc_center_range / check_pullback_to_center / is_volume_contraction
#    配套参数）。原判定是「高 75 分位 / 低 25 分位当中枢 + MACD 金叉 + 缩量」，
#    与新缠论口径（czsc 笔+中枢 + 几何判定，见 core/chan_points.py）无关。
#    当前买点由 core/chan_strategy.py 给出，只在 K 线图上标注，不做提醒。
GOLDEN_CROSS_LOOKBACK_DAYS = 3      # 金叉回溯天数（回测模块 core/backtest 使用）

# 图表
CHART_STYLE = "charles"             # mplfinance 样式 —— [未接线] 无引用。
                                    #   mplfinance 依赖已移除（chart_widget 为纯 matplotlib 手绘），
                                    #   该常量随之失效，可直接删除
MA_PERIODS = [5, 10, 20, 60]       # 均线周期
CHART_COLORS = {
    "up": "#DC143C",                # 中国红涨
    "down": "#008000",              # 绿跌
    "alert_stop_loss": "#FF4500",   # 止损警告色
    "alert_take_profit": "#FFD700", # 止盈警告色
    "ma_colors": ["#FFA500", "#00BFFF", "#FF69B4", "#9370DB"],  # MA线颜色
    "volume_up": "#DC143C",
    "volume_down": "#008000",
}

# 分组预设
PRESET_GROUPS = [
    ("持仓中", "holding"),
    ("已清仓", "cleared"),
    ("跟踪中", "tracking"),
]

# UI
WINDOW_TITLE = "A股交易辅助系统"
WINDOW_MIN_WIDTH = 1280
WINDOW_MIN_HEIGHT = 800
SIDEBAR_WIDTH = 180
# 列序与 ui/stock_table.py 的 COL_* 常量一一对应。
# 2026-09-18 删除「买点信号」列 —— 买点提醒已取消，改在 K 线图上标注。
STOCK_TABLE_COLUMNS = [
    "代码", "名称", "现价", "涨跌幅(%)", "涨跌额",
    "成交量(手)", "止损价", "止盈价", "提醒",
]
