# -*- coding: utf-8 -*-
"""缠论买卖点的几何计算 —— 只依赖「笔 + 中枢」，不依赖 czsc 的择时信号

======================================================================
为什么不直接用 czsc 的 cxt_* 买卖点信号？
======================================================================

czsc 的 `cxt_first_buy_V221126` / `cxt_second_bs_V230320` /
`cxt_third_bs_V230318` 是**逐 bar 的择时状态信号**，不是「买点事件」。
实测某标的 30 分钟数据（1470 根有效 bar），原始序列长这样：

    2026-02-02 14:30   一买_9笔_任意_0      其他_任意_任意_0
    2026-02-02 15:00   其他_任意_任意_0      二卖_任意_任意_0
    2026-02-03 10:00   一买_9笔_任意_0      其他_任意_任意_0
    2026-02-03 11:30   一买_5笔_任意_0      其他_任意_任意_0

同一段下跌行情里，「一买」和「二卖」逐根 bar 交替闪断，**笔序号还在
9 → 5 → 11 之间来回跳**（czsc 用滚动窗口重算，笔的计数基准会漂移）。
按「关键字首次出现」计数得到一买 26 次，而按缠论定义真正的一买只有
21 个 —— 多出来的是**状态抖动**，不是买点。

**注意结论的范围 —— 别把「cxt_ 系列全都不可靠」读出来**（2026-09-18 补测）：

上面测到闪断、笔序号漂移的是**带笔序号**的信号（一买 / 三买，输出形如
`一买_9笔_任意_0`）—— 漂的是笔的计数基准。而 `cxt_second_bs_V230320`
（二买）输出 `二买_任意_任意_0`，**不带笔序号**；实测它在 30 分钟数据上
的状态跃迁点相邻间隔最小 6 根、间隔 ≤ 2 根的相邻对 0/50，**没有逐根
闪断**（`core/chan_strategy.py` docstring 硬约束 3 有完整数据）。

所以本模块改用几何定义，理由不只是闪断，还有第二层原因：czsc 的信号是
**持续状态**而非**点事件**（一次「二买」状态可连续覆盖几十个交易日），
拿它做"某处存在买卖点"的标注，本身就要额外定义"什么算出现"。几何判定
的位置直接落在**笔的端点**上，图上与折线拐点重合 —— 这才符合"标注图"
的用途。稳定、可解释、不闪断。

======================================================================
定义（缠论标准，简化版）
======================================================================

一买：向下笔的终点 P，满足
      (a) P.low 是最近 ``lookback`` 个向下笔终点中的最低
      (b) P.low 严格低于 P 之前最近一个中枢的下沿 zd（下跌趋势终结）
      (c) 同一段下跌只取最低点 —— 收敛以**参照中枢**为界，中枢换了即断段
二买：一买之后的下一个向下笔终点 Q（中间隔一个向上笔），Q.low > 一买 P.low
三买：某中枢 Z 的**离开笔**之后，第一次回抽（向下笔）的终点 R，
      R.low > Z.zg（回调不回中枢）
卖点（一卖 / 二卖 / 三卖）完全对称，**但收敛方向要反过来**：同一段上涨里的
多个一卖候选只保留 ``high`` **最大**的那个（2026-09-20 修 —— 原实现沿用了
买侧的 ``min``，把一个上涨段里最低的高点标成一卖，并连带让二卖判不出来，
详见 `_converge_by_center` 的 docstring）。

⚠️ 「离开」在两处含义不同，别混用（2026-09-18 跨 26 个日线中枢实测）：

czsc 的 `zs.bis` = 所有**仍与中枢区间 [zd, zg] 有重叠**的笔，**不含**完全脱离
的那一笔；`zs.edt` == `zs.bis[-1].edt`（最后一笔的终点），**不是**「离开笔的终点」。
实测紫金矿业某中枢 `edt=2026-03-23`，`zs.bis` 5 笔，最后一笔
`Down 2026-03-02 → 2026-03-23 [29.00, 39.81]` 与中枢 `[35.24, 38.78]`
**仍有重叠**（区间包含关系），所以按 czsc / 中心定理一的口径它**不是**离开笔；
但它的**终点 29.00 已经跌破 zd**，价格实际上已在枢外。

本模块的三买/三卖用的是**「走势终点越过边沿」**判据（第二条口径），
所以这一笔本身就是「离开」的起点。原实现用 `bi.sdt >= zs.edt` 找
「中枢之后的笔」，把这一笔跳过了，于是把再下一笔当成离开笔 →
**三买/三卖整体晚一笔**（紫金矿业日线：三卖标到 2026-05-12 @34.39，
正确是 2026-04-15 @35.04 —— 老三手绘标注的 `3S` 也落在这里）。
改用 `bi.edt >= zs.edt`：起扫点回到 `zs.edt`，`zs.bis[-1]` 重新进入候选。

条件 (b) 的推论：**数据最开头、还没有中枢的那一段不会有一买** ——
没有中枢就谈不上「趋势」，这个排除是刻意的。

**已知简化**（如需更严口径要老三拍板）：

1. 严格缠论的一买要求「下跌趋势（≥2 个依次下移的同级别中枢）终结 +
   背驰」，本实现放宽为「跌破最近一个已完成中枢的下沿」—— **没有要求
   ≥2 个中枢**；背驰判定已补上（MACD 面积背驰，见 `bi_macd_areas` /
   `require_divergence` 参数），但需要调用方把 bar 级收盘价传进来
   （``bars``）才生效，不传则退化为旧行为（不过滤）。放宽的结果是：
   单中枢盘整的向下离开也会被标成一买（对应缠论的「盘整背驰」，
   缠师也认，但级别低于趋势一买）。
2. czsc 1.0.1 没有线段（`xd_list`），一/二/三买都在**笔**级别判定，
   没有做「线段级别」的递归。所以本模块的「日线中枢」严格说是
   **日线笔中枢**，级别低于缠师体系里递归定义的「日线级别中枢」。
3. `bis` / `centers` 的时间必须可比（同源，来自 `core.chan`）。
"""

