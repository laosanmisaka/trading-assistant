# 项目全面评审报告（2026-10-03）

## 1. 结论与评审基线

项目已有可复用的行情管理、SQLite 存储、PyQt 界面、缠论桥接和离线测试基础，但**当前版本尚不能据此确认策略具有可实盘复现的超额收益，也不宜把界面中的盈亏和提醒状态视为可靠账本**。主要阻碍是信号确认时序、回测样本与实时事件不等价、收益基准权重不一致，以及持仓、提醒持久化和行情更新中的确定性错误。

本报告登记 **19 个代码问题组、5 类文档与交付问题**。问题组中包含同一模块的多个边界错误，并非声称发现了 19 个彼此独立的新缺陷。与既有 KI 台账重叠的部分明确标注，不重复计为新发现。未发现有充分证据应定为 P0 的问题。

- 上游：`laosanmisaka/trading-assistant`，默认分支 `master`。
- Fork：`ThereWasAYang/trading-assistant`，默认分支 `master`。
- 同步前本地：`fea145d`；工作区干净。
- 已获取两端远程引用，并将本地 `master` 快进到 **`2edb3fb6b8a85a11f75b9ad2f387637c52e4c92a`**；同步时两端 `master` 一致，比原本地前进 67 个提交。
- 评审基线：[2edb3fb](https://github.com/laosanmisaka/trading-assistant/tree/2edb3fb6b8a85a11f75b9ad2f387637c52e4c92a)，提交时间 2026-10-02 22:30:12 +08:00。
- 本次交付只增加评审报告、离线复现脚本和结果快照，**未修复或改写被评审的业务代码**。

优先级：P1 表示会实质影响信号、资金或数据可靠性，应优先修复；P2 表示特定场景的功能错误、稳定性或维护风险；P3 表示结构、表述和导航改进。证据分为“复现”“静态”和“材料不足”。静态发现给出触发条件，不把潜在后果描述成已经发生的生产事故。

## 2. 范围与方法

基线共 102 个受 Git 管理的文件，其中 76 个 Python 文件、23,477 行 Python 文本（含注释和空行），`docs/` 下有 10 份 Markdown 文档。按目录核查入口、调用关系、数据边界、异常处理、现有测试及说明的一致性；高风险链路进一步离线复现。

| 范围 | 基线数量 | 重点与覆盖方式 |
| --- | ---: | --- |
| 根目录 | 10 个文件，3 个 Python | 启动、配置、依赖、环境、Git 忽略规则、README、AGENTS、批处理 |
| `core/` | 19 个 Python，5,443 行 | 提醒、缠论结构/买卖点/多周期/三买、图表、通用回测、做 T 模型；调用链与边界核查 |
| `data/` | 5 个 Python，2,168 行 | SQL、交易汇总、缓存、行情来源、初始加载及增量更新 |
| `ui/` | 8 个 Python，2,866 行 | 主窗口调度、线程、交易/提醒对话框、图表、表格、纪律设置；静态及无头测试 |
| `scripts/` | 21 个文件，其中 14 个 Python、5,850 行 | 数据获取、选池、盘后例程、实时监控、策略评估、诊断、报告导出；池文件格式与用途 |
| `utils/` | 3 个 Python，205 行 | 时间判断、日志、TTL 缓存 |
| `tests/` | 24 个 Python，6,762 行 | 全量默认套件、fixture 隔离、跳过项及关键遗漏 |
| 文档 | 10 份 `docs/*.md`，另有 README/AGENTS | 当前说明、历史决策、代码引用、证据可达性、依赖与命令一致性 |
| 静态资源 | 2 个文件 | 交易纪律文本及 ECharts 文件头/使用方式；未对压缩后的第三方 JS 做逐行安全审计 |

验证方法包括：全部受控 Python 文件 AST 解析、默认 pytest 套件、真实 `czsc 1.0.1` 的逐前缀重算、隔离 SQLite、合成行情、受控故障输入、Markdown 相对链接检查。没有用真实资金或用户交易库复现问题。

边界：没有启动长期监控、注册 Windows 计划任务或运行终止进程的批处理；没有进行 Windows 托盘/气泡的真实桌面验收、外部行情端到端测试或大样本联网回测；没有把测试通过率当作代码覆盖率。未提供的历史 `outputs/` 报告、行情快照及私有记录不能由本次评审补作真实性认证。

## 3. 代码问题总表

| ID | 级别 | 问题 | 证据 |
| --- | --- | --- | --- |
| R01 | P1 | “下一笔终点”早于下一笔实际可观测时刻，确认和成交被回填 | 真实 czsc 前缀复现；KI-009 的当前链路补充 |
| R02 | P1 | 全历史选点与盘中候选事件不等价，“无未来函数”结论过强 | 静态；部分已见 KI-009 |
| R03 | P1 | 日线缓存不含今日时，今日实时买点被分组过滤 | 离线复现 |
| R04 | P2 | 滚动窗口中的笔索引不是稳定事件身份 | 真实 czsc 复现 |
| R05 | P2 | 修订记录按确认时间竞争，旧价格/旧资格不能可靠撤销 | 离线复现及静态 |
| R06 | P1 | 同时设置手动止损和止盈，后一次写入清掉前一项 | 隔离数据库复现 |
| R07 | P1 | 交易对话框盈亏重复扣减买入成本 | 实际 UI 方法离线复现 |
| R08 | P1 | 已清仓交易进入新持仓成本与首次买入日期 | 隔离数据库复现及静态 |
| R09 | P1 | 初始今日行情混入多日数据；回退路径丢弃真实 OHLC | 离线复现及静态 |
| R10 | P1 | 历史缺口与周/月/60 分钟数据没有持续刷新闭环 | 静态调用链 |
| R11 | P2 | 初始取数队列依赖早于线程结束的完成信号 | 静态线程时序 |
| R12 | P1 | 通用回测的最低佣金、零权重、初始回撤处理错误 | 三项离线复现 |
| R13 | P1 | 无重叠合并、部分下载成功被当作完整复权缓存 | 离线复现及静态 |
| R14 | P2 | 盘后例程参数未传递，失败后继续使用旧 watchlist 并成功退出 | 静态 |
| R15 | P2 | 一只股票的异常 bar 可退出整个监控，状态提交有中断窗口 | 解析错误复现及静态 |
| R16 | P2 | 监控日历/跨日更新与启动脚本不具备可移植的运行契约 | 周末判断复现及静态 |
| R17 | P2 | 无匹配中文字体时图表图例抛异常 | 默认套件 2 项失败及强制回退复现 |
| R18 | P1 | 点收益与标的等权基准混减，bootstrap 统计对象不一致 | 数值复现 |
| R19 | P2 | HTML 标题与内联 JSON 未做上下文转义 | 生成源码复现，未执行注入脚本 |

## 4. 详细代码发现

### R01 — 确认时刻不能直接取下一笔的终点（P1）

位置：[core/triple_buy.py:58–63][src-confirm]，同类实现见 `scripts/eval_daily_5min.py::daily_confirm_time`，消费者包括 `triple_buy_trades` 与 `monitor_buy.detect_entries`。

函数把 `bis[bi_idx + 1].edt` 当成“后续反向笔已经形成”的时间。实际 `edt` 是笔的几何端点；端点所在 bar 到来时，不代表该笔已出现在当时可得的结构中。复现使用 600 根合成工作日 5 分钟 bar 和真实 czsc：一买端点为 `2024-01-04 13:45`，声明确认时刻为 `14:25`，只输入到 `14:25` 时所需下一笔不存在，直到 `14:30` 才首次观察到该笔。

因此在 `14:25` 收盘价上记成交并不满足模块声明的确认规则。该例只证明时序违约，不能外推所有信号的延迟都为 5 分钟。旧 KI-009 已指出确认滞后，本次补充的是当前日线+5min 主线仍未解决它，而“下一笔终点是保守上界”的旧表述也不能直接沿用。

建议逐 bar 记录信号首次可观测时刻、结构版本与撤销事件，执行使用当时可用数据及明确的成交规则；不要靠固定顺延一根笔或一根 bar 宣布问题解决。验收需同时覆盖信号首次出现、延伸、消失、复现以及真实可用价格。

### R02 — 事后成立的样本不能替代当时面对的候选集合（P1）

位置：[core/triple_buy.py:169–223][src-trades]、[core/chan_points.py:132–169][src-converge]、`scripts/eval_triple_prob.py::find_candidates/eval_one`。

`triple_buy_trades` 在完整历史上构造日线三买，再从突破端点到日线确认之间回溯选择第一个 5min 一买；盘中链路则消费前一轮扫描已经观察到的活跃窗口。最终能成为三买的回调集合，并不等于当时所有突破/回调候选。`_converge_by_center` 还会用同一中枢后续出现的更低候选替换先前候选，导致全量图上的交易集合与当时已发出的信号不同。后一问题已登记在 KI-009，此处确认它仍影响新主线，未计为全新算法发现。

反向统计脚本虽改为突破候选，但仍先用完整结构选中枢/突破笔，再回找 `cross`；“中枢参数最终不变”不能证明其收口及候选资格在入场时已经知道。报告中的“唯一没有未来函数”缺少逐前缀证据。`AGENTS.md` 的“1010/1010 入场相同”即使成立，也仅能说明那批事后样本的两种窗口计算一致，不能证明实时可达性。

建议统一事件回放入口，分别记录候选生成、失效、窗口关闭、成交及修订，用同一段行情逐时驱动回测和监控并比较完整事件集合；重新计算收益前撤下无条件的可实盘复现结论。本次未取得原始样本，不能量化偏差幅度。

### R03 — 盘中最新买点被“最近两个交易日”过滤（P1）

位置：[core/buy_points.py:38–65][src-days]、`ui/main_window.py:449–458`。

有日线 CSV 时 `recent_trading_days` 直接返回末尾 n 行日期，不使用传入的 `today` 确认当前交易日。9 月 30 日盘中，缓存只到 9 月 29 日，函数返回 28、29 日；9 月 30 日的有效信号即被 `recent_buy_points` 丢弃，GUI“策略买点”分组无法及时收录它。参考股票停牌或缓存过期会进一步扩大偏差。

`_buy_times_map` 仅按信号文件 mtime 缓存，跨日或交易日缓存更新但信号文件未变时也不重新过滤。建议使用交易所日历和明确的 as-of 时刻；将当前交易日及日历版本纳入缓存键。验收至少含盘前、盘中、盘后、停牌参考股、长假与文件未变化的跨日情形。

### R04 — `bi_idx` 无法作为持久化去重身份（P2）

位置：[scripts/monitor_buy.py:168–187][src-dedup]。

监控每次只取最新 5001 根 bar，却用 `code|window_start|bi_idx` 跨轮去重。真实 czsc 复现中，删除输入头部 100 根后，同一条端点为 `2024-01-18 09:40 → 11:15` 的笔从索引 47 变为 38。当前身份会随窗口滚动漂移，可造成同一事件重报，或把新事件当作旧事件修订。

建议使用持久化事件 ID 与可追踪的几何锚点，显式处理末端延伸和结构重排；仅换成可漂移的 `conf` 仍不能解决身份问题。验收需让监控窗口至少完整滚动一次，并检查重复和遗漏。

### R05 — 消费侧没有按事件版本重建最新状态（P2）

位置：[core/buy_points.py:68–105][src-revisions]，生产侧同见 R04。

`recent_buy_points` 先过滤 `strategy_entry` 和日期，再按股票选最大 `conf`，不解析事件身份和修订顺序。复现：原记录 `10:00@10`，后续 revision 纠正为 `09:55@9.5`，界面仍取旧价格 10。若后续修订把 `strategy_entry` 改为 false，先过滤会留下旧的 true 记录。生产侧仅在 `conf` 改变时写修订，同一确认时刻的价格或首选资格变动也会被跳过。

建议先按稳定事件 ID 折叠最新版本与撤销，再按交易日和首选资格筛选，最后按股票聚合；覆盖“确认提前/延后、只改价格、首选撤销、旧点消失”。

### R06 — 手动止损和止盈相互覆盖（P1）

位置：[core/alert_engine.py:48–64][src-manual]、[data/database.py:488–502][src-manual-db]、`ui/main_window.py:863–881`。

`set_manual_sl` 只传 SL，`set_manual_tp` 只传 TP，而数据库方法把未传的另一项默认写为关闭。界面先保存两项，再依次调用两个 engine setter，最终持久化只保留最后一项。隔离数据库中设置止损 9、止盈 12 后，内存仍标记手动止损开启，DB 却是 `sl_active=false, sl_price=0`。重启后手动止损丢失。

建议一次原子更新完整配置，再同步内存；或提供只更新指定字段的 API。验收必须跨新建 `AlertEngine`/重启加载，两侧分别启用、禁用及同时启用均需验证。

### R07 — 浮动盈亏重复扣减成本（P1）

位置：[ui/trade_dialog.py:127–145][src-pnl]。

`profit = total_sell_amt - total_buy_amt` 后，又增加 `(current_price - avg_cost) * hold_qty`。未卖出仓位的成本已经在首式扣过，后式再扣一次。调用实际界面方法复现：买入 100 股 × 10 元、现价 10 元、费用 0，显示 **`-1,000.00 (+0.00%)`**，正确总盈亏应为 0。

建议分清“累计已实现”“当前浮动”“总盈亏”，总盈亏可按卖出净收入 + 当前市值 − 买入总支出核对；百分比也必须有一致的分母和标签。验收覆盖未卖、部分卖、清仓、再买、手续费与无行情。

### R08 — 新持仓继承已结束持仓的成本与日期（P1）

位置：[data/database.py:328–367][src-cost]，消费者包括 `AlertEngine.calc_take_profit/calc_stop_loss` 和主窗口持仓盈亏。

`avg_cost` 使用历史全部买入金额除以历史全部买入数量，卖出只影响持有数量。复现：100 股 @10 买入、@11 清仓，再买 100 股 @20；当前成本输出 15，当前仓位实际买入成本为 20。`get_first_buy_date` 也取历史最早买入日，而非本轮持仓开始日，可能让自动止损追溯到上一轮持仓。

建议先定义明确的移动平均/成本结转规则，按交易日期及同日稳定顺序回放，清仓后重置持仓周期；将“历史平均买价”与“剩余仓位成本”分为不同字段。清仓再开仓时还要清理旧提醒锁定状态。该问题不属于未接 UI 的做 T 资金 bug，而发生在当前交易记录链路。

### R09 — 今日行情聚合范围错误，回退数据缺少价格极值（P1）

位置：[data/market_data_manager.py:343–394][src-initial]、[data/market_data.py:411–482][src-sina-fallback]。

初始加载将整段历史 1min 数据的开盘、高低与成交量聚合为最新日现价记录，没有先按最后交易日期过滤。复现三个交易日价格分别 10、20、30，每日成交量 100：最新日正确开盘应为 30、量 100，实际得到开盘 10、量 300。错误数据进入现价缓存，影响行情显示和消费 `quote.low` 的止损逻辑，直到后续有效刷新覆盖。

另一条独立错误发生在 Sina 回退：上游返回完整分钟 OHLC，`fetch_intraday_data` 只保留 close，随后用 close 填满 open/high/low。某分钟真实 high=12、low=8、close=10，回退后高低都变为 10。建议日线严格按交易日分组，回退保留真实 OHLC；测试多日初始化、缺分钟、换源后高低价保持及复权标识。

### R10 — “已有缓存”被误当成“数据已更新”（P1）

位置：[ui/main_window.py:775–782][src-ensure]、[data/market_data_manager.py:408–469][src-refresh]、`core/alert_engine.py:225–242`。

初始化才获取周线、月线和 60min；常规刷新只更新当日 1min 与今日日线。`_ensure_kline_data` 发现任意日线就退出，未比较末日与应有交易日，应用关闭期间缺掉的日线没有补齐路径。周/月/60min 数据随运行时间老化，手动 F5 也没有完整补齐这些周期的逻辑。

止盈从旧 60min 表寻找顶分型并锁定，没有按本轮买入日期过滤，可能锁定开仓前的历史分型。外部 `daily_routine` 更新的是另一份 CSV 缓存，不会自动修复 GUI 的 SQLite 缺口。

建议每个周期维护末次成功日期/来源/复权方式和缺口状态；启动、跨日和强刷走可重试的区间补齐流程。验收应模拟关机数个交易日后重启、60min 顶分型晚于开仓、日周月一致性，不能只检查“有数据”。

### R11 — 初始取数 Worker 生命周期和队列存在竞态（P2）

位置：[ui/main_window.py:784–810][src-queue]、`ui/main_window.py:982–1011,1141–1169`、`data/market_data.py:712–726`。

`InitialFetchWorker.run` 在返回前发送 `all_done/error_occurred`；槽函数随即调用 `_pump_init_fetch_queue`，后者若发现旧线程仍在运行就返回，且没有连接 `finished` 再推进队列。若槽执行早于 `run` 真正结束，排队工作可停住。手动连续添加股票又直接覆盖 `_init_worker`，没有统一串行入口和存活对象集合；退出应用也未等待活动 worker 结束。

这是静态时序风险，本次未制造本机 GUI 崩溃。建议结果信号只处理数据，`finished` 负责释放/推进队列，手动与自动取数共用调度器，退出先停止接收任务再等待退出。现有 ChanMarkWorker 的单飞测试不能证明 InitialFetchWorker 同样安全；需补快速完成、慢完成、连续添加及退出中取数的事件循环测试。

### R12 — 通用回测的三个财务边界错误（P1）

位置：[core/backtest/engine.py:154–159][src-afford]、`core/backtest/engine.py:192–210,261–268`；绘图回撤也未以初始资金建立峰值。

1. **R12a 最低佣金**：可买手数只按比例佣金估算，未计最低 5 元。资金 1001、价格 10 时买 100 股，加佣金支出 1005，现金变成 −4。
2. **R12b 零权重**：`float(getattr(s, "weight", 1.0) or 1.0)` 将明确的 `weight=0` 变成满权重；复现发生买入。
3. **R12c 初始回撤**：峰值从负无穷开始，忽略初始资金。初始 1000、曲线 `[900, 950]` 输出最大回撤 0，而从初始资本应为 10%。

建议预算满足含全部费用的约束，显式区分 None 与 0，以初始资金建立权益峰值；补不超支、零权重不成交、首日亏损、空曲线和正负费用边界用例。该执行器目前不是最新三买脚本的收益统计入口，不能把修复它当作已经修复 R01/R02/R18。

### R13 — 缓存完整性和复权验证不足（P1）

位置：[scripts/daily_routine.py:77–164][src-cache]、[scripts/fetch_min.py:254–302][src-chunks]。

`merge_incremental` 没有重叠行时仍直接返回 `appended`，没有证据判断旧新序列处于相同复权尺度。复现旧区间 close=10、新区间 close=5，无重叠仍得到 `[10,10,5]`；这证明函数接受了未经验证的拼接，并不声称该人工样例真实发生过除权。

全量取数允许部分分段成功并把失败区间放在统计信息中；`update_one` 用 `df, _` 丢弃统计，在首次下载或复权重抓时直接覆盖 CSV。若中段失败但尾段到今天，下次只看末日便返回 `fresh`，内部缺口长期保留；覆盖写中断也可能截断原有效缓存。实时 Sina 序列与 baostock 前复权缓存还需明确换源/调整契约，不能仅凭都是 5min 认为结构可对账。

建议无重叠时扩窗验证或全量重抓，记录分段完整性和失败区间，校验成功后用临时文件原子替换，保留旧有效版本；对停牌缺口与下载失败分别标记。验收要故障注入“中段失败尾段成功”“复权后无重叠”“写入中断”。

### R14 — 盘后任务会扫描错误缓存并以成功状态结束失败流程（P2）

位置：[scripts/daily_routine.py:303–363][src-routine]。

`--cache-dir` 只传给取数，没有传给 `scan_candidates.py` 子进程；`--limit` 只限制更新池，扫描仍使用完整原池。自定义目录或调试小池时，结果会混入旧默认缓存。扫描退出非零只记录日志，随后仍读取已有全局 `WATCHLIST`、同步 GUI 自动分组并返回 0；全量取数失败也缺少整体失败退出契约。

建议沿流程传递同一配置和本次运行 ID，只消费本次成功生成且校验通过的产物；失败时保留旧分组但明确其过期状态，返回非零；明确“只更新”和“只扫描”的依赖关系。验收需断言子进程参数、失败退出码和旧产物不被当作新结果。

### R15 — 异常行情缺少逐股隔离与可靠状态提交（P2）

位置：[scripts/monitor_buy.py:63–88][src-parse]、`scripts/monitor_buy.py:155–163,305–306`。

请求和 JSON 解析在 try 内，bar 字段转换在 try 外；`open="bad"` 即向外抛 ValueError，`run_round` 对每只股票没有兜底，后续标的停止扫描。已发出的事件逐条追加到 JSONL，但去重 state 只在整轮结束写入；轮中异常/退出后重启可能重报，直接 `write_text` 中断还可能损坏状态文件。

建议校验时间、有限正价格、OHLC 关系和字段类型，逐股隔离；事件与状态采用可恢复的提交/回放协议和原子写。坏数据应带来源、代码、原因计数，不能只吞掉异常。复现仅注入一条无效数字，没有调用真实行情。

### R16 — 监控跨日和启动脚本依赖个人机器（P2）

位置：`scripts/monitor_buy.py:58–60,250–257,281–309`、[scripts/start_monitor.bat:1–7][src-start]、`run.bat:2–3`。

`_in_session` 只看小时分钟，复现周六 `2026-10-03 10:00` 被当作交易时段。watchlist 只在进程启动时读一次，次日不会自动重载；当前设计依赖外部每天重启，而 AGENTS 明确记录计划任务尚未注册。根启动脚本和监控脚本使用个人绝对 Python 路径；监控脚本按命令行包含 `monitor_buy.py` 就强制终止 Python，可能误伤另一份 checkout 的监控。

建议共用带交易日历的会话判断，跨日验证/重载 watchlist，运行路径从仓库/所选解释器导出；若需要替换旧实例，应按本项目实例的 PID、完整路径和启动身份核对。验收包含周末长假、跨日无重启、双 checkout，以及非开发者机器。此评审未执行终止脚本或安装计划任务。

### R17 — 字体回退导致回测图片无法生成（P2）

位置：[core/backtest/viz.py:22–29,99][src-font]。

本机找不到候选中文字体时返回字符串 `sans-serif`，随后 `legend(prop={"family": font})` 被当前 Matplotlib 作为 fontconfig pattern 解析，在连字符处抛 ValueError。默认套件两个图表测试因此失败。复现脚本强制这一回退分支后得到同一异常，说明不只是测试临时目录问题。

建议用明确的字体 family 列表或正确构造 FontProperties，并测试无中文字体的无头环境；字体缺字应可降级，不能阻止导出。需要另外对 Windows/中文字体存在时做验证。

### R18 — “点均超额”混用了不同样本权重（P1）

位置：[scripts/eval_daily_5min.py:750–837][src-metrics]。

`pt_mean` 按每个信号加权，`base` 却按出现信号的股票等权，然后二者相减作为 `ex_pt`。例：A 股票有 9 个信号，每次收益 10%，该股基准也是 10%；B 有 1 个信号，收益和基准均为 0。每个信号相对各自基准的超额都是 0，但代码返回点均 9%、基准 5%、**超额 +4 个百分点**。标的等权超额 `ex_eqw=0` 则符合这个例子。

`cluster_bootstrap` 延续混合权重，而 `block_bootstrap` 为每个点复制相应股票基准，二者估计的不是同一个统计量。不能把两种区间并列当作同一个“超额”的两种稳健验证。

建议先定义估计对象：点均应对每点的 `ret - base[code]` 求均值；标的等权应先按股聚合再等权。点估计、各类 bootstrap、去重叠及删样本分析必须使用同一函数。重新出报告并注明这是校正权重，不等于建立了时间匹配或可交易的对照基准。本次证明的是统计定义错误，未重算既有真实市场收益数字。

### R19 — 生成 HTML 未转义外部文本与 script 边界（P2）

位置：[core/chan_viz.py:1027–1111][src-html]。

名称/代码直接进入标题和 HTML，`json.dumps` 的结果直接嵌入 `<script>`。JSON 字符串中的字面 `</script>` 会结束 HTML script 元素；普通 JSON 转义不能替代 HTML 上下文转义。复现仅用无副作用注释标记检查生成源码，确认边界原样保留，没有执行脚本。

风险前提是名称/自定义 payload 含不可信文本且用户打开生成的 HTML，并非声称存在公网服务被攻破。建议文本节点 HTML escape，内联 JSON 对 `<` 等字符做安全编码或使用安全的数据载入方案；覆盖 `</script>`、引号、`&` 和正常中文。

## 5. 文档评审

### D01 — 当前说明与历史记录缺少清晰导航（P2/P3）

README 已补充最新三买监控能力，不能笼统称其完全过时；但导航只突出 USER_GUIDE/TECHNICAL_REFERENCE，新监控的运行细节主要在给编码助手的 AGENTS 内。`docs/` 平铺当前手册、历史评估、规划和未完成业务确认，缺少一份告诉读者“现在按哪个版本做”的索引。

建议保留历史材料，建立 `docs/README.md`，按“使用/运维”“当前架构与策略定义”“已知问题”“历史决策与实验”导航。历史报告保留原基线，在开头标记适用版本、已取代部分及替代链接；不要直接把过去正确的事实改写成从未发生过。

### D02 — 文档中的当前事实与代码不一致（P2）

| 文档 | 核查结果 | 建议处理 |
| --- | --- | --- |
| [README](../../README.md) | 依赖示例仍含已移除的 mplfinance，漏列当前依赖；204 passed 是有日期的旧结果，不能代表当前版本。测试节说明真实库隔离已修，接手建议第 3 条却仍要求修同一个问题 | 依赖只引用维护清单；保留旧测试结果为历史或更新为有环境/基线的新结果；删除矛盾的待办 |
| [AGENTS](../../AGENTS.md) | 当前主线、自动分组和脚本覆盖较全；450 passed/9 skipped 是预期，当前 macOS 环境未达到。指定个人 Windows Python 路径，不适用于全部贡献者 | 区分项目规则、开发者本机记录与可移植环境说明；记录平台验证矩阵 |
| [USER_GUIDE](../USER_GUIDE.md) | 主界面仍写 9 列，当前 `config.STOCK_TABLE_COLUMNS` 有 10 列并含买点时间；没有完整说明自动分组全量接管、监控/盘后先后顺序、状态文件及恢复；旧买点“不提醒”叙述需限定 GUI | 补脚本监控章节、自动分组使用限制、启动/停机/故障恢复；分清 GUI 标注与脚本气泡 |
| [TECHNICAL_REFERENCE](../TECHNICAL_REFERENCE.md) | 配置表称 `TRADING_*` 未引用，实际 `utils.trading_sessions` 已读取；AlertType 仍列已删除 buy_point；资源表称只有 discipline，漏 ECharts；scripts 只介绍 scan_pool；表格仍写 9 列；`_connect` 仍描述每次设置 WAL | 按当前导出符号和数据流复核；WAL 的初始化与连接参数分开写；补 CSV/JSONL/SQLite 的边界 |
| [KNOWN_ISSUES](../KNOWN_ISSUES.md) | KI-004 开头已标删除，表格仍“未修”；KI-005“全项目只有 1/60min”不再适用；KI-008 称无逐股兜底，当前 `refresh_minute_bars_batch` 已逐股捕获；KI-010 称三买只影响图，当前三买已是主线 | 更新状态和影响面，保留原始日期及证据；把旧链路与新链路风险拆开 |
| [BUSINESS_RULES_CONFIRMATION](../BUSINESS_RULES_CONFIRMATION.md) | 原始基线已注明，是历史确认材料；Q3 等待做 T 决策与 KI-001“暂不实现”未闭环，周期问题的项目级描述已经过时 | 加决策结果/日期/负责人/后继文档，明确哪些问题仍待确认 |
| [PROJECT_ASSESSMENT](../PROJECT_ASSESSMENT.md) | 接手时快照有历史价值，涉及已改动/删除模块及旧规模、旧测试，不能作为当前架构清单 | 加历史状态与本次报告链接，保留原结论的时间上下文 |
| [TASK_LIST_REVIEW](../TASK_LIST_REVIEW.md) | 描述的是当时任务清单；其中同步名称阻塞、旧 scanner、旧依赖项等已发生变化 | 标为历史复核，逐条链接后续修复/放弃记录，避免重新开启已闭环任务 |
| [CZSC_INTEGRATION](../CZSC_INTEGRATION.md) | 实测版本和输入约束有价值；“1.0.1 最新”限定于 9 月 17 日，不应当作当前版本查询；尚未完成/API 接入描述与后续链路交错 | 明确测试版本/平台/命令，引用相应版本源码；把实测事实与对未来版本的推断分开 |
| [PLAN_BUYSELL_GEOMETRY](../PLAN_BUYSELL_GEOMETRY.md) | 多轮修订与被否决方案共存，保留了决策原因，但阅读者难以找到最终状态 | 顶部增加当前状态与历史版本目录，链接最终实现和仍未解决的因果性问题 |
| [STRATEGY_CHAN_MULTIFREQ](../STRATEGY_CHAN_MULTIFREQ.md) | 记录日线+30min 旧研究；AGENTS 已弃用其作为研究主线，但 `ChanMarkWorker` 仍计算该标注，因此既不能说“当前唯一策略”，也不能说“代码已删除” | 明确研究地位、GUI 兼容显示和未修偏差，指向当前日线+5min 策略 |
| [TRIPLE_BUY_WALKTHROUGH](../TRIPLE_BUY_WALKTHROUGH.md) | 写于 9 月 23 日；主实现仍定位到 eval_daily_5min 的 1094 行快照，遗漏后来抽出的 core/triple_buy 及实时链路；大量手写函数行号漂移 | 按函数名/固定提交链接替代易失行号，补候选→实时→修订→GUI→统计的完整图谱 |

代码 docstring 也属于文档：`core/backtest/viz.py` 声称由 `ui/backtest_tab.py` 消费，但此文件不存在；`core/triple_buy.py` 称信号两处同源、不许各写一份，实际 eval 脚本仍复制确认及执行逻辑；`core/chan_viz.py` 的 T+1 说明未覆盖 KI-009 的结构确认延迟。建议文档测试包含符号/路径存在性，而非只检查 Markdown 语法。

### D03 — 关键策略数字缺少可取回、可定位的证据（P1）

`AGENTS.md` 要求数字以 `outputs/` 为准，但这些产物被忽略；新 clone 无法取得 `outputs/better_buy_sell_20260928.md`、多份 alpha/稳健性报告和 `.workbuddy/memory/MEMORY.md`。所提 `scripts/pool_random501.txt` 也未入库。当前 7 个显式 Markdown 相对链接的目标均存在，**这不能证明引用完整**：最重要的证据多数只是反引号路径，不在普通链接检查范围内。

因此本次未确认收益、胜率、1010/1010 对账及参数选择的原始数字；也不据此断言原作者没有做过实验。应将可公开的精简报告、命令、基线 SHA、依赖版本、池快照及哈希放入可版本化目录或稳定发布附件；行情若不能分发，至少提供来源、取数时间/复权方式/失败区间、哈希与再生成步骤。结论须能追到逐笔记录，不能让忽略目录充当唯一权威。

另外，`scripts/build_pool.py::build_random` 从**当前**上市列表抽样且按当前名称排除 ST/退市，并不能消除历史回测的存活偏差；固定随机种子不等于当时可得样本。建议使用各历史时点的可投资范围，或将结论明确限定为当前存活样本，记录缺失/新上市标的。此处未估算偏差大小。

### D04 — 环境清单与可运行边界没有闭环（P2）

- `requirements.txt` 限定 `czsc>=1.0.1,<2`，`environment.yml` 缺少 `<2`，与两文件“保持同步”的声明冲突。
- baostock 是 `fetch_min`/`daily_routine`/`build_pool` 的直接依赖，却未在两份清单声明。本次环境中原本未安装；安装的 akshare/czsc/mootdx 元数据也均未声明 baostock 依赖，不能依赖间接安装。
- pandas 声明下限 2.0，但 `data/market_data.py:121` 使用 `ME` 频率别名；该别名对应 pandas 2.2 的变更，最低支持版本与代码不一致。参见 [pandas 2.2 官方发布说明](https://pandas.pydata.org/docs/whatsnew/v2.2.0.html#deprecate-aliases-m-q-y-etc-in-favour-of-me-qe-ye-etc-for-offsets)及 [pandas 2.0 时间序列文档](https://pandas.pydata.org/pandas-docs/version/2.0/user_guide/timeseries.html#offset-aliases)。本次在 pandas 3.0.3 运行，未另建最低版本环境。
- 没有受控的完整锁定快照或自动化 CI 配置。开放版本范围本身不是缺陷，但必须用已验证版本与最小/主要平台测试说明实际支持范围；个人批处理不能充当跨平台安装指南。

建议一个依赖来源生成另一份环境清单，显式区分 GUI、研究/监控和开发依赖；保留一份带日期的平台验证快照，至少让干净环境能 import 并运行默认套件和离线复现。

### D05 — 测试、维护与资源来源说明仍有缺口（P2/P3）

现有测试已具备临时数据库、网络开关、多个策略边界和线程契约测试，这是可继续建设的基础。但 `tests/test_triple_buy.py:70` 用 `skipif(True)` 永久跳过结构链对账，理由是 scripts 曾经对账；这种私有/一次性验证不能持续覆盖共享主线。本次套件通过的部分也没有覆盖手动 SL+TP 重启、清仓再买、实时事件修订、滚动身份及真实前缀因果性。

建议优先补本报告每项的行为验收，并把可复用策略合同测试纳入默认离线套件，再接最少一个无头 CI 作持续验证。无需为所有纯展示改动机械增加测试。

仓库未包含项目级 LICENSE/NOTICE 或第三方来源清单。ECharts 压缩文件保留了版权头，不能说其署名被删除；但依赖说明不足以帮助下游确认项目授权及资源来源。建议维护者明确项目授权，并记录 vendored ECharts 的版本、上游地址、哈希和相应许可材料。本条是交付可追溯性缺口，不作侵权判断。

## 6. 既有问题台账复核

| 既有项 | 本次处置 |
| --- | --- |
| KI-001 / KI-002 做 T 资金与强平 | 未接主 UI；仍应禁止把模拟结果当验证完成。沿用既有风险，不重复登记为新 P1，也不擅自恢复已决定暂不实现的功能 |
| KI-003 止损 120 天窗口 | warning 不等于完整历史；属于已知业务/窗口限制，与本次 R08/R10 的旧持仓及缺行情问题区分 |
| KI-004 旧 scanner | 代码已删除；应关闭为历史，不再列为待修 |
| KI-005 周期口径 | 自动止盈实际仍读 60min；全项目不支持 30min 的旧描述已失效；应按具体链路确认 |
| KI-006 WAL 优化 | 当前已在 init_db 设置；不要根据旧技术文档再次改回高频连接路径 |
| KI-007 止损止盈显示 | 表格占位与图表未接线仍是既有问题；不能用“提醒能触发”掩盖本次发现的持久化错误 |
| KI-008 空列表与数据源失败 | 问题边界仍在，但“调用链无逐股容错”的阻塞理由应更新 |
| KI-009 时序与重绘 | 仍有效；R01/R02 将范围延伸并验证到当前 5min 主线，旧量化数字本次未重跑 |
| KI-010 重复中枢/声明一致性 | 当前三买链路改变了影响面，不能继续仅称图形污染；依赖上限只在一份清单落实 |

## 7. 验证结果与可复现材料

### 7.1 环境与结果

使用项目专用 conda 环境 **`trading-assistant-py312`**，按本次用户要求补装了 czsc、mootdx、baostock 及其依赖；没有提交本机环境文件或包缓存。

| 项目 | 实测 |
| --- | --- |
| 平台 / Python | macOS / Python 3.12.13 |
| pytest / pandas / numpy | 9.0.3 / 3.0.3 / 2.4.6 |
| PyQt5 / Matplotlib | 5.15.11 / 3.10.9 |
| czsc / mootdx / baostock / akshare | 1.0.1 / 0.11.7 / 0.9.4 / 1.18.64 |
| `pip check` | No broken requirements found |
| 全量默认测试 | **2 failed, 448 passed, 9 skipped in 29.05s** |
| Python 静态语法 | 基线全部 76 个 Python 文件 AST 解析通过 |
| 复现脚本 | 15 个 JSON 证据记录，脚本断言全部通过；R12 一条记录含三个边界 |

失败用例：

```text
tests/test_backtest.py::test_report_figure_has_two_panels_and_marks
tests/test_backtest.py::test_save_report_chart_writes_png
core/backtest/viz.py:99
ValueError: sans-serif
ParseException: Expected end of text, found '-' (at char 4)
```

9 个跳过项由默认网络测试开关及永久跳过的 triple-buy 结构对账组成。没有将它们记为通过。初次尝试遇到 czsc 默认缓存目录的沙箱写入限制，随后用 `CZSC_HOME` 指向仓外临时目录完成正式运行；表中是正式运行结果，不是首次收集失败结果。

### 7.2 复现方法

在仓库根目录、所列依赖环境下运行：

```bash
conda activate trading-assistant-py312
TA_REVIEW_TMP=$(mktemp -d /tmp/trading-assistant-review.XXXXXX)
export CZSC_HOME="$TA_REVIEW_TMP/czsc"
export MPLCONFIGDIR="$TA_REVIEW_TMP/mpl"
export QT_QPA_PLATFORM=offscreen
export PYTHONDONTWRITEBYTECODE=1
python -m pytest -q --basetemp="$TA_REVIEW_TMP/pytest"
python docs/reviews/reproduce_2026_10_03.py
python -m pip check
```

附件：[复现脚本](reproduce_2026_10_03.py)、[本次 JSON 输出](evidence_2026_10_03.json)。脚本不联网，不操作用户数据库，不执行终止进程命令；临时 CSV/JSON/SQLite 自动回收，应用正常日志可能写入忽略的 `logs/`。合成 bar 使用工作日内的 5 分钟会话，仅用于结构验证，不假装是交易所历史数据或假日日历。

注意：这是**缺陷复现脚本**，断言验证“该基线确实表现出错误”；修复后对应断言应失败并被替换为正式正确性测试。不能把脚本退出 0 解释为项目无缺陷，也不应直接把这些反向断言作为未来 CI 的通过条件。

## 8. 建议整改顺序与验收出口

1. **先修实际数据与账户显示**：R06/R07/R08/R09/R10。用隔离数据库复现完整持仓周期、重启恢复及行情补齐，证明提醒配置不会丢失，账面量价及盈亏可核对。
2. **建立因果事件合同**：R01/R02/R04/R05，并处理 R03。真实 czsc 逐前缀计算，离线回放与监控使用同一事件状态机；稳定记录首次出现、修订、撤销、候选失效与成交，不允许把后来信息回填成当时已知。
3. **保证数据与运行可靠性**：R11/R13/R14/R15/R16。覆盖下载部分失败、崩溃恢复、跨日、换源、任务队列结束和退出；自动任务的退出状态必须可判断。
4. **重做统计和通用回测核对**：R12/R18。统一权重后重跑公开可定位的数据快照；连同失败/未成形候选一起报告，使用样本外时段，区分按点收益与组合资金收益。
5. **补平台与文档闭环**：R17/R19、D01–D05。完成字体回退、HTML 边界、依赖同步、当前文档索引和版本化证据；再更新测试基线与已知问题状态。

本报告完成的是基线评审和证据交付。现有真实收益数字的纠正、生产代码修复及 Windows/真实行情验收仍属于后续整改工作，不能因报告 PR 合入而视为已解决。

## 9. 固定基线代码引用

以下引用全部固定到被评审提交，避免后续修复后行号漂移。文档矩阵中的相对链接用于导航，历史事实以本报告顶部 SHA 为准。

[src-confirm]: https://github.com/laosanmisaka/trading-assistant/blob/2edb3fb6b8a85a11f75b9ad2f387637c52e4c92a/core/triple_buy.py#L58-L63
[src-trades]: https://github.com/laosanmisaka/trading-assistant/blob/2edb3fb6b8a85a11f75b9ad2f387637c52e4c92a/core/triple_buy.py#L169-L223
[src-converge]: https://github.com/laosanmisaka/trading-assistant/blob/2edb3fb6b8a85a11f75b9ad2f387637c52e4c92a/core/chan_points.py#L132-L169
[src-days]: https://github.com/laosanmisaka/trading-assistant/blob/2edb3fb6b8a85a11f75b9ad2f387637c52e4c92a/core/buy_points.py#L38-L65
[src-dedup]: https://github.com/laosanmisaka/trading-assistant/blob/2edb3fb6b8a85a11f75b9ad2f387637c52e4c92a/scripts/monitor_buy.py#L168-L187
[src-revisions]: https://github.com/laosanmisaka/trading-assistant/blob/2edb3fb6b8a85a11f75b9ad2f387637c52e4c92a/core/buy_points.py#L68-L105
[src-manual]: https://github.com/laosanmisaka/trading-assistant/blob/2edb3fb6b8a85a11f75b9ad2f387637c52e4c92a/core/alert_engine.py#L48-L64
[src-manual-db]: https://github.com/laosanmisaka/trading-assistant/blob/2edb3fb6b8a85a11f75b9ad2f387637c52e4c92a/data/database.py#L488-L502
[src-pnl]: https://github.com/laosanmisaka/trading-assistant/blob/2edb3fb6b8a85a11f75b9ad2f387637c52e4c92a/ui/trade_dialog.py#L127-L145
[src-cost]: https://github.com/laosanmisaka/trading-assistant/blob/2edb3fb6b8a85a11f75b9ad2f387637c52e4c92a/data/database.py#L328-L367
[src-initial]: https://github.com/laosanmisaka/trading-assistant/blob/2edb3fb6b8a85a11f75b9ad2f387637c52e4c92a/data/market_data_manager.py#L343-L394
[src-sina-fallback]: https://github.com/laosanmisaka/trading-assistant/blob/2edb3fb6b8a85a11f75b9ad2f387637c52e4c92a/data/market_data.py#L411-L482
[src-ensure]: https://github.com/laosanmisaka/trading-assistant/blob/2edb3fb6b8a85a11f75b9ad2f387637c52e4c92a/ui/main_window.py#L775-L782
[src-refresh]: https://github.com/laosanmisaka/trading-assistant/blob/2edb3fb6b8a85a11f75b9ad2f387637c52e4c92a/data/market_data_manager.py#L408-L469
[src-queue]: https://github.com/laosanmisaka/trading-assistant/blob/2edb3fb6b8a85a11f75b9ad2f387637c52e4c92a/ui/main_window.py#L784-L810
[src-afford]: https://github.com/laosanmisaka/trading-assistant/blob/2edb3fb6b8a85a11f75b9ad2f387637c52e4c92a/core/backtest/engine.py#L154-L159
[src-cache]: https://github.com/laosanmisaka/trading-assistant/blob/2edb3fb6b8a85a11f75b9ad2f387637c52e4c92a/scripts/daily_routine.py#L77-L164
[src-chunks]: https://github.com/laosanmisaka/trading-assistant/blob/2edb3fb6b8a85a11f75b9ad2f387637c52e4c92a/scripts/fetch_min.py#L254-L302
[src-routine]: https://github.com/laosanmisaka/trading-assistant/blob/2edb3fb6b8a85a11f75b9ad2f387637c52e4c92a/scripts/daily_routine.py#L303-L363
[src-parse]: https://github.com/laosanmisaka/trading-assistant/blob/2edb3fb6b8a85a11f75b9ad2f387637c52e4c92a/scripts/monitor_buy.py#L63-L88
[src-start]: https://github.com/laosanmisaka/trading-assistant/blob/2edb3fb6b8a85a11f75b9ad2f387637c52e4c92a/scripts/start_monitor.bat#L1-L7
[src-font]: https://github.com/laosanmisaka/trading-assistant/blob/2edb3fb6b8a85a11f75b9ad2f387637c52e4c92a/core/backtest/viz.py#L22-L99
[src-metrics]: https://github.com/laosanmisaka/trading-assistant/blob/2edb3fb6b8a85a11f75b9ad2f387637c52e4c92a/scripts/eval_daily_5min.py#L750-L837
[src-html]: https://github.com/laosanmisaka/trading-assistant/blob/2edb3fb6b8a85a11f75b9ad2f387637c52e4c92a/core/chan_viz.py#L1027-L1111
