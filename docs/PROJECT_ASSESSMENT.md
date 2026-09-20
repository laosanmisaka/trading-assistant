# 项目评估报告

评估对象：`trading-assistant`（A 股桌面交易辅助工具）
评估方式：全量代码通读 + 文档交叉核对 + git 历史审阅 + **对做 T 引擎的隔离环境实证运行**
评估日期：2026-09-17

> **v2 更新（同日）**：工作区 3 个修复已提交（`6f54be0`），基线转为干净。在隔离 venv 中实际驱动 `TTrader` 跑通了完整开平仓流程，P0 清单有两处重要变化——`4.1` 补充了**方向相反的对称缺陷**，`4.3` 的定性需要修正。详见下文标注「v2」的段落。

---

## 1. 总体判断

**这是一个「架构分层意识正确、功能骨架完整、但正确性与工程卫生未收口」的半成品。**

作为个人自用工具，它的可用度已经不错：分组管理、行情刷新、K 线/分时图、交易记录、止损止盈提醒都能跑通（原「买点扫描」已于 2026-09-18 删除，买点改为在图上标注）。但作为要交给别人接手、甚至要碰真金白银的系统，当前状态**不安全**——做 T 模块存在明确的资金计算缺陷，止损口径存在方向性疑问，而唯一能兜底的测试又依赖真实网络。

一句话：**可以研究、可以自用观察，不可以按现状用于任何自动化的交易决策。**

## 2. 项目体量

| 维度 | 数值 |
| --- | --- |
| Python 源码 | 约 5,450 行（不含测试） |
| 测试代码 | 1,734 行，125 个测试函数 |
| 最大文件 | `ui/main_window.py` 1,030 行 |
| 数据库表 | 9 张（`trading_assistant.db`，SQLite + WAL） |
| git 提交 | 12 次，最新 `75a8157 feat: 新增可插拔策略的回测引擎` |
| 工作区状态 | 干净（v2 复核：3 个修复已在 `6f54be0` 提交，本报告在 `74ab5be` 提交） |

## 3. 做得好的部分

**分层意识明确。** 目录划分 `ui / core / data / utils` 清晰，依赖方向基本单向（`ui -> core -> data -> utils/config`），新代码有明确去处。这在个人项目里并不常见。

**缓存架构有设计。** `MarketDataManager` 做了「内存 + SQLite」双层缓存，区分了「今日 bar」（内存，定时 flush）和「历史 K 线」（DB），并用 `get_klines()` 统一屏蔽数据来源。这是理解成本较高、但收益也较高的一步。

**线程模型有考虑。** 所有网络请求走 QThread Worker（`StockSearchWorker`、`IncrementalRefreshWorker`、`InitialFetchWorker`、`BuyPointScanWorker`），不阻塞 GUI；且区分了「API 取」和「本地 DB 取」两套 Worker，说明有性能意识。

> 2026-09-18：`BuyPointScanWorker` 已随老伪缠论买点链路删除，替代者是 `ui/chan_worker.py::ChanMarkWorker`（契约不变）。其余三个仍在。

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

**结论（v2 已落地）**：这四个 P0 已于 `6f54be0` 提交，基线干净，回退风险解除。报告写的「直接 commit」这条行动项已完成。

据此，**本报告第 4.1–4.3 节列出的三项，才是当前仍然存活、尚未修复的 P0。**

### 4.1 做 T 模块资金与仓位双向不守恒（v2 实证扩充）

**v2 补充：缺陷是双向的，且方向相反。** 原报告只写了 `BUY_FIRST` 一条路径；在隔离 venv 中实际驱动引擎跑完整开平仓后，`SELL_FIRST` 路径存在**反向**的对称缺陷，且两条路径都会污染底仓。

在 `core/trading/t_trader.py` 中，开仓与平仓对「资金/仓位」这两个状态量的记账是不对称的：

| 路径 | `open()` 做的事 | `check_close()` 做的事 | 净效果 |
| --- | --- | --- | --- |
| `BUY_FIRST` | 扣资金 10,000（第 156 行） | 只加底仓 `base_position += quantity`（第 219 行） | 资金每轮 **-10,000**，底仓每轮 **+qty** |
| `SELL_FIRST` | 只减底仓（第 162 行），**卖出所得从未入账** | 只加资金 `available_funds += trade.amount`（第 224 行） | 资金每轮 **+10,000**，底仓每轮 **-qty** |

