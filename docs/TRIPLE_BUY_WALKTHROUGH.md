# 三买策略：文件地图与逐函数剖析

> 目的：把「三买策略」涉及的每个函数讲清楚 —— 它吃什么、吐什么、怎么实现、坑在哪。
> 只描述**当前代码的实际行为**，不描述"应该"怎样。发现的问题单列在最后一节。
> 生成时间：2026-09-23

---

## 0. 先回答「三买策略文件是哪个」

**不是一个文件，是三层四个文件。** 按数据流从上到下：

| 层 | 文件 | 行数 | 职责 | 谁是"策略" |
| --- | --- | --- | --- | --- |
| ① 结构层 | `core/chan.py` | 361 | czsc 桥接：K 线 → 分型/笔/中枢 | 不算策略，是地基 |
| ② 判定层 | **`core/chan_points.py`** | 475 | 按缠论几何定义产出六类买卖点，**三买在这里定义** | 策略的**规则本体** |
| ③ 执行层 | **`scripts/eval_daily_5min.py`** | 1094 | 日线三买 → 5min 回调窗口找买点 → 成交口径 → 收益/统计 | **当前主线的"策略实现"** |
| ④ 研究层 | `scripts/eval_triple_prob.py` | 525 | 反着问：突破之后成三买的**概率**是多少（无循环论证） | 参数研究，不是交易策略 |

**如果你要改三买的判定条件，改 ②；要改进出场/统计口径，改 ③。**

历史遗留（**已弃用，别改**）：`scripts/eval_triple_buy.py`（单级别 30min 三买，实测跑输随机，已被 ③ 取代）。
它的 `load_pool` / `summarize` 仍被 ③ 复用，所以文件不能删。

---

## 1. 调用链总览

```
CSV 缓存 (outputs/cache_min/{period}_{code}.csv)
   │  eval_daily_5min.load_cached()        ← 防停牌空 volume 崩溃
   ▼
KLineData 序列
   │  chan.build(klines, period)           ← klines_to_df 补 amount / 丢 NaN / 排序去重
   ▼
ChanResult (czsc 对象 + bars_raw + index_of)
   │  chan.bis(res)      → 笔  [{direction, high, low, sdt, edt, start_idx, end_idx}]
   │  chan.centers(res)  → 中枢 [{high=zg, low=zd, mid=zz, sdt, edt, is_valid}]
   ▼
chan_points.buy_sell_points(bis, centers)  ← 三买判定的唯一实现
   │
   ├─ 日线侧：只取 kind=="三买" → breakout_amp() 算突破幅度
   └─ 5min 侧：取 一买/二买（按 --min-mode）→ daily_confirm_time() 定确认时刻
   ▼
eval_daily_5min.analyze_one()   ← 逐日线三买点配对 5min 买点，算成交/收益/回撤
   ▼
robustness() / mae_table()      ← 统计功效 + 双 bootstrap + 止损拆解
   ▼
build_report()                  ← 生成 §1~§7 的 Markdown
```

---

## 2. `core/chan.py` —— czsc 桥接层

模块 docstring（1~47 行）列了 **czsc 1.0.1 的 7 条硬约束**，改这个文件前必读。逐条对应到下面的函数。

### `_mark_kind(mark)` — 79 行
把 czsc 的分型标记归一成 `"top"` / `"bottom"`。
- `FX.mark` 是 **`Mark` 枚举**，`str()` 返回中文"顶分型"。直接 `mark == "G"` **恒为 False**（初版踩过：890 个分型全被判成底分型）。
- 正确读法是 `mark.name`，同时兼容旧版字符串形态。
- 无法识别 → 抛 `ValueError`（不静默）。

### `_direction_name(direction)` — 98 行
`BI.direction` 是 `Direction` 枚举 → 取 `.name` 得 `"Up"` / `"Down"`。全项目所有 `str(b["direction"]) == "Up"` 的判断都建立在这里。

### `_load_czsc()` — 103 行
延迟 import czsc（未安装时只有缠论功能不可用）。公开别名 `load_czsc`（`chan_strategy.py` 用）。

### `_freq_of(period)` — 118 行
项目周期字符串 → `czsc.Freq` 成员，映射表 `_FREQ_NAMES`：`1min/5min/15min/30min/60min/daily/weekly/monthly` → `F1/F5/F15/F30/F60/D/W/M`。不支持的抛 `ValueError`。

### `klines_to_df(klines)` — 127 行
`KLineData` 序列 → czsc 要求的 8 列 DataFrame（`symbol/dt/open/close/high/low/vol/amount`）。四件事按顺序：
1. **补 `amount = vol × close`** —— czsc 必需列，项目数据模型没有（约束 1）。
2. 丢 `dt` 无效行。
3. **丢 OHLC 有 NaN 的行** —— 新浪/东财盘中会多给一根占位 bar（OHLC 全 NaN、volume 有值），czsc 的 `BarGenerator` 遇 NaN 直接抛 `ValueError`。症状：**同一脚本盘后跑得通、盘中崩**（约束 7）。
4. **按 dt 升序 + 去重**（`keep="last"`）—— czsc 自己不排序也不校验，乱序**静默算错**（实测 200 根打乱后分型 95 → 3，约束 2）。