from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd

#: 六类买卖点（与 `core.chan_viz` 的图例顺序一致）
KINDS = ("一买", "二买", "三买", "一卖", "二卖", "三卖")

#: 一买 / 一卖判定时回看的同向笔终点个数
DEFAULT_LOOKBACK = 3


def _ts(value) -> pd.Timestamp:
    """统一成朴素 Timestamp，避免时区/字符串比较踩坑"""
    t = pd.Timestamp(value)
    if t.tz is not None:
        t = t.tz_localize(None)
    return t


def _last_center_before(centers: Sequence[dict], dt) -> dict | None:
    """dt 之前（严格早于）最近一个已完成中枢

    依赖 ``centers`` 按 ``edt`` 升序（因此循环可以提前 break）。
    该前提已实测成立：``core.chan.centers()`` 的输出在 1500 根合成数据上
    **edt 逆序对数 = 0、相邻中枢时间重叠对数 = 0**。
    若将来上游改成会输出延伸/重叠中枢（edt 可能倒退），这里的 break
    会静默漏判 —— 届时改为「取 edt < target 中 edt 最大者」即可。
    """
    target = _ts(dt)
    found = None
    for z in centers:                      # centers 已按时间升序
        if _ts(z["edt"]) < target:
            found = z
        else:
            break
    return found


def _center_key(z: dict) -> tuple[str, str]:
    """中枢的身份键 —— 判定两个候选是否「同属一段下跌」"""
    return (str(_ts(z["sdt"])), str(_ts(z["edt"])))


