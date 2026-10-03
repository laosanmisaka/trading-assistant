# -*- coding: utf-8 -*-
"""盘后例程（`scripts/daily_routine.py`）的纯逻辑测试

不联网：只测增量合并 `merge_incremental` —— 这是整条链路里唯一
「写错会静默污染缓存」的环节：
- 前复权平移漏判 ⇒ 新旧两套复权基准拼在一份缓存里，中枢/幅度全算错；
- 停牌误判漂移 ⇒ 每次除权误报都触发全量重取（5min 一只约 27 段），
  盘后例程从分钟级变小时级。
取数（baostock）与计划任务注册不在本文件覆盖范围内。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "daily_routine.py"


@pytest.fixture(scope="module")
def dr():
    """按路径加载脚本模块（scripts/ 不是包）"""
    spec = importlib.util.spec_from_file_location("daily_routine", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["daily_routine"] = mod
    spec.loader.exec_module(mod)
    return mod


def _df(rows: list[tuple[str, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        {"dt": [r[0] for r in rows], "open": [r[1] for r in rows],
         "high": [r[1] for r in rows], "low": [r[1] for r in rows],
         "close": [r[1] for r in rows], "volume": [1] * len(rows)})


# ----------------------------------------------------------------------
# merge_incremental
# ----------------------------------------------------------------------

def test_append_new_bars(dr):
    old = _df([("2026-09-25", 10.0), ("2026-09-28", 10.5)])
    new = _df([("2026-09-28", 10.5), ("2026-09-29", 11.0),
               ("2026-09-30", 11.2)])
    merged, status = dr.merge_incremental(old, new)
    assert status == "appended"
    assert list(merged["dt"]) == ["2026-09-25", "2026-09-28",
                                  "2026-09-29", "2026-09-30"]
    assert list(merged["close"]) == [10.0, 10.5, 11.0, 11.2]


def test_overlap_dedupes_keep_single_row(dr):
    """重叠日只保留一行（重叠区 close 不一致按设计即判 shifted，
    不存在「以新修正」的口径，所以这里只验证不重复、不丢行）"""
    old = _df([("2026-09-29", 11.0), ("2026-09-30", 11.2)])
    new = _df([("2026-09-30", 11.2), ("2026-10-09", 12.0)])
    merged, status = dr.merge_incremental(old, new)
    assert status == "appended"
    assert list(merged["dt"]) == ["2026-09-29", "2026-09-30", "2026-10-09"]
    assert merged["dt"].is_unique


def test_no_new_data_when_empty(dr):
    old = _df([("2026-09-29", 11.0), ("2026-09-30", 11.2)])
    new = _df([])
    merged, status = dr.merge_incremental(old, new)
    assert status == "no_new"
    assert len(merged) == 2


def test_no_new_when_nothing_beyond_tail(dr):
    """非交易日重跑：取回的全是重叠区，没有新行 ⇒ 不重写缓存"""
    old = _df([("2026-09-29", 11.0), ("2026-09-30", 11.2)])
    new = _df([("2026-09-29", 11.0), ("2026-09-30", 11.2)])
    merged, status = dr.merge_incremental(old, new)
    assert status == "no_new"


def test_shift_detected_on_adjustment(dr):
    """分红除权 ⇒ 前复权历史整体平移，重叠区收盘价对不上 ⇒ shifted"""
    old = _df([("2026-09-28", 20.0), ("2026-09-29", 20.5),
               ("2026-09-30", 21.0)])
    # 除权后历史价按比例缩小（约 ×0.95），同日 close 全部漂移
    new = _df([("2026-09-29", 19.475), ("2026-09-30", 19.95),
               ("2026-10-09", 20.4)])
    merged, status = dr.merge_incremental(old, new)
    assert status == "shifted"
    assert len(merged) == 3  # old 原样返回，由调用方全量重取


def test_tiny_float_noise_not_shifted(dr):
    """同源同基准的浮点尾差不能误判成漂移"""
    old = _df([("2026-09-29", 11.0), ("2026-09-30", 11.2)])
    new = _df([("2026-09-29", 11.0000001), ("2026-09-30", 11.2000001),
               ("2026-10-09", 11.5)])
    merged, status = dr.merge_incremental(old, new)
    assert status == "appended"


def test_no_overlap_requires_verified_refetch(dr):
    """停牌还是分段缺失无法单凭无重叠判断，禁止拼接未知复权基准。"""
    old = _df([("2026-09-10", 8.0), ("2026-09-11", 8.1)])
    new = _df([("2026-09-29", 8.1), ("2026-09-30", 8.3)])
    merged, status = dr.merge_incremental(old, new)
    assert status == "unverified"
    pd.testing.assert_frame_equal(merged, old)


def test_5min_datetime_strings_compare_chronologically(dr):
    """5min 的 dt 是 'YYYY-MM-DD HH:MM:SS'，字典序即时间序（合并依赖这一点）"""
    old = _df([("2026-09-30 14:55:00", 11.0), ("2026-09-30 15:00:00", 11.1)])
    new = _df([("2026-09-30 15:00:00", 11.1), ("2026-10-09 09:35:00", 11.3)])
    merged, status = dr.merge_incremental(old, new)
    assert status == "appended"
    assert list(merged["dt"]) == ["2026-09-30 14:55:00", "2026-09-30 15:00:00",
                                  "2026-10-09 09:35:00"]


# ----------------------------------------------------------------------
# sync_watchlist_group（用 conftest 的 temp_db，不碰真实库）
# ----------------------------------------------------------------------

def _watch(*items) -> dict:
    return {"items": [dict(code=c, name=n) for c, n in items]}


def test_sync_creates_group_and_adds(dr, temp_db):
    from data.database import get_all_groups, get_stocks_by_group
    st = dr.sync_watchlist_group(_watch(("sh600519", "贵州茅台"),
                                        ("sz000001", "平安银行")))
    assert st == {"added": 2, "removed": 0, "total": 2}
    g = next(g for g in get_all_groups() if g.name == dr.WATCH_GROUP_NAME)
    stocks = {s.code: s.name for s in get_stocks_by_group(g.id)}
    # 入库存 6 位数字代码（与 GUI 手动添加一致）
    assert stocks == {"600519": "贵州茅台", "000001": "平安银行"}


def test_sync_removes_closed_windows(dr, temp_db):
    from data.database import get_all_groups, get_stocks_by_group
    dr.sync_watchlist_group(_watch(("sh600519", "贵州茅台"),
                                   ("sz000001", "平安银行")))
    st = dr.sync_watchlist_group(_watch(("sz000001", "平安银行"),
                                        ("sh601899", "紫金矿业")))
    assert st == {"added": 1, "removed": 1, "total": 2}
    g = next(g for g in get_all_groups() if g.name == dr.WATCH_GROUP_NAME)
    assert {s.code for s in get_stocks_by_group(g.id)} == {"000001", "601899"}


def test_sync_does_not_touch_other_groups(dr, temp_db):
    from data.database import get_all_groups, add_stock, get_stocks_by_group
    manual = next(g for g in get_all_groups() if g.name == "跟踪中")
    add_stock("600519", "贵州茅台", manual.id)   # 用户手动跟踪的同代码票
    st = dr.sync_watchlist_group(_watch(("sh600519", "贵州茅台")))
    assert st["added"] == 1                       # 自动分组照加
    assert {s.code for s in get_stocks_by_group(manual.id)} == {"600519"}
    # 下一轮 watchlist 空了：只清自动分组，手动分组原样保留
    dr.sync_watchlist_group(_watch())
    assert {s.code for s in get_stocks_by_group(manual.id)} == {"600519"}


def test_sync_empty_watchlist_clears_group(dr, temp_db):
    from data.database import get_all_groups, get_stocks_by_group
    dr.sync_watchlist_group(_watch(("sh600519", "贵州茅台")))
    st = dr.sync_watchlist_group(_watch())
    assert st == {"added": 0, "removed": 1, "total": 0}
    g = next(g for g in get_all_groups() if g.name == dr.WATCH_GROUP_NAME)
    assert get_stocks_by_group(g.id) == []


def test_partial_fetch_preserves_verified_cache(dr, tmp_path, monkeypatch):
    from data.cache_files import publish, metadata
    path = tmp_path / "daily_sh600000.csv"
    old = _df([("2026-09-30", 10)])
    publish(path, old, source="baostock", adjustment="qfq", start="2016-01-01", end="2026-09-30")
    before = path.read_bytes()
    monkeypatch.setattr(dr, "_fetch_recent", lambda *a: _df([("2026-10-09", 9)]))
    monkeypatch.setattr(dr.fetch_min, "fetch_baostock", lambda *a: (_df([("2026-10-09", 9)]), {"失败段": 1}))
    with pytest.raises(RuntimeError, match="不完整"):
        dr.update_one(None, "sh600000", "daily", tmp_path)
    assert path.read_bytes() == before and metadata(path)
    path.write_text(path.read_text() + "2026-10-10,1,1,1,1,1\n")
    assert metadata(path) is None


def test_scan_arguments_and_failure_stop_sync(dr, tmp_path, monkeypatch):
    from types import SimpleNamespace
    commands = []
    args = SimpleNamespace(pool=tmp_path / "pool.txt", cache_dir=tmp_path / "custom", limit=2,
                           watchlist=tmp_path / "new.json", skip_scan=False, skip_sync=False,
                           skip_5min=False, sleep=0)
    class Session:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(dr.fetch_min, "BaostockSession", Session)
    monkeypatch.setattr(dr.fetch_min, "load_pool", lambda *a: [("sh600000", "")])
    monkeypatch.setattr(dr, "update_pool", lambda *a, **k: {"full": 1})
    monkeypatch.setattr(dr.subprocess, "run", lambda cmd, **kw: (commands.append(cmd),
                        SimpleNamespace(returncode=1, stdout="", stderr="failed"))[1])
    monkeypatch.setattr(dr, "sync_watchlist_group", lambda *a: pytest.fail("stale watchlist used"))
    with pytest.raises(RuntimeError, match="扫描失败"):
        dr._run_daily(args, lambda *a: None)
    command = commands[0]
    assert command[command.index("--cache-dir") + 1] == str(args.cache_dir)
    assert command[command.index("--out") + 1] == str(args.watchlist)
    assert command[command.index("--limit") + 1] == "2"
