# 技术文档

本文档面向刚加入项目的开发者，记录当前工程结构、运行数据流、数据库结构、主要类和函数。对业务口径不确定的地方不做猜测，应向项目维护者或原开发者确认。

## 1. 总体架构

运行入口是 [main.py](../main.py)。启动流程：

1. 注册全局 Python 异常钩子和 Qt 日志处理器。
2. 创建 `QApplication`。
3. 延迟导入并实例化 `ui.main_window.MainWindow`。
4. `MainWindow.__init__()` 调用 `init_db()` 初始化数据库。
5. 构建菜单、主界面、系统托盘和定时器。
6. 从数据库加载股票和历史行情缓存；交易时段再触发 AKShare 增量刷新。

核心依赖方向：

```txt
ui -> core -> data -> utils/config
ui -> data
tests -> core/data/ui
```

主要数据流：

```txt
AKShare
  -> data.market_data
  -> data.market_data_manager
  -> SQLite trading_assistant.db
  -> core.alert_engine / core.buy_point_scanner / ui.chart_widget / ui.stock_table
```

## 2. 目录职责

| 路径 | 职责 |
| --- | --- |
| `main.py` | GUI 程序入口，注册异常处理，启动主窗口。 |
| `config.py` | 刷新间隔、交易时间、数据库路径、图表颜色、预设分组等全局常量。 |
| `resources/` | 静态资源。目前只有 `discipline.txt` 交易纪律文本；代码还引用了未提交的 `resources/icons/app.png`。 |
| `data/` | 数据模型、SQLite CRUD、AKShare 封装、行情缓存管理器。 |
| `core/` | 技术指标、止损止盈、买点扫描、做 T 模型/模拟器。 |
| `ui/` | PyQt5 窗口、表格、图表和对话框。 |
| `utils/` | 日志、TTL 缓存、交易时间工具函数。 |
| `tests/` | pytest 测试。包含单元测试、部分 GUI/Worker 测试和真实网络请求测试。 |
| `docs/` | 接手后新增文档。 |

## 3. 数据库

数据库路径由 `config.DB_PATH` 决定，默认位于项目根目录：

```txt
trading_assistant.db
```

连接由 `data.database._connect()` 创建：

- `row_factory = sqlite3.Row`
- `PRAGMA foreign_keys = ON`
- `PRAGMA journal_mode = WAL`
- `PRAGMA busy_timeout = 5000`

### 3.1 表结构

| 表 | 作用 | 关键字段 |
| --- | --- | --- |
| `groups` | 股票分组 | `id`, `name`, `type`, `sort_order` |
| `stocks` | 分组中的股票 | `id`, `code`, `name`, `group_id`, `added_date`, `UNIQUE(code, group_id)` |
| `trades` | 买卖交易记录 | `id`, `stock_code`, `trade_type`, `price`, `quantity`, `fee`, `trade_date`, `notes` |
| `alerts_disabled` | 关闭提醒记录 | `stock_code`, `alert_type`, `disabled_at` |
| `settings` | 通用键值设置 | `key`, `value` |
| `discipline_rules` | 交易纪律文本 | `id`, `stock_code`, `rule_text` |
| `stock_names` | 股票代码名称缓存 | `code`, `name`, `updated_at` |
| `klines` | K 线缓存 | `code`, `date`, `open`, `high`, `low`, `close`, `volume`, `period`, `UNIQUE(code, date, period)` |

## 4. 配置文件

### `config.py`

只定义常量，无函数。

| 常量 | 用途 |
| --- | --- |
| `REALTIME_REFRESH_MS` | 主窗口增量行情刷新间隔。 |
| `BUYPOINT_SCAN_INTERVAL_MS` | 买点扫描间隔。 |
| `KLINE_REFRESH_MS` | 当前图表刷新间隔。 |
| `KLINE_FLUSH_INTERVAL_SEC` | 内存今日 bar 写回数据库的间隔。 |
| `KLINE_CACHE_TTL_SEC` | K 线内存缓存 TTL。 |
| `KLINE_INITIAL_MONTHS` | 新股初始历史数据月数；当前代码实际使用 `days_map`，未直接使用该常量。 |
| `DAILY_STOP_LOSS_HOUR/MINUTE` | 收盘后每日止损更新时间。 |
| `TRADING_*` | A 股交易时间字符串；当前 `utils.is_trading_time()` 使用硬编码时间对象，未引用这些常量。 |
| `DB_PATH` | SQLite 数据库文件名。 |
| `TAKE_PROFIT_LIMITUP_RATIO` | 初始止盈比例。 |
| `TOP_FRACTAL_LOOKBACK` | 顶分型回溯配置；当前未直接使用。 |
| `GOLDEN_CROSS_LOOKBACK_DAYS` | MACD 金叉回溯天数。 |
| `VOLUME_CONTRACTION_RATIO` | 缩量阈值。 |
| `CENTER_LOOKBACK_WEEKS` | 中枢回溯配置。 |
| `CHART_STYLE`, `MA_PERIODS`, `CHART_COLORS` | 图表样式配置。 |
| `PRESET_GROUPS` | 初始化数据库时创建的预设分组。 |
| `WINDOW_*`, `SIDEBAR_WIDTH`, `STOCK_TABLE_COLUMNS` | UI 尺寸和表格列。 |

## 5. 数据模型

### `data/models.py`