### `resample_daily(df)` — 168 行
分钟 DataFrame → 日线 DataFrame。`set_index("dt").resample("1D")` + `dropna()`：A 股非交易日无成交 ⇒ 与交易所日历等价。2026-09-18 从 `chan_viz.py` 上移，供几何策略与可视化共用。

### `df_to_klines(df, period, code)` — 187 行
`klines_to_df` 的逆向。用于"拿合成日线再建 czsc 对象"。行序原样保留（`build()` 内部会排序）。

### `class ChanResult` — 201 行
dataclass：`obj`（czsc 实例）/ `period` / `bars`（**实际参与计算的** K 线）/ `index_of`（`{Timestamp: 下标}`）。
三个 property：`fractal_count` / `bi_count` / `center_count`。

### `build(klines, period="30min")` — 228 行 ★
**主入口。** 流程：
1. `klines_to_df` → 少于 3 根返回 `None`。
2. `czsc.format_standard_kline(df, freq)`。
3. **`max_bi_num = max(500, len(df))`** —— czsc 默认 50 会**静默截断历史**（实测 2000 根输入：默认值下分型 890→300、中枢 26→7）。本层不暴露该参数，直接放到输入长度级别（宁大勿小，调大不改变结果）。
4. 用 `obj.bars_raw` 重建 `bars` 与 `index_of`。

**⚠️ 最重要的一条**：`bars_raw` **自第一个笔的起点开始**，可能少于输入根数（实测 300 根输入只保留 256 根）。所以：
- `start_idx` / `end_idx` / `fractal.idx` **全部以 `bars_raw` 为基准**，不能拿去索引调用方传入的原始序列，只能按 dt 对齐。
- 不要假定 `ChanResult.bars` 与输入等长。

### `fractals(result, kind=None)` — 268 行
分型列表 `[{kind, price, dt, idx}]`，按时间升序。`kind` 过滤 `"top"`/`"bottom"`。`price = float(fx.fx)`。

### `latest_fractal(result, kind)` — 288 行
倒序遍历 `fx_list`，返回最近一个指定类型的分型（没找到 `None`）。**UI 的 30min 顶/底分型止盈提示走这里。**

### `centers(result)` — 302 行 ★
中枢列表 `[{high, low, mid, sdt, edt, is_valid}]`，映射关系：`high=zs.zg`（上沿）、`low=zs.zd`（下沿）、`mid=zs.zz`（中轴）。

实测口径（8 只 × 26 个日线中枢，26/26 命中）：
- `sdt == zs.bis[0].sdt` —— 中枢从第一笔起点开始。
- `edt == zs.bis[-1].edt` —— 结束于最后一笔终点。
- **`zs.bis` 只含「仍与 `[zd, zg]` 有重叠」的笔，不含完全脱离的那一笔**（缠论中心定理一口径）。

⚠️ **`is_valid` 是方法不是属性**（Rust 侧签名 `(self, /)`）。原写法 `bool(zs.is_valid)` 取的是绑定方法对象，**恒为 True**，字段一直在撒谎 —— 2026-09-20 修为 `bool(zs.is_valid())`。

### `latest_center(result)` — 342 行
`centers()` 的最后一个（列表已是时间升序）。

### `bis(result)` — 348 行 ★
笔列表 `[{direction, high, low, sdt, edt, start_idx, end_idx}]`。
- `direction` 已归一为 `"Up"` / `"Down"`。
- `high` / `low` 是**整根笔的极值**（不是端点价）。
- `start_idx` / `end_idx` 基准是 `bars_raw`。

> **`centers()` 不返回 `bis` 键** —— 见 §5 的 `find_candidates` 有处误用。

---

## 3. `core/chan_points.py` —— 三买判定核心 ★★★

**这是"三买策略"的规则本体。** 模块 docstring（1~84 行）说明了为什么不用 czsc 的 `cxt_*` 信号：那些是**逐 bar 的择时状态**，会闪断（同一段下跌里"一买"和"二卖"逐根交替、笔序号在 9→5→11 之间漂移），不是**点事件**。几何判定的位置直接落在笔端点上，与折线拐点重合。

常量：`KINDS`（六类买卖点名，94 行）、`DEFAULT_LOOKBACK = 3`（一买回看同向笔数，97 行）、`MACD_FAST/SLOW/SIGNAL = 12/26/9`（176 行）。

### `_ts(value)` — 100 行
任意时间形态 → **朴素** `pd.Timestamp`（`tz_localize(None)` 去时区）。全模块时间比较的统一入口，避免时区/字符串比较踩坑。

### `_last_center_before(centers, dt)` — 108 行
`dt` **严格早于**（`<`）的最后一个已完成中枢。依赖 `centers` 按 `edt` 升序 ⇒ 循环可 `break`（该前提已实测：1500 根合成数据上 edt 逆序对 = 0）。
⚠️ 若上游将来输出延伸/重叠中枢（edt 可能倒退），这个 `break` 会**静默漏判**，届时改成"取 edt < target 中 edt 最大者"。

