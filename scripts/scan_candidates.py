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


def _scan_one(code, name, cache, args):
    from data.cache_files import metadata
    from core.trading_calendar import latest_closed_session, local_now
    meta = metadata(cache / f"daily_{code}.csv")
    if not meta or meta["requested_end"] < latest_closed_session():
        raise ValueError("日线来源/新鲜度未验证，请先完成 daily_routine 或 fetch_min 更新")
    wins = candidate_windows(code, cache, max_amp=args.max_amp, recent_days=args.recent_days)
    items = []
    for window in wins:
        if (local_now().date() - datetime.fromisoformat(window["window_start"]).date()).days > args.recent_days:
            continue
        days_open = (local_now() - datetime.fromisoformat(window["available_at"])).days
        items.append({"code": code, "name": name, **window,
                      "days_open": max(days_open, 0), "hot": days_open <= args.hot_days})
    return items


def main(argv=None):
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
    a = ap.parse_args(argv)

    pool = load_pool(Path(a.pool))
    if a.limit:
        pool = pool[:a.limit]
    cache = Path(a.cache_dir)

    items, missing = [], []
    t0 = time.time()
    failures = []
    for code, name in pool:
        try:
            if not (cache / f"daily_{code}.csv").exists():
                missing.append(code)
                continue
            items.extend(_scan_one(code, name, cache, a))
        except Exception as error:
            failures.append(f"{code}: {error}")
    if missing or failures:
        print(f"扫描不完整，保留旧 watchlist；缺缓存 {missing}；失败 {failures}", file=sys.stderr)
        return 1
    items.sort(key=lambda x: (-x["hot"], -x["days_open"]))
    out = {"generated": datetime.now().isoformat(timespec="seconds"),
           "pool": str(a.pool), "max_amp": a.max_amp,
           "recent_days": a.recent_days, "hot_days": a.hot_days,
           "items": items, "missing": missing}
    from utils.atomic import atomic_json
    from core.trading_calendar import latest_closed_session
    out["schema"] = 2
    out["as_of"] = latest_closed_session()
    atomic_json(a.out, out)

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
    sys.exit(main())