| 类型 | 结构 | 用法 |
| --- | --- | --- |
| `GroupType` | `holding`, `cleared`, `tracking`, `custom` | 分组类型枚举。 |
| `TradeType` | `buy`, `sell` | 交易方向枚举。 |
| `AlertType` | `stop_loss`, `take_profit`, `buy_point`, `all` | 提醒类型枚举。 |
| `Group` | `id`, `name`, `type`, `sort_order` | 映射 `groups` 行。 |
| `Stock` | `id`, `code`, `name`, `group_id`, `added_date` | 映射 `stocks` 行。 |
| `Trade` | `id`, `stock_code`, `trade_type`, `price`, `quantity`, `fee`, `trade_date`, `notes` | 映射交易记录。 |
| `AlertDisabled` | `stock_code`, `alert_type`, `disabled_at` | 映射关闭提醒记录。 |
| `DisciplineRule` | `id`, `stock_code`, `rule_text` | 映射交易纪律。 |
| `RealtimeQuote` | `code`, `name`, `price`, `change_pct`, `change_amt`, `volume`, `turnover`, `high`, `low`, `open`, `pre_close`, `timestamp` | 行情快照。 |
| `KLineData` | `code`, `date`, `open`, `high`, `low`, `close`, `volume`, `period` | K 线数据。 |
| `AlertState` | 止损价、止盈价、触发状态、手动覆写状态 | `AlertEngine` 内存状态。 |
| `BuyPointState` | 三个买点条件、综合触发、详情、检查时间 | `BuyPointScanner` 内存状态。 |

## 6. 入口与工具

### `main.py`

| 函数 | 输入 | 返回 | 说明 |
| --- | --- | --- | --- |
| `_global_exception_hook(exc_type, exc_value, exc_tb)` | Python 异常类型、异常对象、traceback | `None` | 将未捕获异常写入日志和 stderr，并尝试弹窗。 |
| `_qt_message_handler(msg_type, context, message)` | Qt 消息类型、上下文、消息文本 | `None` | 把 Qt 内部消息写入 Python 日志。 |
| `main()` | 无 | 不返回；调用 `sys.exit(app.exec_())` | 创建 QApplication 和 MainWindow。 |

### `utils/logger.py`

| 函数 | 输入 | 返回 | 说明 |
| --- | --- | --- | --- |
| `get_logger(name="trading")` | 日志器名称 | `logging.Logger` | 返回 `trading` 根日志器或子日志器。模块导入时会创建 `logs/` 和滚动日志 handler。 |

### `utils/cache.py`

| 类/函数 | 输入 | 返回 | 说明 |
| --- | --- | --- | --- |
| `TTLCache(default_ttl=30.0)` | 默认 TTL 秒数 | 缓存实例 | 线程安全的内存 TTL 缓存。 |
| `TTLCache.get(key)` | 字符串 key | 命中值或 `None` | 过期时删除并计 miss。 |
| `TTLCache.set(key, value, ttl=None)` | key、值、可选 TTL | `None` | 写入缓存。 |
| `TTLCache.delete(key)` | key | `None` | 删除单项。 |
| `TTLCache.clear()` | 无 | `None` | 清空缓存。 |
| `TTLCache.stats()` | 无 | `dict` | 返回 entries/hits/misses/hit_rate。 |
| `get_kline_cache()` | 无 | `TTLCache` | 全局 K 线缓存。 |
| `get_search_cache()` | 无 | `TTLCache` | 全局搜索缓存。 |
| `get_30min_cache()` | 无 | `TTLCache` | 全局分钟线缓存。 |
| `cached(ttl=30.0)` | TTL | 装饰器 | 使用全局 K 线缓存按函数参数缓存结果；当前项目未使用。 |

### `utils/__init__.py`

| 函数 | 输入 | 返回 | 说明 |
| --- | --- | --- | --- |
| `is_trading_time()` | 无 | `bool` | 判断当前本机时间是否为周一至周五 09:30-11:30 或 13:00-15:00。 |

## 7. 数据层

### `data/database.py`

