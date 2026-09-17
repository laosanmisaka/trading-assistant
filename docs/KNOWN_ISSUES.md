# 已知未处理缺陷台账 (Known Issues)

本文件登记**已确认存在、但当前决定不修**的缺陷，以及**需要业务口径才能动**的项。
存在它的唯一目的：**避免静默略过**。接手人必须知道坑在哪里、什么时候必须处理。

- 完整分析见 `docs/PROJECT_ASSESSMENT.md`
- 任务清单复核意见见 `docs/TASK_LIST_REVIEW.md`
- 最后更新：2026-09-17

---

## KI-001 做 T 引擎资金与仓位双向不守恒

| 项 | 内容 |
| --- | --- |
| 位置 | `core/trading/t_trader.py`：`open()` 第 156 / 162 行，`check_close()` 第 219 / 224 行 |
| 状态 | **未修** |
| 当前影响 | **无** —— `ui/` 层零引用 `TTrader`，仅 `core/trading/simulator.py` 与测试使用 |
| 阻塞条件 | **接线做 T 界面之前必修** |

**缺陷**：`BUY_FIRST` 路径开仓扣资金、平仓却不回补；`SELL_FIRST` 路径卖出所得从未入账、平仓却把资金加回。即「卖出所得」在两条路径上都被记漏，且方向相反。

**实测**（`n_lots=5`，初始资金 50,000 / 底仓 5,000 股，10.00 开、10.10 平）：

```
BUY_FIRST 单轮:      资金 50000 -> 40000    底仓 5000 -> 6000
BUY_FIRST 连做 3 轮:  资金 50000 -> 20000    底仓 5000 -> 8000
SELL_FIRST 单轮:     资金 50000 -> 60000    底仓 5000 -> 4000
```

**最危险的后果**：`BUY_FIRST` 每轮净减 10,000，而每日上限 `max_trades = n_lots = 5` —— **第 5 轮资金归零后 `open()` 静默返回 `None`，不报错、不记日志**。做 T 模块会「无声停摆」，而不是崩溃。

**隐性影响**：`TTSimulator` 是项目唯一的策略验证通道，**基于它得出的任何做 T 结论目前都不可信**。

**修复要点**：每条路径写成完整借贷闭环（买入扣资金 / 卖出加资金；卖出加资金 / 回补扣资金），并补「资金守恒 + 底仓不变」的断言测试。

---

## KI-002 `force_close()` 不具备强制平仓语义

| 项 | 内容 |
| --- | --- |
| 位置 | `core/trading/t_trader.py:238` |
| 状态 | **未修** |
| 当前影响 | **无**（UI 零引用）；但 `core/trading/simulator.py:157` 中它是活的 |
| 阻塞条件 | **接线做 T 之前必修** |

**缺陷**：`force_close()` 直接转调 `check_close()` 且不传 `signal`，因而沿用「盈利 ±0.5% / 止损 ±1%」的平仓条件。价格落在 `open_price × 0.995 ~ 1.005` 带内时两个条件都不满足，直接返回 `None`。

**实测**：价格 10.02 / 10.00 / 9.98 三次 `force_close()` 全部返回 `None`，状态机停在 `WAIT_SELL`。

**级联后果**：状态机卡在非 `IDLE` 时，`can_open()` 第 118 行直接否决 —— **漏一次平仓 = 丢掉当天剩余全部做 T 机会**（`trades_today` 仍为 0，计数未消耗，但再也开不了仓）。

**修复要点**：把「强制」实现为不看盈亏价格、直接按现价平仓；补一条「状态机不会卡在 `WAIT_SELL` / `WAIT_BUY`」的测试。

---

## KI-003 止损取数窗口只覆盖 120 天且无告警（部分修复）

| 项 | 内容 |
| --- | --- |
| 位置 | `core/alert_engine.py:83` `calc_stop_loss()` |
| 状态 | **部分修复**（2026-09-17：docstring 已对齐实现、已补 warning 日志） |
| 剩余风险 | **取数窗口本身未改**，长线持仓的止损线仍可能偏低 |
| 阻塞条件 | 需原开发者确认止损口径 |

**说明**：第 113 行只取最近 120 天日线；第 117-120 行用 `>=` 匹配「第一个不早于买入日的 K 线」，因此**买入日早于窗口时 `buy_idx` 落到 0（不是 −1）**——止损被算成 `max(lows[0:])`，即最近 120 天内的最高 low，**遗漏更早的历史极值 → 止损线偏低（比应有值更宽松）**。

另一条独立路径：所有日线日期都早于买入日时（典型为当日买入、日线尚未更新），`buy_idx` 保持 `-1`，退化为今日最低价——该分支结果恰好合理。

**两条路径现在都会打 warning**，但不会自动放宽窗口：因为「止损只上移不下移」这条业务口径本身尚未经确认（见 KI-005）。

---

## KI-004 买点扫描底分型确认取值疑似偏移一根

| 项 | 内容 |
| --- | --- |
| 位置 | `core/buy_point_scanner.py:111-114` |
| 状态 | **未修** |
| 阻塞条件 | 需确认缠论二买的确认规则 |