### `_center_key(z)` — 127 行
`(str(sdt), str(edt))` —— 中枢身份键，用来判断两个候选是否「同属一段走势」（配合下一个函数）。

### `_converge_by_center(cands, *, extreme="low")` — 132 行 ★
**按参照中枢分段收敛。** 输入 `[(bi_idx, dt, price, center_key)]`（时间升序），输出每段只保留一个点。

- `extreme` **必须按买卖方向分派**：买点 `"low"` 取最低、卖点 `"high"` 取最高。
- **为什么不能做链式收敛**（"相邻候选更低就吞并"）：会跨越数月/多个中枢，把互不相干的两段跌势并成一段。实测茅台 10 个候选被吞到 3 个、平安银行 `9.83→9.37→8.65→7.55→7.53` 里 9.83 明明高 17% 且有完整反弹，也被一路并掉。
- **2026-09-20 修过一个错**：原实现买卖两侧一律取 `min`（照买侧写的），卖侧因此取到"这一段上涨里**最低**的那个高点"，位置标错；更麻烦的是**连带让二卖判不出来**（二卖要求后续向上笔 `high < high0`，`high0` 被压低后真正的次高点落不进条件）。

### `macd_bar(closes)` — 179 行
收盘价 → MACD 柱 `2 × (DIF − DEA)`（通达信口径）。EMA 用 `ewm(adjust=False)` 递推，与行情软件一致。输入先 `ffill().bfill()` 兜 NaN。

### `_bars_to_arrays(bars)` — 193 行
把三种形态统一成 `(dt 数组, close 数组)`：DataFrame（取 `dt`/`date` 列）、dict 序列（取 `dt`/`date` 键）、`KLineData` 序列（取 `date` 属性）。收盘价缺失行丢弃，结果按时间升序。

### `bi_macd_areas(bis, bars)` — 238 行
每根笔的 **MACD 柱面积**（力度）：向下笔取绿柱绝对值之和、向上笔取红柱之和，均为正值。返回 `{笔下标: 面积}`；`bars=None` 整体返回 `None`；某一笔跨度对不上任何 bar → 该笔为 `None`（**应视为"无法判定"，不是"面积为零"**）。
⚠️ **不要用 `DatetimeIndex.asi8`**：pandas 2.x 从 Python datetime 构造的索引分辨率是 us、`asi8` 跟着变 us，而 `Timestamp.value` 恒为 ns —— 直接比较会**全部落空**（表现：面积全 None）。逐元素取 `.value` 才稳。

### `is_divergence(areas, cur_idx, ref_idx)` — 268 行
同向走势"面积变小 = 力度衰竭"。返回 `True`/`False`/`None`（任一面积缺失即无法判定）。

### `buy_sell_points(bis, centers=(), lookback=3, *, bars=None, require_divergence=False)` — 282 行 ★★★
**唯一的买卖点生产函数。** 返回 `[{kind, dt, price, bi_idx, direction, divergence, (zg, zd)}]`，按 `(dt, kind)` 升序。

实现按段推进，`add()`（333 行）统一构造记录：

| 段 | 行号 | 判定条件 | 关键实现 |
| --- | --- | --- | --- |
| **一买** | 343~373 | 向下笔终点 ① 创近 `lookback` 个同向笔新低 ② 严格低于最近中枢 `zd` | 无中枢那一段不产一买（刻意的）。背驰参照 = 窗口内**最低点**那根笔；`require_divergence=True` 且判出 `False` 才过滤（`None` 保留，免数据缺口误杀）。最后过 `_converge_by_center` |
| **一卖** | 375~396 | 对称：创新高 + 升破 `zg` | 参照 = 窗口内**最高点**那根笔；收敛用 `extreme="high"` |
| **二买** | 398~404 | 一买之后第一个向下笔终点且 `low > 一买 low` | `for j in range(i0+1, ...)` 遇 Down 判一次即 `break` —— 中间**天然隔一个向上笔** |
| **二卖** | 406~412 | 对称 | — |
| **三买** | 414~431 | 向上笔越过 `zg` 后，第一根向下笔的终点 `low > zg`（回抽不回中枢） | 见下 |
| **三卖** | 433~447 | 对称：向下越过 `zd`，反弹 `high < zd` | — |

**三买的实现细节（414~431 行，最关键）**：

```python
for z in centers:
    z_edt, zg, zd = _ts(z["edt"]), float(z["high"]), float(z["low"])
    for j, b in enumerate(bis):
        if _ts(b["edt"]) < z_edt:      # ← 起扫点是 edt，不是 sdt
            continue
        if str(b["direction"]) != "Up" or float(b["high"]) <= zg:
            continue                    # 找到第一根「向上且越过上沿」的笔
        for m in range(j + 1, len(bis)):
            if str(bis[m]["direction"]) == "Down":
                if float(bis[m]["low"]) > zg:
                    add("三买", m, ..., zg=zg, zd=zd)
                break                   # 只认第一根回抽笔
        break                           # 每个中枢只取第一次有效突破
```

三个设计点值得单独说：

