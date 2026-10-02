# -*- coding: utf-8 -*-
"""盘后一键例程：增量更新行情缓存 → 刷新 watchlist → 更新监控标的 5min 缓存

    python scripts/daily_routine.py                    # 盘后全流程
    python scripts/daily_routine.py --skip-5min        # 只更日线 + 扫描
    python scripts/daily_routine.py --limit 20         # 调试：只跑前 20 只
    python scripts/daily_routine.py --register-tasks   # 注册 Windows 计划任务（一次性）

做什么
------
1. 日线缓存**增量更新**（默认池 = 层①扫描用的 pool_mainboard.txt）；
2. 调 `scan_candidates.py` 刷新 `outputs/watchlist.json`；
3. watchlist 同步成 GUI 分组「三买监控」（全量接管该分组：新窗口补齐、
   关闭窗口移除；其他分组不碰，移出分组不影响交易记录）；顺带把近两日
   策略买点（monitor 落的 monitor_signals.jsonl）同步成分组「策略买点」；
4. 对 watchlist 里的标的做 5min 缓存增量更新 —— 这份缓存供 GUI 三买标注
   和回测/复盘链使用；盘中监控（层② `monitor_buy.py`）吃的是新浪实时
   5min，**不依赖**这份缓存，所以 5min 只更监控名单内的票，不全池更。

为什么增量而不是全量重取
------------------------
全量重取 5min 要从 2020 年起分约 27 段/只，全池跑一遍以小时计；日线全量
单段近 5000 行，超出 baostock 单次查询的实测安全上限（约 3,200 行）。
增量只取「缓存末尾 − N 天」到今天（单段/只），再用**重叠区间收盘价校验**
识别前复权平移（分红除权后 baostock 历史价整体漂移，见 `fetch_min.py`
docstring 的缓存过期警告）：不一致则对该只自动全量重取。校验窗口内停牌
无新 bar 时视为「无新数据」，不误判漂移。

计划任务（`--register-tasks` 注册）
-----------------------------------
- ``TradingAssistant-DailyUpdate``：周一至周五 18:05 跑本脚本（baostock
  日线晚间才就绪；若当天数据未出，次日开盘前手动补跑一次即可，幂等）；
- ``TradingAssistant-Monitor``：周一至周五 09:25 跑 `start_monitor.bat`
  （清掉昨天的残留进程，以最新 watchlist 重启盘中监控）。

日志追加到 `outputs/daily_routine.log`。
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
    ov = old.merge(new, on="dt", suffixes=("_o", "_n"))
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


def update_one(session: fetch_min.BaostockSession, sym: str, period: str,
               cache_dir: Path, *, overlap_days: int = OVERLAP_DAYS) -> str:
    """增量更新一只的缓存 → 状态串

    ``fresh`` 已是最新｜``full`` 无缓存全量建｜``appended`` 增量追加｜
    ``no_new`` 无新数据｜``shifted_refetch`` 检出前复权平移、已全量重取。
    取数异常向上抛，由调用方按只捕获。
    """
    f = fetch_min.cache_path(cache_dir, sym, period)
    today = date.today().strftime("%Y-%m-%d")
    if not f.exists() or f.stat().st_size == 0:
        df, _ = fetch_min.fetch_baostock(session, sym, period,
                                         fetch_min.DEFAULT_START[period], today)
        cache_dir.mkdir(parents=True, exist_ok=True)
        df.to_csv(f, index=False)
        return "full"
    old = pd.read_csv(f)
    if old.empty:
        f.unlink()
        return update_one(session, sym, period, cache_dir,
                          overlap_days=overlap_days)
    last_day = str(old["dt"].iloc[-1])[:10]
    if last_day >= today:
        return "fresh"
    start = (pd.Timestamp(last_day)
             - pd.Timedelta(days=overlap_days)).strftime("%Y-%m-%d")
    new = _fetch_recent(session, sym, period, start, today)
    merged, status = merge_incremental(old, new)
    if status == "shifted":
        df, _ = fetch_min.fetch_baostock(session, sym, period,
                                         fetch_min.DEFAULT_START[period], today)
        df.to_csv(f, index=False)
        return "shifted_refetch"
    if status == "appended":
        merged.to_csv(f, index=False)
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
    pyw = Path(sys.executable).with_name("pythonw.exe")
    py = pyw if pyw.exists() else Path(sys.executable)
    jobs = [
        ("TradingAssistant-DailyUpdate", "18:05",
         f'"{py}" "{ROOT / "scripts" / "daily_routine.py"}"'),
        ("TradingAssistant-Monitor", "09:25",
         f'"{ROOT / "scripts" / "start_monitor.bat"}"'),
    ]
    for name, st, tr in jobs:
        cmd = ["schtasks", "/Create", "/TN", name, "/TR", tr,
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

def main() -> int:
    p = argparse.ArgumentParser(
        description="盘后例程：增量更缓存 → 刷 watchlist → 更监控标的 5min",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pool", type=Path, default=DEFAULT_POOL,
                   help="日线更新池（默认与层①扫描同池）")
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    p.add_argument("--skip-5min", action="store_true",
                   help="跳过 watchlist 标的的 5min 增量更新")
    p.add_argument("--skip-scan", action="store_true",
                   help="跳过 scan_candidates（只更数据）")
    p.add_argument("--skip-sync", action="store_true",
                   help="跳过 watchlist → GUI 分组同步")
    p.add_argument("--limit", type=int, default=0, help="只跑池内前 N 只（调试）")
    p.add_argument("--sleep", type=float, default=0.3, help="每只间隔秒数")
    p.add_argument("--no-toast", action="store_true")
    p.add_argument("--register-tasks", action="store_true",
                   help="注册 Windows 计划任务后退出")
    a = p.parse_args()

    # 子进程与父进程同为本地默认编码（GBK），不能按 UTF-8 解码 —
    # GBK 字节对会被误当成合法 UTF-8（如「扫」→ ɨ），再打印即崩
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")

    lines: list[str] = []

    def log(msg: str) -> None:
        print(msg, flush=True)
        lines.append(msg)

    if a.register_tasks:
        register_tasks(log)
        return 0

    t0 = time.time()
    log(f"=== 盘后例程 {datetime.now():%Y-%m-%d %H:%M:%S} ===")

    pool = fetch_min.load_pool(a.pool)
    if a.limit:
        pool = pool[: a.limit]
    log(f"池 {len(pool)} 只（{a.pool.name}）")

    session = fetch_min.BaostockSession()
    with session:
        log("[1/3] 日线增量更新 …")
        c_d = update_pool(session, pool, "daily", a.cache_dir,
                          sleep=a.sleep, log=log)
        log(f"  日线完成：{c_d}")

        if not a.skip_scan:
            log("[2/4] 扫描活跃窗口（scan_candidates）…")
            r = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "scan_candidates.py"),
                 "--pool", str(a.pool)],
                capture_output=True, text=True)   # 子进程同机同编码（GBK），用默认 locale 解码
            for ln in (r.stdout or "").splitlines():
                log(f"  {ln}")
            if r.returncode != 0:
                log(f"  ⚠️ scan_candidates 退出码 {r.returncode}："
                    f"{(r.stderr or '').strip()[:500]}")

        watch = (json.loads(WATCHLIST.read_text(encoding="utf-8"))
                 if WATCHLIST.exists() else None)
        if watch is not None and not a.skip_sync:
            st = sync_watchlist_group(watch)
            log(f"[3/4] watchlist → GUI 分组「{WATCH_GROUP_NAME}」："
                f"新增 {st['added']}、移除 {st['removed']}、现存 {st['total']} 只")
            from core.buy_points import (BUY_GROUP_NAME,
                                         sync_buy_points_group)
            st = sync_buy_points_group()
            log(f"      近两日策略买点 → GUI 分组「{BUY_GROUP_NAME}」："
                f"新增 {st['added']}、移除 {st['removed']}、现存 {st['total']} 只")

        if not a.skip_5min:
            if watch is not None:
                codes = sorted({it["code"] for it in watch["items"]})
                log(f"[4/4] watchlist {len(codes)} 只的 5min 增量更新 …")
                c5 = update_pool(session, [(c, "") for c in codes], "5min",
                                 a.cache_dir, sleep=a.sleep,
                                 progress_every=20, log=log)
                log(f"  5min 完成：{c5}")
            else:
                log("[4/4] 无 watchlist.json，跳过 5min 更新")

    summary = f"例程完成（{time.time()-t0:.0f}s）：日线 {c_d}"
    log(summary)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    if not a.no_toast:
        try:
            from monitor_buy import toast
            toast("盘后例程完成", summary)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
