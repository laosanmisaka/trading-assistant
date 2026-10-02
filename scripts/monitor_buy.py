# -*- coding: utf-8 -*-
"""层②：盘中 5min 轮询 → 三买策略买点实时报警

对 watchlist（`scan_candidates.py` 的输出）里的活跃窗口逐只拉取新浪
5min K 线（实测盘中延迟 ≈0、无会话限制），喂 czsc 判 5min 一买，
**确认即报警** —— 判定与回测链同源（`core.triple_buy` 的同一套函数），
不另写一份。

用法
----
    python scripts/monitor_buy.py                     # 盘中循环（自动守时）
    python scripts/monitor_buy.py --once              # 跑一轮就停（测试）
    python scripts/monitor_buy.py --codes sh600000,sz000001  # 临时指定
    python scripts/monitor_buy.py --no-toast           # 只打控制台+落文件

节奏（已批方案）：热窗口（窗口起点 ≤3 自然日）每 5 分钟一轮；
老窗口每 30 分钟一轮。报警 = 控制台高亮 + Windows 气泡通知 +
追加 `outputs/monitor_signals.jsonl`；已报过的点记进 state 文件，
重启不会重复报。

注意
----
- 5min 数据源是新浪 `money.finance`（datalen=5001 ≈ 104 个交易日），
  只够覆盖**近期**窗口的笔结构 —— 正好是监控要的（窗口起点 ≤90 天）。
  与回测链的 baostock 长历史在起点不同，但 czsc 判笔是逐根递推的，
  近端笔结构不受起点影响。
- 新浪 dt 是 **bar 结束时刻**：14:52 时 14:55 的 bar 还没走完，
  本层只认 dt ≤ 当前时刻的 bar（无未来函数）。
"""
from __future__ import annotations

import argparse
import base64
import json
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
SESSIONS = ((9, 35, 11, 30), (13, 0, 15, 0))


def _in_session(now: datetime) -> bool:
    t = now.hour * 60 + now.minute
    return any(h1 * 60 + m1 <= t <= h2 * 60 + m2 for h1, m1, h2, m2 in SESSIONS)


def fetch_sina_5min(code: str, *, datalen: int = 5001,
                    timeout: float = 10.0) -> list[KLineData] | None:
    """新浪 5min K 线 → 只保留**已收口**（dt ≤ 现在）且非停牌占位的 bar"""
    url = SINA_KLINE.format(sym=code, datalen=datalen)
    try:
        txt = requests.get(url, timeout=timeout).text
        data = json.loads(txt)
    except Exception as e:                    # noqa: BLE001
        print(f"  [取数失败] {code}: {type(e).__name__} {str(e)[:60]}", flush=True)
        return None
    if not isinstance(data, list) or not data:
        return None
    now = pd.Timestamp.now()
    out = []
    for b in data:
        t = pd.Timestamp(b["day"])
        if t > now:                           # 正在走的 bar，没收口
            continue
        o, h, l, c = (float(b["open"]), float(b["high"]),
                      float(b["low"]), float(b["close"]))
        v = int(float(b.get("volume") or 0))
        if c <= 0 or h <= 0 or v <= 0:         # 停牌占位 / 异常行
            continue
        out.append(KLineData(code=code, date=str(t), open=o, high=h,
                             low=l, close=c, volume=v, period="5min"))
    return out or None


def toast(title: str, msg: str) -> None:
    """Windows 气泡通知（PowerShell NotifyIcon，-EncodedCommand 避免中文乱码）"""
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
    """跑 5min 判定链 → 落在活跃窗口内的一买确认事件（不查重，调用方管）"""
    r5 = chan_mod.build(k5, "5min")
    if r5 is None:
        return []
    bis = chan_mod.bis(r5)
    zs = chan_mod.centers(r5)
    if not bis:
        return []
    dt5 = [triple_buy._ts(k.date) for k in k5]
    cl5 = [float(k.close) for k in k5]
    cov_lo = dt5[0]

    events = []
    for q in chan_points.buy_sell_points(bis, zs):
        if q["kind"] != "一买":
            continue
        tconf = triple_buy._confirm_time(bis, q["bi_idx"])
        if tconf is None or tconf < cov_lo:
            continue
        for w in windows:
            t_lo = pd.Timestamp(w["window_start"])
            if not (t_lo <= tconf <= now):
                continue
            e5 = triple_buy._last_bar_at_or_before(dt5, tconf)
            events.append({
                "code": code, "conf": str(tconf), "window_start": w["window_start"],
                "entry_price": round(cl5[e5], 3) if e5 is not None else None,
                # bi_idx = 买点所在那根 5min 笔的下标，用作**去重身份**：
                # conf 是 `_confirm_time` 的返回值、依赖后续笔的 edt，盘中会
                # 随笔结构重排不断刷新（实测同一根笔被报成 4.51 → 4.54 → 4.59），
                # 按 conf 做键会把同一笔数成多个事件、反复弹窗。
                "bi_idx": q["bi_idx"],
                "entry_dt": str(dt5[e5])[:16] if e5 is not None else None,
                "signal_date": w["signal_date"], "amp": w["amp"], "zg": w["zg"],
            })
    return events


