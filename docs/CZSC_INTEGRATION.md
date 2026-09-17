# czsc 接入说明

**日期**：2026-09-17
**决策**：引入 czsc 替换项目原有缠论层
**范围**：替换整套缠论层（分型 + 中枢）；自研去留待对比结果 —— 结果见第 5 节

---

## 1. 引入理由

原有缠论实现的能力盘点（逐行核实）：

| 能力 | 自研实现 | 评价 |
| --- | --- | --- |
| 分型 | `technical.py` `_merge_contains` + `detect_top/bottom_fractal` | **实现正确**，与 czsc 逐点等价（见第 5 节） |
| 笔 | 无 | 缺失 |
| 线段 | 无 | 缺失 |
| 中枢 | `technical.py:288` `calc_center_range` = 高点 75 分位 + 低点 25 分位 | **不是缠论中枢**，docstring 自述「简化算法」 |

即：**真分型 + 假中枢 + 无笔线段**。

---

## 2. 版本与依赖代价

- 版本：`czsc 1.0.1`
- 预编译 wheel：`czsc-1.0.1-cp310-abi3-win_amd64.whl`（23.2 MB）
  - `abi3` 稳定 ABI → **不需要 Rust 工具链**（只有从源码构建才要）
  - 要求 Python >= 3.10（本机 3.13.14 已验证通过）
- 新增依赖：**29 个包 / 约 300 MB**。大头是 `polars`(51.3 MB) + `polars-runtime-32`、
  `pyarrow`、`scipy`、`statsmodels`、`plotly`
- 副作用：`requests` 会被升到 2.34.2
- `TA-Lib` 为可选依赖，未安装时走纯 Python 兜底，不阻塞

---

## 3. 实测约束（改代码前必读）

### 3.1 入参要求
- `format_standard_kline` 要求 DataFrame 含 8 列：
  `symbol / dt / open / close / high / low / vol / amount`
- **`amount` 不可缺**，缺了直接抛 `ValueError: missing column 'amount'`。
  项目 `KLineData` 没有该字段 → 桥接层用 `volume * close` 近似。
- 列名顺序不敏感（已实测）。

### 3.2 czsc 不排序，也不校验顺序
- 乱序输入**不报错**，但结果错误：实测 200 根打乱后，分型从 95 个降到 **3 个**。
- 桥接层强制按 `dt` 升序 + 去重，这一步不能省。

### 3.3 `max_bi_num` 默认 50 会静默截断历史
- 实测 2000 根输入：默认值只保留 641 根，分型 890 → **300**，中枢 26 → **7**。
- 调到 200 才是全量。
- 桥接层自动放大为 `max(500, len(df))` —— 实测把该值调到远大于实际笔数不会改变结果。

### 3.4 `bars_raw` 从「首个笔的起点」开始
- `bars_raw` 的根数**不等于**输入根数（300 根输入 → 256 根）。
- **与 `max_bi_num` 无关**（50 ~ 100000 结果一致）；临界值也不固定
  （280 根 → 240、300 根 → 256、320 根 → 320）。
- 决定性证据：**「完整 300 根」与「只喂末 256 根」结果完全相同**（fx=115、bi=29），
  说明被丢掉的前段确实没有参与计算。
- **结论：`idx` 的基准只能是 `bars_raw`，不能拿去索引调用方传入的原始序列。**

### 3.5 API 与网络旧文档的差异
- **1.0.1 没有线段**：`xd_list` / `XD` 不存在。
  网上流传的 `bars_raw→bars_ubi→fx_list→bi_list→xd_list→zs_list` 六段流水线是**旧版文档**。
- `CZSC` 实例属性：
  `bars_raw / bars_ubi / fx_list / bi_list / zs_list / ubi / ubi_fxs / signals / update`
- **`FX.mark` 是枚举**（`Mark.G` 顶 / `Mark.D` 底），而 `str()` 返回中文「顶分型」「底分型」。
  → 用 `mark == "G"` 判断**恒为 False**，会把所有分型判成底分型
    （实测 890 个分型 → 顶 0 / 底 890）。
  → 正确读法是 `mark.name`。
