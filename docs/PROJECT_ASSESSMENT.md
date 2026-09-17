# 项目评估报告

评估对象：`trading-assistant`（A 股桌面交易辅助工具）
评估方式：全量代码通读 + 文档交叉核对 + git 历史审阅
评估日期：2026-09-17

---

## 1. 总体判断

**这是一个「架构分层意识正确、功能骨架完整、但正确性与工程卫生未收口」的半成品。**

作为个人自用工具，它的可用度已经不错：分组管理、行情刷新、K 线/分时图、交易记录、止损止盈提醒、买点扫描都能跑通。但作为要交给别人接手、甚至要碰真金白银的系统，当前状态**不安全**——做 T 模块存在明确的资金计算缺陷，止损口径存在方向性疑问，而唯一能兜底的测试又依赖真实网络。

一句话：**可以研究、可以自用观察，不可以按现状用于任何自动化的交易决策。**

## 2. 项目体量

| 维度 | 数值 |
| --- | --- |
| Python 源码 | 约 5,450 行（不含测试） |
| 测试代码 | 1,734 行，125 个测试函数 |
| 最大文件 | `ui/main_window.py` 1,030 行 |
| 数据库表 | 9 张（`trading_assistant.db`，SQLite + WAL） |
| git 提交 | 12 次，最新 `75a8157 feat: 新增可插拔策略的回测引擎` |
| 工作区状态 | 3 个文件已修改未提交，1 个测试文件未跟踪 |

## 3. 做得好的部分

**分层意识明确。** 目录划分 `ui / core / data / utils` 清晰，依赖方向基本单向（`ui -> core -> data -> utils/config`），新代码有明确去处。这在个人项目里并不常见。

**缓存架构有设计。** `MarketDataManager` 做了「内存 + SQLite」双层缓存，区分了「今日 bar」（内存，定时 flush）和「历史 K 线」（DB），并用 `get_klines()` 统一屏蔽数据来源。这是理解成本较高、但收益也较高的一步。

**线程模型有考虑。** 所有网络请求走 QThread Worker（`StockSearchWorker`、`IncrementalRefreshWorker`、`InitialFetchWorker`、`BuyPointScanWorker`），不阻塞 GUI；且区分了「API 取」和「本地 DB 取」两套 Worker，说明有性能意识。

**异常兜底做得扎实。** `main.py` 注册了全局 `sys.excepthook` 和 Qt 消息处理器，未捕获异常会同时进日志、进 stderr、弹窗。`_on_incremental_complete_safe()` 这类「Worker 异常退出也要刷一次 UI」的安全网，说明作者吃过线程静默失败的亏。

**接手文档质量高于代码本身。** `docs/USER_GUIDE.md` 与 `docs/TECHNICAL_REFERENCE.md` 覆盖了目录职责、数据流、9 张表结构、逐函数说明、开发注意事项。文档作者还主动标注了「待确认口径」而不是猜测，这个态度是对的。

**技术指标实现是认真的。** `core/technical.py` 里的缠论包含处理做了正确的向上/向下分情况合并，还用 `index_map` 把合并序列的分型索引精确还原回原始序列（`_resolve_fractal_index` 注释解释了为什么取「最早出现」）。这部分明显是认真读过缠论定义才写的。

## 4. 必须修复的缺陷（P0）

### 4.0 基线澄清（重要）

**本报告评估的是工作区（working tree），不是 HEAD。** 这一点直接决定了哪些缺陷"还活着"。

`git diff` 显示有 3 个文件存在未提交改动，经逐行核对，**它们全部是在修 P0 级缺陷**，且修复方向正确：

| 文件 | HEAD 中的缺陷 | 工作区状态 |
| --- | --- | --- |
| `core/technical.py` | 分型索引用 `idx = min(mi * 2, n - 1)` 近似映射回原始序列，一旦发生 K 线包含合并必然错位，导致止损止盈基于错误 K 线的高点计算 | **已修**。改为 `_merge_contains()` 维护 `index_map`，新增 `_resolve_fractal_index()` 精确还原索引 |
| `data/market_data_manager.py` | `flush_today_bars()` 读 `b["time"]` / `b.get("price")`，而实际数据键为 `timestamp/open/high/low/close`，内存中有分钟数据时必然 `KeyError`；且在主线程触发，盘中每 5 分钟弹一次异常框 | **已修**。整段分钟 flush 已删除（`refresh_minute_bars` 拉取时已实时入库，该段本就重复） |
| `ui/main_window.py` | ① `setQuitOnLastWindowClosed(False)` 且托盘"退出"连 `self.close` → 窗口关了托盘没了、进程变僵尸且无法再打开界面；② `_daily_stop_loss_done` 从不按日期清空，第二天 15:05 永不执行；`now.minute != 5` 精确匹配导致卡顿即错过当天 | **已修**。① 新增 `_quit_app()` 走 `QApplication.quit()`，`closeEvent` 改为最小化到托盘；② 标记改为 `set[tuple[code, date]]`，时间比较改为 `<` 以容忍迟到 |