即：**卖出所得在两条路径上都被记漏了**——`BUY_FIRST` 平仓时不回补，`SELL_FIRST` 开仓时不入账；而回补动作又单向生效，于是同一笔钱被记了两次或一次都没记。

实测（`n_lots=5`，底仓 5,000 股 / 资金 50,000 元，价格 10.00 开、10.10 平，每次理论盈利 100 元）：

```
[A] BUY_FIRST 单轮：  funds 50000 -> 40000    base_pos 5000 -> 6000   期望 funds 50100
[B] BUY_FIRST 连做3轮：funds 50000 -> 20000    base_pos 5000 -> 8000
[C] SELL_FIRST 单轮： funds 50000 -> 60000    base_pos 5000 -> 4000   期望 funds 50100 / pos 5000
```

**比"账面失真"更严重的后果（v2 新增）**：`BUY_FIRST` 路径下可用资金每轮净减 10,000，而每日最大做 T 次数 `max_trades = n_lots = 5`——**第 5 轮做完，可用资金归零**。此后 `open()` 第 154 行 `if s.available_funds < self.trade_amount: return None` 直接静默返回 `None`，做 T 模块在当天剩余时段全部失效，且不产生任何日志或异常。同时底仓被虚增到 10,000 股（初始的两倍）。

`tests/test_t_trading.py` 不检查资金/仓位守恒，所以测试全绿也发现不了。

**修复要点**：把每条路径的资金流写成完整的借贷闭环——`BUY_FIRST` 买入时 `funds -= 成交额`、卖出时 `funds += 成交额`；`SELL_FIRST` 卖出时 `funds += 成交额`、回补时 `funds -= 成交额`。并补一条 `funds_before + profit == funds_after` 且 `base_position` 不变的守恒测试。

### 4.2 `force_close()` 不具备强制平仓语义，并会导致当日自锁（v2 补充后果）

```python
def force_close(self, code, current_price):
    return self.check_close(code, current_price)   # 不传 signal
```

`check_close()` 的平仓条件是「盈利达 ±0.5%」或「止损 ±1%」。当价格落在 `open_price * 0.995 ~ 1.005` 区间内时，两个条件都不满足，`force_close()` 直接返回 `None`——收盘前该平的仓平不掉。函数名与行为不符，属于隐蔽性很高的缺陷。

**v2 新增的级联后果**：这条缺陷不只是「收盘前平不掉仓」。实测中，价格连续落在带内（10.02 / 10.00 / 9.98）时三次 `force_close()` 全部返回 `None`，状态机停在 `WAIT_SELL`：

```
[D] force_close @10.02 -> None    state=wait_sell
    force_close @10.00 -> None    state=wait_sell
    force_close @9.98  -> None    state=wait_sell
[E] trades_today=0 / max_trades=5  but  can_open=False
```

状态机卡在非 `IDLE` 时，`can_open()` 第 118 行 `if s.state != TTState.IDLE: return False` 直接否决——**一次卡住，当天剩余所有做 T 机会全部作废**（`trades_today` 还是 0，计数没消耗，但再也开不了仓）。所以这条缺陷的真实代价是「漏一次平仓 = 丢掉当天剩余全部交易机会」，而非单笔损失。

### 4.3 止损口径存疑（v2 定性修正）

`AlertEngine.calc_stop_loss()` 第 121-125 行：

```python
initial_stop = float(arr["lows"][buy_idx])
for i in range(buy_idx, len(arr["lows"])):
    initial_stop = max(initial_stop, float(arr["lows"][i]))
```

**v2 修正**：原报告称这里「像是把移动止损写漏了区间约束」，这个定性不准确。把 `max(lows[buy_idx:])` 展开看，它**恰好等价于**「从买入日起逐日执行 `max(昨日止损, 今日最低价)`」累积到今天的结果——代码并没有写漏约束，它的实质是**首次设置止损时一次性"追赶"到移动止损应有的位置**，在数学上与逐日更新等价。

真正值得质疑的是另外三层：