1. **起扫点用 `b["edt"] >= z_edt` 而不是 `sdt`**。czsc 的 `zs.bis[-1].edt` 恰好 `== z.edt`，所以"最后一根延伸笔"也在候选里 —— 它可能已经把价格带出上沿。用 `sdt >= z.edt` 会跳过它，导致三买/三卖**整体晚一笔**（紫金矿业实例：三卖标到 2026-05-12 @34.39，正确是 2026-04-15 @35.04 —— 老三手绘的 `3S` 落在后者）。
2. **两处 `break` 的语义不同**：内层是"只认第一根回抽笔"（后面再破再回抽不算），外层是"每个中枢只产出一次"。这对应缠论"三买是离开中枢后的**第一次**回抽"。
3. **`zg` / `zd` 随点带出**。因为上游是 `for z in centers` 逐中枢扫描、最后才去重，**同一根回抽笔可能同时是多个中枢的三买**（各自 `zg` 不同），参照中枢**无法从返回结果反推**。下游要算"突破幅度"必须用判定时那个中枢的 `zg`，所以只能在这里带出（2026-09-23 加）。

**去重（449~464 行）**：按 `(kind, str(dt), round(price, 4))` 保序去重。只有三买/三卖实际生效（一买/一卖已被 `_converge_by_center` 收敛、二买/二卖天然唯一），对前者是恒等操作。实测日线三买 890 → 804（污染约 9.7%）。

**已知简化**（docstring 71~83 行，需要老三拍板才会改）：
1. 严格缠论一买要求"≥2 个依次下移的同级别中枢 + 背驰"，本实现放宽为"跌破最近一个已完成中枢下沿"，**不要求 ≥2 个中枢** ⇒ 单中枢盘整的向下离开也会被标成一买（对应"盘整背驰"，缠师也认，但级别低）。
2. czsc 1.0.1 **没有线段**，所以这里的"日线中枢"严格说是**日线笔中枢**，级别低于递归定义的日线级别中枢。
3. `bis` / `centers` 必须同源（都来自 `core.chan`），否则时间不可比。

### `group_by_kind(points)` — 470 行
按 `kind` 分组，保证六类键都存在（没有的给空列表）。UI 图例用。

---

## 4. `scripts/eval_daily_5min.py` —— 策略执行与评估 ★★★

**这是当前主线的策略实现。** 模块 docstring（1~84 行）解释了为什么要"日线 + 5min"而不是"30min + 5min"：czsc 判笔所需 bar 数与级别无关（日线 12.4~14.0 / 30min 13.8~15.2 / 5min 14.4~15.8 根每笔），所以"本级别一笔在次级别有几根笔"≈ 级别比；而三买的**回调笔只有平均笔的一半**，所以 30min→5min 只剩 3 笔 / 0 中枢（覆盖率 18%）、日线→30min 5.3 笔（13.5%），只有**日线→5min 有 42 笔（97.8%）**。**级别差一级不够，必须差两级。**

常量（112~116 行）：`MIN5_BARS_PER_DAY = 48`（一天 240 分钟）、`BUY_KINDS = ("一买","二买")`、`STOP_LEVELS = (3,5,8)`（%）、`BOOT_N = 1000`、`BOOT_SEED = 20260922`（固定种子 ⇒ 报告可复现）。

### `load_cached(code, period, cache_dir)` — 119 行
读 `fetch_min.py` 落的 CSV → `KLineData` 列表。
⚠️ **刻意不复用** `eval_triple_buy.load_cached`：那个版本 `int(r.volume)` 遇到**停牌 bar 的空 volume**（baostock 返回空串）会抛 `ValueError: cannot convert float NaN to integer`（神华日线 2025-08 就有 10 行）。这里用 `to_numeric(...).fillna(0).astype("int64")`。

### `ts(x)` — 140 行
`pd.Timestamp(str(x))`。**必须统一走这个**：缓存 CSV 的 dt 可能是 `"2020-01-02"`，而 `str(Timestamp)` 是 `"2020-01-02 00:00:00"` —— 混用会全部落空。

### `bars_of(bis)` — 144 行
`{笔对象 id: 索引}`。**死代码** —— 全项目 0 引用（`analyze_one` 自己建了 `idx5 = {id(b): i ...}`）。可删。

### `daily_confirm_time(bis, bi_idx)` — 149 行 ★
**可成交确认时刻 = 该笔之后第一根笔的终点 `edt`。**
⚠️ 这是 KI-009（未来函数）的正面体现：三买点 = 回抽笔终点，而**笔要锁定必须等后续反向笔成型**。所以 `bi_idx + 1` 的 `edt` 才是最早能确认的时刻，日线实测滞后中位约 9 交易日。返回 `None` 表示该笔是序列最后一根（无法确认）。
**5min 侧也复用这个函数** —— 5min 的确认滞后只有几十分钟。

### `first_bar_after(dts, t)` — 160 行
`bisect_right` → `t` 之后第一根 bar 的下标（**`t` 本身算在"之前"**）。越界返回 `None`。用于日线侧："确认后**次一交易日**开盘成交"。

### `last_bar_at_or_before(dts, t)` — 166 行
`bisect_right - 1` → `t` 时刻或之前最后一根 bar 的下标。用于 5min 侧定位"确认那根 bar"。