| 函数 | 输入 | 返回 | 说明 |
| --- | --- | --- | --- |
| `_get_path()` | 无 | `str` | 返回项目根目录下的数据库绝对路径。 |
| `_connect()` | 无 | `sqlite3.Connection` | 创建带 WAL 和 busy_timeout 的连接。 |
| `init_db()` | 无 | `None` | 创建表、索引和预设分组。 |
| `get_all_groups()` | 无 | `list[Group]` | 按 `sort_order,id` 返回所有分组。 |
| `add_group(name, gtype="custom")` | 分组名、类型 | `Group` | 新增分组，排序号为当前最大值 + 1。 |
| `update_group(group_id, name)` | 分组 ID、新名称 | `None` | 修改分组名称。 |
| `delete_group(group_id)` | 分组 ID | `None` | 只删除 `type='custom'` 的分组。 |
| `get_stocks_by_group(group_id)` | 分组 ID | `list[Stock]` | 返回指定分组股票。 |
| `get_all_stocks()` | 无 | `list[Stock]` | 返回所有分组中的股票。 |
| `add_stock(code, name, group_id)` | 股票代码、名称、分组 ID | `Stock` 或 `None` | 插入股票；同分组重复时返回 `None`。 |
| `remove_stock(stock_id)` | 股票记录 ID | `None` | 删除分组中的股票记录。 |
| `move_stock(stock_id, new_group_id)` | 股票记录 ID、目标分组 ID | `None` | 移动股票；若目标分组已有同代码，先删除目标记录。 |
| `get_stock_by_code_group(code, group_id)` | 代码、分组 ID | `Stock` 或 `None` | 查询某分组中的股票。 |
| `get_trades(stock_code)` | 股票代码 | `list[Trade]` | 按日期返回交易记录。 |
| `get_all_trades()` | 无 | `list[Trade]` | 返回所有交易记录。 |
| `add_trade(trade)` | `Trade` | `Trade` | 插入交易并回填 `trade.id`。 |
| `update_trade(trade)` | `Trade` | `None` | 按 `trade.id` 更新交易。 |
| `delete_trade(trade_id)` | 交易 ID | `None` | 删除交易记录。 |
| `get_position_summary(stock_code)` | 股票代码 | `dict` | 统计持仓数量、平均成本、买卖金额和买卖数量。 |
| `get_first_buy_date(stock_code)` | 股票代码 | `str` 或 `None` | 返回最早买入日期。 |
| `is_alert_disabled(stock_code, alert_type="all")` | 股票代码、提醒类型 | `bool` | 查询提醒是否关闭。 |
| `disable_alert(stock_code, alert_type="all")` | 股票代码、提醒类型 | `None` | 插入或替换关闭提醒记录。 |
| `enable_alert(stock_code, alert_type="all")` | 股票代码、提醒类型 | `None` | 删除对应关闭提醒记录。 |
| `get_discipline_rule(stock_code="")` | 股票代码，空字符串表示全局 | `DisciplineRule` 或 `None` | 查询纪律文本。当前非空股票代码不会自动回退到全局规则。 |
| `save_discipline_rule(stock_code, rule_text)` | 股票代码、文本 | `DisciplineRule` | 删除同代码旧规则后插入新规则。 |
| `get_setting(key, default="")` | key、默认值 | `str` | 查询设置值。 |
| `set_setting(key, value)` | key、值 | `None` | upsert 设置值。 |
| `get_manual_alert(code)` | 股票代码 | `dict` | 返回手动止损/止盈激活状态和价格。 |
| `set_manual_alert(code, sl_active, sl_price, tp_active, tp_price)` | 股票代码、两组开关和价格 | `None` | 写入手动止损/止盈设置。 |
| `clear_manual_alert(code, field="all")` | 股票代码、`sl`/`tp`/`all` | `None` | 清除手动设置。 |
| `get_stock_name(code)` | 股票代码 | `str` 或 `None` | 查询本地股票名称。 |
| `search_stock_names(keyword, limit=20)` | 关键字、条数 | `list[dict]` | 本地模糊搜索代码或名称。 |
| `get_stock_names_count()` | 无 | `int` | 返回本地名称缓存数量。 |
| `save_stock_names_batch(names)` | `[{code,name}]` | `int` | 批量 upsert 股票名称。 |
| `update_stock_name(code, name)` | 代码、名称 | `None` | upsert 单条股票名称。 |
| `save_klines_batch(klines)` | `list[dict]` | `int` | 批量 upsert K 线。 |
| `get_klines(code, period="daily", days=None, start_date=None, end_date=None)` | 代码、周期、限制条件 | `list[dict]` | 从 DB 按日期升序返回 K 线。 |
| `get_latest_kline_date(code, period="daily")` | 代码、周期 | `str` 或 `None` | 返回最新 K 线日期。 |
| `get_kline_count(code="", period="daily")` | 可选代码和周期 | `int` | 统计 K 线数量。 |

### `data/market_data.py`

| 函数/类 | 输入 | 返回 | 说明 |
| --- | --- | --- | --- |
| `_safe_float(val, default=0.0)` | 任意值、默认值 | `float` | 安全转换浮点数。 |
| `_safe_int(val, default=0)` | 任意值、默认值 | `int` | 安全转换整数。 |
| `_add_market_prefix(code)` | 股票代码 | `str` | 给 A 股代码加新浪前缀：`sz`/`sh`/`bj`。 |
| `fetch_kline(code, period="daily", days=250)` | 代码、周期、条数 | `list[KLineData]` | 从 AKShare 获取日线；周线/月线由日线 resample 聚合。带 TTL 缓存。 |
| `_df_to_klines(df, code, period)` | DataFrame、代码、周期 | `list[KLineData]` | 将行情 DataFrame 转为模型对象。 |
| `fetch_30min_kline(code, days=60)` | 代码、天数 | `list[KLineData]` | 名称为 30min，但当前实际请求 AKShare `period="60"` 并返回 `period="60min"`。 |
| `fetch_intraday_data(code)` | 代码 | `list[dict]` | 获取分时数据，返回 time/date/price/volume/avg_price。 |
| `sync_stock_names_from_api()` | 无 | `int` | 从 AKShare 同步全市场股票代码名称到本地 DB。 |
| `_ensure_stock_names_table()` | 无 | `None` | 兼容旧 DB，确保 `stock_names` 表存在。 |
| `search_stock(keyword)` | 关键字 | `list[dict]` | 本地搜索优先；本地缺失时回退 API 搜索。 |
| `_search_stock_from_api(keyword)` | 关键字 | `list[dict]` | 实时拉全市场名称后过滤；失败三次抛 `RuntimeError`。 |
| `KLineWorker(code, period="daily", days=250)` | 代码、周期、条数 | QThread | 异步拉 K 线，信号 `data_ready(code, period, list)` 或 `error_occurred(str)`。 |
| `IntradayWorker(code)` | 代码 | QThread | 异步拉分时数据，信号 `data_ready(code, list)`。 |
| `StockSearchWorker(keyword)` | 关键字 | QThread | 异步搜索股票，信号 `data_ready(list)`。 |
| `fetch_single_stock_quote(code)` | 代码 | `RealtimeQuote` 或 `None` | 拉最近几天日线，提取最新价格和涨跌幅。 |
| `IncrementalRefreshWorker(codes)` | 股票代码列表 | QThread | 批量增量刷新，信号 `stock_done(code, quote)` 和 `data_ready(dict)`。 |
| `InitialFetchWorker(code)` | 股票代码 | QThread | 新增股票时获取日/周/月初始 K 线并写 DB。 |

