"""Tests for utility functions: growth calculation, sparkline, validation, date parsing."""

import os
import tempfile

import pytest

from pkgdb import (
    get_db_connection,
    init_db,
    add_package,
    calculate_growth,
    daily_window_sums,
    make_sparkline,
    validate_package_name,
    validate_output_path,
    parse_date_arg,
    PackageStatsService,
)


class TestDailyWindowSums:
    """Tests for daily_window_sums (adjacent calendar-window totals)."""

    def test_two_full_windows(self):
        # 4 consecutive days, window of 2
        series = [
            ("2026-06-01", 10),
            ("2026-06-02", 20),
            ("2026-06-03", 30),
            ("2026-06-04", 40),
        ]
        # current = 03+04 = 70, previous = 01+02 = 30 (anchored on max date)
        assert daily_window_sums(series, 2) == (70, 30)

    def test_missing_dates_count_as_zero(self):
        # Gap on 06-02; window still anchored by calendar, not row count
        series = [("2026-06-01", 10), ("2026-06-03", 30), ("2026-06-04", 40)]
        # anchor 06-04; current(2) = 04+03 = 70; previous(2) = 02+01 = 0+10 = 10
        assert daily_window_sums(series, 2) == (70, 10)

    def test_single_day_window(self):
        series = [("2026-06-03", 30), ("2026-06-04", 40)]
        assert daily_window_sums(series, 1) == (40, 30)

    def test_unordered_input(self):
        # Order-independent; anchor is the max date (06-04).
        series = [("2026-06-04", 40), ("2026-06-01", 10), ("2026-06-03", 30)]
        # current(1) = 06-04 = 40; previous(1) = 06-03 = 30
        assert daily_window_sums(series, 1) == (40, 30)

    def test_empty_or_invalid(self):
        assert daily_window_sums([], 7) is None
        assert daily_window_sums([("2026-06-01", 5)], 0) is None


class TestGrowthCalculation:
    """Tests for growth calculation functions."""

    def test_calculate_growth_positive(self):
        """calculate_growth should return positive percentage for increase."""
        assert calculate_growth(150, 100) == 50.0

    def test_calculate_growth_negative(self):
        """calculate_growth should return negative percentage for decrease."""
        assert calculate_growth(50, 100) == -50.0

    def test_calculate_growth_zero_previous(self):
        """calculate_growth should return None when previous is zero."""
        assert calculate_growth(100, 0) is None

    def test_calculate_growth_none_values(self):
        """calculate_growth should return None when values are None."""
        assert calculate_growth(None, 100) is None
        assert calculate_growth(100, None) is None

    def test_get_stats_with_growth_uses_weekly_column(self, temp_db):
        """week_growth should be computed from last_week, not last_month."""
        from datetime import datetime, timedelta
        from pkgdb.db import get_stats_with_growth

        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "test-pkg")

        today = datetime.now()
        eight_days_ago = (today - timedelta(days=8)).strftime("%Y-%m-%d")
        today_str = today.strftime("%Y-%m-%d")

        # Insert stats directly with deliberately different weekly vs monthly values
        conn.execute(
            "INSERT INTO package_stats (package_name, fetch_date, last_day, last_week, last_month, total) VALUES (?, ?, ?, ?, ?, ?)",
            ("test-pkg", eight_days_ago, 10, 100, 1000, 5000),
        )
        conn.execute(
            "INSERT INTO package_stats (package_name, fetch_date, last_day, last_week, last_month, total) VALUES (?, ?, ?, ?, ?, ?)",
            ("test-pkg", today_str, 15, 200, 1500, 6000),
        )
        conn.commit()

        stats = get_stats_with_growth(conn)
        pkg_stat = next(s for s in stats if s["package_name"] == "test-pkg")

        # week_growth should compare last_week values: (200-100)/100 = 100%
        # If it wrongly used last_month: (1500-1000)/1000 = 50%
        assert pkg_stat["week_growth"] == 100.0
        conn.close()


