# -*- coding: utf-8 -*-
"""多级别行情取数与落盘缓存（baostock 源，前复权）

    python scripts/fetch_min.py --period 30min                  # 默认 50 只池
    python scripts/fetch_min.py --period 5min --pool scripts/pool_random500.txt
    python scripts/fetch_min.py --period 5min --codes sh601899 --start 2020-01-01
    python scripts/fetch_min.py --period 30min --cache-days 3    # 跨日复查吃旧缓存
    python scripts/fetch_min.py --period 5min --limit 5 --no-cache
    python scripts/fetch_min.py --period daily --codes sh601899 --start 2018-01-01

为什么换源
----------
项目原有取数（`core/chan_viz.fetch_klines`）走**新浪** `stock_zh_a_minute`：
接口对每个周期都只返回**最近 1970 根**，所以级别越低、历史越短 ——

| 级别 | 根数 | 覆盖 | 折合交易日 |
| --- | --- | --- | --- |
| 30min | 1970 | — | 约 247 日 |
| 5min | 1970 | — | 约 40 日 |
| 1min | 1970 | — | 约 9 日 |

5 分钟的 40 天只够看方向，**不够算成功率**。baostock 的分钟接口没有这个限制，
但**有下限**：5min 最早只到 **2020-01-02**（2019 及以前一律返回 0 行，实测）。
所以「对齐」日线与 5min 的可行区间就是 2020-01-02 起：

| 级别 | 根数（2020-01-02 起） | 覆盖 |
| --- | --- | --- |
| 5min | 约 78,240 | 6.7 年 |
| 15min | 约 26,080 | 6.7 年 |
| 30min | 约 13,040 | 6.7 年 |
| daily | 一次查完即可 | 更长（1990 起） |
| 1min | **不支持**（官方只到 5min） | — |

⚠️ 东财 / 腾讯的分钟接口在本机被**环境级代理**拦死（`ProxyError`，关沙箱也一样），
不是沙箱白名单问题。baostock 走自己的 TCP 协议，不受影响。
1min 仍只能走新浪（约 9 天），本脚本对 `--period 1min` 自动切新浪并打印警告。

⚠️ 必须分段取数
----------------
baostock 对**单次查询的数据量**有上限（与并发无关）：实测 1,944 行 ✓、3,168 行 ✓、
13,040 行 ✗、23,281 行 ✗ —— 超限时表现为连接被掐（`WinError 10054`）或传输中途截断
（曾出现 10,001 行的静默截断）。所以本脚本按级别分 `CHUNK_DAYS` 逐段取、
**重试粒度降到单段**，最后合并去重。段长保证每段 ≤ 约 3,200 行。

⚠️ 缓存会过期
--------------
baostock 的**前复权**以最新价为基准，历史价随每次分红除权整体平移。所以缓存
只在「同一天内」可信；跨日重跑请用 `--cache-days 0` 重取（默认），只有确认
中间没有新交易日（比如周末复查）才用 `--cache-days 3` 吃旧缓存。

缓存格式与 `scripts/scan_pool.py` 产出的一致（`dt,open,high,low,close,volume`），
直接喂 `core.chan.build()` 即可。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import chan_viz                              # noqa: E402

DEFAULT_CACHE = ROOT / "outputs" / "cache_min"

#: 级别 → baostock 的 frequency 参数（``d`` = 日线）
BAOSTOCK_FREQ = {"daily": "d", "5min": "5", "15min": "15", "30min": "30", "60min": "60"}

#: 级别 → 分段天数（保证单段 ≤ 约 3,200 行，绕开 baostock 单次查询上限）；0 = 不分段
CHUNK_DAYS = {"daily": 0, "5min": 90, "15min": 180, "30min": 365, "60min": 730, "1min": 0}

#: 级别 → 默认起始日期（5min 的物理下限是 2020-01-02，取更早只是白跑）
DEFAULT_START = {
    "1min": "2026-09-01",      # 新浪源，实际只有约 9 天
    "5min": "2020-01-01",
    "15min": "2020-01-01",
    "30min": "2020-01-01",
    "60min": "2020-01-01",
    "daily": "2006-01-01",
}

#: 取数字段（日线无 time 列）
FIELDS_MIN = "date,time,open,high,low,close,volume"
FIELDS_DAY = "date,open,high,low,close,volume"


# ======================================================================
# 标的池
# ======================================================================

def load_pool(path: Path) -> list[tuple[str, str]]:
    """读标的池文件 → ``[(代码, 名称), ...]``（格式与 `scan_pool.load_pool` 一致）"""
    pool: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        code = chan_viz.normalize_code(parts[0])
        if code in seen:
            continue
        seen.add(code)
        pool.append((code, parts[1] if len(parts) > 1 else ""))
    return pool


def to_baostock_code(sym: str) -> str | None:
    """`sh600519` → `sh.600519`；北交所（bj）返回 None（baostock 无此市场）"""
    sym = chan_viz.normalize_code(sym)
    if sym.startswith("bj"):
        return None
    return f"{sym[:2]}.{sym[2:]}"


# ======================================================================
# baostock 会话（连接不稳定，必须带重试）
# ======================================================================

class BaostockSession:
    """baostock 登录会话 —— 实测 ``login()`` 会随机失败（多服务器地址择优）

    第 1 次探测成功、第 2 次探测失败，所以任何一次 login 都要当可能失败处理。
    """

    def __init__(self, attempts: int = 5, sleep: float = 6.0):
        self.attempts = attempts
        self.sleep = sleep
        self.bs = None
        self.logged_in = False

    def __enter__(self) -> "BaostockSession":
        import baostock as bs
        self.bs = bs
        last = ""
        for i in range(self.attempts):
            try:
                lg = bs.login()
                if lg.error_code == "0":
                    self.logged_in = True
                    return self
                last = lg.error_msg
            except Exception as exc:                    # 网络层异常也算失败
                last = f"{type(exc).__name__}: {exc}"
            if i < self.attempts - 1:
                time.sleep(self.sleep)
        raise RuntimeError(f"baostock 登录失败（重试 {self.attempts} 次）：{last}")

    def __exit__(self, *exc) -> None:
        if self.logged_in:
            try:
                self.bs.logout()
            except Exception:
                pass
        return None

    def relogin(self) -> bool:
        """强制重登 —— 实测 baostock 会在**同一个会话里约 13 次查询后**失效，
        之后所有查询都失败。段级重试若只是原地重试，永远救不回来，必须先重登。
        """
        try:
            self.bs.logout()
        except Exception:
            pass
        self.logged_in = False
        for i in range(3):
            try:
                lg = self.bs.login()
                if lg.error_code == "0":
                    self.logged_in = True
                    return True
            except Exception:
                pass
            time.sleep(3.0)
        return False


# ======================================================================
# 取数
# ======================================================================

def _split_chunks(start: str, end: str, chunk_days: int) -> list[tuple[str, str]]:
    """把 ``start~end`` 切成每段 ``chunk_days`` 天（``chunk_days<=0`` 则不分段）"""
    if chunk_days <= 0:
        return [(start, end)]
    out: list[tuple[str, str]] = []
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    step = pd.Timedelta(days=chunk_days - 1)
    while s <= e:
        seg_end = min(s + step, e)
        out.append((s.strftime("%Y-%m-%d"), seg_end.strftime("%Y-%m-%d")))
        s = seg_end + pd.Timedelta(days=1)
    return out


def _fetch_baostock_segment(session: BaostockSession, code: str, period: str,
                            start: str, end: str) -> pd.DataFrame:
    """单段查询 → DataFrame（列同缓存格式）

    ``adjustflag="2"`` = 前复权，与项目原来用的新浪 ``adjust="qfq"`` 同口径。

    ⚠️ baostock 的 ``time`` 字段是 17 位 ``YYYYMMDDHHMMSSsss``，且标记的是
    bar 的**结束时刻**（10:00 那根 = 09:30~10:00），与新浪 ``stock_zh_a_minute``
    一致 —— 两级数据的时间轴可直接比较。
    查询成功但区间内无交易日时返回**空 DataFrame**（不是异常）。
    """
    if period == "daily":
        rs = session.bs.query_history_k_data_plus(
            code, FIELDS_DAY, start_date=start, end_date=end,
            frequency="d", adjustflag="2")
        if rs.error_code != "0":
            raise RuntimeError(f"baostock error {rs.error_code}: {rs.error_msg}")
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return pd.DataFrame(columns=["dt", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
        df["dt"] = df["date"]
    else:
        rs = session.bs.query_history_k_data_plus(
            code, FIELDS_MIN, start_date=start, end_date=end,
            frequency=BAOSTOCK_FREQ[period], adjustflag="2")
        if rs.error_code != "0":
            raise RuntimeError(f"baostock error {rs.error_code}: {rs.error_msg}")
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return pd.DataFrame(columns=["dt", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame(rows, columns=["date", "time", "open", "high", "low",
                                         "close", "volume"])
        df["dt"] = df["date"] + " " + df["time"].str[8:10] + ":" + df["time"].str[10:12] + ":00"

    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0)      # 停牌 bar 无成交量，填 0 而非留 NaN
    # 停牌日 baostock 会返回 open=high=low=close=0 的占位 bar（不含成交信息），
    # 留着会让下游统计拿 0 当分母（除零）。整根丢弃。
    zero = (df[["open", "high", "low", "close"]] == 0).all(axis=1)
    if zero.any():
        df = df[~zero]
    return df[["dt", "open", "high", "low", "close", "volume"]]


def fetch_baostock(session: BaostockSession, sym: str, period: str,
                   start: str, end: str, *, chunk_days: int | None = None,
                   seg_attempts: int = 3, seg_sleep: float = 1.0,
                   ) -> tuple[pd.DataFrame, dict]:
    """分段取一只 → ``(df, 统计)``；全部段失败才抛异常

    ``统计`` 含 ``段数 / 成功段 / 失败段 / 覆盖``，用于暴露静默截断。
    """
    code = to_baostock_code(sym)
    if code is None:
        raise RuntimeError(f"{sym}：baostock 不支持北交所")

    ck = CHUNK_DAYS.get(period, 0) if chunk_days is None else chunk_days
    segs = _split_chunks(start, end, ck)

    frames: list[pd.DataFrame] = []
    failed: list[tuple[str, str]] = []
    n_relogin = 0
    for s, e in segs:
        seg: pd.DataFrame | None = None
        for i in range(seg_attempts):
            if i > 0:
                time.sleep(2.0 * i)
            try:
                seg = _fetch_baostock_segment(session, code, period, s, e)
                break
            except Exception:
                # 连续两次失败基本是会话失效（实测约 13 次查询后），先重登再试
                if i >= 1 and session.relogin():
                    n_relogin += 1
        if seg is None:
            failed.append((s, e))
        elif not seg.empty:
            frames.append(seg)
        if seg_sleep > 0 and len(segs) > 1:
            time.sleep(seg_sleep)

    if not frames:
        raise RuntimeError(f"{sym} 未取到 {period} 行情（{start}~{end}，"
                           f"{len(segs)} 段全失败）")
    df = (pd.concat(frames, ignore_index=True)
            .sort_values("dt", kind="stable")
            .drop_duplicates("dt", keep="last")
            .reset_index(drop=True))
    stat = {"段数": len(segs), "成功段": len(segs) - len(failed), "失败段": len(failed),
            "重登": n_relogin,
            "覆盖": f"{df['dt'].iloc[0][:10]} ~ {df['dt'].iloc[-1][:10]}"}
    if failed:
        stat["失败区间"] = ", ".join(f"{s}~{e}" for s, e in failed[:3])
    return df, stat


def fetch_sina(sym: str, period: str) -> pd.DataFrame:
    """新浪分钟 K 线（1min 的唯一可用源；深度见模块 docstring）"""
    klines = chan_viz.fetch_klines(sym, period)
    return pd.DataFrame({
        "dt": [k.date for k in klines],
        "open": [k.open for k in klines],
        "high": [k.high for k in klines],
        "low": [k.low for k in klines],
        "close": [k.close for k in klines],
        "volume": [k.volume for k in klines],
    })


def cache_path(cache_dir: Path, sym: str, period: str) -> Path:
    return Path(cache_dir) / f"{period}_{sym}.csv"


def cache_valid(path: Path, cache_days: int, *,
                start: str = "", end: str = "", slack_days: int = 15) -> bool:
    """缓存判据：存在、非空、mtime 不超过 ``cache_days`` 天，且**覆盖请求区间**

    ⚠️ 只看 mtime 会出大错：先取「近 1 个月」再取「近 5 年」，第二次会命中
    第一份短缓存，样本被静默截短。所以必须再校验区间覆盖 —— 用 ``slack_days``
    容忍起止落点不是交易日（以及停牌）造成的自然偏差。
    """
    if not path.exists() or path.stat().st_size == 0:
        return False
    age = (date.today() - datetime.fromtimestamp(path.stat().st_mtime).date()).days
    if age > cache_days:
        return False
    if not (start or end):
        return True
    try:
        df = pd.read_csv(path, usecols=["dt"])
    except Exception:
        return False
    if df.empty:
        return False
    lo = str(df["dt"].iloc[0])[:10]
    hi = str(df["dt"].iloc[-1])[:10]
    slack = pd.Timedelta(days=slack_days)
    if start and pd.Timestamp(lo) > pd.Timestamp(start) + slack:
        return False
    if end and pd.Timestamp(hi) < pd.Timestamp(end) - slack:
        return False
    return True


# ======================================================================
# 主流程
# ======================================================================

def fetch_one(sym: str, period: str, *, cache_dir: Path, start: str, end: str,
              use_cache: bool, cache_days: int, attempts: int, sleep: float,
              chunk_days: int | None,
              session: BaostockSession | None) -> tuple[pd.DataFrame, bool, dict]:
    """取一只（或读缓存）→ ``(df, 是否走缓存, 统计)``"""
    f = cache_path(cache_dir, sym, period)
    if use_cache and cache_valid(f, cache_days, start=start, end=end):
        df = pd.read_csv(f)
        return df, True, {"段数": 0, "成功段": 0, "失败段": 0,
                          "覆盖": f"{str(df['dt'].iloc[0])[:10]} ~ {str(df['dt'].iloc[-1])[:10]}"}

    last: Exception | None = None
    for i in range(attempts):
        try:
            if period == "1min":
                df, stat = fetch_sina(sym, period), {"段数": 1, "成功段": 1,
                                                     "失败段": 0, "覆盖": ""}
            else:
                assert session is not None
                df, stat = fetch_baostock(session, sym, period, start, end,
                                          chunk_days=chunk_days)
            break
        except Exception as exc:
            last = exc
            if i < attempts - 1:
                time.sleep(3.0 * (i + 1))
    else:
        raise RuntimeError(f"取数失败（重试 {attempts} 次，最后一次：{last}）") from last

    if not stat.get("覆盖") and len(df):
        stat["覆盖"] = f"{str(df['dt'].iloc[0])[:10]} ~ {str(df['dt'].iloc[-1])[:10]}"
    cache_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(f, index=False)
    return df, False, stat


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="多级别行情取数与落盘缓存（baostock 源，分段取数）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--period", default="30min",
                   choices=["1min", "5min", "15min", "30min", "60min", "daily"])
    p.add_argument("--pool", type=Path, default=ROOT / "scripts" / "pool_liquid50.txt")
    p.add_argument("--codes", default="", help="只取指定代码（逗号分隔，覆盖 --pool）")
    p.add_argument("--start", default="", help="起始日期（默认按级别取 DEFAULT_START）")
    p.add_argument("--end", default="", help="结束日期（默认今天）")
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    p.add_argument("--cache-days", type=int, default=0,
                   help="缓存容忍天数（0=只认今天取的；跨日复查可用 3）")
    p.add_argument("--no-cache", action="store_true", help="忽略缓存，全部重取")
    p.add_argument("--limit", type=int, default=0, help="只取前 N 只")
    p.add_argument("--attempts", type=int, default=3, help="单只（全段失败时）重试次数")
    p.add_argument("--sleep", type=float, default=0.3, help="每只成功取数后的间隔秒数")
    p.add_argument("--chunk-days", type=int, default=-1,
                   help="分段天数（-1=按级别取 CHUNK_DAYS；0=不分段）")
    p.add_argument("--seg-attempts", type=int, default=3, help="单段失败重试次数")
    p.add_argument("--seg-sleep", type=float, default=1.0, help="段间间隔秒数")
    a = p.parse_args(argv)

    period = a.period
    start = a.start or DEFAULT_START[period]
    end = a.end or date.today().strftime("%Y-%m-%d")
    chunk_days = None if a.chunk_days < 0 else a.chunk_days

    if a.codes:
        pool = [(chan_viz.normalize_code(c), "") for c in
                (x.strip() for x in a.codes.split(",")) if c]
    else:
        pool = load_pool(a.pool)
    if a.limit:
        pool = pool[: a.limit]
    if not pool:
        print("标的池为空", file=sys.stderr)
        return 2

    if period == "1min":
        print("⚠️  1min 只能走**新浪**源（baostock 不支持），历史仅约 9 个交易日 —— "
              "够做实时触发验证，不够做统计。")

    ck = CHUNK_DAYS.get(period, 0) if chunk_days is None else chunk_days
    segs = _split_chunks(start, end, ck)
    print(f"取数：{len(pool)} 只 | {period} | {start} ~ {end} | "
          f"{ck if ck else '不分'}段×{len(segs)} | 缓存 → {a.cache_dir}")

    n_cache = n_net = n_fail = 0
    rows: list[dict] = []
    started = time.time()

    ctx = BaostockSession()
    with ctx:
        for i, (sym, name) in enumerate(pool, 1):
            t0 = time.time()
            try:
                df, cached, stat = fetch_one(sym, period, cache_dir=a.cache_dir,
                                             start=start, end=end,
                                             use_cache=not a.no_cache,
                                             cache_days=a.cache_days,
                                             attempts=a.attempts, sleep=a.sleep,
                                             chunk_days=chunk_days,
                                             session=ctx)
                n_cache += cached
                n_net += not cached
                segtxt = "" if cached else f" 段{stat['成功段']}/{stat['段数']}"
                if not cached and stat.get("重登"):
                    segtxt += f" 重登{stat['重登']}次"
                rows.append({"代码": sym, "名称": name, "行数": len(df),
                             "覆盖": stat.get("覆盖", ""),
                             "来源": "缓存" if cached else "网络"})
                print(f"[{i}/{len(pool)}] {sym} {name}: {len(df)} 行 "
                      f"({stat.get('覆盖', '')}){segtxt} "
                      f"{'缓存' if cached else '网络'} {time.time() - t0:.1f}s",
                      flush=True)
            except Exception as exc:
                n_fail += 1
                rows.append({"代码": sym, "名称": name, "行数": 0,
                             "错误": str(exc)[:120]})
                print(f"[{i}/{len(pool)}] {sym} {name}: ✗ {exc}", flush=True)
                continue

            if not cached and a.sleep > 0:
                time.sleep(a.sleep)

    ok = [r for r in rows if not r.get("错误")]
    total = sum(r["行数"] for r in ok)
    print("\n" + "=" * 56)
    print(f"完成：成功 {len(ok)} 只（缓存 {n_cache} / 网络 {n_net}），失败 {n_fail} 只，"
          f"共 {total:,} 行，耗时 {time.time() - started:.0f}s")
    if n_fail:
        print("失败清单：" + ", ".join(r["代码"] for r in rows if r.get("错误")))
    print(f"缓存目录：{a.cache_dir}")
    return 0 if not n_fail else 1


if __name__ == "__main__":
    sys.exit(main())
