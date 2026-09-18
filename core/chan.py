"""缠论计算桥接层 — 基于 czsc（缠中说禅技术分析工具）

本模块把项目的 `KLineData` 适配到 czsc，并统一暴露「分型 / 笔 / 中枢」，
另提供日线合成（`resample_daily` / `df_to_klines`）供策略与可视化共用。

--------------------------------------------------------------------
czsc 1.0.1 实测约束（改这个文件之前先读完）
--------------------------------------------------------------------
1. **`amount` 是必需列**。`format_standard_kline` 要求 DataFrame 含
   `symbol / dt / open / close / high / low / vol / amount` 八列，
   少任何一列直接抛 `ValueError`。项目的 `KLineData` 没有 amount，
   本层用 `volume * close` 近似。（列名顺序不敏感，已实测。）

2. **czsc 自身不排序，也不校验顺序**。乱序输入不会报错，但会算出
   错误结果 —— 实测 200 根 K 线打乱后，分型从 95 个降到 3 个。
   本层因此强制按 `dt` 升序并去重。

3. **`max_bi_num` 默认 50 会静默截断历史**。实测 2000 根输入：
   默认值时只保留 641 根、分型 890 → 300、中枢 26 → 7；
   调到 200 才是全量。本层按输入长度自动放大，不暴露该参数。
   注意 `bars_raw` 的保留根数**不等于**输入根数，截断规则未公开，
   因此不要假定 `ChanResult.bars` 与输入等长。

4. **1.0.1 没有线段**。只有 `fx_list`（分型）、`bi_list`（笔）、
   `zs_list`（中枢）、`ubi`（未完成笔）。网上流传的
   `bars_raw→bars_ubi→fx_list→bi_list→xd_list→zs_list` 六段流水线
   是旧版文档，`xd_list` / `XD` 在 1.0.1 中不存在。

5. **分型只给时间，不给数组下标**。`FX` 的字段是
   `dt / fx / mark / high / low / elements`，没有 index。
   本层负责建立 `dt → 计算序下标` 的映射供调用方使用。

6. **`FX.mark` 是枚举，不是字符串**。实测为 `Mark.G`（顶）/ `Mark.D`（底），
   而 `str(mark)` 返回中文「顶分型」/「底分型」—— 用 `mark == "G"`
   判断会恒为 False，**把所有分型都识别成底分型**（本层初版即踩此坑：
   890 个分型全被标成 bottom，顶分型数为 0）。正确读法是 `mark.name`。
   同理 `BI.direction` 是 `Direction` 枚举，`.name` 取 "Up" / "Down"。

7. **拒绝 NaN OHLCV，而盘中取数恰好会带回一根 NaN 占位 bar**。
   1.0.1 的 `BarGenerator` 在聚合前校验，遇 NaN 直接抛
   `ValueError: update_signals 失败 (dt=...) : bar.open = NaN`。
   而新浪 `stock_zh_a_minute` 在**盘中**会多返回一根「当日第一根 bar」
   的占位行：OHLC 全 NaN、volume/amount 有值（2026-09-18 10:00 那根
   实测如此）。症状是**同一个脚本盘后跑得通、盘中直接崩**。
   本层因此在 `klines_to_df` 里丢掉 OHLC 缺失的行，调用方不必自己清。
--------------------------------------------------------------------
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from data.models import KLineData
from utils.logger import get_logger

logger = get_logger(__name__)

# 项目 period 字符串 → czsc Freq 成员名
_FREQ_NAMES = {
    "1min": "F1",
    "5min": "F5",
    "15min": "F15",
    "30min": "F30",
    "60min": "F60",
    "daily": "D",
    "weekly": "W",
    "monthly": "M",
}

# czsc 1.0.1 的 FX.mark 是 Mark 枚举（Mark.G / Mark.D），不是字符串，
# str() 得到的是中文。这里同时兼容「枚举名」与「旧版字符串」两种形态。
_MARK_TOP_NAMES = ("G", "顶分型", "顶")
_MARK_BOTTOM_NAMES = ("D", "底分型", "底")

_DF_COLUMNS = ["symbol", "dt", "open", "close", "high", "low", "vol", "amount"]


def _mark_kind(mark) -> str:
    """把 czsc 的分型标记归一化为 'top' / 'bottom'

    czsc 1.0.1 中 mark 为 Mark 枚举，str() 返回「顶分型」「底分型」，
    直接与 "G" 比较会恒为 False，从而把所有分型判成底分型。
    """
    name = getattr(mark, "name", None)
    if name in _MARK_TOP_NAMES:
        return "top"
    if name in _MARK_BOTTOM_NAMES:
        return "bottom"
    text = str(mark)
    if text in _MARK_TOP_NAMES:
        return "top"
    if text in _MARK_BOTTOM_NAMES:
        return "bottom"
    raise ValueError(f"无法识别的分型标记: {mark!r}")


def _direction_name(direction) -> str:
    """把 czsc 的笔方向归一化为 'Up' / 'Down'"""
    return getattr(direction, "name", None) or str(direction)


def _load_czsc():
    """延迟导入 czsc —— 未安装时只有缠论功能不可用，不影响项目其余部分。"""
    try:
        import czsc
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise ImportError(
            "缠论计算需要 czsc：pip install czsc（见 requirements.txt）"
        ) from exc
    return czsc


# 公开别名：其它模块（如 core/chan_strategy.py）需要自行加载 czsc 时用这个
load_czsc = _load_czsc


def _freq_of(period: str):
    czsc = _load_czsc()
    name = _FREQ_NAMES.get(period)
    if name is None:
        raise ValueError(
            f"不支持的周期 {period!r}，可选：{sorted(_FREQ_NAMES)}")
    return getattr(czsc.Freq, name)


def klines_to_df(klines: Iterable[KLineData]):
    """KLineData 序列 → czsc 要求的 DataFrame（已按 dt 升序去重）

    czsc 不排序也不校验，乱序会静默算错，所以排序去重在这里做，不能省。
    """
    import pandas as pd

    rows = []
    for k in klines:
        vol = float(k.volume)
        close = float(k.close)
        rows.append({
            "symbol": str(k.code),
            "dt": k.date,
            "open": float(k.open),
            "close": close,
            "high": float(k.high),
            "low": float(k.low),
            "vol": vol,
            # czsc 要求 amount 列，项目数据模型没有，用 量×收 近似
            "amount": vol * close,
        })

    if not rows:
        return pd.DataFrame(columns=_DF_COLUMNS)

    df = pd.DataFrame(rows)
    df["dt"] = pd.to_datetime(df["dt"], errors="coerce")
    df = df.dropna(subset=["dt"])
    # 新浪 / 东财的分钟接口在**盘中**会多给一根「当日第一根 bar」的占位行：
    # OHLC 全 NaN、volume/amount 有值（2026-09-18 10:00 那根实测如此）。
    # czsc 1.0.1 的 BarGenerator 明确拒绝 NaN OHLCV，会抛
    #   ValueError: update_signals 失败 ... bar.open = NaN
    # 而且未走完的 bar 参与信号计算本来就会误导，所以收口在这里丢掉。
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = (df.sort_values("dt", kind="stable")
            .drop_duplicates(subset=["dt"], keep="last")
            .reset_index(drop=True))
    return df[_DF_COLUMNS]


def resample_daily(df):
    """分钟 K 线 DataFrame → 日线 DataFrame（dt/open/high/low/close/vol）

    按自然日 `resample("1D")` 后 `dropna`，因此非交易日不产出行 —— A 股
    非交易日无成交，与交易所日历等价。

    2026-09-18 从 `core/chan_viz.py` 上移到这里：几何买卖点策略需要
    「日线 czsc 对象」（算日线一买/二买），可视化层也要，两处共用一份
    实现，避免各自维护。
    """
    return (df.set_index("dt")
              .resample("1D")
              .agg(open=("open", "first"), high=("high", "max"),
                   low=("low", "min"), close=("close", "last"),
                   vol=("vol", "sum"))
              .dropna()
              .reset_index())


def df_to_klines(df, period: str = "30min", code: str = "") -> list[KLineData]:
    """DataFrame → KLineData 序列（`klines_to_df` 的逆向）

    供「拿合成出来的日线 DataFrame 再建一个 czsc 对象」这类场景使用
    （2026-09-18 从 `core/chan_viz.py::_to_klines` 上移）。
    行序原样保留 —— `build()` 内部会按 dt 排序去重。
    """
    return [KLineData(code=code or "UNKNOWN", date=str(r.dt), open=float(r.open),
                      high=float(r.high), low=float(r.low), close=float(r.close),
                      volume=int(r.vol), period=period)
            for r in df.itertuples()]


@dataclass
class ChanResult:
    """一次缠论计算的结果

    obj:      czsc.CZSC 实例
    period:   计算周期（如 "30min"）
    bars:     实际参与计算的 K 线（见模块 docstring 第 3 条，可能少于输入）
    index_of: {Timestamp: 在 bars 中的下标}
    """

    obj: object
    period: str
    bars: list = field(default_factory=list)
    index_of: dict = field(default_factory=dict)

    @property
    def fractal_count(self) -> int:
        return len(self.obj.fx_list)

    @property
    def bi_count(self) -> int:
        return len(self.obj.bi_list)

    @property
    def center_count(self) -> int:
        return len(self.obj.zs_list)


def build(klines: Iterable[KLineData], period: str = "30min") -> Optional[ChanResult]:
    """计算缠论结构。K 线不足（< 3 根）时返回 None。"""
    czsc = _load_czsc()

    df = klines_to_df(klines)
    if len(df) < 3:
        logger.debug(f"缠论计算跳过：K 线不足（{len(df)} 根）")
        return None

    freq = _freq_of(period)
    bars = czsc.format_standard_kline(df, freq=freq)

    # czsc 默认 max_bi_num=50 会截断历史（见模块 docstring 第 3 条）。
    # 实测 300 根输入按 len//4 取 100 仍被截断（bars_raw 300 → 256），
    # 故直接放到输入长度级别：宁大勿小 —— 实测把 max_bi_num 调到远大于
    # 实际笔数不会改变结果，只会避免静默丢数据。
    max_bi_num = max(500, len(df))
    obj = czsc.CZSC(bars, max_bi_num=max_bi_num)

    ordered = [
        KLineData(code=str(r.symbol), date=str(r.dt), open=float(r.open),
                  high=float(r.high), low=float(r.low), close=float(r.close),
                  volume=int(r.vol), period=period)
        for r in obj.bars_raw
    ]
    index_of = {r.dt: i for i, r in enumerate(obj.bars_raw)}

    # czsc 的 bars_raw 自「首个笔的起点」开始，因此可能少于输入根数
    # （实测 300 根输入只保留 256 根；且与「只喂末 256 根」得到完全
    #  相同的分型/笔数，说明被丢掉的前段确实未参与计算）。
    # 结论：idx 的基准是 bars_raw，不是调用方传入的序列 —— 务必按 dt 对齐，
    # 不要拿 idx 去索引原始输入。
    if len(obj.bars_raw) < len(df):
        logger.debug(
            f"缠论: czsc 保留 {len(obj.bars_raw)}/{len(df)} 根 K 线"
            "（bars_raw 自首个笔起点起算，idx 以此为基准）")

    return ChanResult(obj=obj, period=period, bars=ordered, index_of=index_of)


def fractals(result: ChanResult, kind: Optional[str] = None) -> list[dict]:
    """分型列表

    kind: None=全部 / "top"=顶分型 / "bottom"=底分型
    返回: [{kind, price, dt, idx}, ...]，按时间升序；idx 为 bars 中的下标。
    """
    out = []
    for fx in result.obj.fx_list:
        fx_kind = _mark_kind(fx.mark)
        if kind is not None and fx_kind != kind:
            continue
        out.append({
            "kind": fx_kind,
            "price": float(fx.fx),
            "dt": fx.dt,
            "idx": result.index_of.get(fx.dt, -1),
        })
    return out


def latest_fractal(result: ChanResult, kind: str) -> Optional[dict]:
    """最近一个指定类型的分型，没有则 None。kind: "top" | "bottom" """
    for fx in reversed(result.obj.fx_list):
        fx_kind = _mark_kind(fx.mark)
        if fx_kind == kind:
            return {
                "kind": fx_kind,
                "price": float(fx.fx),
                "dt": fx.dt,
                "idx": result.index_of.get(fx.dt, -1),
            }
    return None


def centers(result: ChanResult) -> list[dict]:
    """中枢列表（时间升序）

    返回: [{high, low, mid, sdt, edt, is_valid}, ...]
          high = zg 中枢上沿, low = zd 中枢下沿, mid = zz 中枢中轴

    ⚠️ 起始 / 终止的实测口径（2026-09-18，8 只标的 × 26 个日线中枢）：

    - `sdt` == `zs.bis[0].sdt` —— 中枢从**第一笔的起点**开始（26/26 命中）。
      缠论：中枢由前三段连续次级别走势的重叠确定，区间跨度的左端就是第一段起点。
    - `edt` == `zs.bis[-1].edt` —— 中枢结束于**最后一笔的终点**（26/26 命中）。
    - `zs.bis` = 所有**仍与中枢区间 [zd, zg] 有重叠**的笔（26/26 命中），
      **不含**完全脱离中枢的那一笔。

    即 czsc 判「还在中枢里」用的是缠论中心定理一的口径
    （任意段 [dn, gn] 与 [zd, zg] 有重叠），所以数据末尾之前的中枢，
    其**离开笔是 `zs.bis` 后面的第一笔**（实测中间中枢 18/18 符合；
    只有数据末尾那个中枢会因尾部未确认而提前收口）。

    ⇒ 但要注意：`zs.bis[-1]` 与中枢**有重叠**不代表价格还在中枢里 ——
      它完全可能已经把**终点**打到 zd 之下（或 zg 之上）。三买/三卖用的
      是「走势终点越过边沿」这另一套判据，因此不能假定 `zs.edt` 之后
      才是离开笔。详见 `core/chan_points.py` 模块 docstring。
    """
    out = []
    for zs in result.obj.zs_list:
        out.append({
            "high": float(zs.zg),
            "low": float(zs.zd),
            "mid": float(zs.zz),
            "sdt": zs.sdt,
            "edt": zs.edt,
            "is_valid": bool(zs.is_valid),
        })
    return out


def latest_center(result: ChanResult) -> Optional[dict]:
    """最近一个中枢，没有则 None"""
    all_centers = centers(result)
    return all_centers[-1] if all_centers else None


def bis(result: ChanResult) -> list[dict]:
    """笔列表（时间升序）"""
    out = []
    for bi in result.obj.bi_list:
        out.append({
            "direction": _direction_name(bi.direction),
            "high": float(bi.high),
            "low": float(bi.low),
            "sdt": bi.sdt,
            "edt": bi.edt,
            "start_idx": result.index_of.get(bi.fx_a.dt, -1),
            "end_idx": result.index_of.get(bi.fx_b.dt, -1),
        })
    return out