### `hold_ret(closes, entry_idx, entry_px, hold_bars)` — 171 行
持有 `hold_bars` 根后的**收盘**收益 %。**入场价单独传入**，不取 `opens[entry_idx]` —— 因为 5min 侧入场价是"确认 bar 的收盘价"，既不是该 bar 开盘、也不在下一根。越界/非正价 → `None`。

### `excursion(highs, lows, entry_idx, entry_px, hold_bars, *, incl_entry_bar=True)` — 186 行
持有窗内的 `(MAE%, MFE%)`。
- `incl_entry_bar=True`：入场价是**开盘价** ⇒ 入场那根 bar 的极值也在暴露窗口内（日线侧）。
- `incl_entry_bar=False`：入场价是**收盘价** ⇒ 那根 bar 已经走完，窗口**自下一根起**（5min 侧）。
- 出场在 `entry_idx + hold_bars` 那根的收盘 ⇒ 右端**闭**。

### `stop_ret(opens, lows, closes, entry_idx, entry_px, hold_bars, stop_pct, *, incl_entry_bar=True)` — 206 行
触价止损口径收益。窗口内首次 `low <= 止损价` 即出：
- 正常情况按**止损价**成交；
- **跳空低开**（该 bar 开盘已在止损价之下）按**开盘价**成交 —— 这是乐观/悲观的分界，不能假装能挂在止损价上。
- 全程未破 → 与 `hold_ret` 同口径（第 N 根收盘出）。

### `baseline_ret(entry_px, exit_px, hold_bars)` — 227 行
同池随机入场基准：**每根 bar** 都按该级别的入场价买、持有 `hold_bars` 后按该级别出场价卖，取全体 bar（非抽样）。
- 日线侧传 `(open, close)`（开盘买）；5min 侧传 `(close, close)`（确认 bar 收盘买）。
- **必须与信号口径同构** —— 拿 open 进的基准去比 close 进的信号，差的那一截是隔夜跳空，不是择时能力。
- 滤掉 0 价 bar（停牌占位）。

### `breakout_amp(bis, p)` — 249 行
**突破幅度 % = 突破笔高点 ÷ 参照中枢 `zg` − 1。**
- 突破笔 = 三买点所在回抽笔的**前一根**（czsc 的笔严格交替 ⇒ 恒为 `i-1`），并校验它确实是 Up。
- `zg` 由 `buy_sell_points()` 随点带出（见 §3）。
- 这是 `--min-breakout` 闸门和报告"幅度中位"的来源。

### `analyze_one(code, cache_dir, *, holds, min_mode="any", verbose=False, diag=None, min_breakout=0.0)` — 271 行 ★★★
**单只标的的完整策略流程。** 十步：

① **读缓存**：`load_cached` 取 daily + 5min，缺任一个 → 写 `diag["reason"]`，返回 `None`。
② **日线结构**：`chan.build(kd, "daily")` → `bis_d` / `zs_d`；`buy_sell_points(bis_d, zs_d)` **只取 `kind=="三买"`**（`pts_all`）；逐点算 `breakout_amp` 存入 `amps`（用 `id(p)` 做键，因为点里没有唯一 id 字段）。
③ **幅度闸门**：`min_breakout > 0` 时剔除 `None` 或低于阈值的点，记 `n_drop`。闸门位置在"突破那一刻"，无额外未来函数。
④ **5min 结构**：`chan.build(k5, "5min")`；`buy_sell_points(bis_5, zs_5, bars=k5 if need_div else None, require_divergence=need_div)` —— 只有 `--min-mode one_div` 才传 bars 开背驰。
⑤ **5min 候选**（332~356 行）：按 `min_mode` 筛 `want`（`any`→一买+二买 / `one`→只一买 / `one_div` 同 `one` 但开背驰）。每个候选：
   - `tconf5 = daily_confirm_time(bis_5, q["bi_idx"])` —— 5min 确认时刻；
   - `entry5 = last_bar_at_or_before(dt5, tconf5)` —— **确认那根 bar 的收盘价直接成交**。
     关键前提（已实测 **258/258 全中**）：5min 的确认时刻恰好等于某根 bar 的 `dt`（baostock 的 `time` 标记 bar **结束**时刻），此刻 `close` 已知 ⇒ **无未来函数**。不是"等下一根 bar 开盘"—— 那会白等 5 分钟，跨 14:55 确认时甚至要等到次一交易日 09:35。
⑥ **逐日线三买点配对**（358~373 行）：
   - `t_lo = 突破笔终点 edt`（回调窗口起）、`t_pt = 三买点 dt`（回调低点）、`t_dc = daily_confirm_time(bis_d, i)`（日线可成交确认时刻）；
   - `t_dc is None` 或 `t_pt < cov_lo`（5min 未覆盖）→ `continue`；
   - `entry_d = first_bar_after(dtd, t_dc)` —— 日线**次日开盘**（日线确认发生在收盘后，物理上只能次日，这不是保守是制度）；
   - `inwin = [c for c in cand5 if t_lo <= c["conf"] < t_dc]` —— 窗口内 5min 候选，**按 5min 的确认时刻**落在 `[窗口起, 日线确认)` 内筛选，取最早的 `inwin[0]`。