1. **docstring 与实现语义不一致。** 第 91 行承诺「初始止损 = 买入当日最低价」，但实现取的是买入日**至今**所有 low 的最大值。公式（第 92 行）与实现（第 122-125 行）对不上，第 128 行的日志文案「买入后最低价最大值」才如实反映了实际行为。接手人读 docstring 会被误导。
2. **策略层面的问题需要业务确认。** 「只上移不下移」本身是一种激进的口径——连续上涨后止损会被顶到历史最高 low 附近，此后任何正常回调都可能触发。对趋势股可行，对震荡股会持续被自己的波动打出。**这属于需要原开发者书面确认的业务规则，不是代码缺陷。**
3. **取数窗口只覆盖 120 天，且无告警（v2 修正）。** 第 113 行 `manager.get_klines(code, "daily", days=120)` 只取 120 天日线。第 117-120 行用 `>=` 匹配「第一个不早于买入日的 K 线」，所以**买入日早于窗口时 `buy_idx` 落到 0，而不是 −1**——止损被算成 `max(lows[0:])`，即**最近 120 天内的最高 low**，遗漏 120 天以前的历史极值。对长线持仓，止损线因此**偏低**（比应有值更宽松），风险敞口被放大。

   另一条独立路径：若所有日线日期都早于买入日（典型场景是**当日买入而日线数据尚未更新**），`buy_idx` 保持 `-1`，整段跳过，落到第 132-133 行 `new_sl = current_daily_low`。这个分支的结果恰好合理，但同样**不记日志、不告警**。

   **两条路径的共同问题是没有回退提示**——使用者无法察觉止损是基于不完整的取数窗口算出来的。建议至少在 `buy_idx == 0` 且买入日早于 `klines[0].date` 时打一条 warning。

**结论不变：确认前不要把它当作可用的风控逻辑。** 但修复顺序应调整为：先修 1（对齐 docstring 与实现）和 3（加告警或放宽取数窗口），2 交给原开发者定口径。

## 5. 工程债（P1）

**`ui/main_window.py` 是上帝对象。** 1,030 行、43 个方法，同时承担：菜单/布局/托盘搭建、定时器编排、Worker 生命周期管理、行情合并、持仓盈亏汇总、止损止盈计算调用与冲突弹窗、买点状态维护、股票增删与搜索编排。技术文档自己也写了「业务计算尽量放在 core/，不要继续堆到 main_window.py」——说明作者意识到了，但没来得及拆。

**依赖声明不完整。** `data/market_data.py` 有两处 `from mootdx.quotes import Quotes`（第 160、332 行），用于通达信行情，但 `requirements.txt` 和 `environment.yml` 都没有 `mootdx`。在干净环境里按文档装依赖，交易时段走 TDX 的路径会直接 ImportError。

**测试依赖真实网络。** `tests/test_market_data.py` 24 个测试中包含真实 AKShare 请求，没有 `mock` 或网络标记，无法在离线/CI 环境稳定运行。文档也已承认「覆盖范围有限，尤其没有覆盖做 T 资金/仓位守恒」。

**`utils.is_trading_time()` 存在硬编码。** 使用硬编码时间对象，未引用 `config.TRADING_*` 常量——改配置不会生效。

**`core/` 与 `data/` 反向依赖 PyQt。** `core/buy_point_scanner.py:9` 与 `data/market_data.py:6` 直接 `from PyQt5.QtCore import QThread, pyqtSignal`。业务层依赖 GUI 框架，导致 `core/` 无法脱离 QApplication 单测。Worker 类应上移到 `ui/services/`。

**2026-09-18 更新：`core/` 这一侧已解决。** `core/buy_point_scanner.py` 删除后，新的后台 worker 落在 `ui/chan_worker.py`，`core/` 已不再 import PyQt5；`data/market_data.py` 仍依赖，留待后续处理。

**网络失败被静默吞掉。** `data/market_data.py:122-124` 用裸 `except Exception` → `logger.error` + `return []`。调用方无法区分「数据源挂了」和「停牌无数据」，两者都会渲染成空图表。

**数据库连接模式浪费严重。** `data/database.py` 有 38 处 `_connect()` 调用点，每处都是「开连接 → 操作 → 关连接」。每只股票每轮刷新约产生 10 次独立连接建立。建议模块级共享连接（WAL 已开启，支持并发读）+ 持仓摘要改聚合 SQL。