def _converge_by_center(cands: Sequence[tuple], *, extreme: str = "low") -> list[tuple]:
    """按参照中枢分段收敛：同属一个中枢的候选只保留那一段的**极值**点

    ``cands`` 元素为 ``(bi_idx, dt, price, center_key)``，按时间升序。
    中枢换了 → 新的一段走势开始，重新计数。

    ``extreme`` 必须按买卖方向分派：
      - ``"low"``  买点（向下笔终点）取**最低价** —— 该段最值得买的位置；
      - ``"high"`` 卖点（向上笔终点）取**最高价** —— 该段最值得卖的位置。

    ⚠️ 2026-09-20 修：原实现对买卖两侧一律取 ``min``（是照买侧写的），
    卖侧因此取到「这一段上涨里**最低**的那个高点」，位置标错；更麻烦的是
    连带让二卖判不出来 —— 二卖要求后续向上笔 ``high < high0``，而 ``high0``
    被压低了，真正的次高点就落不进条件里。示例：同组候选 112 / 118，
    修复前标 112 且二卖消失，修复后标 118 且二卖正常。
    测试：`tests/test_chan_points.py::test_first_sell_converges_to_highest`
    （修复前的实现会让它失败）。

    为什么不能按「相邻候选更低就吞并」做链式收敛（2026-09-18 实测）：
    链式传播会跨越数月、跨越多个中枢，把互不相干的两段下跌并成一段。
    5 年日线上贵州茅台 10 个候选被吞到 3 个、平安银行 11 个吞到 3 个；
    平安银行 `9.83 → 9.37 → 8.65 → 7.55 → 7.53` 这一串里，9.83 比
    8.38 高 17%、中间有完整反弹，是独立的一段，却被一路并掉。
    """
    pick = min if extreme == "low" else max
    out: list[tuple] = []
    group: list[tuple] = []
    for item in cands:
        if group and group[-1][3] == item[3]:
            group.append(item)
        else:
            if group:
                out.append(pick(group, key=lambda x: x[2]))
            group = [item]
    if group:
        out.append(pick(group, key=lambda x: x[2]))
    return out


# ----------------------------------------------------------------------
# MACD 面积背驰
# ----------------------------------------------------------------------

#: MACD 参数（快线 / 慢线 / 信号线），与通达信口径一致
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9


def macd_bar(closes: Sequence[float]) -> "pd.Series":
    """收盘价序列 → MACD 柱（2 × (DIF − DEA)，通达信口径）

    EMA 用 ``adjust=False`` 的递推式，与行情软件一致。前置 NaN 已在
    `_bars_to_arrays` 收口丢掉，这里假定输入是干净浮点序列。
    """
    s = pd.Series(pd.to_numeric(pd.Series(closes), errors="coerce"),
                  dtype=float).ffill().bfill()
    dif = (s.ewm(span=MACD_FAST, adjust=False).mean()
           - s.ewm(span=MACD_SLOW, adjust=False).mean())
    dea = dif.ewm(span=MACD_SIGNAL, adjust=False).mean()
    return 2 * (dif - dea)


def _bars_to_arrays(bars) -> tuple | None:
    """把 DataFrame / dict 序列 / KLineData 序列统一成 (dt 数组, close 数组)

    三种形态都接受（调用方手里分别是这三种）：DataFrame 用 ``dt``/``date``
    列；dict 用 ``dt``/``date`` 键；KLineData 用 ``date`` 属性。收盘价缺失
    的行丢弃，结果按时间升序。
    """
    if bars is None:
        return None
    if isinstance(bars, pd.DataFrame):
        if bars.empty:
            return None
        dt_col = "dt" if "dt" in bars.columns else "date"
        pairs = []
        for t, c in zip(pd.to_datetime(bars[dt_col], errors="coerce"),
                        pd.to_numeric(bars["close"], errors="coerce")):
            if pd.isna(t) or pd.isna(c):
                continue
            pairs.append((_ts(t), float(c)))
        if not pairs:
            return None
        pairs.sort(key=lambda x: x[0])
        dts = [p[0] for p in pairs]
        closes = [p[1] for p in pairs]
    else:
        pairs = []
        for b in bars:
            if isinstance(b, dict):
                dt, close = b.get("dt", b.get("date")), b.get("close")
            else:
                dt = getattr(b, "dt", None) or getattr(b, "date", None)
                close = getattr(b, "close", None)
            if dt is None or close is None:
                continue
            pairs.append((_ts(dt), float(close)))
        if not pairs:
            return None
        pairs.sort(key=lambda x: x[0])
        dts = [p[0] for p in pairs]
        closes = [p[1] for p in pairs]
    if len(dts) != len(closes) or not dts:
        return None
    return dts, closes