def run_round(watch: dict, state: dict, args, rnd: int) -> int:
    """一轮轮询：热窗口每轮扫，老窗口每 old_every 轮扫。返回本轮报警数"""
    now = pd.Timestamp.now()
    hot, old = [], []
    for it in watch["items"]:
        (hot if it["hot"] else old).append(it)
    todo = hot + (old if (rnd % args.old_every == 0 or args.once) else [])
    by_code: dict[str, list[dict]] = {}
    for it in todo:
        by_code.setdefault(it["code"], []).append(it)

    t0, alerts = time.time(), 0
    for code, wins in by_code.items():
        name = next((w["name"] for w in wins if w.get("name")), "")
        k5 = fetch_sina_5min(code, datalen=args.datalen)
        if k5 is None:
            continue
        evs = detect_entries(code, k5, wins, now)
        for ev in evs:
            nth = sum(1 for e in evs
                      if e["window_start"] == ev["window_start"]
                      and pd.Timestamp(e["conf"]) <= pd.Timestamp(ev["conf"]))
            first = nth == 1
            # 去重身份 = 「买点所在的那根 5min 笔」（+ 窗口），**不是 conf**。
            # 见 `detect_entries` 里 bi_idx 的注释：conf 盘中会漂。
            key = f"{ev['code']}|{ev['window_start']}|{ev['bi_idx']}"
            prev = state["alerts"].get(key)
            if isinstance(prev, dict):
                old_conf, old_price = str(prev.get("conf")), prev.get("entry_price")
                if old_conf == ev["conf"]:
                    continue                      # 完全重复：同一笔同一 conf
                # 同一根笔 conf/成交价被刷新（盘中笔结构重排）→ 修正记录。
                # 不弹气泡、不计入报警数，只更新 state + 追加一条 revision。
                prev.update({"conf": ev["conf"], "entry_price": ev["entry_price"],
                             "revision": int(prev.get("revision", 0)) + 1,
                             "ts": datetime.now().isoformat(timespec="seconds")})
                print(f"  ~~ 修正 {ev['code']} {name} 第{nth}个买点 "
                      f"{old_conf[5:16]}@{old_price} → "
                      f"{ev['conf'][5:16]}@{ev['entry_price']}（盘中笔结构刷新）",
                      flush=True)
                with open(args.signals, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts": prev["ts"], **ev, "nth": nth,
                                        "strategy_entry": first,
                                        "revision": prev["revision"]},
                                       ensure_ascii=False) + "\n")
                continue
            if (f"{ev['code']}|{ev['window_start']}|{ev['conf']}"
                    in state.get("legacy_alerts", ())):
                # v1 已报过这条（conf 完全相同）→ 迁移期静默入 state，不重报
                state["alerts"][key] = {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "conf": ev["conf"], "entry_price": ev["entry_price"],
                    "revision": 0, "migrated": True}
                continue
            state["alerts"][key] = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "conf": ev["conf"], "entry_price": ev["entry_price"],
                "revision": 0}
            age_h = (now - pd.Timestamp(ev["conf"])).total_seconds() / 3600
            tag = ("[三买入场] " if first else "[窗口内又一买·非首选] ")
            fresh = "" if age_h <= 1.5 else f"（{age_h:.0f}小时前确认，补报）"
            line = (f"{tag}{ev['code']} {name} 5min一买确认 "
                    f"@{ev['entry_price']} @ {ev['conf']} | 窗口自 "
                    f"{ev['window_start']} 幅度{ev['amp']}% 本窗口第{nth}个{fresh}")
            print(f"  >>> {line}", flush=True)
            with open(args.signals, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": state["alerts"][key]["ts"], **ev,
                                    "nth": nth, "strategy_entry": first,
                                    "revision": 0},
                                   ensure_ascii=False) + "\n")
            if not args.no_toast:
                toast("三买策略买点", line.replace(">>> ", ""))
            alerts += 1
        time.sleep(args.sleep)
    dt = time.time() - t0
    print(f"[{datetime.now():%H:%M:%S}] 第{rnd}轮：扫 {len(by_code)} 只 "
          f"({dt:.0f}s) 新报警 {alerts} 条", flush=True)
    return alerts


