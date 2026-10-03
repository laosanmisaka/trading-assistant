# -*- coding: utf-8 -*-
"""轮询可观察窗口，共享逐 bar 信号算法；信号日志是恢复的唯一事实来源。

新浪与 baostock 的起点、复权和修订可能不同，因此共享算法不保证不同输入
产生同一信号。只处理收口 bar。旧 watchlist 必须重新扫描；旧信号保留为历史。
新增事件先 fsync 日志，再替换检查点，最后尝试桌面通知。崩溃可能漏通知，但
不会丢已持久化事件。仅近期新增且有效的首次入场弹窗，历史回放只记日志。
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import chan as chan_mod          # noqa: E402
from core import chan_points               # noqa: E402
from core import triple_buy                # noqa: E402
from data.models import KLineData          # noqa: E402

SINA_KLINE = ("https://money.finance.sina.com.cn/quotes_service/api/"
              "json_v2.php/CN_MarketData.getKLineData"
              "?symbol={sym}&scale=5&ma=no&datalen={datalen}")
from core.causal_signals import confirmed_points
from core.signal_journal import commit_event, recover, merge_observation, JournalWriteError
from core.trading_calendar import local_now
from utils import is_trading_time
from utils.process_lock import process_lock


def _in_session(now: datetime) -> bool:
    return is_trading_time(now)


def _parse_bar(code, row, now):
    stamp = pd.Timestamp(row["day"])
    if pd.isna(stamp) or stamp > now:
        return None
    o, h, l, c = [float(row[key]) for key in ("open", "high", "low", "close")]
    volume = float(row.get("volume") or 0)
    if not all(math.isfinite(v) for v in (o, h, l, c, volume)):
        raise ValueError("Nonfinite OHLCV")
    if min(o, h, l, c) <= 0 or volume <= 0:
        return None
    if not l <= min(o, c) <= max(o, c) <= h:
        raise ValueError("Inconsistent OHLC")
    return KLineData(code=code, date=str(stamp), open=o, high=h, low=l,
                     close=c, volume=int(volume), period="5min")


def fetch_sina_5min(code: str, *, datalen=5001, timeout=10.0):
    """Bad responses fail this stock only, never the entire polling round."""
    response = requests.get(SINA_KLINE.format(sym=code, datalen=datalen), timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError("Sina response must be an array")
    now = pd.Timestamp(local_now())
    parsed = [_parse_bar(code, row, now) for row in data]
    by_time = {bar.date: bar for bar in parsed if bar is not None}
    return [by_time[key] for key in sorted(by_time)] or None


def toast(title: str, msg: str) -> None:
    """Windows 气泡通知（PowerShell NotifyIcon，-EncodedCommand 避免中文乱码）"""
    title, msg = title.replace("'", "''"), msg.replace("'", "''")
    ps = ("[void][System.Reflection.Assembly]::LoadWithPartialType("
          "'System.Windows.Forms');$n=New-Object System.Windows.Forms.NotifyIcon;"
          "$n.Icon=[System.Drawing.SystemIcons]::Information;"
          "$n.Visible=$true;"
          f"$n.ShowBalloonTip(9000,'{title}','{msg}',"
          "[System.Windows.Forms.ToolTipIcon]::None);"
          "Start-Sleep -Seconds 10;$n.Dispose()")
    enc = base64.b64encode(ps.encode("utf-16-le")).decode()
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-EncodedCommand", enc],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:                          # noqa: BLE001
        pass


def detect_entries(code: str, k5: list[KLineData], windows: list[dict],
                   now: pd.Timestamp) -> list[dict]:
    """共享逐 bar 回放；旧 watchlist 缺可观测时刻时拒绝猜测，请重新扫描。"""
    from core.causal_signals import replay_entries
    if any("available_at" not in window or "window_id" not in window for window in windows):
        raise ValueError("watchlist 缺少可观察窗口版本，请重新运行 scan_candidates.py")
    return replay_entries(code, k5, windows, as_of=now)


def _include_retractions(code, bars, events, previous):
    current = chan_mod.build(bars, "5min")
    points = confirmed_points(chan_mod.bis(current), chan_mod.centers(current)) if current else {}
    # Retractions still propagate when a window disappeared from the watchlist.
    seen = {event["event_id"] for event in events}
    for key, old in list(previous.items()):
        if old.get("code") != code or not old.get("event_id") or key in seen:
            continue
        if old.get("stroke_start", "") < str(bars[0].date):
            continue  # no longer enough warm-up to reassess this geometry
        point = points.get(old["point_id"])
        events.append({**old, **(point or {}), "active": point is not None})


def _record_observations(code, bars, windows, state, args, now):
    events = detect_entries(code, bars, windows, now)
    previous = state["alerts"]
    consumed = {ev.get("window_id") for ev in previous.values() if ev.get("strategy_entry")}
    consumed.update((ev["code"], ev["window_start"]) for ev in previous.values()
                    if ev.get("strategy_entry") and not ev.get("window_id"))
    _include_retractions(code, bars, events, previous)
    count = 0
    for event in events:
        old = previous.get(event["event_id"])
        merged = merge_observation(event, old, consumed)
        if merged is None:
            continue
        merged["ts"] = str(now)
        merged["updated_at"] = str(now)
        commit_event(state, merged, args.signals, args.state)
        if merged["strategy_entry"]:
            consumed.add(merged["window_id"])
        fresh = pd.Timedelta(0) <= now - pd.Timestamp(merged["conf"]) <= pd.Timedelta(minutes=10)
        if old is None and fresh and merged["active"] and merged["strategy_entry"]:
            message = f"{code} {merged['conf']} @ {merged['entry_price']:.3f}"
            print(f"  >>> {message}", flush=True)
            if not args.no_toast:
                toast("三买策略买点", message)
            count += 1
    return count


def run_round(watch, state, args, rnd):
    now = pd.Timestamp(local_now())
    by_code = {}
    for window in watch["items"]:
        if window.get("hot") or rnd % args.old_every == 0 or args.once:
            by_code.setdefault(window["code"], []).append(window)
    for event in state["alerts"].values():
        if event.get("event_id") and now - pd.Timestamp(event["conf"]) <= pd.Timedelta(days=7):
            by_code.setdefault(event["code"], [])
    count, failures = 0, 0
    for code, windows in by_code.items():
        try:
            bars = fetch_sina_5min(code, datalen=args.datalen)
            if bars:
                count += _record_observations(code, bars, windows, state, args, now)
        except JournalWriteError:
            raise
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
            failures += 1
            print(f"[本股失败] {code}: {type(error).__name__}: {error}", flush=True)
        time.sleep(args.sleep)
    print(f"[{now:%H:%M:%S}] 第{rnd}轮：{len(by_code)}只，失败{failures}，新通知{count}", flush=True)
    state["round_failures"] = failures
    return count


def _load_watch(args):
    if args.codes:
        from scripts.scan_candidates import _scan_one
        from types import SimpleNamespace
        items = []
        scan = SimpleNamespace(max_amp=40.0, recent_days=90, hot_days=3)
        for code in args.codes.split(","):
            items.extend(_scan_one(code.strip(), "", ROOT / "outputs/cache_min", scan))
        return {"items": items}
    watch = json.loads(Path(args.watchlist).read_text(encoding="utf-8"))
    if not isinstance(watch.get("items"), list):
        raise ValueError("watchlist.items 必须为列表")
    from core.trading_calendar import latest_closed_session
    if str(watch.get("as_of", "")) < latest_closed_session():
        raise ValueError("watchlist 已过期，请更新日线并重新扫描")
    for window in watch["items"]:
        if not {"code", "window_id", "available_at"}.issubset(window):
            raise ValueError("旧 watchlist 缺可观察时刻，请重跑 scan_candidates.py")
    return watch


def _loop(args):
    state = recover(args.signals, args.state)
    rnd = 0
    while True:
        if args.once or _in_session(local_now()):
            try:
                watch = _load_watch(args)  # reload every round, including across dates
                rnd += 1
                run_round(watch, state, args, rnd)
            except JournalWriteError:
                raise
            except (OSError, ValueError) as error:
                print(f"[轮询暂停] {error}", flush=True)
                if args.once:
                    return 1
            if args.once:
                return 1 if state.get("round_failures") else 0
        time.sleep(args.interval if _in_session(local_now()) else min(args.interval, 60))


def main(argv=None):
    parser = argparse.ArgumentParser(description="可观察三买窗口盘中监控")
    for flag, filename in (("watchlist", "watchlist.json"), ("state", "monitor_state.json"),
                           ("signals", "monitor_signals.jsonl")):
        parser.add_argument(f"--{flag}", default=str(ROOT / "outputs" / filename))
    parser.add_argument("--codes", default="", help="从本地日线重新提取指定标的真实候选窗口")
    parser.add_argument("--datalen", type=int, default=5001)
    parser.add_argument("--sleep", type=float, default=0.3)
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--old-every", type=int, default=6)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-toast", action="store_true")
    args = parser.parse_args(argv)
    if args.interval <= 0 or args.old_every <= 0 or args.sleep < 0:
        parser.error("轮询周期和频率必须为正数，拉取间隔不能为负数")
    with process_lock(str(Path(args.signals).resolve()) + ".lock"):
        return _loop(args)


if __name__ == "__main__":
    sys.exit(main())
