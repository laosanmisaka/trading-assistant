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

所以本模块改用几何定义：买卖点的位置就是**笔的端点**，由笔与中枢的
相对位置判定。稳定、可解释、不闪断。

======================================================================
定义（缠论标准，简化版）
======================================================================

一买：向下笔的终点 P，满足
      (a) P.low 是最近 ``lookback`` 个向下笔终点中的最低
      (b) P.low 严格低于 P 之前最近一个中枢的下沿 zd（下跌趋势终结）
      (c) 后面不再出现更低的低点 —— 连续创新低段只取**最后一个**
二买：一买之后的下一个向下笔终点 Q（中间隔一个向上笔），Q.low > 一买 P.low
三买：某中枢 Z 之后，向上笔的 high 突破 Z.zg，紧随其后的向下笔终点
      R.low > Z.zg（回调不回中枢）
卖点（一卖 / 二卖 / 三卖）完全对称。

条件 (b) 的推论：**数据最开头、还没有中枢的那一段不会有一买** ——
没有中枢就谈不上「趋势」，这个排除是刻意的。

**已知简化**（如需更严口径要老三拍板）：

1. 严格缠论的一买要求「下跌趋势中最后一个中枢的三卖之后」，本实现
   放宽为「跌破最近一个已完成中枢的下沿」。
2. czsc 1.0.1 没有线段（`xd_list`），一/二/三买都在**笔**级别判定，
   没有做「线段级别」的递归。
