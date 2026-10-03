# -*- coding: utf-8 -*-
"""盘后完整更新 → 候选扫描 → 5min 更新 → GUI 分组同步。

无重叠/复权漂移完整重取；部分失败不覆盖旧缓存，不消费过期 watchlist。
--cache-dir/--limit/--watchlist 传递给扫描。--skip-scan 只更新日线。
调度显式 --register-tasks 才注册，名称带仓库标识；不终止其他实例。
详细合同见 docs/OPERATIONS.md。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import fetch_min  # noqa: E402
from core import chan_viz  # noqa: E402

DEFAULT_POOL = ROOT / "scripts" / "pool_mainboard_all.txt"
DEFAULT_CACHE = ROOT / "outputs" / "cache_min"
WATCHLIST = ROOT / "outputs" / "watchlist.json"
LOG_FILE = ROOT / "outputs" / "daily_routine.log"

#: watchlist 在 GUI 里对应的自动分组名 —— 由同步逻辑全量接管（增删都只看
#: 当天 watchlist），用户手动跟踪的票请放「跟踪中」等其他分组，别放这里。
WATCH_GROUP_NAME = "三买监控"

#: 增量回取的重叠窗口（自然日）：既要覆盖停牌/节假日的空档，又要给
#: 前复权平移校验留够样本。15 天 ≈ 10 个交易日，单段取数（< 各级别分段长）。
OVERLAP_DAYS = 15


# ======================================================================
# 纯逻辑（可离线单测）
# ======================================================================

def merge_incremental(old: pd.DataFrame, new: pd.DataFrame, *,
                      rtol: float = 1e-4) -> tuple[pd.DataFrame, str]:
    """合并增量 → ``(merged, status)``

    status：
    - ``"no_new"``   —— new 为空，或没有超过 old 末尾的行（非交易日/停牌）；
    - ``"appended"`` —— 正常追加（重叠区收盘价一致）；
    - ``"shifted"``  —— 重叠区收盘价不一致 ⇒ 前复权平移，old 整份作废，
      调用方必须全量重取。
    """
    if new.empty:
        return old, "no_new"
    if old.empty:
        return new.sort_values("dt").drop_duplicates("dt", keep="last"), "appended"
    ov = old.merge(new, on="dt", suffixes=("_o", "_n"))
    if ov.empty:
        return old, "unverified"
    if len(ov):
        diff = (ov["close_o"] - ov["close_n"]).abs() > ov["close_n"].abs() * rtol
        if diff.any():
            return old, "shifted"
    newer = new[new["dt"] > old["dt"].iloc[-1]]
    if newer.empty:
        return old, "no_new"
    merged = (pd.concat([old, new], ignore_index=True)
              .drop_duplicates("dt", keep="last")
              .sort_values("dt", kind="stable")
              .reset_index(drop=True))
    return merged, "appended"


# ======================================================================
# 取数（联网）
# ======================================================================

def _fetch_recent(session: fetch_min.BaostockSession, sym: str, period: str,
                  start: str, end: str) -> pd.DataFrame:
    """增量单段取数：空区间（停牌/非交易日）返回**空 df 不抛异常**，网络层
    失败按 fetch_min 的惯例重试 + 重登。重叠窗口 15 天小于各级别分段长，
    保证单段。"""
    code = fetch_min.to_baostock_code(sym)
    if code is None:
        raise RuntimeError(f"{sym}：baostock 不支持北交所")
    last: Exception | None = None
    for i in range(3):
        if i:
            time.sleep(2.0 * i)
        try:
            return fetch_min._fetch_baostock_segment(session, code, period,
                                                     start, end)
        except Exception as exc:
            last = exc
            if i >= 1:
                session.relogin()
    raise RuntimeError(f"{sym} {period} 增量取数失败（重试 3 次）：{last}")


def _full_cache(session, sym, period, cache_dir, today):
    from data.cache_files import publish
    start = fetch_min.DEFAULT_START[period]
    frame, stats = fetch_min.fetch_baostock(session, sym, period, start, today)
    if stats.get("失败段", 0):
        raise RuntimeError(f"取数不完整，保留原缓存：{stats}")
    publish(fetch_min.cache_path(cache_dir, sym, period), frame, source="baostock",
            adjustment="qfq", start=start, end=today)


def update_one(session, sym, period, cache_dir, *, overlap_days=OVERLAP_DAYS):
    """Only hash-verified complete caches can be extended; no overlap means refetch."""
    from data.cache_files import metadata, publish, validate_frame
    from core.trading_calendar import local_now
    path = fetch_min.cache_path(cache_dir, sym, period)
    today = str(local_now().date())
    meta = metadata(path)
    if not meta or (meta.get("source"), meta.get("adjustment")) != ("baostock", "qfq"):
        _full_cache(session, sym, period, cache_dir, today)
        return "full"
    old = validate_frame(pd.read_csv(path, float_precision="round_trip"))
    if old.empty or meta["requested_start"] > fetch_min.DEFAULT_START[period]:
        _full_cache(session, sym, period, cache_dir, today)
        return "full"
    last_day = str(old["dt"].iloc[-1])[:10]
    # A current tail alone never certifies an old partial download.
    if last_day >= today and meta["requested_end"] >= today:
        return "fresh"
    start = (pd.Timestamp(last_day) - pd.Timedelta(days=overlap_days)).strftime("%Y-%m-%d")
    new = validate_frame(_fetch_recent(session, sym, period, start, today))
    merged, status = merge_incremental(old, new)
    if status in ("shifted", "unverified"):
        _full_cache(session, sym, period, cache_dir, today)
        return f"{status}_refetch"
    if status == "appended":
        publish(path, merged, source="baostock", adjustment="qfq",
                start=meta["requested_start"], end=today)
    return status


def update_pool(session: fetch_min.BaostockSession,
                pool: list[tuple[str, str]], period: str, cache_dir: Path,
                *, sleep: float = 0.3, progress_every: int = 100,
                log=print) -> dict[str, int]:
    """按池增量更新 → 各状态的只数统计；单只失败不中断整池。"""
    counts: dict[str, int] = {}
    failures: list[str] = []
    t0 = time.time()
    for i, (sym, _name) in enumerate(pool, 1):
        try:
            st = update_one(session, sym, period, cache_dir)
        except Exception as exc:
            st = "failed"
            failures.append(f"{sym}（{exc}）")
        counts[st] = counts.get(st, 0) + 1
        if progress_every and i % progress_every == 0:
            log(f"  {period} 进度 {i}/{len(pool)}（{time.time()-t0:.0f}s）"
                f" {dict(counts)}")
        time.sleep(sleep)
    if failures:
        log(f"  {period} 失败 {len(failures)} 只："
            + "、".join(failures[:10])
            + (" …" if len(failures) > 10 else ""))
    return counts


# ======================================================================
# watchlist → GUI 分组
# ======================================================================

def sync_watchlist_group(watch: dict, *,
                         group_name: str = WATCH_GROUP_NAME) -> dict:
    """把 watchlist 同步成 GUI 里的一个分组 → ``{"added", "removed", "total"}``

    **全量接管**该分组：在 watchlist 里的补齐、不在的移除 —— 窗口关闭了
    的票留在这只会误导。其他分组（持仓中/跟踪中/自定义）一概不碰；
    交易记录按 stock_code 关联，移出分组不影响历史成交。

    代码格式：watchlist 是 ``sh600519``，GUI 库里存 6 位数字（与手动添加
    一致，实时行情/图表各自再做归一化）。名称为空时回查本地名称库。
    """
    from data.database import (init_db, get_all_groups, add_group,
                               get_stocks_by_group, add_stock, remove_stock,
                               get_stock_name)
    init_db()
    groups = {g.name: g for g in get_all_groups()}
    g = groups.get(group_name) or add_group(group_name)

    want: dict[str, str] = {}
    for it in watch.get("items", []):
        c = chan_viz.normalize_code(it["code"])
        code = c[2:] if c[:2] in ("sh", "sz", "bj") else c
        want.setdefault(code, it.get("name") or "")

    have = {s.code: s for s in get_stocks_by_group(g.id)}
    added = removed = 0
    for code, name in want.items():
        if code not in have:
            add_stock(code, name or (get_stock_name(code) or ""), g.id)
            added += 1
    for code, s in have.items():
        if code not in want:
            remove_stock(s.id)
            removed += 1
    return {"added": added, "removed": removed, "total": len(want)}


# ======================================================================
# 计划任务
# ======================================================================

def register_tasks(log=print) -> None:
    """注册两个工作日计划任务（/F 覆盖同名，/IT 当前用户交互式运行）"""
    import os
    import hashlib
    if os.name != "nt":
        raise RuntimeError("计划任务注册只支持 Windows；其他平台使用系统调度器")
    suffix = hashlib.sha256(str(ROOT).encode()).hexdigest()[:8]
    py = Path(sys.executable)
    jobs = [
        ("TradingAssistant-DailyUpdate", "18:05",
         f'"{py}" "{ROOT / "scripts" / "daily_routine.py"}"'),
        ("TradingAssistant-Monitor", "09:25",
         f'"{py}" "{ROOT / "scripts" / "monitor_buy.py"}"'),
    ]
    for name, st, tr in jobs:
        cmd = ["schtasks", "/Create", "/TN", f"{name}-{suffix}", "/TR", tr,
               "/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI",
               "/ST", st, "/F", "/IT"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        log(f"  {name}: {'OK' if r.returncode == 0 else r.stderr.strip()}")
        if r.returncode != 0:
            raise SystemExit(f"注册 {name} 失败：{r.stderr or r.stdout}")
    log("计划任务已注册：盘后 18:05 更新+扫描，盘前 09:25 启动监控；"
        "可用 schtasks /Delete /TN <名称> 删除")


# ======================================================================
# 主流程
# ======================================================================

def _scan_watch(args, log):
    command = [sys.executable, str(ROOT / "scripts/scan_candidates.py"),
               "--pool", str(args.pool), "--cache-dir", str(args.cache_dir),
               "--out", str(args.watchlist), "--limit", str(args.limit)]
    result = subprocess.run(command, capture_output=True, text=True)
    log(result.stdout or "")
    if result.returncode:
        raise RuntimeError(f"扫描失败({result.returncode})：{result.stderr[:500]}")
    return json.loads(args.watchlist.read_text(encoding="utf-8"))


def _run_daily(args, log):
    pool = fetch_min.load_pool(args.pool)
    if args.limit:
        pool = pool[:args.limit]
    if not pool:
        raise ValueError("标的池为空")
    with fetch_min.BaostockSession() as session:
        counts = update_pool(session, pool, "daily", args.cache_dir, sleep=args.sleep, log=log)
        log(f"日线结果：{counts}")
        if counts.get("failed"):
            raise RuntimeError("日线更新不完整，本次不扫描、不改自动分组")
        if args.skip_scan:
            log("跳过扫描：本次只更新日线，不消费旧 watchlist")
            return
        watch = _scan_watch(args, log)
        if not args.skip_5min:
            codes = sorted({item["code"] for item in watch["items"]})
            counts = update_pool(session, [(code, "") for code in codes], "5min",
                                 args.cache_dir, sleep=args.sleep, log=log)
            log(f"5min 结果：{counts}")
            if counts.get("failed"):
                raise RuntimeError("5min 更新不完整，本次不改自动分组")
        if not args.skip_sync and not args.limit:
            from core.buy_points import sync_buy_points_group
            log(f"三买监控同步：{sync_watchlist_group(watch)}")
            log(f"策略买点同步：{sync_buy_points_group()}")
        elif args.limit:
            log("局部调试池不全量接管 GUI 自动分组")


def main(argv=None):
    parser = argparse.ArgumentParser(description="盘后完整更新 → 扫描 → 同步")
    parser.add_argument("--pool", type=Path, default=DEFAULT_POOL)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--watchlist", type=Path, default=None)
    for flag in ("skip-5min", "skip-scan", "skip-sync", "no-toast", "register-tasks"):
        parser.add_argument(f"--{flag}", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=0.3)
    args = parser.parse_args(argv)
    args.watchlist = args.watchlist or args.cache_dir.parent / "watchlist.json"
    lines = []
    def log(message):
        print(message, flush=True)
        lines.append(message)
    if args.register_tasks:
        register_tasks(log)
        return 0
    status = 0
    try:
        _run_daily(args, log)
        log("盘后例程成功")
    except Exception as error:
        status = 1
        log(f"盘后例程失败：{type(error).__name__}: {error}")
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as stream:
        stream.write(f"{datetime.now().isoformat()}\n" + "\n".join(lines) + "\n")
    if not args.no_toast:
        from monitor_buy import toast
        toast("盘后例程失败" if status else "盘后例程完成", lines[-1])
    return status


if __name__ == "__main__":
    sys.exit(main())