**结论：这四个 P0 不需要再修，只需要提交。** 工作区那 3 个文件应从"待定去留"改判为"已验证的缺陷修复，直接 commit"。

据此，**本报告第 4.1–4.3 节列出的三项，才是当前仍然存活、尚未修复的 P0。**

### 4.1 做 T 模块资金不守恒

`core/trading/t_trader.py`：

- `open()` 在 `BUY_FIRST` 分支执行 `s.available_funds -= self.trade_amount`（第 156 行）；
- `check_close()` 在 `BUY_FIRST` 平仓分支只执行 `s.base_position += trade.quantity`（第 219 行），**没有把卖出所得回补到 `available_funds`**。

后果：每完成一次「先买后卖」，可用资金净减少 10,000 元且底仓同步虚增。交易次数越多，账面越失真。`tests/test_t_trading.py` 目前不检查资金/仓位守恒，所以测试全绿也发现不了。

### 4.2 `force_close()` 不具备强制平仓语义

```python
def force_close(self, code, current_price):
    return self.check_close(code, current_price)   # 不传 signal
```

`check_close()` 的平仓条件是「盈利达 ±0.5%」或「止损 ±1%」。当价格落在 `open_price * 0.995 ~ 1.005` 区间内时，两个条件都不满足，`force_close()` 直接返回 `None`——收盘前该平的仓平不掉。函数名与行为不符，属于隐蔽性很高的缺陷。

### 4.3 止损口径存疑

`AlertEngine.calc_stop_loss()` 第 121-125 行：

```python
initial_stop = float(arr["lows"][buy_idx])
for i in range(buy_idx, len(arr["lows"])):
    initial_stop = max(initial_stop, float(arr["lows"][i]))
```

初始止损取的是**买入日之后所有日线 low 的最大值**。这意味着只要买入后任意一天出现过较高的低点，止损线就会被顶到那一天的 low 上——价格很容易在正常波动中跌破，止损几乎必然被触发。这通常不是有意的交易规则，更像是把「移动止损」写漏了区间约束。**需要向原开发者确认业务意图，确认前不要把它当作可用的风控逻辑。**

## 5. 工程债（P1）

**`ui/main_window.py` 是上帝对象。** 1,030 行、43 个方法，同时承担：菜单/布局/托盘搭建、定时器编排、Worker 生命周期管理、行情合并、持仓盈亏汇总、止损止盈计算调用与冲突弹窗、买点状态维护、股票增删与搜索编排。技术文档自己也写了「业务计算尽量放在 core/，不要继续堆到 main_window.py」——说明作者意识到了，但没来得及拆。

**依赖声明不完整。** `data/market_data.py` 有两处 `from mootdx.quotes import Quotes`（第 160、332 行），用于通达信行情，但 `requirements.txt` 和 `environment.yml` 都没有 `mootdx`。在干净环境里按文档装依赖，交易时段走 TDX 的路径会直接 ImportError。

**测试依赖真实网络。** `tests/test_market_data.py` 24 个测试中包含真实 AKShare 请求，没有 `mock` 或网络标记，无法在离线/CI 环境稳定运行。文档也已承认「覆盖范围有限，尤其没有覆盖做 T 资金/仓位守恒」。

**`utils.is_trading_time()` 存在硬编码。** 使用硬编码时间对象，未引用 `config.TRADING_*` 常量——改配置不会生效。

**`core/` 与 `data/` 反向依赖 PyQt。** `core/buy_point_scanner.py:9` 与 `data/market_data.py:6` 直接 `from PyQt5.QtCore import QThread, pyqtSignal`。业务层依赖 GUI 框架，导致 `core/` 无法脱离 QApplication 单测。Worker 类应上移到 `ui/services/`。

**网络失败被静默吞掉。** `data/market_data.py:122-124` 用裸 `except Exception` → `logger.error` + `return []`。调用方无法区分「数据源挂了」和「停牌无数据」，两者都会渲染成空图表。

**数据库连接模式浪费严重。** `data/database.py` 有 38 处 `_connect()` 调用点，每处都是「开连接 → 操作 → 关连接」。每只股票每轮刷新约产生 10 次独立连接建立。建议模块级共享连接（WAL 已开启，支持并发读）+ 持仓摘要改聚合 SQL。