- `BI.direction` 同理：`Direction.Up` / `Direction.Down`，`str()` 是「向上」「向下」。

### 3.6 关键字段
| 对象 | 字段 |
| --- | --- |
| `FX` | `dt / fx(价位) / mark / high / low / elements` —— **没有数组下标** |
| `BI` | `sdt / edt / high / low / direction / fx_a / fx_b / length / angle / slope / power` |
| `ZS` | `zg`(上沿) / `zd`(下沿) / `zz`(中轴) / `gg` / `dd` / `sdt` / `edt` / `bis` / `sdir` / `edir` / `is_valid` |

---

## 4. 桥接层 `core/chan.py`

| 函数 | 作用 |
| --- | --- |
| `klines_to_df(klines)` | `KLineData` 序列 → czsc 要求的 DataFrame（已按 dt 升序去重，补 amount） |
| `build(klines, period)` | 计算缠论结构，返回 `ChanResult`；K 线不足 3 根返回 `None` |
| `fractals(result, kind)` | 分型列表 `[{kind, price, dt, idx}]`，`kind` 可筛 `top`/`bottom` |
| `latest_fractal(result, kind)` | 最近一个指定类型的分型 |
| `centers(result)` | 中枢列表 `[{high(zg), low(zd), mid(zz), sdt, edt, is_valid}]` |
| `latest_center(result)` | 最近一个中枢 |
| `bis(result)` | 笔列表 `[{direction, high, low, sdt, edt, start_idx, end_idx}]` |

支持的周期字符串：`1min / 5min / 15min / 30min / 60min / daily / weekly / monthly`
→ 映射到 `Freq.F1/F5/F15/F30/F60/D/W/M`。

`ChanResult` 字段：`obj`(czsc 实例) / `period` / `bars`(实际参与计算的 K 线) /
`index_of`(`{Timestamp: 下标}`)。

---

## 5. 对比验证结果（2026-09-17）

**方法**：2000 根确定性合成 30min K 线，同时喂给 czsc 与自研实现。

> **限制**：项目数据库为空（`klines` / `klines_minute` 均 0 行），
> 因此对比基于合成数据，**结论强度有限，拿到真实行情后应复核**。

| 项 | 自研 | czsc |
| --- | --- | --- |
| 分型总数 | 891（顶 446 / 底 445） | 890（顶 445 / 底 445） |
| **逐点匹配** | **890 个完全一致**，仅自研多 1 个边界点（2024-01-01 10:00 顶分型） | |
| 中枢 | `calc_center_range(100)`：上沿 62.14 / 下沿 59.37 | 最新中枢：上沿(zg) 63.70 / 下沿(zd) 62.02 / 中轴 62.86 |
| 中枢差异 | 上沿差 2.5%，下沿差 4.3% | |
| 中枢总数 | 1 个「区间」 | **26 个** |
| 笔 | 无 | **170 条** |

**结论**：

1. **自研分型与 czsc 逐点等价** → 替换分型检测**不会改变策略行为**，属安全切换。
2. **自研中枢是分位数区间，与缠论中枢不是同一概念** → 必须替换，且替换会改变
   买点第三条件（缩量回踩中枢）的触发结果。
3. czsc 额外提供笔（170 条）与未完成笔（`ubi`），是纯新增能力。

---

## 6. 尚未完成

- **切换调用方**：`core/alert_engine.py:226`（止盈顶分型）与
  `core/buy_point_scanner.py:100/141`（周线底分型、缩量回踩中枢）仍走自研实现。
  切换会改变买点触发结果，需与老三确认后再动。
- **30min 数据源**：项目目前只采集 `1min` 与 `60min`。czsc 的 `Freq.F30` 已就绪，
  但数据层尚未提供 30min。
  （注：`akshare` 的 `stock_zh_a_hist_min_em` 原生支持 `period="30"`，
  历史约 1–2 年，无需重采样。）
- **自研实现的去留**：对比结果已出（分型等价、中枢概念错误），待老三定夺。
