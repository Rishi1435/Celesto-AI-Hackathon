import pytest

from app.reporting import reliability


def test_twenty_rows_even_median_nearest_rank_p90_and_failed_rows():
    rows = [
        dict(
            sandbox_name=f"sbx-eval{i:08}",
            details=dict(
                fresh=True,
                success=i % 4 != 0,
                wall_clock_s=i,
                boot_to_ready_s=i / 2,
                cost_usd=0.01,
                outcome="failed" if i % 4 == 0 else "succeeded",
                scenario="normal",
            ),
        )
        for i in range(1, 21)
    ]
    result = reliability(rows)
    assert result["run_count"] == 20
    assert result["success_rate"] == 0.75  # Below 80% remains valid and keeps every row.
    assert result["median_wall_clock_s"] == 10.5
    assert result["p90_wall_clock_s"] == 18
    assert result["median_boot_to_ready_s"] == 5.25
    assert result["total_cost_usd"] == pytest.approx(0.2)