**回测引擎是 O(n²)。** ~~`core/backtest/strategy.py:149` 与 `160-163`~~ —— **2026-09-20 作废**：该文件里的 `BuyPointStrategy`（伪缠论回测策略）整体取缔，O(n²) 随之消失。原问题描述保留在此仅作历史记录：`while i < n` 循环内每次调用 `_resample_weekly(daily[:i+1])` 与 `detect_macd_golden_cross(closes[:i+1], ...)`，对逐日增长的前缀反复全量重算；曾用 `WeeklyAggregator` 增量维护把 1000 天从 3.02s 降到 0.44s。**注意这条教训仍然有效**：以后给 `core/backtest/engine.py` 接新策略时，别在每日循环里对前缀做全量重算。

**数据源策略碎片化。** 日线走新浪、1min 走通达信、60min 走东方财富、分时又回新浪，四处重试逻辑各自实现。建议抽象 `DataSource` 接口 + 统一 fallback 链。次要一点：`utils/cache.py` 的 `cached()` 装饰器定义了但全项目未使用。

## 6. 一致性与卫生（P2）

- ~~**悬空引用**~~ **（已修复）**：`docs/USER_GUIDE.md` 第 170、261 行两处写「详见 BUG 报告」「请以 BUG 报告为准继续补测试」，但仓库中不存在任何 BUG 报告文件。**实际共三处**——第三处在 `docs/TECHNICAL_REFERENCE.md:526`「必须先修复 BUG 报告中的资金/仓位问题」，前两次核对均漏掉。三处已改为指向 `docs/KNOWN_ISSUES.md` 的具体条目。
- ~~**文档与代码不同步（测试删库）**~~ **（已修复）**：README 第 70 行与 USER_GUIDE 第 235 行均称「测试会删除项目根目录的 `trading_assistant.db`」，但 `tests/conftest.py` 已通过 `monkeypatch` + `tempfile` 把 `_get_path` 指向临时文件，**实际不会碰真实库**。这条警告已经过期，且**被三份外部材料连续误引**（README → 外部评估 → 他人任务清单）。两处已更正，并附上 `--run-network` 的说明。
- **未使用的依赖（已修复）**：`mplfinance` 曾列在依赖里，但 `ui/chart_widget.py` 是纯 matplotlib 手绘实现。已在 `d24bb9e` 从两份清单删除，并补上漏声明的 `mootdx`。
- **未被引用的配置常量（已核实修正）**：原表述「`TDX_HOST/PORT/TIMEOUT`、`TOP_FRACTAL_LOOKBACK`、`TRADING_START_*/END_*`、`KLINE_INITIAL_MONTHS` 均无引用」**有两处错误**，全量核实后更正如下：

  | 常量 | 原判断 | 实际 |
  | --- | --- | --- |
  | `TDX_HOST` / `TDX_PORT` / `TDX_TIMEOUT` | 无引用 | **有引用**，`data/market_data.py:182-187` 与 `:363-367` |
  | `TRADING_START_*` / `TRADING_END_*` | 无引用 | **有引用**（曾错在 `is_trading_time()` 硬编码未接），现已接入 `utils/__init__.py` |
  | `KLINE_CACHE_TTL_SEC` | 未提及 | **无引用**，本轮新发现 |
  | `KLINE_INITIAL_MONTHS` | 无引用 | 无引用；实际取数窗口写死在 `market_data_manager.py:271` 的 `days_map`（`daily: 126` ≈ 6 个月） |
  | `STOP_LOSS_DEFAULT` | 未提及 | **无引用**，本轮新发现 |
  | `TOP_FRACTAL_LOOKBACK` | 无引用 | 无引用；且原注释「30分钟顶分型」与实现（读 60min）不符，已列入 `BUSINESS_RULES_CONFIRMATION.md` Q1 |
  | `CHART_STYLE` | 未提及 | **无引用**；随 `mplfinance` 移除而失效，本轮新发现 |

  处理方式：**标注而非删除**（`config.py` 中标记 `[未接线]`）。理由是这些常量可能是原作者预留的未完成功能，删除是破坏信息的；现已把「哪些无引用、实际取值写在哪里」记录下来，确认无用后可安全清理。