⑦ **提前量**：`j_dc - e5`（5min bar 数），除以 48 得交易日。
⑧ **价优与拆解**：`价优% = 日线次日开盘 ÷ 5min 入场收盘 − 1`；`同期基准%` = 以 `提前_5mbar` 当持有期的同池随机基准（用 `_base5_cache` 记忆化，避免重复算）—— 这是价优里的 **beta 部分**，`价优 − 同期基准` 才是"买得早"的净优势。
⑨ **收益/回撤/止损**：日线口径算 `D{h}` + `DMAE{h}`/`DMFE{h}`/`DSL{h}_{s}`（`incl_entry_bar=True`）；5min 口径算 `M{h}` + `MAE{h}`/`MFE{h}`/`SL{h}_{s}`（`incl_entry_bar=False`）。**两侧各用自己级别的 bar** —— 早先版本两者共用 5min 的 MAE，等于拿"5min 入场的回撤"描述"日线入场的回撤"，是错的。
⑩ **汇总**：`summarize()` 出胜率/平均/中位/盈亏比/最佳/最差；基准两级别各配同构口径。

返回含 `明细`（逐点）、`汇总`、`基准` 的 dict。无样本时**区分四种原因**写入 `diag`（无缓存 / 日线结构建不出 / 5min 结构建不出 / 日线全期无三买 / 全被幅度过滤 / 全部早于 5min 覆盖起点）—— **报告必须如实列出"池子里哪几只没进统计、为什么"，不能静默少几只**。

### `collect_points(rows, side, h)` — 486 行
汇总所有标的的逐点收益 → `[{code, ret, idx, dt}]`。`idx` = 入场 bar 序号（日线侧用日线 bar、5min 侧用 5min bar），去重叠与时间分块都要用它。

### `base_of(rows, side, h)` — 511 行
`{代码: 该标的基准均值}`。

### `deoverlap(points, hold_bars)` — 516 行
同一标的内**贪心去重叠**：按入场时间排序，只保留持有窗与上一条不重叠的点。得到的是**近似独立样本数** —— 这套统计真正的自由度。

### `metrics(points, base)` — 536 行
一次统计：点数 / 标的数 / 点均 / **等权标的均** / 基准 / 超额(点均) / 超额(等权)。
- 「点均」与报告 §3 的样本加权平均是**同一个数**。
- 「等权标的均」是另一种口径：每只标的权重相同，免得数据长的标的吃掉数据短的。

### `cluster_bootstrap(points, base, *, n_boot=1000, seed=20260922)` — 557 行
按**标的**有放回重抽 → 超额的 95% 区间 + `P(超额 ≤ 0)`。点之间不独立（同标的重叠、日内相关），所以只能以标的为**聚类**重抽。统计量 = 重抽集合的「点均 − 基准均」。标的最少 5 只才给结果。

### `block_bootstrap(points, base, *, n_boot=1000, seed=20260922)` — 587 行
按**时间块（自然季度）**有放回重抽。
**为什么还要这个**：cluster bootstrap 只剔掉**截面**噪声，剔不掉**时间/市场态**噪声 —— 48 只标的全落在同一段 2.7 年行情里，点之间高度同涨同跌。真正稀缺的自由度是"几段行情"，不是"几只股票"。季度块重抽会把这一层依赖反映进区间里，区间通常**宽得多**（实测 20 日超额：cluster `[+0.28,+2.78]`、P=0.6% → 季度 `[−0.59,+2.30]`、P=9.9%）。需要 ≥4 个季度块。

### `robustness(rows, side, h)` — 621 行
把一组稳健性指标打成 dict：`base`（`metrics`）/ `indep`（`deoverlap` 后）/ `boot`（cluster）/ `boot_time`（季度块）/ `drop_top5`（剔贡献最大 5 个点）/ `drop_best_stock`（剔表现最好的 1 只标的，名字记在 `drop_best_stock_name`）。

### `mae_table(rows, side, h)` — 646 行
回撤拆解：
- 「点数 n / 均值」+ MAE 中位、MAE 10 分位、MFE 中位；
- `touch`：MAE 触及 −3/−5/−8% 的比例（= 会被打掉的比例）；
- 各档止损的均值 `sl3_mean`/`sl5_mean`/`sl8_mean`；
- 基准 `base`；
- **仅 5min 侧**：`adv_med`（价优中位）、`sync_med`（同期基准中位）、`net_med`（= 价优 − 同期基准，真正"买得早"的超额）、`adv_corr`（价优与该点最终收益的相关系数 —— 判断价优本身有没有预测力）。
⚠️ 按 `f"DMAE{h}"`/`f"MAE{h}"` 分派，两侧各用自己级别的 bar。

### `_fmt(v, nd=2)` / `_pct(v, nd=1)` / `_ex(m)` — 728 / 739 / 743 行
格式化：`None`/NaN → `"--"`；`_ex` 取一次统计的 `ex_pt` 加符号。

### `build_report(rows, *, min_mode, holds, pool_size=0, skipped=None, min_breakout=0.0)` — 748 行 ★
组装报告，§1~§7：