3. `bis` / `centers` 的时间必须可比（同源，来自 `core.chan`）。
"""

from __future__ import annotations

from typing import Sequence

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
    """dt 之前（严格早于）最近一个已完成中枢"""
    target = _ts(dt)
    found = None
    for z in centers:                      # centers 已按时间升序
        if _ts(z["edt"]) < target:
            found = z
        else:
            break
    return found


def buy_sell_points(
    bis: Sequence[dict],
    centers: Sequence[dict] = (),
    lookback: int = DEFAULT_LOOKBACK,
) -> list[dict]:
    """在笔序列上按缠论定义推买卖点

    参数
    ----
    bis      : `core.chan.bis()` 的输出，含 direction / high / low / sdt / edt
    centers  : `core.chan.centers()` 的输出，含 high(zg) / low(zd) / sdt / edt
    lookback : 一买 / 一卖判定回看的同向笔终点个数

    返回
    ----
    ``[{kind, dt, price, bi_idx, direction}, ...]``，按 dt 升序。
    ``dt`` 是笔的终点时刻（秒级 Timestamp），``price`` 是笔端点的极值
    （买点取 low，卖点取 high）。
    """
    out: list[dict] = []
    if len(bis) < 2:
        return out

    # 笔的终点：(笔下标, 终点时刻, 方向, 端点价格)
    ends = []
    for i, b in enumerate(bis):
        up = str(b["direction"]) == "Up"
        ends.append((i, _ts(b["edt"]), "Up" if up else "Down",
                     float(b["high"]) if up else float(b["low"])))

    def add(kind: str, i: int, dt, price: float, direction: str):
        out.append({"kind": kind, "dt": dt, "price": price,
                    "bi_idx": i, "direction": direction})

    down_ends = [e for e in ends if e[2] == "Down"]
    up_ends = [e for e in ends if e[2] == "Up"]

    # ---------------- 一买：向下笔终点创出最近 lookback 笔新低 + 跌破中枢下沿
    #
    # 注意「连续创新低」要收敛成一个点：一段跌势里可能连着好几根向下笔
    # 都在创新低，若每根都标一买，就把一次建仓机会数成了好几次。
    # 缠论的一买是「下跌趋势的终结」，因此只保留**连续创新低段的最后
    # 一个**（后面还有更低的低点 → 下跌没结束，当前这根不算）。
    cand_buys: list[tuple[int, pd.Timestamp, float]] = []
    for k, (i, dt, _, low) in enumerate(down_ends):
        prior = [e[3] for e in down_ends[max(0, k - lookback):k]]
        if prior and low > min(prior):
            continue                        # 没创出新低
        z = _last_center_before(centers, dt)
        # 一买是「下跌趋势的终结」，趋势至少要有一个中枢；没有中枢就谈不上
        # 一买（数据最开头那段会被这条规则排除，符合缠论）。
        if z is None or low >= float(z["low"]):
            continue
        cand_buys.append((i, dt, low))

    first_buys: list[tuple[int, pd.Timestamp, float]] = []
    for idx, (i, dt, low) in enumerate(cand_buys):
        if idx + 1 < len(cand_buys) and cand_buys[idx + 1][2] < low:
            continue                        # 后面还有更低的低点 → 不是终结
        first_buys.append((i, dt, low))
        add("一买", i, dt, low, "Down")

    # ---------------- 一卖：向上笔终点创出最近 lookback 笔新高 + 升破中枢上沿
    cand_sells: list[tuple[int, pd.Timestamp, float]] = []
    for k, (i, dt, _, high) in enumerate(up_ends):
        prior = [e[3] for e in up_ends[max(0, k - lookback):k]]
        if prior and high < max(prior):
            continue
        z = _last_center_before(centers, dt)
        if z is None or high <= float(z["high"]):
            continue
        cand_sells.append((i, dt, high))

    first_sells: list[tuple[int, pd.Timestamp, float]] = []
    for idx, (i, dt, high) in enumerate(cand_sells):
        if idx + 1 < len(cand_sells) and cand_sells[idx + 1][2] > high:
            continue
        first_sells.append((i, dt, high))
        add("一卖", i, dt, high, "Up")

    # ---------------- 二买：一买之后第一个向下笔终点，不创新低（中间必隔向上笔）
    for i0, _, low0 in first_buys:
        for j in range(i0 + 1, len(bis)):
            if str(bis[j]["direction"]) == "Down":
                if float(bis[j]["low"]) > low0:
                    add("二买", j, _ts(bis[j]["edt"]), float(bis[j]["low"]), "Down")
                break

    # ---------------- 二卖
    for i0, _, high0 in first_sells:
        for j in range(i0 + 1, len(bis)):
            if str(bis[j]["direction"]) == "Up":
                if float(bis[j]["high"]) < high0:
                    add("二卖", j, _ts(bis[j]["edt"]), float(bis[j]["high"]), "Up")
                break

    # ---------------- 三买：中枢之后向上笔突破 zg，回调低点仍在 zg 之上
    for z in centers:
        z_edt, zg = _ts(z["edt"]), float(z["high"])
        for j, b in enumerate(bis):
            if _ts(b["sdt"]) < z_edt:
                continue
            if str(b["direction"]) != "Up" or float(b["high"]) <= zg:
                continue
            for m in range(j + 1, len(bis)):
                if str(bis[m]["direction"]) == "Down":
                    if float(bis[m]["low"]) > zg:
                        add("三买", m, _ts(bis[m]["edt"]), float(bis[m]["low"]), "Down")
                    break
            break                            # 每个中枢只取第一次有效突破

    # ---------------- 三卖
    for z in centers:
        z_edt, zd = _ts(z["edt"]), float(z["low"])
        for j, b in enumerate(bis):
            if _ts(b["sdt"]) < z_edt:
                continue
            if str(b["direction"]) != "Down" or float(b["low"]) >= zd:
                continue
            for m in range(j + 1, len(bis)):
                if str(bis[m]["direction"]) == "Up":
                    if float(bis[m]["high"]) < zd:
                        add("三卖", m, _ts(bis[m]["edt"]), float(bis[m]["high"]), "Up")
                    break
            break

    out.sort(key=lambda x: (x["dt"], x["kind"]))
    return out


def group_by_kind(points: Sequence[dict]) -> dict[str, list[dict]]:
    """按 kind 分组，保证六类键都存在（没有的给空列表）"""
    grouped: dict[str, list[dict]] = {k: [] for k in KINDS}
    for p in points:
        grouped.setdefault(p["kind"], []).append(p)
    return grouped
