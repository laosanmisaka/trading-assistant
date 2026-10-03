"""Aggregate per-signal research rows with paired stock baselines."""
import numpy as np
import pandas as pd
from core.research_stats import metrics, cluster_bootstrap, block_bootstrap

MIN5_BARS_PER_DAY = 48
STOP_LEVELS = (3.0, 5.0, 8.0)


def collect_points(rows: list[dict], side: str, h: int) -> list[dict]:
    """汇总所有标的的逐点收益 → ``[{code, ret, idx, dt}]``

    ``idx`` = 该点的入场 bar 序号（日线侧用日线 bar，5min 侧用 5min bar），
    去重叠时要用它判断两个持有窗是否重叠；``dt`` 用于时间分块。
    """
    key = f"D{h}" if side == "日线" else f"M{h}"
    ikey = "_entry_d" if side == "日线" else "_entry5"
    tkey = "日线确认" if side == "日线" else "最早买点"
    out = []
    for r in rows:
        for d in r["明细"]:
            v, i = d.get(key), d.get(ikey)
            if v is None or i is None:
                continue
            try:
                if pd.isna(v):
                    continue
            except (TypeError, ValueError):
                continue
            out.append({"code": d["代码"], "ret": float(v), "idx": int(i),
                        "dt": d.get(tkey)})
    return out


def base_of(rows: list[dict], side: str, h: int) -> dict[str, float]:
    return {r["代码"]: float(r["基准"][side][h]["mean"]) for r in rows
            if r.get("基准", {}).get(side, {}).get(h, {}).get("mean") is not None}


def trail_points(rows: list[dict], key: str = "TR") -> list[dict]:
    """动态止损的逐点收益 → ``[{code, ret, idx, dt}]``

    与 ``collect_points`` 返回同结构，可直接喂给 ``metrics`` / 两个 bootstrap。
    动态止损没有固定持有期，``deoverlap`` 用不上（用 ``max_hold`` 的 bar 数）。
    """
    out = []
    for r in rows:
        for d in r["明细"]:
            v, i = d.get(key), d.get("_entry5")
            if v is None or i is None:
                continue
            try:
                if pd.isna(v):
                    continue
            except (TypeError, ValueError):
                continue
            out.append({"code": d["代码"], "ret": float(v), "idx": int(i),
                        "dt": d.get("最早买点")})
    return out


def trail_base_of(rows: list[dict], mode: str = "stage") -> dict[str, float]:
    """每只标的的动态止损随机基准（样本均值）"""
    out: dict[str, float] = {}
    for r in rows:
        b = (r.get("基准TR") or {}).get(mode)
        if b and b.get("mean") is not None:
            out[r["代码"]] = float(b["mean"])
    return out


def deoverlap(points: list[dict], hold_bars: int) -> list[dict]:
    """同一标的内贪心去重叠：与上一条已保留的持有窗重叠的点丢弃

    持有期重叠会让「194 个样本」远小于 194 个独立机会。保留规则是按入场
    时间排序、只留窗不重叠的，得到的是**近似独立**的样本数。
    """
    by: dict[str, list[dict]] = {}
    for p in points:
        by.setdefault(p["code"], []).append(p)
    kept: list[dict] = []
    for ps in by.values():
        ps.sort(key=lambda x: x["idx"])
        last = None
        for p in ps:
            if last is None or p["idx"] >= last + hold_bars:
                kept.append(p)
                last = p["idx"]
    return kept


def robustness(rows: list[dict], side: str, h: int) -> dict:
    """一组稳健性指标：截面 bootstrap / 时间块 bootstrap / 剔前 5 点 / 剔最好标的"""
    base = base_of(rows, side, h)
    pts = [p for p in collect_points(rows, side, h) if p["code"] in base]
    hold_bars = h if side == "日线" else h * MIN5_BARS_PER_DAY
    out: dict = {"base": metrics(pts, base),
                 "indep": metrics(deoverlap(pts, hold_bars), base),
                 "boot": cluster_bootstrap(pts, base),
                 "boot_time": block_bootstrap(pts, base)}
    # 剔除贡献最大的 5 个点
    top5 = set()
    for p in sorted(pts, key=lambda x: -(x["ret"] - base[x["code"]]))[:5]:
        top5.add(id(p))
    out["drop_top5"] = metrics([p for p in pts if id(p) not in top5], base)
    # 剔除表现最好的 1 只标的
    by: dict[str, list[float]] = {}
    for p in pts:
        by.setdefault(p["code"], []).append(p["ret"])
    if by:
        best = max(by, key=lambda c: float(np.mean(by[c])) - base[c])
        out["drop_best_stock"] = metrics([p for p in pts if p["code"] != best], base)
        out["drop_best_stock_name"] = best
    return out


def _number(value):
    try:
        value = float(value)
        return value if np.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _column(points, key):
    return np.array([value for point in points
                     if (value := _number(point.get(key))) is not None])


def _price_advantage(points, ret_key):
    result = {}
    advantage = _column(points, "价优%")
    if len(advantage):
        result["adv_med"] = float(np.median(advantage))
    paired = [(_number(p.get("价优%")), _number(p.get("同期基准%"))) for p in points]
    paired = [(a, b) for a, b in paired if a is not None and b is not None]
    if paired:
        result["sync_med"] = float(np.median([b for _, b in paired]))
        result["net_med"] = float(np.median([a - b for a, b in paired]))
    correlation = [(_number(p.get("价优%")), _number(p.get(ret_key))) for p in points]
    correlation = [(a, b) for a, b in correlation if a is not None and b is not None]
    if len(correlation) > 2:
        a, b = zip(*correlation)
        if np.std(a) > 0 and np.std(b) > 0:
            result["adv_corr"] = float(np.corrcoef(a, b)[0, 1])
    return result


def mae_table(rows: list[dict], side: str, h: int) -> dict | None:
    """每个级别使用自己的收益/极值；基准按同一有效逐点样本加权。"""
    if side == "5min":
        ret_key, mae_key, mfe_key, stop_key = f"M{h}", f"MAE{h}", f"MFE{h}", f"SL{h}"
    else:
        ret_key, mae_key, mfe_key, stop_key = f"D{h}", f"DMAE{h}", f"DMFE{h}", f"DSL{h}"
    points = [p for row in rows for p in row["明细"] if _number(p.get(ret_key)) is not None]
    if not points:
        return None
    result = {"n": len(points), "mean": float(np.mean(_column(points, ret_key)))}
    mae, mfe = _column(points, mae_key), _column(points, mfe_key)
    if len(mae):
        result.update(mae_med=float(np.median(mae)), mae_p10=float(np.percentile(mae, 10)),
                      touch={level: float(np.mean(mae <= -abs(level))) for level in STOP_LEVELS})
    if len(mfe):
        result["mfe_med"] = float(np.median(mfe))
    for level in STOP_LEVELS:
        values = _column(points, f"{stop_key}_{int(level)}")
        if len(values):
            result[f"sl{int(level)}_mean"] = float(np.mean(values))
    bases = base_of(rows, side, h)
    if all(p["代码"] in bases for p in points):
        result["base"] = float(np.mean([bases[p["代码"]] for p in points]))
    if side == "5min":
        result.update(_price_advantage(points, ret_key))
    return result