| 节 | 内容 |
| --- | --- |
| §1 概览 | 日线三买点合计 / 落在 5min 覆盖内数 / 覆盖率 / 提前量中位 / 价优中位 / 幅度中位 |
| §2 逐标的 | 每只的笔数、中枢数、三买点数、被幅度剔除数、覆盖点数、覆盖率、提前中位、价优中位、幅度中位 |
| §3 收益对比 | 每个持有期 × 日线/5min 两个口径：样本/胜率/平均/中位/盈亏比/最佳/最差/随机基准/超额。胜率是**样本加权**（各标的点数不等），基准按**标的等权**平均 |
| §4 统计功效 | 点数/标的数/**独立点数**/点均/等权标的均/基准/超额(点均)/超额(等权) |
| §5 稳健性 | 超额 + cluster 95%/P(≤0) + 季度 block 95%/P(≤0) + 剔前 5 点 + 剔最好标的 |
| §6 回撤拆解 | 价优中位/同期基准/择时净优势/MAE 中位/MAE 10 分位/MFE 中位/触及各档比例/均值/基准/各档止损 |
| §7 逐点明细 | 前 40 条原始记录 |

报告头会列出**未纳入统计的标的及原因**（`skipped`），尾部带**完整生成命令**（可复现）。

### `_base_pt(rows, side, h)` — 991 行
基准的组平均（按标的等权），`mae_table` 用。

### `dump_points(rows, path, holds)` — 1001 行
逐点结果落 CSV，**剔除 `_` 开头的私有键**（`_entry5` / `_entry5px` 等），供外部复核或自己拿去算别的。

### `main(argv=None)` — 1016 行
参数：`--pool` / `--codes`（覆盖 pool）/ `--cache-dir` / `--holds` / `--min-mode` / `--min-breakout` / `--dump-points` / `--limit` / `--out` / `--verbose`。
逐只循环，**异常与 `None` 都记入 `skipped`** 并打印原因（不允许静默丢标的）；全无样本时提示先跑 `fetch_min.py`。

---

## 5. `scripts/eval_triple_prob.py` —— 成三买概率（反着问）★

**这份不是交易策略，是参数研究。** 目的：把分母从"最终成了三买的点"换成"**突破那一刻的候选**"，消除循环论证。

### `last_center_at_or_before(centers, dt)` — 114 行
`edt <= dt` 的最后一个中枢。比 `chan_points._last_center_before`（严格 `<`）**宽一格**：因为"末中枢提前收口"时 `zs.bis[-1].edt == z.edt`，而三买判定要求 `b.edt >= z.edt` ⇒ 这里也必须含相等。

### `find_candidates(bis, centers, *, bars=None)` — 162 行 ★
逐根向上笔：取 `edt` 之前（含相等）的最近中枢，若该笔 `high > zg` 且**这个中枢还没被认领过**（`seen_center`）⇒ 一个候选。记录：
- `中枢edt` / `zg` / `zd` / `突破高`；
- `突破幅度% = 突破高 ÷ zg − 1`；
- `中枢宽度% = (zg − zd) ÷ zg`；
- `突破笔涨幅% = 高 ÷ 笔低 − 1`；
- **`力度比` = 本向上笔 MACD 面积 ÷ 上一向上笔面积，封顶 20** —— 前一向上笔面积可接近 0（横盘小笔），比值能飙到 1e4 把分档区间拉爆；
- `中枢笔数`；
- `状态`：找得到下一根向下笔 → `成功`（`low > zg`）/ `失败`；找不到 → `未定`；
- `回抽低` / `回抽时刻`。

⚠️ **`"中枢笔数": len(z.get("bis") or [])` 恒为 0** —— `chan.centers()` 返回的 dict **没有 `bis` 键**（见 §2）。该字段是死的。所幸它**没进报告的 `FEATURES` 列表**（只用了突破幅度、中枢宽度、突破笔涨幅、力度比四个），所以不影响任何结论。要么删掉、要么在 `centers()` 里真实带上 `zs.bis`。

### `eval_one(code, klines, period, *, holds)` — 228 行
两种入场口径：
- **`cross`**：中枢收口之后第一根**收盘站上 `zg`** 的 bar，次一根开盘进 —— **唯一无未来函数的入场**；
- **`confirm`**：回抽笔确认未破中枢之后（**只对成功的候选有定义** ⇒ 它的分母含答案，是上界，不是可读收益）。
⚠️ 键必须与 `find_candidates` 里 `_ts()` 的字符串形式一致（`pos` 字典，243~246 行）—— 缓存 dt 是 `"2020-01-02"` 而 `str(Timestamp)` 是 `"2020-01-02 00:00:00"`，混用会**全部落空**（表现：所有收益列都是空，2026-09-22 踩过）。

### `bucket_table(df, feat, holds)` — 306 行
按特征做**等频三分位**分档（`pd.qcut(3)`），每档给形成率与 `cross` 收益。样本 < 12 或特征取值数 < 3 → 返回空。

### `render_report` / `main` — 344 / 454 行
§1 形成率 / §2 特征分档（`FEATURES` 四个特征）/ §3 两种入场口径收益 / §4 逐标的。