def main() -> None:
    ap = argparse.ArgumentParser(description="三买策略买点盘中监控")
    ap.add_argument("--watchlist", default=str(ROOT / "outputs" / "watchlist.json"))
    ap.add_argument("--state", default=str(ROOT / "outputs" / "monitor_state.json"))
    ap.add_argument("--signals", default=str(ROOT / "outputs" / "monitor_signals.jsonl"))
    ap.add_argument("--codes", default="", help="逗号分隔，临时监控指定标的")
    ap.add_argument("--datalen", type=int, default=5001)
    ap.add_argument("--sleep", type=float, default=0.3, help="相邻拉取间隔秒")
    ap.add_argument("--interval", type=int, default=300, help="轮询周期秒")
    ap.add_argument("--old-every", type=int, default=6,
                    help="老窗口每 N 轮扫一次（6×300s=30 分钟）")
    ap.add_argument("--once", action="store_true", help="跑一轮就停（测试/盘后复查）")
    ap.add_argument("--no-toast", action="store_true")
    a = ap.parse_args()

    if a.codes:
        watch = {"items": [{"code": c, "name": "", "hot": True,
                            "window_start": (datetime.now()
                                             - timedelta(days=90)).strftime("%Y-%m-%d"),
                            "signal_date": "", "amp": None, "zg": 0.0,
                            "breakout_high": 0.0, "days_open": 90}
                           for c in a.codes.split(",")]}
    else:
        wp = Path(a.watchlist)
        if not wp.exists():
            sys.exit(f"watchlist 不存在：{wp}\n先跑 python scripts/scan_candidates.py")
        watch = json.loads(wp.read_text(encoding="utf-8"))
        if not watch["items"]:
            sys.exit("watchlist 为空：当前没有活跃窗口，无需监控")

    sp = Path(a.state)
    state = (json.loads(sp.read_text(encoding="utf-8"))
             if sp.exists() else {"alerts": {}})
    state.setdefault("alerts", {})
    # 迁移：v1 的去重键是 ``code|window_start|conf``（conf 盘中会漂，见
    # `detect_entries`），v2 改成 ``...|bi_idx``。v1 报过的那批事件原样记进
    # legacy_alerts，迁移期按**完整键**再静默一遍 —— 否则升级后同一批点会被
    # 当成新事件整批重报（实测 53 只池一次补报 87 条）。
    legacy = set(state.get("legacy_alerts") or [])
    for k in list(state["alerts"]):
        parts = str(k).split("|")
        if len(parts) == 3 and not parts[2].isdigit():
            legacy.add(str(k))
    state["legacy_alerts"] = sorted(legacy)
    state.pop("legacy_windows", None)

    hot = sum(1 for x in watch["items"] if x["hot"])
    print(f"监控 {len({x['code'] for x in watch['items']})} 只 / "
          f"{len(watch['items'])} 个活跃窗口（热 {hot}）；"
          f"已有报警记录 {len(state['alerts'])} 条", flush=True)

    rnd = 0
    while True:
        if not a.once:
            now = datetime.now()
            if not _in_session(now):
                nxt = now.replace(hour=9, minute=35, second=0)
                if now > now.replace(hour=15, minute=0):
                    nxt = (now + timedelta(days=1)).replace(hour=9, minute=35)
                if now.replace(hour=11, minute=30) < now < now.replace(hour=13):
                    nxt = now.replace(hour=13, minute=0)
                print(f"[{now:%H:%M:%S}] 非交易时段，睡到 {nxt:%m-%d %H:%M}",
                      flush=True)
                time.sleep(max((nxt - now).total_seconds(), 5))
                continue
            # 对齐下一个 5 分钟收口 +7s（等新浪把 bar 备好）
            boundary = (now + timedelta(minutes=5)).replace(
                minute=(now.minute // 5 + 1) * 5 % 60, second=7, microsecond=0)
            if boundary <= now:
                boundary += timedelta(minutes=5)
            if (boundary - now).total_seconds() > a.interval:
                time.sleep(a.interval)
            else:
                time.sleep(max((boundary - now).total_seconds(), 1))
        rnd += 1
        run_round(watch, state, a, rnd)
        sp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        if a.once:
            break


if __name__ == "__main__":
    main()