### `data/market_data_manager.py`

| 函数/类 | 输入 | 返回 | 说明 |
| --- | --- | --- | --- |
| `_dict_to_kline(d)` | DB dict | `KLineData` | DB 行转模型对象。 |
| `_dicts_to_klines(dicts)` | DB dict 列表 | `list[KLineData]` | 批量转换。 |
| `MarketDataManager` | 无 | 管理器实例 | 管理现价、今日 bar、K 线缓存和初始获取 pending 状态。 |
| `update_quotes(quotes)` | `{code: RealtimeQuote}` | `None` | 批量更新现价缓存。 |
| `get_quote(code)` | 代码 | `RealtimeQuote` 或 `None` | 获取单股现价。 |
| `get_all_quotes()` | 无 | `dict[str, RealtimeQuote]` | 获取现价快照副本。 |
| `startup_load_quotes(codes)` | 代码列表 | `int` | 启动时从 DB 日线末尾恢复现价。 |
| `update_today_bar(code, period, bar)` | 代码、周期、bar dict | `None` | 更新内存今日 bar 并使缓存失效。 |
| `get_today_bar(code, period)` | 代码、周期 | `dict` 或 `None` | 获取今日 bar。 |
| `_invalidate_kline_cache(code, period=None)` | 代码、可选周期 | `None` | 清理指定股票 K 线缓存。 |
| `get_klines(code, period="daily", days=None, force_refresh=False)` | 代码、周期、条数、强刷 | `list[KLineData]` | 从内存缓存或 DB + 今日 bar 构造 K 线。 |
| `_build_klines(code, period, days)` | 代码、周期、条数 | `list[KLineData]` | 从 DB 加载并拼接今日 bar。 |
| `is_pending(code)` | 代码 | `bool` | 判断是否在初始获取中。 |
| `mark_pending(code)` | 代码 | `None` | 标记初始获取中。 |
| `unmark_pending(code)` | 代码 | `None` | 移除 pending 标记。 |
| `fetch_and_store_initial(code, kline_callback=None)` | 代码、可选回调 | `dict` | 并行拉日/周/月 K 线，串行写 DB，并更新现价缓存。 |
| `refresh_quote(code)` | 代码 | `RealtimeQuote` 或 `None` | 拉最新行情，更新今日 bar 和现价缓存。 |
| `refresh_quotes_batch(codes)` | 代码列表 | `dict[str, RealtimeQuote]` | 并行刷新多只股票。 |
| `flush_today_bars()` | 无 | `int` | 将内存今日 bar 写入 DB。 |
| `should_flush(interval_seconds=300.0)` | 间隔秒数 | `bool` | 判断是否需要 flush。 |
| `get_pending_codes()` | 无 | `set[str]` | 返回 pending 代码集合。 |
| `get_data_manager()` | 无 | `MarketDataManager` | 全局单例入口。 |

## 8. 核心算法

### `core/technical.py`

| 函数 | 输入 | 返回 | 说明 |
| --- | --- | --- | --- |
| `calc_ma(closes, period)` | 收盘价数组、周期 | `np.ndarray` | 简单移动平均线，不足周期的位置为 NaN。 |
| `calc_ema(closes, period)` | 收盘价数组、周期 | `np.ndarray` | 指数移动平均线。 |
| `calc_macd(closes, fast=12, slow=26, signal=9)` | 收盘价数组、参数 | `(dif, dea, macd_bar)` | 计算 MACD；长度不足 slow 时返回 NaN 数组。 |
| `_merge_contains(highs, lows)` | 最高价、最低价数组 | `(list, list)` | 缠论 K 线包含处理。 |
| `detect_top_fractal(highs, lows)` | 最高价、最低价数组 | `list[int]` | 检测顶分型索引。 |
| `detect_bottom_fractal(highs, lows)` | 最高价、最低价数组 | `list[int]` | 检测底分型索引。 |
| `get_latest_top_fractal(highs, lows)` | 最高价、最低价数组 | `(bool, int, float)` | 返回最近顶分型是否存在、索引、最高价。 |
| `get_latest_bottom_fractal(highs, lows)` | 最高价、最低价数组 | `(bool, int)` | 返回最近底分型是否存在和索引。 |
| `detect_golden_cross(closes, fast_period=5, slow_period=10, lookback=3)` | 收盘价、均线参数 | `(bool, int)` | 检测 SMA 金叉。 |
| `detect_macd_golden_cross(closes, fast=12, slow=26, signal=9, lookback=3)` | 收盘价、MACD 参数 | `(bool, int)` | 检测 DIF 上穿 DEA。 |
| `detect_death_cross(closes, fast_period=5, slow_period=10, lookback=3)` | 收盘价、均线参数 | `(bool, int)` | 检测 SMA 死叉。 |
| `calc_center_range(highs, lows, lookback=20)` | 最高价、最低价、回看长度 | `(float, float)` | 用分位数近似计算中枢上沿/下沿。 |
| `check_pullback_to_center(close, center_high, center_low, tolerance=0.02)` | 当前价、中枢上下沿、容差 | `bool` | 判断价格是否回踩中枢区间。 |
| `is_volume_contraction(volumes, period=5, ratio=0.7)` | 成交量数组、周期、比例 | `bool` | 判断最近一根是否缩量。 |
| `is_volume_expansion(volumes, period=5, ratio=1.5)` | 成交量数组、周期、比例 | `bool` | 判断最近一根是否放量。 |
| `kline_to_arrays(kline_list)` | `list[KLineData]` | `dict[str, np.ndarray]` | 转换为 dates/opens/highs/lows/closes/volumes 数组。 |
| `find_stop_loss_price(daily_lows, prev_stop=0.0)` | 日线最低价数组、旧止损 | `float` | 返回 `max(prev_stop, latest_low)`；无旧止损时返回最新最低价。 |

