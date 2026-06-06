"""全局配置常量"""

# 数据刷新
REALTIME_REFRESH_MS = 60000         # 增量行情刷新间隔 (毫秒, 60秒)
BUYPOINT_SCAN_INTERVAL_MS = 300000  # 买点扫描间隔 (毫秒, 5分钟)
KLINE_REFRESH_MS = 60000            # K线数据刷新间隔 (毫秒, 60秒, 仅当前查看的股票)
KLINE_FLUSH_INTERVAL_SEC = 300      # 内存K线 flush 到 DB 间隔 (秒, 5分钟)
KLINE_CACHE_TTL_SEC = 60            # 内存K线缓存TTL (秒)
KLINE_INITIAL_MONTHS = 6            # 新股初始获取历史数据月数
DAILY_STOP_LOSS_HOUR = 15           # 每日止损更新时间 (15点收盘)
DAILY_STOP_LOSS_MINUTE = 5          # 收盘后5分钟触发

# 交易时间 (中国A股)
TRADING_START_MORNING = "09:30"
TRADING_END_MORNING = "11:30"
TRADING_START_AFTERNOON = "13:00"
TRADING_END_AFTERNOON = "15:00"

# 通达信行情服务器 (MOOTDX)
TDX_HOST = "58.251.33.227"
TDX_PORT = 443
TDX_TIMEOUT = 15

# 数据库
DB_PATH = "trading_assistant.db"

# 止损止盈
STOP_LOSS_DEFAULT = 0.0             # 默认止损价 (运行时计算)
TAKE_PROFIT_LIMITUP_RATIO = 1.10    # 涨停止盈比例 (10%)
TOP_FRACTAL_LOOKBACK = 10           # 30分钟顶分型回溯K线数

# 买点扫描
GOLDEN_CROSS_LOOKBACK_DAYS = 3      # 金叉回溯天数
VOLUME_CONTRACTION_RATIO = 0.7      # 缩量判断比例 (当前量 < 前5均量*0.7)
CENTER_LOOKBACK_WEEKS = 20          # 中枢回溯周数

# 图表
CHART_STYLE = "charles"             # mplfinance 样式
MA_PERIODS = [5, 10, 20, 60]       # 均线周期
CHART_COLORS = {
    "up": "#DC143C",                # 中国红涨
    "down": "#008000",              # 绿跌
    "alert_stop_loss": "#FF4500",   # 止损警告色
    "alert_take_profit": "#FFD700", # 止盈警告色
    "alert_buy_point": "#00CED1",   # 买点信号色
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
STOCK_TABLE_COLUMNS = [
    "代码", "名称", "现价", "涨跌幅(%)", "涨跌额",
    "成交量(手)", "止损价", "止盈价", "买点信号", "提醒",
]
