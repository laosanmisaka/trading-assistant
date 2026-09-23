# AGENTS.md — trading-assistant 项目说明（给 AI 编码助手）

> 给 Kimi Code CLI / Claude Code / Cursor 一类工具读。**先看这里，再看 `docs/`。**
> 本文件与 `.workbuddy/memory/MEMORY.md` 是同一套结论的两个入口；数字/口径对不上时以 `outputs/` 里的报告为准。

## 项目是什么

A 股桌面交易辅助工具。PyQt5 + AKShare + SQLite + czsc（缠论库），约 9,000 行（含测试）。
**接手别人的半成品** —— 功能骨架完整，正确性与工程卫生未收口。
接手评估见 `docs/PROJECT_ASSESSMENT.md`，坑位清单见 `docs/KNOWN_ISSUES.md`（KI-001~010）。

## 环境

- Python（**必须用这个 venv**）：
  `C:/Users/yaowenwu/.workbuddy/binaries/python/envs/default/Scripts/python.exe`
  已装 numpy / pandas / pytest / PyQt5 / matplotlib / **czsc 1.0.1** / akshare / baostock
- Windows + Git Bash。跑命令前先：
  `export PATH="/usr/bin:/bin:/mingw64/bin:/c/Windows/System32:$PATH"`
- 测试：`python -m pytest -q --basetemp=<仓外目录>` → 应为 **443 passed, 8 skipped**
  （`--basetemp` 必须给，且必须在仓库外）

## 数据在哪

- **行情缓存**：`outputs/cache_min/{period}_{code}.csv`，`period ∈ daily / 30min / 5min`
  列：`dt,open,high,low,close,volume`。覆盖 `scripts/pool_liquid50.txt` 的 50 只龙头。
  `daily` 从 2016 起（给结构 warm-up）、`30min` 从 2023-09、`5min` 从 2024-01
  ⇒ **统计窗口 = 近两年**。
- **报告**：`outputs/*.md`；同名 `.html` 是单文件网页版（内嵌 CSS，可直接发人）。
- 重新取数：`python scripts/fetch_min.py --periods daily,5min --pool scripts/pool_liquid50.txt`

## 常用脚本

| 脚本 | 干什么 |
| --- | --- |
| `scripts/fetch_min.py` | baostock 取数落缓存（分段 + 被踢自动重登 + 丢弃停牌 0 价 bar）|
| `scripts/eval_triple_buy.py` | **单级别**三买统计（点数 / 胜率 / MAE / 止损）|
| `scripts/eval_daily_5min.py` | **主线**：日线出三买 → 5min 在回调窗口内找买点提前入场 |
| `scripts/eval_triple_prob.py` | 三买**反向统计**：突破候选 → 形成率 + 特征分档 + 两种入场口径 |
| `scripts/eval_six_pulse.py` | 六脉神剑策略评估（与缠论无关的独立策略）|
| `scripts/md_to_html.py` | 报告 md → 单文件 HTML（`--all` 顺带生成目录页）|
| `visualize_chan.py` | 单只 K 线 + 缠论结构 → 离线 HTML（`--name 中芯国际`）|

## 口径（最容易搞错的地方）

1. **按点统计，不是资金曲线**。同一标的的信号可以密集出现、持有期互相重叠，收益不可相加。
2. **报告必须有「同池随机入场」基准列**：没有对照的胜率没有意义（牛市里随便买都赚）。
3. **日线三买约 800 个，其中只有约 200 个落在 5min 覆盖区间内**（5min 数据只到 2024-01）。
4. `eval_daily_5min` 的分母是「已知最终形成三买」的点 ⇒ 数字**偏高**（循环论证）。
   无偏的前瞻口径看 `eval_triple_prob.py`（分母 = 所有突破候选）。
5. **评价策略只能用随机池** `pool_random500.txt`。按「当前成交额」挑出来的池子是**后视镜挑赢家**
   （同池买入持有被抬到 +195%，随机池只有 +54%）。

## 红线（改代码前必读）

- 改 `core/chan.py` 前先读它的 docstring：`amount` 是**必需列**、**czsc 不排序**（乱序会静默算错）、
  `max_bi_num` 默认 50 会**静默截断**、`FX.mark` 是**枚举**（必须 `.name`，`== "G"` 恒 False）。
- **czsc 判笔所需的 bar 数与级别无关**（各级都是 12~16 根/笔）⇒「本级别一笔在次级别上有几根笔」
  ≈ 级别比。所以只有**日线 → 5min**（差两级）才有足够结构；30min → 5min 差一级，不够。
- **停牌 bar 会返回全 0 价 + 空 volume**：除零和 `int(NaN)` 都在这里崩过。
- **笔端点是未来函数**（KI-009）：笔要等后续反向笔成型才锁定。任何「拿笔的终点当成交时刻」的
  回测都要算上这个滞后 —— 日线级实测中位 **≈6.5 交易日**，30 分钟级 ≈9 根 bar。
- `core/backtest/` 的 `Signal.date` **恒等于成交日**（不是触发日），找触发日要回退一根。

## 当前结论（2026-09-22，48 只龙头 / 近两年）

- **日线 + 5min 提前入场**（日线出三买 → 5min 在回调窗口内找一/二买），入场口径：
  **5min 侧 = 确认那根 bar 的收盘价直接成交**（确认时刻 = 该 bar 收盘，close 已知，无未来函数，
  不等下一根）；**日线侧 = 确认后次一交易日开盘**（日线确认在收盘后，物理上只能次日）。
  随机基准与各自口径同构（日线 open 进 / 5min close 进，均 close 出）：

  | 持有 | 5min 确认 胜率/平均 | 随机基准 | 超额 |
  | --- | --- | --- | --- |
  | 5 日 | 44.8% / −0.11% | +0.23% | **−0.34%** |
  | 10 日 | 49.5% / +0.80% | +0.49% | **+0.31%** |
  | 20 日 | 56.3% / +2.54% | +1.06% | **+1.48%** |

  ⇒ **必须持有 ≥ 2 周，短线无效**。日线自己确认后买入则全程 −3.6~−4.5%（等日线笔确认 = 买在山顶）。
  20 日的 +1.48% 经**季度 block bootstrap 后区间跨 0**（[−0.56, +2.37]，P(≤0)=9.5%）——
  只在这一段行情里成立，别当稳定的边际。
- **反向统计**（`triple_prob_daily.md`）：突破中枢上沿之后成三买的概率 **92.4%**；
  **突破幅度 < 10% 时只有 78.8%**，>10% 时 98.7%+ ⇒ 这个特征只能用在「决定要不要等回调」，
  **不能**用来过滤已经等到回调的点（实测单调恶化，见下）。
- **突破瞬间买入**（唯一无未来函数的入场）：持有 20 日超额 **−0.11%** ⇒ 突破即买**没有 alpha**，
  价值全在「等回调」这一步。**突破幅度过滤**（幅度 <N% 剔除）实测**单调恶化**：
  超额 +1.48 → +1.48 → +1.08 → **+0.48**（N=5/10/20）⇒ **不合入**，详见
  `outputs/daily5min_breakout_sweep.md`。
- 六脉神剑：三个池子**全部跑输**同池买入持有；买点超额全在 ±0.5% 内 ⇒ 无 alpha，
  收益差距完全由**仓位**（在场率 30%）解释。**别再调参**，要动就换共振逻辑。