### `core/alert_engine.py`

| 方法 | 输入 | 返回 | 说明 |
| --- | --- | --- | --- |
| `AlertEngine()` | 无 | 引擎实例 | 维护 `_states: dict[str, AlertState]`。 |
| `get_state(code)` | 股票代码 | `AlertState` | 首次创建时从 DB 恢复手动止损/止盈设置。 |
| `set_manual_sl(code, price)` | 代码、价格 | `None` | 设置内存状态并持久化手动止损。 |
| `set_manual_tp(code, price)` | 代码、价格 | `None` | 设置内存状态并持久化手动止盈。 |
| `clear_manual(code, field="all")` | 代码、`sl`/`tp`/`all` | `None` | 清除手动设置并恢复自动模式。 |
| `calc_stop_loss(code, current_daily_low)` | 代码、当前日低点 | `(float, dict|None)` | 计算自动止损；手动模式直接返回手动值。 |
| `calc_take_profit(code, current_price)` | 代码、当前价 | `(float, dict|None)` | 计算自动止盈；含 60 秒分钟线顶分型检测限流。 |
| `check_alerts(code, quote)` | 代码、`RealtimeQuote` | `dict` | 判断止损/止盈是否触发，返回触发状态、类型、价格、消息。 |
| `update_daily_stop_loss(code)` | 代码 | `(float, dict|None)` | 收盘后用日线更新止损。 |

### `core/buy_point_scanner.py`

| 类/方法 | 输入 | 返回 | 说明 |
| --- | --- | --- | --- |
| `BuyPointScanner()` | 无 | 扫描器实例 | 维护 `_states: dict[str, BuyPointState]`。 |
| `get_state(code)` | 股票代码 | `BuyPointState` | 获取或创建状态。 |
| `scan(code, callback=None)` | 股票代码、可选回调 | `BuyPointState` | 计算三项买点条件，满足两项触发。 |
| `_check_weekly_bottom_fractal(code)` | 代码 | `bool` | 使用周线 K 线检测底分型。 |
| `_check_daily_macd_golden_cross(code)` | 代码 | `bool` | 使用日线检测 MACD 金叉并检查成交量。 |
| `_check_shallow_pullback(code)` | 代码 | `bool` | 使用短周期 K 线判断缩量回踩中枢。 |
| `BuyPointScanWorker(code)` | 股票代码 | QThread | 后台扫描单只股票。 |
| `BuyPointScanWorker.run()` | 无 | `None` | 调用 `BuyPointScanner.scan()`，通过 `scan_done` 发回结果。异常分支存在未定义变量 BUG。 |
| `BuyPointScanWorker._on_result(code, result)` | 代码、结果 dict | `None` | 把同步回调转发为 Qt 信号。 |

## 9. 做 T 模块

该模块位于 `core/trading/`，当前未接入主窗口。

### `core/trading/model.py`

| 类型/方法 | 输入 | 返回 | 说明 |
| --- | --- | --- | --- |
| `TTDirection` | 枚举 | `buy_first`/`sell_first`/`hold` | 做 T 信号方向。 |
| `TTSignal` | dataclass 字段 | 信号对象 | 包含方向、胜率、目标价、止损价、置信度、原因。 |
| `TTModel.predict(features)` | 特征 dict | `TTSignal` | 模型接口，抽象方法。 |
| `TTModel.name()` | 无 | `str` | 模型名称，抽象方法。 |
| `MockTTModel(seed=42, win_rate_base=0.55)` | 随机种子、基础胜率 | 模拟模型 | 测试用规则模型。 |
| `MockTTModel.predict(features)` | 特征 dict | `TTSignal` | 按日内均价偏离和 MACD 生成模拟信号。 |

### `core/trading/t_trader.py`

| 类型/方法 | 输入 | 返回 | 说明 |
| --- | --- | --- | --- |
| `TTState` | 枚举 | `idle`/`wait_sell`/`wait_buy` | 做 T 状态。 |
| `TTTrade` | dataclass 字段 | 交易对象 | 记录开平仓时间、方向、价格、数量、盈亏。 |
| `TTStatus` | dataclass 字段 | 状态对象 | 记录底仓、资金、收益、当日次数、pending 交易。 |
| `TTrader(n_lots=5)` | 做 T 份数 | 引擎实例 | `n_lots` 同时决定底仓资金、可用资金和每日最大次数。 |
| `base_capital` | property | `float` | `n_lots * 10000`。 |
| `trade_amount` | property | `float` | 固定 `10000`。 |
| `init_stock(code, base_cost)` | 代码、底仓均价 | `TTStatus` | 初始化底仓股数和可用资金。 |
| `get_status(code)` | 代码 | `TTStatus` 或 `None` | 获取状态，不自动创建。 |
| `get_or_init(code, base_cost)` | 代码、成本 | `TTStatus` | 获取或初始化状态。 |
| `can_open(code)` | 代码 | `bool` | 判断是否空闲且未达每日次数上限。 |
| `open(code, price, signal)` | 代码、价格、信号 | `TTTrade` 或 `None` | 根据信号开仓。当前资金/底仓处理有 BUG。 |
| `check_close(code, current_price, signal=None)` | 代码、当前价、可选信号 | `TTTrade` 或 `None` | 按目标、止损或信号反转平仓。 |
| `force_close(code, current_price)` | 代码、当前价 | `TTTrade` 或 `None` | 名称为强制平仓，但当前只是调用 `check_close()`，未必平仓。 |
| `reset_daily(code)` | 代码 | `None` | 清空当日次数和 pending 交易。 |
| `get_stats(code)` | 代码 | `dict` | 返回交易次数、胜率、收益、底仓、资金等统计。 |