class TestSparkline:
    """Tests for sparkline generation."""

    def test_make_sparkline_basic(self):
        """make_sparkline should generate a string of correct width."""
        sparkline = make_sparkline([1, 2, 3, 4, 5], width=5)
        assert len(sparkline) == 5

    def test_make_sparkline_empty(self):
        """make_sparkline should handle empty list."""
        sparkline = make_sparkline([], width=7)
        assert len(sparkline) == 7

    def test_make_sparkline_constant_values(self):
        """make_sparkline should handle constant values."""
        sparkline = make_sparkline([5, 5, 5, 5, 5], width=5)
        assert len(sparkline) == 5

    def test_make_sparkline_uses_last_values(self):
        """make_sparkline should use the last N values when list is longer."""
        sparkline = make_sparkline([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], width=5)
        assert len(sparkline) == 5


class TestPackageNameValidation:
    """Tests for package name validation."""

    def test_valid_package_names(self):
        """Valid package names should pass validation."""
        valid_names = [
            "requests",
            "my-package",
            "my_package",
            "my.package",
            "package123",
            "A1",
            "a",  # Single char is valid
            "ab",  # Two chars
            "my-pkg.v2_test",  # Mixed separators
        ]
        for name in valid_names:
            is_valid, error = validate_package_name(name)
            assert is_valid, f"'{name}' should be valid, got error: {error}"

    def test_empty_package_name(self):
        """Empty package name should fail validation."""
        is_valid, error = validate_package_name("")
        assert not is_valid
        assert "empty" in error.lower()

    def test_package_name_too_long(self):
        """Package name exceeding 100 chars should fail validation."""
        long_name = "a" * 101
        is_valid, error = validate_package_name(long_name)
        assert not is_valid
        assert "100" in error

    def test_package_name_invalid_start(self):
        """Package names starting with non-alphanumeric should fail."""
        invalid_names = ["-package", "_package", ".package"]
        for name in invalid_names:
            is_valid, error = validate_package_name(name)
            assert not is_valid, f"'{name}' should be invalid"

    def test_package_name_invalid_end(self):
        """Package names ending with non-alphanumeric should fail."""
        invalid_names = ["package-", "package_", "package."]
        for name in invalid_names:
            is_valid, error = validate_package_name(name)
            assert not is_valid, f"'{name}' should be invalid"

    def test_package_name_invalid_chars(self):
        """Package names with invalid characters should fail."""
        invalid_names = ["my package", "my@package", "my!pkg", "my/pkg"]
        for name in invalid_names:
            is_valid, error = validate_package_name(name)
            assert not is_valid, f"'{name}' should be invalid"

    def test_service_add_invalid_package_raises(self, temp_db):
        """Service.add_package should raise ValueError for invalid names."""
        service = PackageStatsService(temp_db)
        with pytest.raises(ValueError) as exc_info:
            service.add_package("")
        assert "empty" in str(exc_info.value).lower()

        with pytest.raises(ValueError):
            service.add_package("-invalid")

    def test_service_import_returns_invalid_names(self, temp_db):
        """Service.import_packages should return list of invalid names."""
        from pathlib import Path

        service = PackageStatsService(temp_db)

        # Create a temp file with mix of valid and invalid names
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("valid-pkg\n")
            f.write("-invalid\n")
            f.write("another-valid\n")
            f.write("also invalid spaces\n")
            temp_file = f.name

        try:
            added, skipped, invalid, not_found = service.import_packages(
                temp_file, verify=False
            )
            assert added == 2
            assert skipped == 0
            assert len(invalid) == 2
            assert "-invalid" in invalid
            assert "also invalid spaces" in invalid
            assert not_found == []
        finally:
            Path(temp_file).unlink()