def bi_macd_areas(bis: Sequence[dict], bars) -> dict[int, float | None] | None:
    """每根笔的 MACD 柱面积：向下笔取绿柱面积、向上笔取红柱面积（均为正值）

    面积 = 笔跨度 [sdt, edt] 内同向 MACD 柱的绝对值之和，衡量这段走势的
    「力度」。笔跨度对不上任何 bar 时为 None（调用方应视为「无法判定」，
    而不是「面积为零」）。``bars`` 为 None 时整个返回 None。
    """
    arrays = _bars_to_arrays(bars)
    if arrays is None:
        return None
    dts, closes = arrays
    bar = macd_bar(closes).to_numpy()
    # ⚠️ 不要用 DatetimeIndex.asi8：pandas 2.x 从 Python datetime 构造的索引
    # 分辨率是 us，asi8 跟着变 us，而 Timestamp.value 恒为 ns —— 两者直接
    # 比较会全部落空（面积全 None）。逐元素取 .value 最稳。
    dt_ns = np.array([t.value for t in dts], dtype=np.int64)
    areas: dict[int, float | None] = {}
    for i, b in enumerate(bis):
        lo_ns, hi_ns = _ts(b["sdt"]).value, _ts(b["edt"]).value
        vals = bar[(dt_ns >= lo_ns) & (dt_ns <= hi_ns)]
        if vals.size == 0:
            areas[i] = None
            continue
        if str(b["direction"]) == "Down":
            areas[i] = float(-vals[vals < 0].sum())
        else:
            areas[i] = float(vals[vals > 0].sum())
    return areas


def is_divergence(areas: dict[int, float | None] | None,
                  cur_idx: int, ref_idx: int) -> bool | None:
    """cur 笔相对 ref 笔是否背驰（同向走势，面积变小 = 力度衰竭）

    返回 True=背驰 / False=未背驰 / None=无法判定（任一面积缺失）。
    """
    if areas is None:
        return None
    a_cur, a_ref = areas.get(cur_idx), areas.get(ref_idx)
    if a_cur is None or a_ref is None:
        return None
    return a_cur < a_ref


