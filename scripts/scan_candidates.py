# -*- coding: utf-8 -*-
"""层①：日线三买候选扫描 → 当日监控名单（watchlist）

盘后/开盘前跑一遍：对池内每只用**日线缓存**跑 `core.triple_buy.candidate_windows`
（与回测链同源），输出所有「突破幅度 < 40% 且确认笔未走完」的活跃窗口，
供 `monitor_buy.py` 盘中轮询消费。

用法
----
    python scripts/scan_candidates.py                       # 全主板（默认池）
    python scripts/scan_candidates.py --pool scripts/pool_random2000.txt
    python scripts/scan_candidates.py --limit 100           # 抽样试跑

输出
----
    outputs/watchlist.json：
    {"generated", "pool", "max_amp", "recent_days", "items": [...],
     "missing": [无日线缓存的代码...]}
    每个 item：{"code", "name", "window_start", "signal_date", "amp",
               "zg", "breakout_high", "days_open", "hot"}
    ``hot`` = 窗口起点在 ``--hot-days``（默认 3 自然日）内 ⇒ 层②每 5 分钟轮询；
    其余老窗口降频到 30 分钟。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.triple_buy import candidate_windows  # noqa: E402


def load_pool(path: Path) -> list[tuple[str, str]]:
    """读池文件 → [(code, name)]；兼容「只有代码」的行"""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        out.append((parts[0], parts[1] if len(parts) > 1 else ""))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="日线三买活跃窗口扫描 → watchlist")
    ap.add_argument("--pool", default=str(ROOT / "scripts" / "pool_mainboard_all.txt"))
    ap.add_argument("--cache-dir", default=str(ROOT / "outputs" / "cache_min"))
    ap.add_argument("--out", default=str(ROOT / "outputs" / "watchlist.json"))
    ap.add_argument("--max-amp", type=float, default=40.0,
                    help="突破幅度闸门（%%），与定稿策略一致")
    ap.add_argument("--recent-days", type=int, default=90,
                    help="窗口起点早于数据末尾 N 自然日的不再监控")
    ap.add_argument("--hot-days", type=int, default=3,
                    help="窗口起点 N 自然日内 = 热窗口（层②每轮都扫）")
    ap.add_argument("--limit", type=int, default=0, help="只扫前 N 只（调试）")
    a = ap.parse_args()

    pool = load_pool(Path(a.pool))
    if a.limit:
        pool = pool[:a.limit]
    cache = Path(a.cache_dir)

    items, missing = [], []
    t0 = time.time()
    for n, (code, name) in enumerate(pool, 1):
        wins = candidate_windows(code, cache, max_amp=a.max_amp,
                                 recent_days=a.recent_days)
        if not wins:
            if not (cache / f"daily_{code}.csv").exists():
                missing.append(code)
            continue
        dfp = cache / f"daily_{code}.csv"
        last_day = None
        if dfp.exists() and dfp.stat().st_size > 0:
            with dfp.open(encoding="utf-8") as f:
                for line in f:
                    pass
                last_day = line.split(",")[0].strip()
        if not last_day:
            continue          # 缓存缺失/空 → 算不出 days_open，跳过（正常不该发生）
        for w in wins:
            days_open = (datetime.fromisoformat(last_day)
                         - datetime.fromisoformat(w["window_start"])).days
            items.append({"code": code, "name": name, **w,
                          "days_open": max(days_open, 0),
                          "hot": days_open <= a.hot_days})

    items.sort(key=lambda x: (-x["hot"], -x["days_open"]))
    out = {"generated": datetime.now().isoformat(timespec="seconds"),
           "pool": str(a.pool), "max_amp": a.max_amp,
           "recent_days": a.recent_days, "hot_days": a.hot_days,
           "items": items, "missing": missing}
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1),
                           encoding="utf-8")

    hot = [x for x in items if x["hot"]]
    print(f"扫描 {len(pool)} 只（{time.time()-t0:.0f}s）："
          f"活跃窗口 {len(items)} 个 / {len({x['code'] for x in items})} 只，"
          f"其中热窗口 {len(hot)} 个；无日线缓存 {len(missing)} 只")
    for x in items[:30]:
        print(f"  {'热' if x['hot'] else '  '} {x['code']} {x['name']:<6} "
              f"窗口 {x['window_start']}（{x['days_open']}天前） "
              f"幅度 {x['amp']}%  zg {x['zg']:.2f}  三买点 {x['signal_date']}")
    print(f"watchlist → {a.out}")


if __name__ == "__main__":
    main()