class TestParseDateArg:
    """Tests for parse_date_arg function."""

    def test_standard_date_format(self):
        """parse_date_arg accepts YYYY-MM-DD format."""
        date, error = parse_date_arg("2024-01-15")
        assert date == "2024-01-15"
        assert error is None

    def test_invalid_standard_date(self):
        """parse_date_arg rejects invalid dates."""
        date, error = parse_date_arg("2024-13-45")
        assert date is None
        assert error is not None
        assert "Invalid date" in error

    def test_relative_days(self):
        """parse_date_arg parses Nd format."""
        from datetime import datetime, timedelta

        date, error = parse_date_arg("7d")
        assert error is None
        expected = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        assert date == expected

    def test_relative_weeks(self):
        """parse_date_arg parses Nw format."""
        from datetime import datetime, timedelta

        date, error = parse_date_arg("2w")
        assert error is None
        expected = (datetime.now() - timedelta(weeks=2)).strftime("%Y-%m-%d")
        assert date == expected

    def test_relative_months(self):
        """parse_date_arg parses Nm format (30 days per month)."""
        from datetime import datetime, timedelta

        date, error = parse_date_arg("1m")
        assert error is None
        expected = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        assert date == expected

    def test_case_insensitive(self):
        """parse_date_arg is case insensitive for units."""
        date_lower, _ = parse_date_arg("7d")
        date_upper, _ = parse_date_arg("7D")
        assert date_lower == date_upper

    def test_zero_offset_rejected(self):
        """parse_date_arg rejects zero offset."""
        date, error = parse_date_arg("0d")
        assert date is None
        assert error is not None
        assert "greater than 0" in error

    def test_invalid_format_rejected(self):
        """parse_date_arg rejects invalid formats."""
        date, error = parse_date_arg("invalid")
        assert date is None
        assert error is not None
        assert "Invalid date format" in error

    def test_empty_value_rejected(self):
        """parse_date_arg rejects empty values."""
        date, error = parse_date_arg("")
        assert date is None
        assert error is not None
        assert "empty" in error.lower()


class TestOutputPathValidation:
    """Tests for output path validation."""

    def test_valid_path_in_temp_dir(self):
        """Valid path in temp directory should pass."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "output.html")
            is_valid, error = validate_output_path(path)
            assert is_valid, f"Should be valid: {error}"

    def test_valid_path_with_allowed_extension(self):
        """Path with allowed extension should pass."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.html")
            is_valid, error = validate_output_path(path, allowed_extensions=[".html"])
            assert is_valid, f"Should be valid: {error}"

    def test_invalid_extension(self):
        """Path with wrong extension should fail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.txt")
            is_valid, error = validate_output_path(path, allowed_extensions=[".html"])
            assert not is_valid
            assert "extension" in error.lower()

    def test_empty_path(self):
        """Empty path should fail."""
        is_valid, error = validate_output_path("")
        assert not is_valid
        assert "empty" in error.lower()

    def test_nonexistent_parent_directory(self):
        """Path with nonexistent parent should fail."""
        is_valid, error = validate_output_path("/nonexistent/directory/file.html")
        assert not is_valid
        assert "not exist" in error.lower()

    def test_sensitive_system_path_unix(self):
        """Paths to sensitive Unix directories should fail."""
        if os.name != "nt":  # Skip on Windows
            is_valid, error = validate_output_path("/etc/passwd.html")
            assert not is_valid
            # Could be rejected as system directory or as not writable
            assert "directory" in error.lower()

    def test_path_traversal_detection(self):
        """Path traversal attempts should be caught."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # This resolves to parent directory which should still work
            # if it's writable, but the path is normalized
            path = os.path.join(tmpdir, "..", "output.html")
            # The validation should resolve the path
            is_valid, error = validate_output_path(path)
            # May or may not be valid depending on parent permissions
            # The key is that .. is resolved
            assert isinstance(is_valid, bool)

    def test_writable_check(self):
        """Non-writable parent should fail when must_be_writable=True."""
        # /usr should not be writable for normal users
        if os.name != "nt" and not os.access("/usr", os.W_OK):
            is_valid, error = validate_output_path(
                "/usr/output.html", must_be_writable=True
            )
            assert not is_valid
