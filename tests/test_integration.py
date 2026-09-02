"""Integration tests, error path tests, and performance tests."""

import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from pkgdb import (
    get_db_connection,
    get_db,
    init_db,
    add_package,
    get_packages,
    get_latest_stats,
    get_all_history,
    store_stats,
    load_packages,
    load_packages_from_file,
    fetch_package_stats,
    fetch_python_versions,
    fetch_os_stats,
    generate_html_report,
    PackageStatsService,
)


@pytest.mark.integration
class TestIntegration:
    """Integration tests that make real API calls.

    Run with: pytest -m integration
    Or set environment variable: RUN_INTEGRATION=1 pytest
    """

    @pytest.mark.skipif(
        os.environ.get("RUN_INTEGRATION") != "1",
        reason="Integration tests disabled (set RUN_INTEGRATION=1 to enable)",
    )
    def test_fetch_real_package_stats(self):
        """fetch_package_stats should return real data for a known package."""
        stats = fetch_package_stats("requests")
        assert stats is not None
        assert stats["total"] > 0
        assert stats["last_month"] > 0
        assert stats["last_week"] >= 0
        assert stats["last_day"] >= 0

    @pytest.mark.skipif(
        os.environ.get("RUN_INTEGRATION") != "1",
        reason="Integration tests disabled (set RUN_INTEGRATION=1 to enable)",
    )
    def test_fetch_real_python_versions(self):
        """fetch_python_versions should return real data for a known package."""
        versions = fetch_python_versions("requests")
        assert versions is not None
        assert len(versions) > 0
        # Should have common Python versions
        categories = [v["category"] for v in versions]
        assert any("3." in cat for cat in categories if cat)

    @pytest.mark.skipif(
        os.environ.get("RUN_INTEGRATION") != "1",
        reason="Integration tests disabled (set RUN_INTEGRATION=1 to enable)",
    )
    def test_fetch_real_os_stats(self):
        """fetch_os_stats should return real data for a known package."""
        os_stats = fetch_os_stats("requests")
        assert os_stats is not None
        assert len(os_stats) > 0
        # Should have common OS categories
        categories = [s["category"] for s in os_stats]
        assert any(cat in ["Linux", "Windows", "Darwin"] for cat in categories if cat)

    @pytest.mark.skipif(
        os.environ.get("RUN_INTEGRATION") != "1",
        reason="Integration tests disabled (set RUN_INTEGRATION=1 to enable)",
    )
    def test_fetch_nonexistent_package(self):
        """fetch_package_stats should return None for nonexistent package."""
        stats = fetch_package_stats("this-package-definitely-does-not-exist-12345")
        assert stats is None


