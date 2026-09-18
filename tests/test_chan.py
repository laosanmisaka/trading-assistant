"""缠论桥接层（core/chan.py）测试

依赖 czsc —— 未安装时整模块跳过，不阻塞其余测试。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("czsc")

from core import chan                      # noqa: E402
from data.models import KLineData          # noqa: E402


# --------------------------------------------------------------- helpers

def make_klines(n: int, seed: int = 7, shuffle: bool = False, dup: int = 0):
    """构造确定性的 30min K 线序列"""
    dt = pd.date_range("2024-01-01 09:30", periods=n, freq="30min")
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, .5, n))
    op = close + rng.normal(0, .2, n)
    hi = np.maximum(op, close) + rng.uniform(0, .5, n)
    lo = np.minimum(op, close) - rng.uniform(0, .5, n)
    vol = rng.integers(10_000, 1_000_000, n)
    ks = [
        KLineData(code="000001", date=str(dt[i]), open=float(op[i]),
                  high=float(hi[i]), low=float(lo[i]), close=float(close[i]),
                  volume=int(vol[i]), period="30min")
        for i in range(n)
    ]
    if dup:
        ks = ks + ks[:dup]
    if shuffle:
        ks = list(np.random.default_rng(1).permutation(ks))
    return ks


def spec_klines(spec, step_hours: int = 2):
    """按 (open, high, low, close) 规格手工构造 K 线"""
    base = pd.Timestamp("2024-03-01 09:30")
    return [
        KLineData(code="T", date=str(base + pd.Timedelta(hours=step_hours * i)),
                  open=o, high=h, low=l, close=c, volume=1000, period="30min")
        for i, (o, h, l, c) in enumerate(spec)
    ]


class _FakeMark:
    """模拟 czsc 的 Mark 枚举：有 .name，str() 不是 'G'/'D'"""

    def __init__(self, name: str):
        self.name = name

    def __str__(self) -> str:
        return f"<Mark.{self.name}>"


# ------------------------------------------------------- _mark_kind 归一化

class TestMarkKind:
    """czsc 1.0.1 的 FX.mark 是枚举，str() 返回中文。

    初版曾用 `mark == "G"` 判断，结果 890 个分型全被判成底分型。
    """

    def test_enum_name_is_used(self):
        assert chan._mark_kind(_FakeMark("G")) == "top"
        assert chan._mark_kind(_FakeMark("D")) == "bottom"

    def test_enum_str_is_not_g_or_d(self):
        """确保这条测试真的能咬：假枚举的 str 不等于 'G'/'D'"""
        assert str(_FakeMark("G")) != "G"

    def test_plain_string_and_chinese_are_tolerated(self):
        assert chan._mark_kind("G") == "top"
        assert chan._mark_kind("D") == "bottom"
        assert chan._mark_kind("顶分型") == "top"
        assert chan._mark_kind("底分型") == "bottom"

    def test_unknown_mark_raises(self):
        with pytest.raises(ValueError):
            chan._mark_kind(_FakeMark("X"))


# ---------------------------------------------------------- klines_to_df

class TestKlinesToDf:
    def test_has_all_columns_required_by_czsc(self):
        df = chan.klines_to_df(make_klines(10))
        assert list(df.columns) == chan._DF_COLUMNS
        # amount 是 czsc 的硬性要求，缺了直接 ValueError
        assert "amount" in df.columns

    def test_amount_is_volume_times_close(self):
        df = chan.klines_to_df(make_klines(10))
        k = make_klines(10)[3]
        row = df.iloc[3]
        assert row["amount"] == pytest.approx(k.volume * k.close)

    def test_sorted_ascending(self):
        df = chan.klines_to_df(make_klines(50, shuffle=True))
        assert df["dt"].is_monotonic_increasing

    def test_duplicates_removed(self):
        df = chan.klines_to_df(make_klines(100, dup=20))
        assert len(df) == 100
        assert df["dt"].is_unique

    def test_empty_input(self):
        df = chan.klines_to_df([])
        assert len(df) == 0
        assert list(df.columns) == chan._DF_COLUMNS


# ------------------------------------------------------------------ build

class TestBuild:
    def test_insufficient_bars_returns_none(self):
        for n in (0, 1, 2):
            assert chan.build(make_klines(n), "30min") is None

    def test_invalid_period_raises(self):
        with pytest.raises(ValueError):
            chan.build(make_klines(50), "3min")

    def test_both_fractal_kinds_are_detected(self):
        """回归：曾出现「顶分型 0 个、底分型 890 个」"""
        r = chan.build(make_klines(600), "30min")
        fx = chan.fractals(r)
        tops = [f for f in fx if f["kind"] == "top"]
        bottoms = [f for f in fx if f["kind"] == "bottom"]
        assert len(tops) > 0, "顶分型被漏检"
        assert len(bottoms) > 0, "底分型被漏检"
        # 分型应顶底交替出现
        kinds = [f["kind"] for f in fx]
        assert all(a != b for a, b in zip(kinds, kinds[1:])), "分型未交替"

    def test_handcrafted_top_then_bottom(self):
        """先涨后跌再涨：应先出顶分型(103.0) 再出底分型(96.0)"""
        r = chan.build(spec_klines([
            (99.0, 100.0, 98.5, 99.5),
            (99.5, 103.0, 99.0, 102.5),   # 顶
            (102.5, 102.8, 99.5, 100.0),
            (100.0, 100.5, 96.0, 97.0),   # 底
            (97.0, 99.0, 96.8, 98.5),
            (98.5, 101.0, 98.0, 100.5),
        ]), "30min")
        fx = chan.fractals(r)
        assert [f["kind"] for f in fx] == ["top", "bottom"]
        assert fx[0]["price"] == pytest.approx(103.0)
        assert fx[1]["price"] == pytest.approx(96.0)

    def test_shuffled_input_gives_identical_result(self):
        """czsc 自身不排序，桥接层必须保证乱序与有序结果一致"""
        a = chan.build(make_klines(600), "30min")
        b = chan.build(make_klines(600, shuffle=True), "30min")
        assert a.fractal_count == b.fractal_count
        assert a.bi_count == b.bi_count
        assert a.center_count == b.center_count

    def test_max_bi_num_does_not_truncate_history(self):
        """回归：默认 max_bi_num=50 时 2000 根输入只保留 641 根、分型 890→300"""
        r = chan.build(make_klines(2000), "30min")
        assert r.fractal_count > 300, "历史被 max_bi_num 截断"

    def test_index_mapping_covers_all_fractals(self):
        """idx 为 bars_raw 的下标，必须全部可解析（不出现 -1）"""
        r = chan.build(make_klines(600), "30min")
        assert all(f["idx"] >= 0 for f in chan.fractals(r))
        assert all(b["start_idx"] >= 0 and b["end_idx"] >= 0 for b in chan.bis(r))

    def test_index_is_within_bars(self):
        r = chan.build(make_klines(600), "30min")
        n = len(r.bars)
        assert all(0 <= f["idx"] < n for f in chan.fractals(r))


# -------------------------------------------------------------- 分型 / 中枢

class TestFractalsAndCenters:
    def test_filter_by_kind(self):
        r = chan.build(make_klines(600), "30min")
        assert len(chan.fractals(r)) == (len(chan.fractals(r, "top"))
                                         + len(chan.fractals(r, "bottom")))
        assert all(f["kind"] == "top" for f in chan.fractals(r, "top"))
        assert all(f["kind"] == "bottom" for f in chan.fractals(r, "bottom"))

    def test_latest_fractal_is_actually_last(self):
        r = chan.build(make_klines(600), "30min")
        tops = chan.fractals(r, "top")
        assert chan.latest_fractal(r, "top")["dt"] == tops[-1]["dt"]

    def test_latest_fractal_missing_kind(self):
        """K 线过少时某一类分型可能不存在 —— 应返回 None 而不是抛异常"""
        r = chan.build(make_klines(20), "30min")
        if r is not None and not chan.fractals(r, "top"):
            assert chan.latest_fractal(r, "top") is None

    def test_centers_upper_ge_lower(self):
        r = chan.build(make_klines(2000), "30min")
        cs = chan.centers(r)
        assert cs, "未识别出任何中枢"
        for c in cs:
            assert c["high"] >= c["low"], "中枢上沿低于下沿"
            assert c["low"] <= c["mid"] <= c["high"]

    def test_centers_time_ordered(self):
        r = chan.build(make_klines(2000), "30min")
        cs = chan.centers(r)
        assert all(cs[i]["edt"] <= cs[i + 1]["edt"] for i in range(len(cs) - 1))

    def test_latest_center_matches_last(self):
        r = chan.build(make_klines(2000), "30min")
        cs = chan.centers(r)
        assert chan.latest_center(r) == cs[-1]

    def test_bis_direction_normalized(self):
        """direction 是枚举，str() 为「向上/向下」—— 必须归一化为 Up/Down"""
        r = chan.build(make_klines(600), "30min")
        dirs = {b["direction"] for b in chan.bis(r)}
        assert dirs <= {"Up", "Down"}, f"未归一化的方向: {dirs}"


# --------------------------------------------------- 中枢的起止与区间口径
class TestCenterBoundaryRules:
    """把 czsc 1.0.1 的中枢口径钉死（2026-09-18 实测确认，见 chan.centers docstring）

    这几条是**特征化测试**：czsc 从 0.x 跳到 1.0 时 API 面目全非，
    将来升到 1.1 若改了中枢构造，这里会第一时间报警。
    """

    @staticmethod
    def _seq(b):
        return (str(b.sdt)[:16], str(b.edt)[:16])

    def test_boundaries_equal_first_and_last_bi(self):
        """起始 = 第一笔起点（zs.bis[0].sdt）；终止 = 最后一笔终点（zs.bis[-1].edt）"""
        r = chan.build(make_klines(2000), "30min")
        zss = list(r.obj.zs_list)
        assert zss, "未识别出任何中枢"
        for zs in zss:
            rb = list(zs.bis)
            assert str(zs.sdt)[:16] == str(rb[0].sdt)[:16], "中枢起点不是第一笔起点"
            assert str(zs.edt)[:16] == str(rb[-1].edt)[:16], "中枢终点不是最后一笔终点"

    def test_bis_all_overlap_the_center_range(self):
        """zs.bis 的每一笔都与 [zd, zg] 有重叠 —— 这是「延伸」的判据

        对应缠论中心定理一（延伸 ⟺ 任意段 [dn,gn] 与 [ZD,ZG] 有重叠），
        也说明 **离开笔不在 zs.bis 里**。
        """
        r = chan.build(make_klines(2000), "30min")
        for zs in r.obj.zs_list:
            zd, zg = float(zs.zd), float(zs.zg)
            for b in zs.bis:
                assert not (float(b.high) < zd or float(b.low) > zg), (
                    f"延伸笔 [{float(b.low)}, {float(b.high)}] 与中枢 "
                    f"[{zd}, {zg}] 无重叠")

    def test_range_is_first_three_bis_and_extremes_are_all_bis(self):
        """zg/zd 取前三笔；gg/dd 取全部笔"""
        r = chan.build(make_klines(2000), "30min")
        for zs in r.obj.zs_list:
            rb = list(zs.bis)
            hs = [float(b.high) for b in rb]
            ls = [float(b.low) for b in rb]
            assert min(hs[:3]) == pytest.approx(float(zs.zg))
            assert max(ls[:3]) == pytest.approx(float(zs.zd))
            assert max(hs) == pytest.approx(float(zs.gg))
            assert min(ls) == pytest.approx(float(zs.dd))

    def test_leaving_bi_is_the_one_after_zs_bis(self):
        """中枢结束后的第一笔与中枢区间无重叠（= 离开笔）

        数据末尾那个中枢例外：尾部还没走完，czsc 会提前收口，
        所以这里只看非末尾的中枢。
        """
        r = chan.build(make_klines(2000), "30min")
        bl = list(r.obj.bi_list)
        seq = [self._seq(b) for b in bl]
        zss = list(r.obj.zs_list)
        checked = 0
        for zs in zss[:-1]:                      # 排除末中枢（尾部未确认）
            rb = list(zs.bis)
            pos = seq.index(self._seq(rb[-1]))
            nxt = bl[pos + 1]
            zd, zg = float(zs.zd), float(zs.zg)
            assert float(nxt.high) < zd or float(nxt.low) > zg, (
                f"中枢后的第一笔 [{float(nxt.low)}, {float(nxt.high)}] "
                f"仍与 [{zd}, {zg}] 重叠")
            checked += 1
        assert checked >= 1, "样本里只有一个中枢，无法验证离开笔规则"