def buy_sell_points(
    bis: Sequence[dict],
    centers: Sequence[dict] = (),
    lookback: int = DEFAULT_LOOKBACK,
    *,
    bars=None,
    require_divergence: bool = False,
) -> list[dict]:
    """在笔序列上按缠论定义推买卖点

    参数
    ----
    bis      : `core.chan.bis()` 的输出，含 direction / high / low / sdt / edt
    centers  : `core.chan.centers()` 的输出，含 high(zg) / low(zd) / sdt / edt
    lookback : 一买 / 一卖判定回看的同向笔终点个数
    bars     : bar 级行情（DataFrame / dict 序列 / KLineData 序列，需含
               dt/date 与 close），用于 MACD 面积背驰判定；不传则所有
               背驰检查退化为「无法判定」
    require_divergence : 一买 / 一卖是否要求 MACD 面积背驰确认。
               True 时未背驰（可判定且力度未衰竭）的候选被过滤；
               无法判定（没传 bars 或笔跨度对不上 bar）的候选**保留**，
               以免数据缺口把真买点误杀。二买 / 二卖 / 三买 / 三卖不受影响
               （缠论上只有一买/一卖是背驰点）。

    返回
    ----
    ``[{kind, dt, price, bi_idx, direction, divergence}, ...]``，按 dt 升序。
    ``dt`` 是笔的终点时刻（秒级 Timestamp），``price`` 是笔端点的极值
    （买点取 low，卖点取 high）。``divergence`` 仅对一买/一卖有意义：
    True=背驰确认 / False=未背驰 / None=未判定；其余类别恒为 None。

    三买 / 三卖**额外带 ``zg`` / ``zd``**（判定所依据的那个中枢的上/下沿）——
    下游要算「突破幅度 = 突破笔高点 ÷ zg − 1」时必须用**判定时那个中枢**的
    zg。上游是 ``for z in centers`` 逐中枢扫描、最后才去重，同一根回抽笔可能
    同时是多个中枢的三买（每个的 zg 不同），所以这个参照**无法从返回结果反推**，
    只能在这里随点带出。其余四类不含这两个键（用 ``p.get("zg")`` 取即可）。
    """
    out: list[dict] = []
    if len(bis) < 2:
        return out

    areas = bi_macd_areas(bis, bars)
    divergence_of: dict[int, bool | None] = {}

    # 笔的终点：(笔下标, 终点时刻, 方向, 端点价格)
    ends = []
    for i, b in enumerate(bis):
        up = str(b["direction"]) == "Up"
        ends.append((i, _ts(b["edt"]), "Up" if up else "Down",
                     float(b["high"]) if up else float(b["low"])))

    def add(kind: str, i: int, dt, price: float, direction: str, **extra):
        rec = {"kind": kind, "dt": dt, "price": price,
               "bi_idx": i, "direction": direction,
               "divergence": divergence_of.get(i)}
        rec.update(extra)                 # 三买/三卖传 zg/zd，见 docstring
        out.append(rec)

    down_ends = [e for e in ends if e[2] == "Down"]
    up_ends = [e for e in ends if e[2] == "Up"]

    # ---------------- 一买：向下笔终点创出最近 lookback 笔新低 + 跌破中枢下沿
    #
    # 收敛范围以**参照中枢**为界（见 `_converge_by_center`）：一段跌势里
    # 连着几根向下笔都在创新低，只标一次（否则一次建仓机会被数成好几次）；
    # 但跨越中枢的两段下跌是两次机会，不合并。
    #
    # 背驰比较对象：lookback 窗口内**上一段最低点**所在的那根向下笔 ——
    # 即本笔所跌破的那个低点。价格更低但 MACD 绿柱面积更小 = 下跌力度衰竭。
    cand_buys: list[tuple] = []
    for k, (i, dt, _, low) in enumerate(down_ends):
        prior_ends = down_ends[max(0, k - lookback):k]
        prior = [e[3] for e in prior_ends]
        if prior and low > min(prior):
            continue                        # 没创出新低
        z = _last_center_before(centers, dt)
        # 一买是「下跌趋势的终结」，趋势至少要有一个中枢；没有中枢就谈不上
        # 一买（数据最开头那段会被这条规则排除，符合缠论）。
        if z is None or low >= float(z["low"]):
            continue
        div = None
        if prior_ends:
            ref_i = min(prior_ends, key=lambda e: e[3])[0]
            div = is_divergence(areas, i, ref_i)
            if require_divergence and div is False:
                continue                    # 力度未衰竭，跌势未尽
        divergence_of[i] = div
        cand_buys.append((i, dt, low, _center_key(z)))

    first_buys = _converge_by_center(cand_buys, extreme="low")
    for i, dt, low, _ in first_buys:
        add("一买", i, dt, low, "Down")

    # ---------------- 一卖：向上笔终点创出最近 lookback 笔新高 + 升破中枢上沿
    cand_sells: list[tuple] = []
    for k, (i, dt, _, high) in enumerate(up_ends):
        prior_ends = up_ends[max(0, k - lookback):k]
        prior = [e[3] for e in prior_ends]
        if prior and high < max(prior):
            continue
        z = _last_center_before(centers, dt)
        if z is None or high <= float(z["high"]):
            continue
        div = None
        if prior_ends:
            ref_i = max(prior_ends, key=lambda e: e[3])[0]
            div = is_divergence(areas, i, ref_i)
            if require_divergence and div is False:
                continue
        divergence_of[i] = div
        cand_sells.append((i, dt, high, _center_key(z)))

    first_sells = _converge_by_center(cand_sells, extreme="high")
    for i, dt, high, _ in first_sells:
        add("一卖", i, dt, high, "Up")

    # ---------------- 二买：一买之后第一个向下笔终点，不创新低（中间必隔向上笔）
    for i0, _, low0, _ in first_buys:
        for j in range(i0 + 1, len(bis)):
            if str(bis[j]["direction"]) == "Down":
                if float(bis[j]["low"]) > low0:
                    add("二买", j, _ts(bis[j]["edt"]), float(bis[j]["low"]), "Down")
                break

    # ---------------- 二卖
    for i0, _, high0, _ in first_sells:
        for j in range(i0 + 1, len(bis)):
            if str(bis[j]["direction"]) == "Up":
                if float(bis[j]["high"]) < high0:
                    add("二卖", j, _ts(bis[j]["edt"]), float(bis[j]["high"]), "Up")
                break

    # ---------------- 三买：向上离开中枢后的**第一次回抽**不回到中枢
    # 起点取 edt >= z.edt：czsc 的 zs.bis[-1].edt 恰好 == z.edt，所以
    # 「最后一根延伸笔」也在候选内 —— 它可能已经把价格带出中枢上沿。
    # 用 sdt >= z.edt 会把它跳过，导致三买/三卖整体晚一笔（见模块 docstring）。
    for z in centers:
        z_edt, zg, zd = _ts(z["edt"]), float(z["high"]), float(z["low"])
        for j, b in enumerate(bis):
            if _ts(b["edt"]) < z_edt:
                continue
            if str(b["direction"]) != "Up" or float(b["high"]) <= zg:
                continue
            for m in range(j + 1, len(bis)):
                if str(bis[m]["direction"]) == "Down":
                    if float(bis[m]["low"]) > zg:
                        add("三买", m, _ts(bis[m]["edt"]), float(bis[m]["low"]),
                            "Down", zg=zg, zd=zd)
                    break
            break                            # 每个中枢只取第一次有效突破

    # ---------------- 三卖
    for z in centers:
        z_edt, zg, zd = _ts(z["edt"]), float(z["high"]), float(z["low"])
        for j, b in enumerate(bis):
            if _ts(b["edt"]) < z_edt:
                continue
            if str(b["direction"]) != "Down" or float(b["low"]) >= zd:
                continue
            for m in range(j + 1, len(bis)):
                if str(bis[m]["direction"]) == "Up":
                    if float(bis[m]["high"]) < zd:
                        add("三卖", m, _ts(bis[m]["edt"]), float(bis[m]["high"]),
                            "Up", zg=zg, zd=zd)
                    break
            break

    # ---------------- 去重：同一根笔的同一端点只算一次机会
    # 一根笔可能同时是多个中枢的「三买」—— 上游 `for z in centers` 是逐中枢扫描，
    # 同一根向下笔若同时满足多个中枢的判定条件，就会被 `add()` 多次，产出的
    # `(kind, dt, price)` 完全相同（KI-010 同源）。它们是**同一次机会**，重复计入
    # 会让统计口径被重复样本加权（实测日线三买 890 → 去重后约 845，污染约 5%）。
    # 注意：一买/一卖已经过 `_converge_by_center` 收敛，二买/二卖由「一买后第一根」
    # 天然唯一，故去重只对三买/三卖实际生效；对前者是恒等操作。
    seen: set[tuple] = set()
    uniq: list[dict] = []
    for p in out:
        key = (p["kind"], str(p["dt"]), round(float(p["price"]), 4))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    out = uniq

    out.sort(key=lambda x: (x["dt"], x["kind"]))
    return out


def group_by_kind(points: Sequence[dict]) -> dict[str, list[dict]]:
    """按 kind 分组，保证六类键都存在（没有的给空列表）"""
    grouped: dict[str, list[dict]] = {k: [] for k in KINDS}
    for p in points:
        grouped.setdefault(p["kind"], []).append(p)
    return grouped
