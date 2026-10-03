# 技术总览

当前入口为 `main.py`，Python 3.12 + PyQt5；数据访问通过 AKShare/mootdx/baostock，SQLite 保存本地状态，czsc 1.0.1 桥接结构。依赖验证边界见 [VALIDATION](VALIDATION.md)。

## 模块与边界

| 模块 | 职责 |
| --- | --- |
| `ui/main_window.py` | GUI、定时器、线程所有权、串行初始取数队列与退出等待 |
| `data/market_data.py` | 行情适配和后台 worker；源故障与正常空结果分开 |
| `data/market_data_manager.py` | 内存快照、历史补齐、周期重建与分钟刷新 |
| `data/history.py` | 最新交易日分钟聚合、周/月聚合和初始化报价 |
| `data/database.py` | 连接、事务、行情缓存与用户数据；持仓移动平均成本 |
| `data/cache_files.py` / `utils/atomic.py` | 请求完整性、哈希元数据、原子文件发布 |
| `core/chan.py` / `core/chan_points.py` | czsc 结构桥接与自研几何点 |
| `core/causal_signals.py` / `core/triple_buy.py` | 可观察窗口、首次确认、不可回写的历史入场 |
| `core/signal_journal.py` / `core/buy_points.py` | 事件持久化、恢复、修订折叠和 GUI 消费 |
| `core/trading_calendar.py` | 上海时区及有支持范围的 XSHG 日历 |
| `core/research_stats.py` / `core/research_summary.py` | 配对基准、同口径 bootstrap、逐点统计 |
| `core/alert_engine.py` | 持仓提醒；与研究策略的固定持有出场不同 |
| `core/backtest/` | 通用账户回测；初始本金、费用、权重和权益回撤 |

## 数据流

盘后缓存 → 可观察日线窗口 → watchlist → 新浪 5min 回放 → 事件日志 → 最近两交易日有效事件 → 自动分组。研究和三买图表用缓存输入同一个事件回放器。共享算法的前提是相同输入；不同来源不保证相同结构。

GUI 行情数据库与研究 CSV 是两个缓存边界。启动先读 SQLite，后台补齐完整可用日线/分钟历史；日线派生周/月线，最新一天分钟 OHLC 生成报价。源切换的分钟 OHLC 不被压成单个 close。完整历史快照按周期事务替换，避免旧复权缓存与新快照拼接；有限深度源只提供有限历史，不能据此宣称全部市场历史完整。

UI 取数不能在主线程执行。`all_done` 仅表示业务结果就绪，队列只有收到 `QThread.finished` 才释放槽位。线程对象由窗口持有，退出保持事件循环直至线程结束；中断请求不能强行打断第三方同步网络调用。

## 数据库

`stocks`、`groups`、`trades`、`settings` 保存用户数据；`stock_names`、`klines`、`klines_minute` 为可刷新数据。准确表定义见 [database.py](../data/database.py) 的 `init_db`，不在文档复制易漂移的 SQL。WAL 在初始化启用，不在每次连接时反复切换。

成交按日期和 ID 排序，成本使用当前剩余持仓的移动平均；清仓结束持仓周期。手动止盈/止损在一次事务中只更新指定键，明确传 `False/0` 才关闭。行情缺失与无效超卖记录不进入持仓提醒。

## 当前与历史算法

当前三买主线见 [策略合同](TRIPLE_BUY_WALKTHROUGH.md)。`core/chan_strategy.py` 与 `visualize_chan.py` 的日线/30min 多周期计算保留为历史诊断；其几何端点不是当时可成交时间。GUI 当前成交标注不使用旧多周期交易结果。做 T 模块保留但未接主 UI，不属于已验证账户执行器。

HTML 模板在 `resources/chan_chart.html`。文本字段做 HTML 转义，内联 JSON 转义 `< > &` 和 Unicode 分隔符，占位符一次替换。ECharts 来源与许可见 [第三方清单](../THIRD_PARTY_NOTICES.md)。

## 开发检查

[AGENTS](../AGENTS.md) 限制适用于新增和实际修改的函数/文件，不要求借此清理无关旧代码。`scripts/check_standards.py` 以 AST 比较修改范围、tokenize 计有效物理行、radon 计圈复杂度，并检查 Markdown 字符数。当前修复基线为 `7fbaba8`。测试统一使用仓外 `--basetemp`；CI 跑默认离线套件。
