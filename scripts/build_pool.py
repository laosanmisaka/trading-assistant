# -*- coding: utf-8 -*-
"""按流动性构造标的池文件

    python scripts/build_pool.py                    # 默认 500 只
    python scripts/build_pool.py --n 300
    python scripts/build_pool.py --out scripts/pool_liquid300.txt

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
    p.add_argument("--mode", choices=("liquid", "random"), default="liquid",
                   help="liquid=按成交额（默认）；random=全市场随机抽样（无前视偏差）")
    p.add_argument("--seed", type=int, default=20260920, help="random 模式的抽样种子")
    p.add_argument("--snapshot", default="", help="本地快照 CSV（列同 stock_zh_a_spot）")
    p.add_argument("--include", default="",
                   help="并入已有池文件（逗号分隔）—— 保证新旧池可比，不影响排序口径")
    p.add_argument("--out", default="", help="输出文件（默认 scripts/pool_liquid<N>.txt）")
    args = p.parse_args(argv)

    if args.mode == "random":
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

    out = Path(args.out) if args.out else Path(__file__).with_name(
        f"pool_{'random' if args.mode == 'random' else 'liquid'}{args.n}.txt")

    if args.mode == "random":
        how = (f"全市场（沪深主板/创业板/科创板，剔 ST）**随机抽样** {len(pool)} 只，"
               f"种子 {args.seed}")
        cmd = f"python scripts/build_pool.py --n {args.n} --mode random --seed {args.seed}"
        note = ("池子构成与后续涨跌**无关**，是评价择时策略的公正样本；\n"
                "# 对比 `pool_liquid500.txt`（按成交额选，含前视选择偏差）。")
    else:
        how = f"新浪全市场快照按**当日成交额**降序取前 {args.n} 只"
        cmd = f"python scripts/build_pool.py --n {args.n}"
        note = ("⚠️ 按**当前**成交额选池会引入**前视选择偏差**：用它回测更早的区间\n"
                "# 等于挑出了那段时间的赢家，同池「买入持有」基线会被显著抬高。\n"
                "# 评价策略时请与 `pool_random500.txt`（随机抽样）一起看。")
    lines = [
        f"# 六脉神剑 / 缠论策略回测标的池 —— {len(pool)} 只",
        "#",
        f"# 选取口径：{how}；",
        "# 剔除 ST/退市、北交所（新浪日线接口不支持）。",
        f"# 生成命令：`{cmd}`",
        "#",
        "# 一行一只：`代码 名称`（代码用新浪格式 sh/sz + 6 位，名称仅作注释）。",
        "#",
        "# ⚠️ 这不是「股票池推荐」，只是为策略评估凑样本量的**回测样本**。",
        f"# {note}",
        "",
    ]
    if added:
        lines.append(f"# ---- 以下 {len(added)} 只由 --include 并入（原 50 只池中成交额不足的） ----")
        lines.append("")
    lines += [f"{code} {name}" for code, name in pool]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"写出 {len(pool)} 只（快照前 {args.n} + 并入 {len(added)}）→ {out}")
    print(f"前 8：{pool[:8]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