### `core/trading/data_provider.py`

| 方法 | 输入 | 返回 | 说明 |
| --- | --- | --- | --- |
| `TTDataProvider(data_manager=None)` | 可选 `MarketDataManager` | 提供器实例 | 无 manager 时为模拟模式。 |
| `gather_features(code, quote=None, avg_cost=0.0, base_position=0, available_funds=0.0, trades_today=0, max_trades=5)` | 代码、行情和持仓参数 | `dict` | 汇集模型输入特征。 |
| `_calc_macd(closes, fast=12, slow=26, signal=9)` | 收盘价列表、参数 | `(list, list, list)` | 计算 MACD。 |
| `generate_mock_features(code="000001", base_price=10.0, n_daily=126, n_intraday=240, seed=42)` | 模拟参数 | `dict` | 生成测试用完整特征。 |

### `core/trading/simulator.py`

| 类型/方法 | 输入 | 返回 | 说明 |
| --- | --- | --- | --- |
| `TTStepResult` | dataclass 字段 | 单步结果 | 记录价格、信号、开仓、平仓和状态。 |
| `TTSimReport` | dataclass 字段 | 回测报告 | 汇总步数、信号数、交易数、收益和日志。 |
| `TTSimulator(model=None, trader=None)` | 可选模型和交易引擎 | 模拟器 | 默认使用 `MockTTModel` 和 `TTrader(5)`。 |
| `step(code, features)` | 股票代码、特征 | `TTStepResult` | 单步预测和交易。 |
| `run_intraday(code="000001", base_price=10.0, n_steps=240, seed=42)` | 模拟参数 | `TTSimReport` | 模拟一个交易日。 |
| `_build_report(code)` | 代码 | `TTSimReport` | 从日志生成报告。 |
| `reset()` | 无 | `None` | 清空日志并重置 trader。 |

## 10. UI 层

### `ui/main_window.py`

`MainWindow` 是总协调者，负责连接菜单、表格、图表、数据 Worker、提醒引擎和买点扫描。

| 方法 | 输入 | 返回 | 说明 |
| --- | --- | --- | --- |
| `__init__()` | 无 | 窗口实例 | 初始化 DB、引擎、Manager、UI、托盘、定时器和启动加载。 |
| `_setup_menu()` | 无 | `None` | 创建文件/分组/视图/帮助菜单。 |
| `_setup_ui()` | 无 | `None` | 构建主布局、分组列表、表格、图表和状态栏。 |
| `_setup_tray()` | 无 | `None` | 创建系统托盘和菜单。 |
| `_on_tray_activated(reason)` | 托盘事件原因 | `None` | 双击托盘显示窗口并清理提醒。 |
| `flash_tray(enable=True)` | 是否闪烁 | `None` | 启停托盘闪烁定时器。 |
| `_toggle_tray_icon()` | 无 | `None` | 在普通和警告图标之间切换。 |
| `_startup_initialize()` | 无 | `None` | 启动时从 DB 恢复行情，交易时段触发刷新。 |
| `_setup_timers()` | 无 | `None` | 启动行情、买点、K 线、每日止损定时器。 |
| `_load_groups()` | 无 | `None` | 从 DB 加载分组到左侧列表。 |
| `_on_group_selected(current, previous)` | 当前/上一个 QListWidgetItem | `None` | 更新当前分组并刷新表格。 |
| `_refresh_current_group_data()` | 无 | `None` | 刷新表格；交易时段启动增量行情 Worker。 |
| `_refresh_kline_if_active()` | 无 | `None` | 交易时段刷新当前查看股票的当前图表标签页。 |
| `_get_all_tracked_codes()` | 无 | `list[str]` | 返回所有分组中的唯一股票代码。 |
| `_get_current_group_codes()` | 无 | `list[str]` | 返回当前分组股票代码。 |
| `_on_stock_incremental_done(code, quote)` | 代码、行情或 None | `None` | 单股刷新到达后刷新表格。 |
| `_on_incremental_complete(quotes)` | 行情 dict | `None` | 一轮增量刷新结束后刷新 UI、flush 今日 bar、检查提醒和盈亏。 |
| `_refresh_table_display()` | 无 | `None` | 合并 DB 股票和 Manager 行情，传给表格。 |
| `_update_profit_status(quotes)` | 行情 dict | `None` | 计算持仓分组总浮盈亏并更新状态栏。 |
| `_check_daily_stop_loss()` | 无 | `None` | 到 15:05 后更新持仓股票每日止损。 |
| `_get_holding_codes()` | 无 | `list[str]` | 返回持仓分组代码。 |
| `_check_alerts(quotes)` | 行情 dict | `None` | 计算止损/止盈并触发提醒。 |
| `_on_alerts_triggered(triggered)` | 触发列表 | `None` | 表格高亮、托盘闪烁和消息。 |
| `_show_alert_conflict(code, conflict)` | 代码、冲突 dict | `None` | 弹窗让用户选择覆盖或保留手动设置。 |
| `_scan_buy_points()` | 无 | `None` | 交易时段为所有跟踪代码启动买点扫描 Worker。 |
| `_on_scan_worker_done(worker)` | Worker | `None` | 维护扫描 pending 数。 |
| `_on_buy_point_result(code, result)` | 代码、结果 dict | `None` | 更新买点状态、表格高亮和托盘。 |
| `_on_stock_double_clicked(code)` | 代码 | `None` | 加载图表，清理提醒，买点触发时弹交易纪律。 |
| `_on_stock_right_clicked(code, action)` | 代码、动作 | `None` | 处理股票右键菜单。 |
| `_move_to_cleared(code)` | 代码 | `None` | 把当前分组中的股票移动到已清仓。 |
| `_on_manual_alert_settings(code)` | 代码 | `None` | 打开手动止盈止损对话框并应用结果。 |
| `_clear_all_highlights()` | 无 | `None` | 清理表格高亮。 |
| `_on_add_stock()` | 无 | `None` | 弹输入框，按代码或关键字添加股票。 |
| `_try_add_by_code(code)` | 6 位代码 | `None` | 本地查名称，必要时同步名称库。 |
| `_do_add_stock(code, name)` | 代码、名称 | `None` | 写 DB、标记 pending、启动初始 K 线 Worker。 |
| `_on_new_stock_kline_ready(code, period, klines)` | 代码、周期、K 线 | `None` | 当前查看股票时更新对应图表。 |
| `_on_new_stock_init_done(code)` | 代码 | `None` | 取消 pending 并刷新表格。 |
| `_fallback_to_search(keyword)` | 关键字 | `None` | 启动搜索 Worker。 |
| `_on_search_result(results, keyword)` | 搜索结果、关键字 | `None` | 弹列表让用户选择股票并添加。 |
| `_on_search_error(error_msg)` | 错误文本 | `None` | 状态栏和弹窗提示搜索失败。 |
| `_on_new_group()` | 无 | `None` | 新建自定义分组。 |
| `_on_delete_group()` | 无 | `None` | 删除当前自定义分组。 |
| `_on_about()` | 无 | `None` | 关于弹窗。 |
| `closeEvent(event)` | Qt close event | `None` | 停止闪烁、隐藏托盘、接受关闭。 |

