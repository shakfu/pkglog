"""Tests for anomaly and milestone detection (pkgdb check)."""

from datetime import datetime, timedelta

from pkgdb import detect_anomaly, detect_milestones, weekly_totals


def _series(daily_values, start="2026-01-05"):
    """Build a contiguous daily series from a list of per-day download counts."""
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    return [
        ((d0 + timedelta(days=i)).isoformat(), v) for i, v in enumerate(daily_values)
    ]


class TestWeeklyTotals:
    def test_two_weeks(self):
        series = _series([10] * 7 + [20] * 7)
        weeks = weekly_totals(series, 2)
        assert [total for _, total in weeks] == [140, 70]  # most recent first

    def test_stops_before_data_start(self):
        series = _series([5] * 7)  # only one week of data
        weeks = weekly_totals(series, 8)
        assert len(weeks) == 1
        assert weeks[0][1] == 35

    def test_empty(self):
        assert weekly_totals([], 4) == []
        assert weekly_totals(_series([1, 2, 3]), 0) == []


class TestDetectAnomaly:
    def test_spike_flat_baseline(self):
        # 8 baseline weeks at 100/day (700/wk), last week at 200/day (1400/wk)
        series = _series([100] * 56 + [200] * 7)
        event = detect_anomaly(series)
        assert event is not None
        assert event["kind"] == "spike"
        assert event["value"] == 1400
        assert event["baseline"] == 700.0
        assert event["change_pct"] == 100.0
        assert event["period"] == "week"

    def test_drop_flat_baseline(self):
        series = _series([100] * 56 + [20] * 7)
        event = detect_anomaly(series)
        assert event is not None
        assert event["kind"] == "drop"
        assert event["value"] == 140
        assert event["change_pct"] < 0

    def test_no_anomaly_when_stable(self):
        # Last week only slightly above a flat baseline -> below the 50% rule
        series = _series([100] * 56 + [103] * 7)
        assert detect_anomaly(series) is None

    def test_variance_baseline_uses_zscore(self):
        # Baseline with spread; a big last week should exceed the z threshold
        weekly = [600, 700, 650, 720, 680, 710, 690, 700]
        daily = []
        for wk in weekly:
            daily += [wk // 7] * 7
        daily += [2000]  # inflate; padded below to a full week
        daily += [0] * 6
        series = _series(daily)
        event = detect_anomaly(series)
        assert event is not None
        assert event["kind"] == "spike"
        assert abs(event["z_score"]) >= 2.5

    def test_skips_low_volume_packages(self):
        # Baseline averages < min_weekly (10); even a big jump is ignored
        series = _series([1] * 56 + [5] * 7)
        assert detect_anomaly(series) is None

    def test_insufficient_history(self):
        # Fewer than baseline_weeks + 1 weeks of data -> no verdict
        series = _series([100] * 20)
        assert detect_anomaly(series) is None

    def test_custom_thresholds(self):
        series = _series([100] * 56 + [130] * 7)  # +30%
        # Default flat rule needs 50%; a 25% z-agnostic override still needs std
        assert detect_anomaly(series) is None
        # But lowering the flat-baseline path isn't exposed; verify baseline_weeks
        short = detect_anomaly(series, baseline_weeks=4)
        # 4 baseline weeks all 700, last 910 -> +30%, flat rule 50% -> still None
        assert short is None


class TestDetectMilestones:
    def test_upward_crossing(self):
        assert detect_milestones(900, 1100, [1000]) == [1000]

    def test_multiple_crossings_sorted(self):
        assert detect_milestones(900, 11000, [1000, 5000, 10000, 50000]) == [
            1000,
            5000,
            10000,
        ]

    def test_no_crossing(self):
        assert detect_milestones(1100, 1200, [1000]) == []

    def test_exact_boundary_is_inclusive(self):
        assert detect_milestones(999, 1000, [1000]) == [1000]

    def test_downward_not_reported(self):
        assert detect_milestones(1200, 1100, [1000]) == []

    def test_none_totals(self):
        assert detect_milestones(None, 1000, [500]) == []
        assert detect_milestones(500, None, [500]) == []