class TestErrorPaths:
    """Tests for error handling and edge cases."""

    def test_database_invalid_path(self):
        """get_db should handle invalid paths gracefully."""
        # Directory that doesn't exist
        with pytest.raises((sqlite3.OperationalError, OSError)):
            with get_db("/nonexistent/path/to/db.sqlite") as conn:
                pass

    def test_database_readonly_after_init(self, temp_db):
        """Database operations should work after init."""
        with get_db(temp_db) as conn:
            # Should be able to add packages
            assert add_package(conn, "test-pkg") is True
            # Should be able to query
            packages = get_packages(conn)
            assert "test-pkg" in packages

    def test_store_stats_with_none_values(self, db_conn):
        """store_stats should handle None values in stats dict."""
        stats = {
            "last_day": None,
            "last_week": None,
            "last_month": None,
            "total": None,
        }
        # Should not raise
        store_stats(db_conn, "test-pkg", stats)

        cursor = db_conn.execute(
            "SELECT * FROM package_stats WHERE package_name = ?", ("test-pkg",)
        )
        row = cursor.fetchone()
        assert row is not None
        assert row["last_day"] is None
        assert row["total"] is None

    def test_load_packages_invalid_json(self):
        """load_packages should handle invalid JSON gracefully."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{invalid json")
            path = f.name

        try:
            with pytest.raises(json.JSONDecodeError):
                load_packages(path)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_load_packages_from_file_invalid_json(self):
        """load_packages_from_file should handle invalid JSON."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{invalid json")
            path = f.name

        try:
            with pytest.raises(json.JSONDecodeError):
                load_packages_from_file(path)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_load_packages_from_file_wrong_json_type(self):
        """load_packages_from_file should handle wrong JSON structure."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump("just a string", f)
            path = f.name

        try:
            packages = load_packages_from_file(path)
            # Should return empty or handle gracefully
            assert packages == [] or packages is None
        finally:
            Path(path).unlink(missing_ok=True)

    def test_fetch_partial_api_failure(self):
        """fetch_all_package_stats should handle partial failures."""
        from pkgdb.api import fetch_all_package_stats

        recent_success = json.dumps(
            {"data": {"last_day": 100, "last_week": 700, "last_month": 3000}}
        )
        overall_success = json.dumps(
            {"data": [{"category": "without_mirrors", "downloads": 50000}]}
        )

        call_count = {"count": 0}

        def mock_recent(pkg, format=None):
            call_count["count"] += 1
            if pkg == "fail-pkg":
                raise ValueError("API error")
            return recent_success

        def mock_overall(pkg, format=None):
            if pkg == "fail-pkg":
                raise ValueError("API error")
            return overall_success

        with patch("pkgdb.api.pypistats.recent", side_effect=mock_recent):
            with patch("pkgdb.api.pypistats.overall", side_effect=mock_overall):
                results = fetch_all_package_stats(
                    ["good-pkg", "fail-pkg", "another-good"]
                )

        # Should have results for all packages
        assert len(results) == 3
        # Good packages should have stats
        assert results["good-pkg"] is not None
        assert results["another-good"] is not None
        # Failed package should be None
        assert results["fail-pkg"] is None

    def test_generate_report_handles_missing_history_gracefully(self):
        """generate_html_report should work with None history values."""
        stats = [
            {
                "package_name": "pkg",
                "total": 1000,
                "last_month": 300,
                "last_week": 70,
                "last_day": 10,
            }
        ]
        history = {
            "pkg": [
                {
                    "fetch_date": "2024-01-01",
                    "total": None,
                    "last_month": None,
                    "last_week": None,
                    "last_day": None,
                }
            ]
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
            output_path = f.name

        try:
            # Should not raise
            generate_html_report(stats, output_path, history)
            assert Path(output_path).exists()
        finally:
            Path(output_path).unlink(missing_ok=True)

    def test_service_cleanup_empty_db(self, temp_db):
        """Service cleanup should work on empty database."""
        service = PackageStatsService(temp_db)
        orphaned, remaining = service.cleanup()
        assert orphaned["total"] == 0
        assert set(orphaned.values()) == {0}
        assert remaining == 0

    def test_service_prune_no_old_data(self, temp_db):
        """Service prune should work when no old data exists."""
        service = PackageStatsService(temp_db)
        deleted = service.prune(days=30)
        assert deleted["total"] == 0
        assert set(deleted.values()) == {0}


@pytest.mark.slow
class TestPerformance:
    """Performance tests for large datasets.

    Run with: pytest -m slow
    """

    @pytest.mark.skipif(
        os.environ.get("RUN_SLOW_TESTS") != "1",
        reason="Slow tests disabled (set RUN_SLOW_TESTS=1 to enable)",
    )
    def test_large_number_of_packages(self, temp_db):
        """Database should handle 100+ packages efficiently."""
        service = PackageStatsService(temp_db)

        # Add 100 packages (skip verify for performance testing)
        start = time.time()
        for i in range(100):
            service.add_package(f"test-package-{i:03d}", verify=False)
        add_time = time.time() - start

        # Should complete in reasonable time (< 5 seconds)
        assert add_time < 5.0, f"Adding 100 packages took {add_time:.2f}s"

        # List should be fast
        start = time.time()
        packages = service.list_packages()
        list_time = time.time() - start

        assert len(packages) == 100
        assert list_time < 1.0, f"Listing 100 packages took {list_time:.2f}s"

    @pytest.mark.skipif(
        os.environ.get("RUN_SLOW_TESTS") != "1",
        reason="Slow tests disabled (set RUN_SLOW_TESTS=1 to enable)",
    )
    def test_large_historical_data(self, temp_db):
        """Database should handle 1000+ days of history efficiently."""
        from datetime import datetime, timedelta

        conn = get_db_connection(temp_db)
        init_db(conn)

        # Insert 1000 days of history for 10 packages
        start = time.time()
        base_date = datetime(2020, 1, 1)
        for pkg_num in range(10):
            pkg_name = f"pkg-{pkg_num}"
            for day in range(1000):
                date = (base_date + timedelta(days=day)).strftime("%Y-%m-%d")
                conn.execute(
                    """
                    INSERT INTO package_stats
                    (package_name, fetch_date, last_day, last_week, last_month, total)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (pkg_name, date, day * 10, day * 70, day * 300, day * 1000),
                )
        conn.commit()
        insert_time = time.time() - start

        # Query should be fast
        start = time.time()
        stats = get_latest_stats(conn)
        query_time = time.time() - start

        conn.close()

        assert len(stats) == 10
        assert insert_time < 30.0, f"Inserting 10k records took {insert_time:.2f}s"
        assert query_time < 1.0, f"Querying latest stats took {query_time:.2f}s"

    @pytest.mark.skipif(
        os.environ.get("RUN_SLOW_TESTS") != "1",
        reason="Slow tests disabled (set RUN_SLOW_TESTS=1 to enable)",
    )
    def test_report_generation_performance(self, temp_db):
        """Report generation should complete in reasonable time."""
        from datetime import datetime, timedelta

        conn = get_db_connection(temp_db)
        init_db(conn)

        # Create test data: 50 packages with 30 days history each
        base_date = datetime(2024, 1, 1)
        for pkg_num in range(50):
            pkg_name = f"package-{pkg_num:03d}"
            for day in range(30):
                date = (base_date + timedelta(days=day)).strftime("%Y-%m-%d")
                conn.execute(
                    """
                    INSERT INTO package_stats
                    (package_name, fetch_date, last_day, last_week, last_month, total)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pkg_name,
                        date,
                        100 + day,
                        700 + day * 7,
                        3000 + day * 30,
                        50000 + day * 100,
                    ),
                )
        conn.commit()

        stats = get_latest_stats(conn)
        history = get_all_history(conn, limit_per_package=30)
        conn.close()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
            output_path = f.name

        try:
            start = time.time()
            generate_html_report(stats, output_path, history)
            gen_time = time.time() - start

            assert Path(output_path).exists()
            # Should generate in reasonable time (< 5 seconds)
            assert gen_time < 5.0, f"Report generation took {gen_time:.2f}s"

            # File should be reasonable size (< 1MB for 50 packages)
            file_size = Path(output_path).stat().st_size
            assert file_size < 1_000_000, f"Report file is {file_size} bytes"
        finally:
            Path(output_path).unlink(missing_ok=True)

    @pytest.mark.skipif(
        os.environ.get("RUN_SLOW_TESTS") != "1",
        reason="Slow tests disabled (set RUN_SLOW_TESTS=1 to enable)",
    )
    def test_get_stats_with_growth_performance(self, temp_db):
        """get_stats_with_growth should be efficient with many packages."""
        from datetime import datetime, timedelta
        from pkgdb.db import get_stats_with_growth

        conn = get_db_connection(temp_db)
        init_db(conn)

        # Create 50 packages with 31 days of history
        base_date = datetime(2024, 1, 1)
        for pkg_num in range(50):
            pkg_name = f"pkg-{pkg_num:03d}"
            for day in range(31):
                date = (base_date + timedelta(days=day)).strftime("%Y-%m-%d")
                conn.execute(
                    """
                    INSERT INTO package_stats
                    (package_name, fetch_date, last_day, last_week, last_month, total)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (pkg_name, date, 100, 700, 3000 + day * 10, 50000 + day * 100),
                )
        conn.commit()

        # Time the growth calculation
        start = time.time()
        stats = get_stats_with_growth(conn)
        calc_time = time.time() - start

        conn.close()

        assert len(stats) == 50
        # Should complete quickly due to optimized query (< 1 second)
        assert calc_time < 1.0, f"Growth calculation took {calc_time:.2f}s"
        # Verify growth is calculated
        assert all("week_growth" in s for s in stats)
        assert all("month_growth" in s for s in stats)