**回测引擎是 O(n²)。** `core/backtest/strategy.py:149` 与 `160-163`，在 `while i < n` 循环内每次调用 `_resample_weekly(daily[:i+1])` 与 `detect_macd_golden_cross(closes[:i+1], ...)`，即对逐日增长的前缀反复全量重算周线重采样与 MACD。注意：前缀切片本身是为避免未来函数（`test_buypoint_strategy_no_lookahead` 覆盖了这点），必须保留；可优化的是把 EMA/MACD 一次算完全序列后按 `i` 取值，周线重采样改增量维护。

**数据源策略碎片化。** 日线走新浪、1min 走通达信、60min 走东方财富、分时又回新浪，四处重试逻辑各自实现。建议抽象 `DataSource` 接口 + 统一 fallback 链。次要一点：`utils/cache.py` 的 `cached()` 装饰器定义了但全项目未使用。

## 6. 一致性与卫生（P2）

- **悬空引用**：`docs/USER_GUIDE.md` 第 170、261 行两处写「详见 BUG 报告」「请以 BUG 报告为准继续补测试」，但仓库中不存在任何 BUG 报告文件。接手人会直接卡在这里。
- **文档与代码不同步**：README 第 70 行与 USER_GUIDE 第 235 行均称「测试会删除项目根目录的 `trading_assistant.db`」，但 `tests/conftest.py` 已通过 `monkeypatch` + `tempfile` 把 `_get_path` 指向临时文件，**实际不会碰真实库**。这条警告已经过期，会误导接手人不敢跑测试。
- **未使用的依赖**：`mplfinance` 列在依赖里，但 `ui/chart_widget.py` 是纯 matplotlib 手绘实现，明确「无 mplfinance 依赖」。
- **未被引用的配置常量**：`TDX_HOST/PORT/TIMEOUT`、`TOP_FRACTAL_LOOKBACK`、`TRADING_START_*/END_*`、`KLINE_INITIAL_MONTHS` 在代码中均无引用。
- **未提交的工作区改动**：`core/technical.py`、`data/market_data_manager.py`、`ui/main_window.py` 处于已修改状态，`tests/test_market_data_manager.py` 未跟踪。接手第一步应先确认这些改动是否要保留。

## 7. 建议的接手顺序

1. **先提交，不要回退。** `git status` 里那 3 个已修改文件经逐行核对是**四个 P0 缺陷的正确修复**（见 4.0 节），还有未跟踪的 `tests/test_market_data_manager.py` 需要一并纳入。**回退它们等于把已修好的崩溃和僵尸进程放回去。** 提交后再以干净基线继续。
2. **补上 4.0 节表格里四个修复的回归测试。** 现在这些修复靠的是人工核对，没有测试保护，下次改动随时可能退化。
3. **补回缺失的输入**：向原开发者索取 BUG 报告和私有审阅记录，书面确认三个业务口径——30 分钟还是 60 分钟周期、止损规则的真实意图、做 T 的资金模型。
4. **修 P0**：做 T 资金守恒、`force_close()` 语义、止损口径。每修一个都同步补一个守恒/边界测试。
5. **让测试可离线**：给所有 AKShare 调用加 mock，把 `test_market_data.py` 拆成「纯逻辑」和「网络集成」两层。
6. **拆 `main_window.py`**：优先把「持仓盈亏汇总」「止损止盈编排与冲突处理」「买点状态维护」三块抽到 `core/` 下的独立模块，GUI 只保留展示与转发。同时把 `core/`、`data/` 里的 QThread Worker 上移，解除业务层的 PyQt 依赖。
7. **性能收口**：回测改增量计算、DB 改共享连接、买点扫描改 `QThreadPool`、冲突弹窗加去重。
8. **清文档**：删除悬空引用，修正过期的测试警告，补齐 `mootdx` 依赖。

## 8. 结论

投入产出比取决于目标：

- **当作个人自选股观察工具**：现状可用，先修 4.2 和 4.3 两处，风险可控。好消息是 HEAD 里四个最急的缺陷（分型索引错位、flush 崩溃、僵尸进程、每日止损失效）在工作区已经修好，只需提交 + 补回归测试，成本远低于重做。
- **当作自动交易或资金管理工具**：当前不可用。做 T 资金模型必须重写并补守恒测试，止损规则必须重新定义并回测验证。
- **当作学习项目/代码样本**：分层设计、双层缓存、线程编排、缠论实现都值得读；`main_window.py` 则是一个很好的反面教材。

---

*本报告基于静态代码审阅，未实际运行程序或执行测试。所有涉及运行时行为的判断均已在正文中标注依据行号，供复核。*