**依据**：`detect_bottom_fractal()` 按 docstring（`core/technical.py:155-156`）返回的是**分型最低价实际所在的原始 K 线**（即中间那根）。旁证：同文件 `:183` 的 `get_latest_top_fractal` 直接用 `highs[idx]` 当顶分型价——「idx = 分型本身」是作者的既有约定。

而这里取的是：

```python
bottom_low    = w_arr["lows"][idx + 1]     # 右侧 K 线的最低价
confirm_close = w_arr["closes"][idx + 2]   # 再下一根的收盘价
```

上方注释却写「确认: 第三K线收盘 > 底分型最低价」。按 `idx` = 中间 K 线的语义，注释意图应写作 `lows[idx]` 与 `closes[idx+1]` —— **代码与自己的注释不自洽，整体后移了一根**。

**注意**：改动前必须先确认缠论二买的正确确认规则，**不要按注释字面直接改**。

---

## KI-005 需要业务口径才能继续的三项

| 项 | 代码现状 | 需要确认 |
| --- | --- | --- |
| 缠论分型周期 | 止盈检测走 **30 分钟**分型（`tests/test_alert_engine.py` 有对应限流测试） | 30min 还是 60min |
| 止损规则 | 实现为「只上移不下移」的移动止损 | 是否为有意设计（见 KI-003） |
| 做 T 资金模型 | 见 KI-001 | 真实记账口径 |

---

## KI-006 数据库连接开销量化结论与「已评估但不做」的项

| 项 | 内容 |
| --- | --- |
| 位置 | `data/database.py` `_connect()` / `init_db()` |
| 状态 | **主项已修复并量化**（2026-09-17） |
| 剩余 | 两个微优化经评估后**主动不做**（理由见下） |

**主修复**：`PRAGMA journal_mode = WAL` 是**持久化属性**，此前写在被高频调用的 `_connect()` 里，属重复劳动。已移到 `init_db()` 设置一次。

实测（300 次取中位数，本机 Win）：

| 写法 | 单次连接 |
| --- | --- |
| 旧（每连接设 journal_mode） | **83.7 ms** |
| 新（去掉该 PRAGMA） | **0.53 ms** |
| 该 PRAGMA 占旧开销 | **99%+** |

业务侧：50 只股票 × 每轮 10 次连接 → 旧写法 ~42 s/轮，现写法 ~0.23 s/轮。

> 注：此前一度以为「84ms 是冷启动假象」——实为测量脚本自身路径算错（`dirname('.')` 多剥了一层，连到了另一个不存在的库文件）。按 `_get_path()` 真实路径重测后，结论稳定可复现。

**已评估但不做（connection 成本已降至 0.5ms 后，收益不足以抵消改活代码的风险）**：

| 项 | 原设想 | 不做的理由 |
| --- | --- | --- |
| `calc_stop_loss` / `calc_take_profit` / `update_daily_stop_loss` 各调一次 `get_manual_alert` | 合并为一次查询 | 单次查询本身约 0.1ms 量级，合并后每只股票每轮省不到 0.3ms；且三处位于**不同函数**，合并需引入参数透传，属活代码结构改动 |
| `_update_profit_status` 逐股 `get_position_summary` | 改聚合 SQL | 同上；且 `get_position_summary` 的持仓/成本口径未获业务确认（近 KI-001），此时重写 SQL 有算错成本的风险 |

**触发重估条件**：若后续持仓池扩大到数百只、或刷新频率调高到秒级，再回头评估。

---

## 附表：已修复并有测试保护的项

| 缺陷 | 位置 | 保护测试 |
| --- | --- | --- |
| 分型索引错位 | `core/technical.py` | `tests/test_regressions.py::TestFractalIndexResolution` |
| `flush_today_bars` KeyError | `data/market_data_manager.py` | `tests/test_regressions.py::TestFlushTodayBars` |
| 托盘退出僵尸进程 | `ui/main_window.py` `closeEvent` / `_quit_app` | `tests/test_regressions.py::TestTrayExit` |
| 每日止损跨天失效 | `ui/main_window.py` `_check_daily_stop_loss` | `tests/test_regressions.py::TestDailyStopLossSchedule` |
| 回测 O(n²) | `core/backtest/strategy.py` `WeeklyAggregator` | `tests/test_backtest.py::TestWeeklyAggregatorEquivalence` |
| DB 连接重复设 journal_mode | `data/database.py` `_connect()` / `init_db()` | 无断言，仅实测数据（见 KI-006） |

**仍缺测试保护的两项**（本次已修复代码，但断言尚未落地）：

| 缺陷 | 位置 | 缺口 |
| --- | --- | --- |
| UI 线程同步网络请求 | `ui/main_window.py` `_try_add_by_code` | 需断言「DB 未命中时不同步调用同步函数、而是启动 worker」 |
| 买点扫描线程泄漏 | `core/buy_point_scanner.py` `BuyPointScanWorker` | 需断言「单轮只创建一个 worker、且不随轮次累积」 |
