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
   背驰」，本实现放宽为「跌破最近一个已完成中枢的下沿」—— **没有背驰
   判定，也没有要求 ≥2 个中枢**。放宽的结果是：单中枢盘整的向下离开
   也会被标成一买（对应缠论的「盘整背驰」，缠师也认，但级别低于趋势一买）。
2. czsc 1.0.1 没有线段（`xd_list`），一/二/三买都在**笔**级别判定，
   没有做「线段级别」的递归。所以本模块的「日线中枢」严格说是
   **日线笔中枢**，级别低于缠师体系里递归定义的「日线级别中枢」。
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
    # 收敛范围以**参照中枢**为界（见 `_converge_by_center`）：一段跌势里
    # 连着几根向下笔都在创新低，只标一次（否则一次建仓机会被数成好几次）；
    # 但跨越中枢的两段下跌是两次机会，不合并。
    cand_buys: list[tuple] = []
    for k, (i, dt, _, low) in enumerate(down_ends):
        prior = [e[3] for e in down_ends[max(0, k - lookback):k]]
        if prior and low > min(prior):
            continue                        # 没创出新低
        z = _last_center_before(centers, dt)
        # 一买是「下跌趋势的终结」，趋势至少要有一个中枢；没有中枢就谈不上
        # 一买（数据最开头那段会被这条规则排除，符合缠论）。
        if z is None or low >= float(z["low"]):
            continue
        cand_buys.append((i, dt, low, _center_key(z)))

    first_buys = _converge_by_center(cand_buys, extreme="low")
    for i, dt, low, _ in first_buys:
        add("一买", i, dt, low, "Down")

    # ---------------- 一卖：向上笔终点创出最近 lookback 笔新高 + 升破中枢上沿
    cand_sells: list[tuple] = []
    for k, (i, dt, _, high) in enumerate(up_ends):
        prior = [e[3] for e in up_ends[max(0, k - lookback):k]]
        if prior and high < max(prior):
            continue
        z = _last_center_before(centers, dt)
        if z is None or high <= float(z["high"]):
            continue
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
        z_edt, zg = _ts(z["edt"]), float(z["high"])
        for j, b in enumerate(bis):
            if _ts(b["edt"]) < z_edt:
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
            if _ts(b["edt"]) < z_edt:
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
