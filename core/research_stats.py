"""Paired excess returns and bootstrap estimates with matching observation weights.

These are per-signal descriptive statistics, not portfolio returns or a claim
that the input signals were observable in real time.
"""
from math import isfinite

import numpy as np
import pandas as pd

BOOT_N = 1000
BOOT_SEED = 20260922


def _points(points, base):
    return [p for p in points if p["code"] in base and isfinite(p["ret"])
            and isfinite(base[p["code"]])]


def paired_excess(points, base):
    return float(np.mean([p["ret"] - base[p["code"]] for p in points]))


def metrics(points: list[dict], base: dict[str, float]) -> dict | None:
    """点均和标的等权分别使用同权重基准，缺基准的点不参与任何一侧。"""
    pts = _points(points, base)
    if not pts:
        return None
    by = {}
    for point in pts:
        by.setdefault(point["code"], []).append(point["ret"])
    point_mean = float(np.mean([p["ret"] for p in pts]))
    equal_mean = float(np.mean([np.mean(values) for values in by.values()]))
    base_point = float(np.mean([base[p["code"]] for p in pts]))
    base_equal = float(np.mean([base[code] for code in by]))
    return {"n": len(pts), "n_stock": len(by), "pt_mean": point_mean,
            "eqw": equal_mean, "base": base_point, "base_eqw": base_equal,
            "ex_pt": paired_excess(pts, base), "ex_eqw": equal_mean - base_equal}


def _bootstrap(groups, base, n_boot, seed):
    if n_boot <= 0:
        raise ValueError("n_boot must be positive")
    rng = np.random.default_rng(seed)
    draws = []
    for _ in range(n_boot):
        picks = rng.integers(0, len(groups), size=len(groups))
        sample = [p for index in picks for p in groups[int(index)]]
        draws.append(paired_excess(sample, base))
    lo, hi = np.percentile(draws, [2.5, 97.5])
    return {"lo": float(lo), "hi": float(hi),
            "p_le0": float(np.mean(np.array(draws) <= 0)), "n_boot": n_boot}


def cluster_bootstrap(points, base, *, n_boot=BOOT_N, seed=BOOT_SEED):
    """按标的重抽，估计与 metrics.ex_pt 相同的逐点配对超额。"""
    groups = {}
    for point in _points(points, base):
        groups.setdefault(point["code"], []).append(point)
    if len(groups) < 5:
        return None
    return _bootstrap([groups[code] for code in sorted(groups)], base, n_boot, seed)


def block_bootstrap(points, base, *, n_boot=BOOT_N, seed=BOOT_SEED):
    """按入场时刻的自然季度重抽同一配对统计量；至少需要四季度。"""
    groups = {}
    for point in _points(points, base):
        if point.get("dt") is None:
            continue
        time = pd.Timestamp(str(point["dt"]))
        key = (time.year, (time.month - 1) // 3)
        groups.setdefault(key, []).append(point)
    if len(groups) < 4:
        return None
    result = _bootstrap([groups[key] for key in sorted(groups)], base, n_boot, seed)
    return {**result, "n_block": len(groups)}