### `ui/stock_table.py`

| 类/方法 | 输入 | 返回 | 说明 |
| --- | --- | --- | --- |
| `StockTableModel` | QAbstractTableModel | model | 显示行情和高亮状态。 |
| `update_data(quotes, alert_codes, bp_codes)` | 行情、提醒代码、买点代码 | `None` | 重置表格数据。 |
| `set_highlights(codes, color)` | 代码集合、颜色 | `None` | 设置高亮行。 |
| `clear_highlights()` | 无 | `None` | 清理高亮。 |
| `rowCount(parent)` | QModelIndex | `int` | 返回行数。 |
| `columnCount(parent)` | QModelIndex | `int` | 返回列数。 |
| `headerData(section, orientation, role)` | Qt 参数 | 文本或 `None` | 返回横向表头。 |
| `data(index, role)` | QModelIndex、role | 显示值/样式/`None` | 返回单元格数据。 |
| `get_code_at(row)` | 行号 | `str` | 返回该行股票代码。 |
| `StockTableWidget` | QTableView | widget | 表格控件，封装右键菜单和闪烁。 |
| `update_quotes(quotes, alert_codes, bp_codes)` | 行情和状态 | `None` | 更新 model。 |
| `highlight_rows(codes, highlight_type="alert")` | 代码列表、类型 | `None` | 开启高亮闪烁。 |
| `clear_highlights()` | 无 | `None` | 停止闪烁并清理高亮。 |
| `clear()` | 无 | `None` | 清空表格。 |
| `_toggle_flash()` | 无 | `None` | 高亮闪烁切换。 |
| `_on_double_click(index)` | QModelIndex | `None` | 发出 `stock_double_clicked(code)`。 |
| `_on_context_menu(pos)` | 鼠标位置 | `None` | 构建右键菜单并发出动作信号。 |

### `ui/chart_widget.py`

| 类/方法 | 输入 | 返回 | 说明 |
| --- | --- | --- | --- |
| `MplCanvas(figsize=(10,6), dpi=100)` | 图大小、DPI | FigureCanvas | Matplotlib 画布。 |
| `ChartTabWidget(period="daily")` | 周期 | widget | 单个图表页。 |
| `load_data(code, force_reload=False)` | 代码、是否强刷 | `None` | 根据周期启动分时或 K 线 Worker，已有数据时重绘。 |
| `_load_intraday()` | 无 | `None` | 启动 `IntradayWorker`。 |
| `_on_intraday_ready(code, data)` | 代码、分时数据 | `None` | 保存数据并绘图。 |
| `_load_kline()` | 无 | `None` | 启动 `KLineWorker`。 |
| `_on_kline_ready(code, period, klines)` | 代码、周期、K 线 | `None` | 保存 K 线并绘图。 |
| `_draw_intraday()` | 无 | `None` | 绘制分时价格、均价和成交量。 |
| `_draw_kline()` | 无 | `None` | 准备 K 线 DataFrame，尝试 mplfinance，最终调用手动绘图。 |
| `_draw_kline_manual(df, title, addplots)` | DataFrame、标题、附加图 | `None` | 手工绘制蜡烛图、均线、成交量、止损/止盈线。 |
| `set_alert_lines(stop_loss, take_profit)` | 止损价、止盈价 | `None` | 设置该图表页止损/止盈线。当前主窗口未实际调用。 |
| `set_bottom_fractals(indices, dates)` | 索引、日期 | `None` | 设置底分型标注。 |
| `ChartWidget` | QWidget | widget | 包含分时/日/周/月四个标签页。 |
| `load_stock(code)` | 代码 | `None` | 加载全部周期。 |
| `refresh_current_tab(code)` | 代码 | `None` | 只刷新当前标签页。 |
| `set_alert_lines(stop_loss, take_profit)` | 止损价、止盈价 | `None` | 给所有标签页设置止损/止盈线。 |

