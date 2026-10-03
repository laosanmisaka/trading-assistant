"""One weighted opportunity set exercises the common estimand and both resamplers."""
import pytest

from core.research_stats import metrics, cluster_bootstrap, block_bootstrap


def test_opportunity_weights_and_bootstrap_share_paired_excess():
    base = {str(i): float(i * 10) for i in range(5)}
    points = [{"code": code, "ret": value, "dt": f"2025-{quarter * 3 + 1:02d}-06"}
              for code, value in base.items() for quarter in range(4)
              for _ in range(int(code) + 1)]
    point_estimate = metrics(points, base)
    assert point_estimate["base"] != point_estimate["base_eqw"]
    assert point_estimate["ex_pt"] == point_estimate["ex_eqw"] == 0
    for estimate in (cluster_bootstrap, block_bootstrap):
        interval = estimate(points, base, n_boot=50)
        assert interval["lo"] == interval["hi"] == 0
        assert interval["p_le0"] == 1
    # A real +2 point improvement must survive the same weighting and resampling.
    improved = [{**point, "ret": point["ret"] + 2} for point in points]
    assert metrics(improved, base)["ex_pt"] == pytest.approx(2)
    for estimate in (cluster_bootstrap, block_bootstrap):
        interval = estimate(improved, base, n_boot=50)
        assert interval["lo"] == interval["hi"] == 2