- **工作区已收口（v2）**：`core/technical.py`、`data/market_data_manager.py`、`ui/main_window.py` 三个修复已连同 `tests/test_market_data_manager.py` 一起提交（`6f54be0`）。此项已关闭。

## 7. 建议的接手顺序

1. ~~**先提交，不要回退。**~~ **（已完成，`6f54be0`）** 四个 P0 修复已连同 `tests/test_market_data_manager.py` 一起提交，基线干净。唯一残留动作：给这四个修复补回归测试（见下条）。
2. **立刻补 4.0 节四个修复的回归测试。** 分型索引还原、`flush_today_bars`、托盘退出、每日止损标记——这四处现在**没有一行测试保护**，全靠人工核对。这是当前投入产出比最高的一件事，改动一旦被后人无意回退，崩溃和僵尸进程会原样回来。
3. **补回缺失的输入**：向原开发者索取 BUG 报告和私有审阅记录，书面确认三个业务口径——30 分钟还是 60 分钟周期、止损规则的真实意图（见 4.3 第 2 点）、做 T 的资金模型。**这一条不完成，第 4 条的两项就没法验收。**
4. **修 P0，按此顺序**：
   - `4.1` 做 T 资金/仓位双向守恒——**最高优先**。修完必须有一条 `资金守恒 + 底仓不变` 的断言测试，否则等于没修。
   - `4.2` `force_close()` 强制平仓语义——把"强制"实现为不看盈亏价格、直接按现价平仓；同时补一条"状态机不会卡在 `WAIT_SELL/WAIT_BUY`"的测试。
   - `4.3` 止损：先做第 1 点（docstring 与实现对齐）和第 3 点（`buy_idx == -1` 时告警），口径问题等第 3 条确认后再动。
5. **让测试可离线**：给所有 AKShare 调用加 mock，把 `test_market_data.py` 拆成「纯逻辑」和「网络集成」两层。**这条是第 2、4 条的前置条件**——当前测试依赖真实网络，意味着任何回归测试在离线和 CI 环境都跑不起来。
6. **拆 `main_window.py`**：优先把「持仓盈亏汇总」「止损止盈编排与冲突处理」「买点状态维护」三块抽到 `core/` 下的独立模块，GUI 只保留展示与转发。同时把 `core/`、`data/` 里的 QThread Worker 上移，解除业务层的 PyQt 依赖。
7. **性能收口**：回测改增量计算、DB 改共享连接、买点扫描改 `QThreadPool`、冲突弹窗加去重。
8. **清文档**：删除悬空引用，修正过期的测试警告，补齐 `mootdx` 依赖。

## 8. 结论

投入产出比取决于目标：

- **当作个人自选股观察工具**：现状可用。四个最急的运行时缺陷已提交；剩下的 4.2 值得优先处理（一条 `if` 就能消除"漏一次平仓 = 丢一天机会"的级联），4.3 按 4.3 节的新顺序拆成两步做。风险可控。
- **当作自动交易或资金管理工具**：**当前不可用，且比 v1 判断更严格。** 做 T 引擎的两条路径都不守恒，且 `BUY_FIRST` 路径在 5 轮内会把可用资金耗尽并静默失效——这意味着它当前不是一个"算错钱"的模块，而是一个"跑几次就停机"的模块。资金模型必须重写并补守恒测试，止损规则必须重新定义并回测验证。
- **当作学习项目/代码样本**：分层设计、双层缓存、线程编排、缠论实现都值得读；`main_window.py` 则是一个很好的反面教材。**另外值得一读的是 4.1——它是"状态量记账不对称"这类缺陷的教科书样本：每个单独的分支看起来都合理，只有把开仓和平仓放在一起对账，才会发现钱被记漏了。**

---

*本报告 v1 基于静态代码审阅；v2 增补部分（做 T 引擎的实测量化）在隔离 venv 中实际驱动 `TTrader` 跑通了 `BUY_FIRST` / `SELL_FIRST` 两条完整开平仓路径及 `force_close()` 边界。程序主体（GUI、网络数据源、数据库层）仍未实际运行，相关判断依据正文标注的行号供复核。*
