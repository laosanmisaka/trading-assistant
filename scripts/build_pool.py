# -*- coding: utf-8 -*-
"""按流动性构造标的池文件

    python scripts/build_pool.py                    # 默认 500 只
    python scripts/build_pool.py --n 300
    python scripts/build_pool.py --out scripts/pool_liquid300.txt
    python scripts/build_pool.py --mode random --n 2000 --out scripts/pool_random2000.txt
    python scripts/build_pool.py --mode mainboard   # 沪深主板全部（监控扫描口径）

三种模式（**用途不同，别混用**）
--------------------------------
- `liquid`（默认）—— 按当日成交额取前 N，**剔 ST/停牌**；**回测样本**。
- `random` —— 全市场随机抽样（固定种子），避免前视选择偏差；**回测样本**。
- `mainboard` —— 沪深主板**全部** 3197 只，不排序、**保留 ST/停牌**；
  供 `scan_candidates.py` / `daily_routine.py` 做**盘中监控的扫描范围**。
  ⚠️ 含 ST 股，**不要拿去回测**。

做什么
------
1. 取**新浪**全市场行情快照（`ak.stock_zh_a_spot()`，代码自带 `sh`/`sz`/`bj` 前缀）
2. 剔除：北交所（`bj`，新浪日线接口不支持）、ST / *ST / 退市、
   停牌（成交额 = 0）
3. 按**当日成交额**降序取前 N
4. 写成 `代码 名称` 一行一只，格式与 `pool_liquid50.txt` 一致

⚠️ 换源说明：原计划用东财 `stock_zh_a_spot_em()`（含总市值），但本机网络对
东财域名不可达（`RemoteDisconnected`），改用新浪快照。新浪快照**没有市值列**，
所以排序口径是**当日成交额**而非总市值 —— 对"流动性好"这个诉求反而更直接，
副作用是会略微偏向当日热点（单日口径 vs 多日平均的差别）。

⚠️ 输出文件只是**回测样本**，不是推荐股票池。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def build_random(n: int, seed: int = 20260920) -> list[tuple[str, str]]:
    """从全市场**随机抽样**（固定种子）

    为什么需要它：按「当日成交额」选池会引入**前视选择偏差** ——
    用 2026-09 的成交额排名去回测 2025-01 起的区间，等于挑出了这一段的赢家，
    同池"买入持有"基线被抬到 +195%（对比 50 只蓝筹池仅 +4.42%）。
    随机抽样与后续涨跌无关，是评价择时策略的公正样本。
    """
    import akshare as ak
    import numpy as np

    df = ak.stock_info_a_code_name()
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["name"] = df["name"].astype(str).str.replace(r"\s+", "", regex=True)
    df = df[~df["name"].str.contains("ST|退", na=False)]
    # 只要沪深主板 / 创业板 / 科创板（剔北交所，新浪日线接口不支持）
    df = df[df["code"].str.startswith(("60", "00", "30", "68"))]

    rng = np.random.default_rng(seed)
    idx = sorted(rng.choice(len(df), size=min(n, len(df)), replace=False))
    sub = df.iloc[idx]
    return [("sh" + c if c[0] == "6" else "sz" + c, nm)
            for c, nm in zip(sub["code"], sub["name"])]


#: 沪深主板代码前缀（baostock 的 9 位带点格式）
MAINBOARD_PREFIX = ("sh.600", "sh.601", "sh.603", "sh.605",
                    "sz.000", "sz.001", "sz.002", "sz.003")


def _latest_trade_day() -> str:
    """最近一个交易日（``YYYY-MM-DD``）—— 交易日历来自 baostock

    ``query_all_stock(day="")`` 实测返回 **0 行**，必须给显式交易日，
    所以得先自己定这一天。往前找 40 个自然日足够覆盖任何长假。
    """
    import baostock as bs

    end = pd.Timestamp.today()
    start = end - pd.Timedelta(days=40)
    rs = bs.query_trade_dates(start_date=start.strftime("%Y-%m-%d"),
                              end_date=end.strftime("%Y-%m-%d"))
    if rs.error_code != "0":
        raise RuntimeError(f"query_trade_dates 失败：{rs.error_msg}")
    days = []
    while rs.next():
        d, is_td = rs.get_row_data()[:2]
        if is_td == "1":
            days.append(d)
    if not days:
        raise RuntimeError("近 40 天内没找到交易日")
    return max(days)


def build_mainboard() -> list[tuple[str, str]]:
    """**沪深主板全部**（不排序、不筛 ST/停牌）—— 监控覆盖口径

    与 `build()` 的区别是**目的不同**：

    | | `build()`（liquid） | `build_mainboard()` |
    |---|---|---|
    | 用途 | 回测样本 | 盘中监控的扫描范围 |
    | 口径 | 按当日成交额取前 N | 主板全部，一只不漏 |
    | ST/退市 | **剔除** | **保留** |
    | 停牌 | **剔除**（成交额=0） | **保留** |

    ⚠️ 所以**不要拿本池回测**：它含 ST/退市股，会污染策略胜率统计。
    回测请继续用 `pool_random*.txt` / `pool_liquid*.txt`。

    为什么用 baostock 而不是 `build()` 用的新浪快照：新浪 `stock_zh_a_spot`
    只列**当日有成交**的票，当日停牌股会缺席；baostock `query_all_stock`
    是交易所当日证券列表，停牌股也在。
    """
    import baostock as bs

    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock 登录失败：{lg.error_msg}")
    try:
        day = _latest_trade_day()
        rs = bs.query_all_stock(day=day)
        if rs.error_code != "0":
            raise RuntimeError(f"query_all_stock 失败：{rs.error_code} {rs.error_msg}")
        out: list[tuple[str, str]] = []
        while rs.next():
            r = rs.get_row_data()               # [code, tradeStatus, code_name]
            code = r[0]
            if code.startswith(MAINBOARD_PREFIX):
                # sh.600526 → sh600526（项目内部统一用新浪格式）
                out.append((code[:2] + code[3:], r[2] if len(r) > 2 else ""))
        return sorted(set(out))
    finally:
        try:
            bs.logout()
        except Exception:                        # noqa: BLE001
            pass


def _read_pool(path: str) -> list[tuple[str, str]]:
    """读现有的池文件（一行一只 `代码 名称`，`#` 开头为注释）"""
    out: list[tuple[str, str]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts:
            out.append((parts[0], parts[1] if len(parts) > 1 else ""))
    return out


def build(n: int, snapshot: str = "") -> list[tuple[str, str]]:
    """`snapshot` 非空时读本地 CSV（列同 `ak.stock_zh_a_spot()`），否则现拉快照

    现拉快照偶发被限流（返回 HTML → `JSONDecodeError`），落一份 CSV 便于复现。
    """
    if snapshot:
        df = pd.read_csv(snapshot, dtype={"代码": str})
    else:
        import akshare as ak
        df = ak.stock_zh_a_spot()

    want = [c for c in ("代码", "名称", "成交额", "最新价") if c in df.columns]
    df = df[want].copy()
    if "成交额" not in df.columns:
        raise RuntimeError(f"快照没有成交额列：{list(df.columns)}")

    df["成交额"] = pd.to_numeric(df["成交额"], errors="coerce").fillna(0.0)
    # 名称里的全角空格（"万  科Ａ"）去掉，方便写进池文件
    df["名称"] = df["名称"].astype(str).str.replace(r"\s+", "", regex=True)
    df = df[df["代码"].astype(str).str.startswith(("sh", "sz"))]   # 剔北交所
    df = df[~df["名称"].str.contains("ST|退", na=False)]
    df = df[df["成交额"] > 0]                                      # 剔停牌
    df = df.drop_duplicates("代码").sort_values("成交额", ascending=False)
    df = df.head(n)
    return [(str(c), str(nm)) for c, nm in zip(df["代码"], df["名称"])]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="按流动性构造标的池文件")
    p.add_argument("--n", type=int, default=500, help="取前 N 只（默认 500）")
    p.add_argument("--mode", choices=("liquid", "random", "mainboard"),
                   default="liquid",
                   help="liquid=按成交额（默认）；random=全市场随机抽样（无前视偏差）；"
                        "mainboard=沪深主板全部（监控扫描口径，不排序不筛 ST/停牌）")
    p.add_argument("--seed", type=int, default=20260920, help="random 模式的抽样种子")
    p.add_argument("--snapshot", default="", help="本地快照 CSV（列同 stock_zh_a_spot）")
    p.add_argument("--include", default="",
                   help="并入已有池文件（逗号分隔）—— 保证新旧池可比，不影响排序口径")
    p.add_argument("--out", default="", help="输出文件（默认 scripts/pool_liquid<N>.txt）")
    args = p.parse_args(argv)

    if args.mode == "mainboard":
        pool = build_mainboard()
    elif args.mode == "random":
        pool = build_random(args.n, args.seed)
    else:
        pool = build(args.n, args.snapshot)
    merged = list(pool)
    seen = {c for c, _ in merged}
    added: list[tuple[str, str]] = []
    for f in filter(None, (s.strip() for s in args.include.split(","))):
        for code, name in _read_pool(f):
            if code not in seen:
                seen.add(code)
                added.append((code, name))
    merged += added
    pool = merged

    if args.out:
        out = Path(args.out)
    elif args.mode == "mainboard":
        out = Path(__file__).with_name("pool_mainboard_all.txt")
    else:
        out = Path(__file__).with_name(
            f"pool_{'random' if args.mode == 'random' else 'liquid'}{args.n}.txt")

    if args.mode == "mainboard":
        how = "baostock 当日全量证券列表中的**沪深主板**全部（不排序、不筛）"
        cmd = "python scripts/build_pool.py --mode mainboard"
        note = ("本池是**监控覆盖**口径：目标是「不漏任何一只主板票」，故**保留**\n"
                "# ST/*ST/退市整理股与当日停牌股。")
        head = "# 沪深主板监控标的池（全量覆盖）"
        filt = "# 保留 ST/退市、停牌股（与回测池口径相反）；仅剔北交所/创业板/科创板。"
        warn = "# ⚠️ 这是**盘中监控的扫描范围**，不是股票池推荐，**也不适合回测**。"
    elif args.mode == "random":
        how = (f"全市场（沪深主板/创业板/科创板，剔 ST）**随机抽样** {len(pool)} 只，"
               f"种子 {args.seed}")
        cmd = f"python scripts/build_pool.py --n {args.n} --mode random --seed {args.seed}"
        note = ("池子构成与后续涨跌**无关**，是评价择时策略的公正样本；\n"
                "# 对比 `pool_liquid500.txt`（按成交额选，含前视选择偏差）。")
        head = "# 缠论策略回测标的池"
        filt = "# 剔除 ST/退市、北交所（新浪日线接口不支持）。"
        warn = "# ⚠️ 这不是「股票池推荐」，只是为策略评估凑样本量的**回测样本**。"
    else:
        how = f"新浪全市场快照按**当日成交额**降序取前 {args.n} 只"
        cmd = f"python scripts/build_pool.py --n {args.n}"
        note = ("⚠️ 按**当前**成交额选池会引入**前视选择偏差**：用它回测更早的区间\n"
                "# 等于挑出了那段时间的赢家，同池「买入持有」基线会被显著抬高。\n"
                "# 评价策略时请与 `pool_random500.txt`（随机抽样）一起看。")
        head = "# 缠论策略回测标的池"
        filt = "# 剔除 ST/退市、北交所（新浪日线接口不支持）。"
        warn = "# ⚠️ 这不是「股票池推荐」，只是为策略评估凑样本量的**回测样本**。"
    lines = [
        f"{head} —— {len(pool)} 只",
        "#",
        f"# 选取口径：{how}；",
        filt,
        f"# 生成命令：`{cmd}`",
        "#",
        "# 一行一只：`代码 名称`（代码用新浪格式 sh/sz + 6 位，名称仅作注释）。",
        "#",
        warn,
        f"# {note}",
        "",
    ]
    if added:
        lines.append(f"# ---- 以下 {len(added)} 只由 --include 并入 ----")
        lines.append("")
    lines += [f"{code} {name}" for code, name in pool]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if args.mode == "mainboard":
        print(f"写出 {len(pool)} 只主板票（并入 {len(added)}）→ {out}")
    else:
        print(f"写出 {len(pool)} 只（快照前 {args.n} + 并入 {len(added)}）→ {out}")
    print(f"前 8：{pool[:8]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