### `ui/trade_dialog.py`

| 类/方法 | 输入 | 返回 | 说明 |
| --- | --- | --- | --- |
| `TradeDialog(stock_code)` | 股票代码 | dialog | 交易记录管理对话框。 |
| `_setup_ui()` | 无 | `None` | 构建表格、按钮和持仓摘要。 |
| `_load_trades()` | 无 | `None` | 从 DB 加载交易记录。 |
| `_update_summary()` | 无 | `None` | 调 `get_position_summary()` 并显示盈亏。 |
| `_get_main_window()` | 无 | `MainWindow` 或 `None` | 沿 parent 链查主窗口。 |
| `_on_add()` | 无 | `None` | 添加交易记录，必要时自动移至已清仓。 |
| `_auto_move_to_cleared(code)` | 代码 | `None` | 将持仓组股票移到已清仓。 |
| `_on_edit()` | 无 | `None` | 编辑当前选中交易。 |
| `_on_delete()` | 无 | `None` | 删除当前选中交易。 |
| `TradeEditDialog(stock_code, trade=None)` | 代码、可选交易 | dialog | 添加/编辑交易表单。 |
| `_validate_and_accept()` | 无 | `None` | 校验价格、数量、手续费后 accept。 |
| `get_trade()` | 无 | `Trade` | 从表单生成交易对象。 |

### `ui/alert_settings_dialog.py`

| 方法 | 输入 | 返回 | 说明 |
| --- | --- | --- | --- |
| `AlertSettingsDialog(code, quote=None)` | 代码、可选行情 | dialog | 手动止损/止盈设置对话框。 |
| `_setup_ui()` | 无 | `None` | 构建表单。 |
| `_load_current()` | 无 | `None` | 从 DB 加载当前手动设置。 |
| `_on_ok()` | 无 | `None` | 校验并写入 DB，保存 `_result`。 |
| `_on_clear_sl()` | 无 | `None` | UI 上清除止损勾选和输入。 |
| `_on_clear_tp()` | 无 | `None` | UI 上清除止盈勾选和输入。 |
| `_on_clear_all()` | 无 | `None` | UI 上清除全部。 |
| `get_result()` | 无 | `dict` | 返回保存结果。 |

### `ui/discipline_dialog.py`

| 方法 | 输入 | 返回 | 说明 |
| --- | --- | --- | --- |
| `DisciplineDialog(stock_code="")` | 可选股票代码 | dialog | 买点触发后的交易纪律弹窗。 |
| `_setup_ui()` | 无 | `None` | 构建标题、文本、确认勾选和保存按钮。 |
| `_load_rules()` | 无 | `None` | 先查 DB，未命中时读 `resources/discipline.txt`。 |
| `_load_default_file()` | 无 | `str` | 读取默认纪律文件，不存在则返回内置默认文本。 |
| `_on_save()` | 无 | `None` | 保存纪律文本到 DB，并覆盖资源文件。 |
| `_on_confirm()` | 无 | `None` | 写入今日确认日期到 `settings` 并关闭。 |
| `set_signal_info(signal_details)` | 信号详情 | `None` | 更新信号说明。 |

### `ui/__init__.py`

空文件，仅用于包标识。

## 11. 测试目录

| 文件 | 覆盖内容 |
| --- | --- |
| `tests/conftest.py` | `temp_db` 和 `db_conn` fixture。注意 `temp_db` 删除项目根目录真实 DB。 |
| `tests/test_database.py` | 分组、股票、交易、手动提醒、纪律、股票名称、K 线 DB 操作。 |
| `tests/test_technical.py` | MA、EMA、MACD、分型、金叉、中枢、成交量、止损计算。 |
| `tests/test_alert_engine.py` | 手动覆写、持久化、提醒触发和止盈限流。 |
| `tests/test_market_data.py` | 代码前缀、本地搜索、K 线拉取、单股行情、QThread Worker、图表和表格模型。包含真实网络请求。 |
| `tests/test_t_trading.py` | 做 T 模型、交易引擎、数据提供器和模拟器。当前未检查资金/底仓守恒。 |

## 12. 开发注意事项

- 业务计算尽量放在 `core/`，不要继续堆到 `ui/main_window.py`。
- 所有 AKShare 调用都应集中在 `data.market_data`，上层优先通过 `MarketDataManager` 访问。
- 新测试应避免依赖真实网络，AKShare 调用应 mock。
- 测试数据库必须改为临时路径，不能复用项目根目录真实 `trading_assistant.db`。
- QThread Worker 需要可靠持有引用，避免运行中对象被释放。
- 做 T 模块投入 UI 或真实交易前，必须先修复 BUG 报告中的资金/仓位问题。