---

## 6. `scripts/eval_triple_buy.py` —— 被复用的两个函数

单级别 30min 三买评估已弃用（实测 confirm 全线跑输随机），但两个工具函数仍被主线依赖：

### `load_pool(path)` — 71 行
池文件 → `[(code, name)]`。剥 `#` 注释、按空白切分、`normalize_code` 归一、**按 code 去重**（保序）。name 可为空字符串。
⚠️ **返回的是元组列表**，不是字符串列表 —— 当字符串用会静默错（`fetch_min.py` 曾因此缓存失效）。

### `summarize(rets)` — 103 行
收益序列 → `{n, 胜率, 平均, 中位, 平均盈, 平均亏, 盈亏比, 最佳, 最差}`。空样本返回全 `None`（不抛异常）。胜率 = `(s > 0).mean()`，即**平盘算负**。

### `load_cached` — 87 行
**已被主线弃用**（`int(volume)` 遇停牌 NaN 会崩），只在单级别脚本内部用。

---

## 7. 关键坑清单（改代码前必看）

| # | 位置 | 坑 | 状态 |
| --- | --- | --- | --- |
| 1 | `chan.build` | czsc `max_bi_num` 默认 50 **静默截断历史** | 已修（自动放大） |
| 2 | `chan.build` | `bars_raw` 可能少于输入 ⇒ `idx` 基准是 `bars_raw`，不能索引原始序列 | 已记录 |
| 3 | `chan.klines_to_df` | czsc 不排序不校验，乱序**静默算错** | 已修（强制排序去重） |
| 4 | `chan.klines_to_df` | 盘中 NaN 占位 bar 会让 czsc 抛异常（盘后能跑） | 已修（丢 NaN 行） |
| 5 | `chan.centers` | `zs.is_valid` 是**方法**，`bool()` 恒 True | 已修（2026-09-20） |
| 6 | `chan.centers` | **不返回 `bis` 键**，`find_candidates` 里 `z.get("bis")` 恒空 | **未修**（死字段，不影响结论） |
| 7 | `chan_points._converge_by_center` | 卖侧误用 `min`（连带二卖判不出来） | 已修（2026-09-20） |
| 8 | `chan_points.buy_sell_points` | 三买起扫点用 `sdt` 会整体**晚一笔** | 已修（改 `edt`） |
| 9 | `chan_points.bi_macd_areas` | `DatetimeIndex.asi8` 与 `Timestamp.value` 分辨率不同 ⇒ 面积全 None | 已修（逐元素 `.value`） |
| 10 | `chan_points.buy_sell_points` | 同一回抽笔被多个中枢各产一次（污染约 9.7%） | 已修（`(kind,dt,price)` 去重） |
| 11 | `eval_daily_5min.load_cached` | 停牌 bar 空 volume ⇒ `int(NaN)` 崩 | 已修（`fillna(0)`） |
| 12 | `eval_daily_5min` 键格式 | `"2020-01-02"` vs `"2020-01-02 00:00:00"` ⇒ 匹配全落空 | 已修（统一过 `ts()`） |
| 13 | `eval_daily_5min.excursion` | 两侧共用 5min 的 MAE ⇒ 描述错 | 已修（各用自己级别） |
| 14 | `eval_daily_5min.bars_of` | **死代码**，0 引用 | 未清（可删） |
| 15 | `eval_triple_prob.find_candidates` | `力度比` 未封顶时把分档区间拉爆 | 已修（封顶 20） |
| 16 | `eval_triple_buy.load_cached` | `int(volume)` 遇停牌 NaN 崩 | 未改（主线绕开） |

---

## 8. 策略的完整判定条件（一页速查）

**日线（锁定）**：
1. czsc 建结构 → 笔 + 笔中枢。
2. 对每个中枢 Z：找 `edt >= Z.edt` 起第一根向上笔且 `high > Z.zg`（**向上离开**）。
3. 该笔之后**第一根向下笔**，若 `low > Z.zg` ⇒ **三买点**（回归不回中枢）。
4. 三买点 = 该向下笔的终点时刻 / 终点价；同时带出 `Z.zg`。
5. **可成交时刻** = 该笔之后第一根笔的终点（笔锁定需要反向笔成型）⇒ 实际下单是**次一交易日开盘**。

**5min（择时）**：
6. 在 `[突破笔终点, 日线确认时刻)` 这个回调窗口内，5min 找一买或二买（`--min-mode`）。
7. 5min 点的**可成交时刻**同样 = 其后第一根反向笔的终点，恰好落在某根 5min bar 的收盘时刻 ⇒ **当根收盘价即买**。

**风控**：
8. `--min-breakout N`：突破幅度 `突破笔高 ÷ zg − 1 < N%` 的点剔除（**实测单调恶化，默认关闭**）。
9. 止损：报告提供 −3/−5/−8% 三档触价口径的对照，**实测全部劣于不止损**。

**持有**：`--holds 5,10,20`（交易日），各自独立统计。实测 **5 日无效**（超额为负），**≥10 日才有正超额**，20 日最强。
