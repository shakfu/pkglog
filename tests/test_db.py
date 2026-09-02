"""Tests for database operations, package management, stats storage, and related DB functionality."""

import json
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from conftest import mock_pypistats, track

from pkgdb import (
    get_db_connection,
    get_db,
    init_db,
    add_package,
    remove_package,
    get_packages,
    get_packages_needing_update,
    record_fetch_attempt,
    import_packages_from_file,
    load_packages_from_file,
    store_stats,
    store_stats_batch,
    store_daily_downloads,
    get_daily_downloads,
    get_latest_stats,
    get_package_history,
    get_all_history,
    get_database_stats,
    get_next_update_seconds,
    cleanup_orphaned_stats,
    prune_old_stats,
    PackageStatsService,
)
from pkgdb.github import (
    get_cached_repo_data,
    store_cached_repo_data,
    clear_github_cache,
    get_github_cache_stats,
    _ensure_cache_table,
)


class TestDatabaseOperations:
    """Tests for database initialization and operations."""

    def test_get_db_connection_creates_connection(self, temp_db):
        """get_db_connection should return a working connection."""
        conn = get_db_connection(temp_db)
        assert conn is not None
        assert isinstance(conn, sqlite3.Connection)
        conn.close()

    def test_get_db_connection_uses_row_factory(self, temp_db):
        """get_db_connection should set row_factory to sqlite3.Row."""
        conn = get_db_connection(temp_db)
        assert conn.row_factory == sqlite3.Row
        conn.close()

    def test_init_db_creates_table(self, temp_db):
        """init_db should create the package_stats table."""
        conn = get_db_connection(temp_db)
        init_db(conn)

        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='package_stats'"
        )
        result = cursor.fetchone()
        assert result is not None
        assert result["name"] == "package_stats"
        conn.close()

    def test_init_db_creates_indexes(self, temp_db):
        """init_db should create indexes on package_name and fetch_date."""
        conn = get_db_connection(temp_db)
        init_db(conn)

        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        indexes = {row["name"] for row in cursor.fetchall()}
        assert "idx_package_name" in indexes
        assert "idx_fetch_date" in indexes
        conn.close()

    def test_init_db_idempotent(self, temp_db):
        """init_db should be safe to call multiple times."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        init_db(conn)  # Should not raise

        cursor = conn.execute(
            "SELECT COUNT(*) as count FROM sqlite_master WHERE type='table' AND name='package_stats'"
        )
        assert cursor.fetchone()["count"] == 1
        conn.close()

    def test_init_db_creates_packages_table(self, temp_db):
        """init_db should create the packages table."""
        conn = get_db_connection(temp_db)
        init_db(conn)

        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='packages'"
        )
        result = cursor.fetchone()
        assert result is not None
        assert result["name"] == "packages"
        conn.close()

    def test_get_db_context_manager(self, temp_db):
        """get_db should provide a context manager that auto-initializes and closes."""
        with get_db(temp_db) as conn:
            # Should be initialized - tables should exist
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='package_stats'"
            )
            assert cursor.fetchone() is not None

            # Should be usable
            add_package(conn, "test-package")
            packages = get_packages(conn)
            assert "test-package" in packages

        # Connection should be closed after context
        # Verify by trying to use it (should fail)
        import sqlite3

        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_get_db_closes_on_exception(self, temp_db):
        """get_db should close connection even when exception occurs."""
        conn_ref = None
        try:
            with get_db(temp_db) as conn:
                conn_ref = conn
                raise ValueError("Test exception")
        except ValueError:
            pass

        # Connection should be closed
        import sqlite3

        with pytest.raises(sqlite3.ProgrammingError):
            conn_ref.execute("SELECT 1")


class TestPackageManagement:
    """Tests for package management functions."""

    def test_add_package_success(self, db_conn):
        """add_package should insert a package and return True."""
        result = add_package(db_conn, "test-package")
        assert result is True

        cursor = db_conn.execute(
            "SELECT package_name FROM packages WHERE package_name = ?",
            ("test-package",),
        )
        row = cursor.fetchone()
        assert row is not None
        assert row["package_name"] == "test-package"

    def test_add_package_duplicate(self, db_conn):
        """add_package should return False for duplicate package."""
        add_package(db_conn, "test-package")
        result = add_package(db_conn, "test-package")
        assert result is False

        cursor = db_conn.execute(
            "SELECT COUNT(*) as count FROM packages WHERE package_name = ?",
            ("test-package",),
        )
        assert cursor.fetchone()["count"] == 1

    def test_remove_package_success(self, db_conn):
        """remove_package should delete a package and return True."""
        add_package(db_conn, "test-package")
        result = remove_package(db_conn, "test-package")
        assert result is True

        cursor = db_conn.execute(
            "SELECT COUNT(*) as count FROM packages WHERE package_name = ?",
            ("test-package",),
        )
        assert cursor.fetchone()["count"] == 0

    def test_remove_package_not_found(self, db_conn):
        """remove_package should return False if package doesn't exist."""
        result = remove_package(db_conn, "nonexistent")
        assert result is False

    def test_get_packages_empty(self, db_conn):
        """get_packages should return empty list when no packages."""
        packages = get_packages(db_conn)
        assert packages == []

    def test_get_packages_returns_list(self, db_conn):
        """get_packages should return list of package names."""
        add_package(db_conn, "package-b")
        add_package(db_conn, "package-a")
        add_package(db_conn, "package-c")

        packages = get_packages(db_conn)
        assert packages == ["package-a", "package-b", "package-c"]  # Sorted

    def test_import_packages_from_json(self, db_conn, temp_packages_file):
        """import_packages_from_file should import packages from JSON."""
        added, skipped = import_packages_from_file(db_conn, temp_packages_file)
        assert added == 2
        assert skipped == 0

        packages = get_packages(db_conn)
        assert "package-a" in packages
        assert "package-b" in packages

    def test_import_packages_skips_duplicates(self, db_conn, temp_packages_file):
        """import_packages_from_file should skip existing packages."""
        add_package(db_conn, "package-a")

        added, skipped = import_packages_from_file(db_conn, temp_packages_file)
        assert added == 1
        assert skipped == 1

    def test_load_packages_from_text_file(self):
        """load_packages_from_file should parse plain text files."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("requests\n")
            f.write("flask\n")
            f.write("# this is a comment\n")
            f.write("  django  \n")
            f.write("\n")  # empty line
            path = f.name

        try:
            packages = load_packages_from_file(path)
            assert packages == ["requests", "flask", "django"]
        finally:
            Path(path).unlink(missing_ok=True)

    def test_load_packages_from_json_list(self):
        """load_packages_from_file should parse JSON array."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(["requests", "flask"], f)
            path = f.name

        try:
            packages = load_packages_from_file(path)
            assert packages == ["requests", "flask"]
        finally:
            Path(path).unlink(missing_ok=True)

    def test_load_packages_from_json_object(self):
        """load_packages_from_file should parse JSON object with packages key."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"packages": ["requests", "flask"]}, f)
            path = f.name

        try:
            packages = load_packages_from_file(path)
            assert packages == ["requests", "flask"]
        finally:
            Path(path).unlink(missing_ok=True)


class TestStoreAndRetrieveStats:
    """Tests for storing and retrieving statistics."""

    def test_store_stats_inserts_record(self, db_conn):
        """store_stats should insert a record into the database."""
        stats = {
            "last_day": 100,
            "last_week": 700,
            "last_month": 3000,
            "total": 50000,
        }
        store_stats(db_conn, "test-package", stats)

        cursor = db_conn.execute(
            "SELECT * FROM package_stats WHERE package_name = ?",
            ("test-package",),
        )
        row = cursor.fetchone()
        assert row is not None
        assert row["package_name"] == "test-package"
        assert row["last_day"] == 100
        assert row["last_week"] == 700
        assert row["last_month"] == 3000
        assert row["total"] == 50000

    def test_store_stats_replaces_on_same_date(self, db_conn):
        """store_stats should replace stats for same package on same date."""
        stats1 = {"last_day": 100, "last_week": 700, "last_month": 3000, "total": 50000}
        stats2 = {
            "last_day": 200,
            "last_week": 1400,
            "last_month": 6000,
            "total": 60000,
        }

        store_stats(db_conn, "test-package", stats1)
        store_stats(db_conn, "test-package", stats2)

        cursor = db_conn.execute(
            "SELECT COUNT(*) as count FROM package_stats WHERE package_name = ?",
            ("test-package",),
        )
        assert cursor.fetchone()["count"] == 1

        cursor = db_conn.execute(
            "SELECT total FROM package_stats WHERE package_name = ?",
            ("test-package",),
        )
        assert cursor.fetchone()["total"] == 60000

    def test_get_latest_stats_returns_most_recent(self, db_conn):
        """get_latest_stats should return only the most recent stats per package."""
        db_conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES
            ('pkg-a', '2024-01-01', 10, 70, 300, 1000),
            ('pkg-a', '2024-01-02', 20, 140, 600, 2000),
            ('pkg-b', '2024-01-01', 5, 35, 150, 500)
        """)
        db_conn.commit()

        stats = get_latest_stats(db_conn)
        assert len(stats) == 2

        pkg_a = next(s for s in stats if s["package_name"] == "pkg-a")
        assert pkg_a["fetch_date"] == "2024-01-02"
        assert pkg_a["total"] == 2000

    def test_get_latest_stats_ordered_by_total(self, db_conn):
        """get_latest_stats should return stats ordered by total downloads DESC."""
        db_conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES
            ('low-pkg', '2024-01-01', 1, 7, 30, 100),
            ('high-pkg', '2024-01-01', 100, 700, 3000, 10000),
            ('mid-pkg', '2024-01-01', 10, 70, 300, 1000)
        """)
        db_conn.commit()

        stats = get_latest_stats(db_conn)
        assert stats[0]["package_name"] == "high-pkg"
        assert stats[1]["package_name"] == "mid-pkg"
        assert stats[2]["package_name"] == "low-pkg"

    def test_get_latest_stats_empty_db(self, db_conn):
        """get_latest_stats should return empty list for empty database."""
        stats = get_latest_stats(db_conn)
        assert stats == []


class TestHistoricalData:
    """Tests for historical data retrieval and analysis."""

    def test_get_package_history_returns_records(self, db_conn):
        """get_package_history should return historical records for a package."""
        db_conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES
            ('pkg-a', '2024-01-01', 10, 70, 300, 1000),
            ('pkg-a', '2024-01-02', 20, 140, 600, 2000),
            ('pkg-a', '2024-01-03', 30, 210, 900, 3000),
            ('pkg-b', '2024-01-01', 5, 35, 150, 500)
        """)
        db_conn.commit()

        history = get_package_history(db_conn, "pkg-a")
        assert len(history) == 3
        # Should be ordered by date descending
        assert history[0]["fetch_date"] == "2024-01-03"
        assert history[2]["fetch_date"] == "2024-01-01"

    def test_get_package_history_respects_limit(self, db_conn):
        """get_package_history should respect the limit parameter."""
        db_conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES
            ('pkg-a', '2024-01-01', 10, 70, 300, 1000),
            ('pkg-a', '2024-01-02', 20, 140, 600, 2000),
            ('pkg-a', '2024-01-03', 30, 210, 900, 3000)
        """)
        db_conn.commit()

        history = get_package_history(db_conn, "pkg-a", limit=2)
        assert len(history) == 2

    def test_get_package_history_empty_for_unknown(self, db_conn):
        """get_package_history should return empty list for unknown package."""
        history = get_package_history(db_conn, "nonexistent")
        assert history == []

    def test_get_all_history_groups_by_package(self, db_conn):
        """get_all_history should return history grouped by package name."""
        db_conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES
            ('pkg-a', '2024-01-01', 10, 70, 300, 1000),
            ('pkg-a', '2024-01-02', 20, 140, 600, 2000),
            ('pkg-b', '2024-01-01', 5, 35, 150, 500)
        """)
        db_conn.commit()

        history = get_all_history(db_conn)
        assert "pkg-a" in history
        assert "pkg-b" in history
        assert len(history["pkg-a"]) == 2
        assert len(history["pkg-b"]) == 1

    def test_get_all_history_orders_by_date_asc(self, db_conn):
        """get_all_history should order records by date ascending within each package."""
        db_conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES
            ('pkg-a', '2024-01-03', 30, 210, 900, 3000),
            ('pkg-a', '2024-01-01', 10, 70, 300, 1000),
            ('pkg-a', '2024-01-02', 20, 140, 600, 2000)
        """)
        db_conn.commit()

        history = get_all_history(db_conn)
        dates = [h["fetch_date"] for h in history["pkg-a"]]
        assert dates == ["2024-01-01", "2024-01-02", "2024-01-03"]


class TestBatchStatsStorage:
    """Tests for batch stats storage functionality."""

    def test_store_stats_batch_basic(self, db_conn):
        """store_stats_batch should store multiple packages in one transaction."""
        stats_list = [
            (
                "pkg-a",
                {"last_day": 10, "last_week": 70, "last_month": 300, "total": 1000},
            ),
            (
                "pkg-b",
                {"last_day": 20, "last_week": 140, "last_month": 600, "total": 2000},
            ),
            (
                "pkg-c",
                {"last_day": 30, "last_week": 210, "last_month": 900, "total": 3000},
            ),
        ]

        count = store_stats_batch(db_conn, stats_list)
        assert count == 3

        # Verify all packages stored
        cursor = db_conn.execute("SELECT COUNT(*) as count FROM package_stats")
        assert cursor.fetchone()["count"] == 3

        # Verify data correctness
        cursor = db_conn.execute(
            "SELECT total FROM package_stats WHERE package_name = ?", ("pkg-b",)
        )
        assert cursor.fetchone()["total"] == 2000

    def test_store_stats_batch_empty_list(self, db_conn):
        """store_stats_batch should handle empty list."""
        count = store_stats_batch(db_conn, [])
        assert count == 0

    def test_store_stats_batch_single_commit(self, db_conn):
        """store_stats_batch should use single commit (more efficient)."""
        # This is a behavioral test - batch should be faster than individual
        stats_list = [
            (
                f"pkg-{i}",
                {
                    "last_day": i,
                    "last_week": i * 7,
                    "last_month": i * 30,
                    "total": i * 100,
                },
            )
            for i in range(10)
        ]

        count = store_stats_batch(db_conn, stats_list)
        assert count == 10

        # All should be stored
        cursor = db_conn.execute("SELECT COUNT(*) as count FROM package_stats")
        assert cursor.fetchone()["count"] == 10

    def test_store_stats_with_commit_false(self, db_conn):
        """store_stats with commit=False should not auto-commit."""
        stats = {"last_day": 100, "last_week": 700, "last_month": 3000, "total": 50000}

        # Store without commit
        store_stats(db_conn, "test-pkg", stats, commit=False)

        # Should be visible in same connection
        cursor = db_conn.execute(
            "SELECT * FROM package_stats WHERE package_name = ?", ("test-pkg",)
        )
        row = cursor.fetchone()
        assert row is not None

        # Manual commit
        db_conn.commit()

    def test_service_fetch_uses_batch_commit(self, temp_db):
        """Service fetch_all_stats should use batch commits."""
        from unittest.mock import patch

        service = PackageStatsService(temp_db)
        service.add_package("pkg-a", verify=False)
        service.add_package("pkg-b", verify=False)

        recent_response = json.dumps(
            {"data": {"last_day": 100, "last_week": 700, "last_month": 3000}}
        )
        overall_response = json.dumps(
            {"data": [{"category": "without_mirrors", "downloads": 50000}]}
        )

        with mock_pypistats(recent=recent_response, overall=overall_response):
            result = service.fetch_all_stats()

        assert result.success == 2
        assert result.failed == 0
        assert result.skipped == 0

        # Both should be stored
        stats = service.get_stats()
        assert len(stats) == 2


class TestFetchAttemptTracking:
    """Tests for fetch attempt tracking functionality."""

    def test_record_fetch_attempt_success(self, temp_db):
        """record_fetch_attempt should store successful attempts."""
        conn = get_db_connection(temp_db)
        init_db(conn)

        record_fetch_attempt(conn, "test-pkg", success=True)

        cursor = conn.execute(
            "SELECT * FROM fetch_attempts WHERE package_name = ?", ("test-pkg",)
        )
        row = cursor.fetchone()
        assert row is not None
        assert row["package_name"] == "test-pkg"
        assert row["success"] == 1
        assert row["attempt_time"] is not None
        conn.close()

    def test_record_fetch_attempt_failure(self, temp_db):
        """record_fetch_attempt should store failed attempts."""
        conn = get_db_connection(temp_db)
        init_db(conn)

        record_fetch_attempt(conn, "test-pkg", success=False)

        cursor = conn.execute(
            "SELECT * FROM fetch_attempts WHERE package_name = ?", ("test-pkg",)
        )
        row = cursor.fetchone()
        assert row is not None
        assert row["success"] == 0
        conn.close()

    def test_record_fetch_attempt_updates_existing(self, temp_db):
        """record_fetch_attempt should update existing records."""
        conn = get_db_connection(temp_db)
        init_db(conn)

        # First attempt fails
        record_fetch_attempt(conn, "test-pkg", success=False)
        # Second attempt succeeds
        record_fetch_attempt(conn, "test-pkg", success=True)

        cursor = conn.execute(
            "SELECT COUNT(*) as count FROM fetch_attempts WHERE package_name = ?",
            ("test-pkg",),
        )
        assert cursor.fetchone()["count"] == 1

        cursor = conn.execute(
            "SELECT success FROM fetch_attempts WHERE package_name = ?", ("test-pkg",)
        )
        assert cursor.fetchone()["success"] == 1
        conn.close()

    def test_get_packages_needing_update_all_packages(self, temp_db):
        """get_packages_needing_update should return all packages when none have been fetched."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "pkg-a")
        add_package(conn, "pkg-b")

        packages = get_packages_needing_update(conn)
        assert set(packages) == {"pkg-a", "pkg-b"}
        conn.close()

    def test_get_packages_needing_update_excludes_recent(self, temp_db):
        """get_packages_needing_update should exclude recently fetched packages."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "pkg-a")
        add_package(conn, "pkg-b")
        add_package(conn, "pkg-c")

        # Mark pkg-a as recently fetched
        record_fetch_attempt(conn, "pkg-a", success=True)

        packages = get_packages_needing_update(conn)
        assert "pkg-a" not in packages
        assert set(packages) == {"pkg-b", "pkg-c"}
        conn.close()

    def test_get_packages_needing_update_includes_old_attempts(self, temp_db):
        """get_packages_needing_update should include packages with old attempts."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "pkg-a")

        # Insert an old attempt (25 hours ago)
        conn.execute(
            """
            INSERT INTO fetch_attempts (package_name, attempt_time, success)
            VALUES (?, datetime('now', '-25 hours'), 1)
            """,
            ("pkg-a",),
        )
        conn.commit()

        packages = get_packages_needing_update(conn)
        assert "pkg-a" in packages
        conn.close()

    def test_service_fetch_skips_recent_packages(self, temp_db):
        """Service fetch_all_stats should skip recently fetched packages."""
        from unittest.mock import patch

        service = PackageStatsService(temp_db)
        service.add_package("pkg-a", verify=False)
        service.add_package("pkg-b", verify=False)

        recent_response = json.dumps(
            {"data": {"last_day": 100, "last_week": 700, "last_month": 3000}}
        )
        overall_response = json.dumps(
            {"data": [{"category": "without_mirrors", "downloads": 50000}]}
        )

        # First fetch - both packages
        with mock_pypistats(recent=recent_response, overall=overall_response):
            result1 = service.fetch_all_stats()

        assert result1.success == 2
        assert result1.skipped == 0

        # Second fetch - both should be skipped
        with mock_pypistats(recent=recent_response, overall=overall_response):
            result2 = service.fetch_all_stats()

        assert result2.success == 0
        assert result2.skipped == 2

    def test_service_fetch_records_failed_attempts(self, temp_db):
        """Service fetch_all_stats should record failed fetch attempts."""
        from unittest.mock import patch

        service = PackageStatsService(temp_db)
        service.add_package("failing-pkg", verify=False)

        # Mock API to fail with ValueError (which is in _API_ERRORS)
        with patch("pkgdb.api.pypistats.recent", side_effect=ValueError("API error")):
            result1 = service.fetch_all_stats()

        assert result1.failed == 1
        assert result1.success == 0

        # Second fetch should retry the failed package (not skip it)
        with patch("pkgdb.api.pypistats.recent", side_effect=ValueError("API error")):
            result2 = service.fetch_all_stats()

        assert result2.failed == 1
        assert result2.skipped == 0

    def test_get_packages_needing_update_retries_after_failure(self, temp_db):
        """get_packages_needing_update should retry packages whose last attempt failed."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "pkg-a")

        # Record a recent failed attempt
        record_fetch_attempt(conn, "pkg-a", success=False)

        packages = get_packages_needing_update(conn)
        assert "pkg-a" in packages
        conn.close()

    def test_get_packages_needing_update_skips_recent_success(self, temp_db):
        """get_packages_needing_update should skip packages with recent successful fetch."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "pkg-a")

        # Record a recent successful attempt
        record_fetch_attempt(conn, "pkg-a", success=True)

        packages = get_packages_needing_update(conn)
        assert "pkg-a" not in packages
        conn.close()


class TestNextUpdateTime:
    """Tests for next update time calculation."""

    def test_get_next_update_seconds_with_recent_success(self, temp_db):
        """get_next_update_seconds should return remaining seconds when packages are throttled."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "pkg-a")
        record_fetch_attempt(conn, "pkg-a", success=True)

        seconds = get_next_update_seconds(conn)
        # Should be close to 24 hours (86400s), with some tolerance
        assert seconds is not None
        assert 86000 < seconds <= 86400
        conn.close()

    def test_get_next_update_seconds_no_attempts(self, temp_db):
        """get_next_update_seconds should return None when no packages are throttled."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "pkg-a")

        seconds = get_next_update_seconds(conn)
        assert seconds is None
        conn.close()


# Zones either side of UTC, chosen so a timestamp written in local time lands
# in the future (east) or the past (west) of SQLite's UTC clock. The POSIX
# sign convention in these names is inverted: Etc/GMT-3 is UTC+3.
@pytest.mark.skipif(
    not hasattr(time, "tzset"), reason="TZ manipulation requires a POSIX platform"
)
@pytest.mark.parametrize("tz", ["Etc/GMT-3", "Etc/GMT+5", "UTC"])
class TestFetchCooldownTimezones:
    """The 24-hour fetch cooldown must be 24 hours in every timezone.

    Attempts were once stamped with `datetime.now()` and then compared against
    SQLite's `datetime('now')`, which is UTC. That made the cooldown last
    24 hours plus the machine's UTC offset, so packages east of UTC stayed
    throttled well past a day, while packages west of it refetched early and
    walked into the PyPI rate limit the cooldown exists to avoid.
    """

    def test_attempt_is_stamped_in_utc(self, temp_db, tz, in_timezone):
        """A recorded attempt must agree with SQLite's own clock."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "pkg-a")
        record_fetch_attempt(conn, "pkg-a", success=True)

        row = conn.execute(
            "SELECT attempt_time, datetime('now') AS sqlite_now FROM fetch_attempts"
        ).fetchone()
        stored = datetime.fromisoformat(row["attempt_time"])
        sqlite_now = datetime.fromisoformat(row["sqlite_now"])

        assert abs((stored - sqlite_now).total_seconds()) < 60
        conn.close()

    def test_package_is_eligible_after_25_hours(self, temp_db, tz, in_timezone):
        """An attempt older than the window must release the package."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "pkg-a")
        _record_attempt_at(conn, "pkg-a", "-25 hours")

        assert get_packages_needing_update(conn) == ["pkg-a"]
        conn.close()

    def test_package_is_throttled_before_24_hours(self, temp_db, tz, in_timezone):
        """An attempt inside the window must keep the package throttled."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "pkg-a")
        _record_attempt_at(conn, "pkg-a", "-23 hours")

        assert get_packages_needing_update(conn) == []
        conn.close()

    def test_next_update_seconds_matches_the_gate(self, temp_db, tz, in_timezone):
        """The reported countdown must agree with when the gate actually opens.

        Both sides read the same stored timestamp, so a countdown computed
        against a different clock than the query used would report a package
        as due while `update` still skipped it.
        """
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "pkg-a")
        _record_attempt_at(conn, "pkg-a", "-23 hours")

        seconds = get_next_update_seconds(conn)
        assert seconds is not None
        # One hour left on a 23-hour-old attempt, allowing for clock skew.
        assert 3540 < seconds <= 3600
        conn.close()


def _record_attempt_at(conn, package_name: str, modifier: str) -> None:
    """Seed a successful attempt at a UTC offset from now, e.g. '-25 hours'.

    Written through SQLite so the row matches the storage contract that
    `record_fetch_attempt` follows: attempt times are UTC.
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO fetch_attempts (package_name, attempt_time, success)
        VALUES (?, datetime('now', ?), 1)
        """,
        (package_name, modifier),
    )
    conn.commit()

    def test_get_next_update_seconds_only_failed(self, temp_db):
        """get_next_update_seconds should return None when only failed attempts exist."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "pkg-a")
        record_fetch_attempt(conn, "pkg-a", success=False)

        seconds = get_next_update_seconds(conn)
        assert seconds is None
        conn.close()

    def test_fetch_result_includes_next_update(self, temp_db):
        """FetchResult should include next_update_seconds when all packages are skipped."""
        from unittest.mock import patch

        service = PackageStatsService(temp_db)
        service.add_package("pkg-a", verify=False)

        # First fetch succeeds
        recent = json.dumps(
            {"data": {"last_day": 10, "last_week": 70, "last_month": 300}}
        )
        overall = json.dumps(
            {"data": [{"category": "without_mirrors", "downloads": 5000}]}
        )
        with mock_pypistats(recent=recent, overall=overall):
            result1 = service.fetch_all_stats()

        assert result1.success == 1

        # Second fetch - all skipped, should include next_update_seconds
        result2 = service.fetch_all_stats()
        assert result2.skipped == 1
        assert result2.next_update_seconds is not None
        assert result2.next_update_seconds > 0


class TestEnvStatsCache:
    """Tests for caching Python version and OS distribution stats in SQLite."""

    def test_store_and_retrieve_python_versions(self, temp_db):
        """store_env_stats should persist Python version data retrievable by get_cached_python_versions."""
        from pkgdb import store_env_stats, get_cached_python_versions

        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "test-pkg")

        py_versions = [
            {"category": "3.12", "downloads": 500},
            {"category": "3.11", "downloads": 300},
            {"category": "3.10", "downloads": 100},
        ]
        store_env_stats(conn, "test-pkg", python_versions=py_versions)

        cached = get_cached_python_versions(conn, "test-pkg")
        assert cached is not None
        assert len(cached) == 3
        # Should be sorted by downloads descending
        assert cached[0]["category"] == "3.12"
        assert cached[0]["downloads"] == 500
        assert cached[2]["category"] == "3.10"
        conn.close()

    def test_store_and_retrieve_os_stats(self, temp_db):
        """store_env_stats should persist OS data retrievable by get_cached_os_stats."""
        from pkgdb import store_env_stats, get_cached_os_stats

        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "test-pkg")

        os_data = [
            {"category": "Linux", "downloads": 800},
            {"category": "Windows", "downloads": 400},
            {"category": "Darwin", "downloads": 200},
        ]
        store_env_stats(conn, "test-pkg", os_data=os_data)

        cached = get_cached_os_stats(conn, "test-pkg")
        assert cached is not None
        assert len(cached) == 3
        assert cached[0]["category"] == "Linux"
        assert cached[0]["downloads"] == 800
        conn.close()

    def test_cached_returns_none_when_empty(self, temp_db):
        """get_cached_* should return None when no data exists."""
        from pkgdb import get_cached_python_versions, get_cached_os_stats

        conn = get_db_connection(temp_db)
        init_db(conn)

        assert get_cached_python_versions(conn, "nonexistent") is None
        assert get_cached_os_stats(conn, "nonexistent") is None
        conn.close()

    def test_store_env_stats_none_inputs(self, temp_db):
        """store_env_stats should handle None inputs gracefully."""
        from pkgdb import (
            store_env_stats,
            get_cached_python_versions,
            get_cached_os_stats,
        )

        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "test-pkg")

        store_env_stats(conn, "test-pkg", python_versions=None, os_data=None)

        assert get_cached_python_versions(conn, "test-pkg") is None
        assert get_cached_os_stats(conn, "test-pkg") is None
        conn.close()

    def test_get_cached_env_summary_aggregates(self, temp_db):
        """get_cached_env_summary should aggregate across all packages."""
        from pkgdb import store_env_stats, get_cached_env_summary

        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "pkg-a")
        add_package(conn, "pkg-b")

        store_env_stats(
            conn,
            "pkg-a",
            python_versions=[{"category": "3.12", "downloads": 100}],
            os_data=[{"category": "Linux", "downloads": 200}],
        )
        store_env_stats(
            conn,
            "pkg-b",
            python_versions=[
                {"category": "3.12", "downloads": 50},
                {"category": "3.11", "downloads": 30},
            ],
            os_data=[
                {"category": "Linux", "downloads": 100},
                {"category": "Windows", "downloads": 80},
            ],
        )

        summary = get_cached_env_summary(conn)
        assert summary is not None

        py_dict = dict(summary["python_versions"])
        assert py_dict["3.12"] == 150
        assert py_dict["3.11"] == 30

        os_dict = dict(summary["os_distribution"])
        assert os_dict["Linux"] == 300
        assert os_dict["Windows"] == 80
        conn.close()

    def test_get_cached_env_summary_returns_none_when_empty(self, temp_db):
        """get_cached_env_summary should return None when no cached data exists."""
        from pkgdb import get_cached_env_summary

        conn = get_db_connection(temp_db)
        init_db(conn)

        assert get_cached_env_summary(conn) is None
        conn.close()

    def test_cleanup_orphaned_stats_cleans_env_tables(self, temp_db):
        """cleanup_orphaned_stats should also remove orphaned env stats."""
        from pkgdb import (
            store_env_stats,
            get_cached_python_versions,
            cleanup_orphaned_stats,
        )

        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "tracked-pkg")

        store_env_stats(
            conn,
            "tracked-pkg",
            python_versions=[{"category": "3.12", "downloads": 100}],
        )
        # Insert env data for a package that isn't tracked
        conn.execute(
            "INSERT INTO python_version_stats (package_name, fetch_date, category, downloads) VALUES (?, ?, ?, ?)",
            ("orphan-pkg", "2026-01-01", "3.12", 50),
        )
        conn.commit()

        cleanup_orphaned_stats(conn)

        assert get_cached_python_versions(conn, "tracked-pkg") is not None
        assert get_cached_python_versions(conn, "orphan-pkg") is None
        conn.close()

    def test_prune_old_stats_prunes_env_tables(self, temp_db):
        """prune_old_stats should also remove old env stats."""
        from pkgdb import store_env_stats, get_cached_python_versions, prune_old_stats

        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "test-pkg")

        # Insert old env data
        conn.execute(
            "INSERT INTO python_version_stats (package_name, fetch_date, category, downloads) VALUES (?, ?, ?, ?)",
            ("test-pkg", "2020-01-01", "3.8", 100),
        )
        # Insert recent env data
        store_env_stats(
            conn, "test-pkg", python_versions=[{"category": "3.12", "downloads": 200}]
        )

        prune_old_stats(conn, days=30)

        cached = get_cached_python_versions(conn, "test-pkg")
        assert cached is not None
        assert len(cached) == 1
        assert cached[0]["category"] == "3.12"
        conn.close()

    def test_fetch_all_stats_stores_env_data(self, temp_db):
        """fetch_all_stats should store env stats alongside download stats."""
        from unittest.mock import patch
        from pkgdb import get_cached_python_versions, get_cached_os_stats

        service = PackageStatsService(temp_db)
        service.add_package("test-pkg", verify=False)

        recent = json.dumps(
            {"data": {"last_day": 10, "last_week": 70, "last_month": 300}}
        )
        overall = json.dumps(
            {"data": [{"category": "without_mirrors", "downloads": 5000}]}
        )
        py_response = json.dumps({"data": [{"category": "3.12", "downloads": 100}]})
        os_response = json.dumps({"data": [{"category": "Linux", "downloads": 200}]})

        with patch("pkgdb.api.pypistats.recent", return_value=recent):
            with patch("pkgdb.api.pypistats.overall", return_value=overall):
                with patch(
                    "pkgdb.api.pypistats.python_minor", return_value=py_response
                ):
                    with patch("pkgdb.api.pypistats.system", return_value=os_response):
                        result = service.fetch_all_stats()

        assert result.success == 1

        conn = get_db_connection(temp_db)
        init_db(conn)
        py = get_cached_python_versions(conn, "test-pkg")
        os_data = get_cached_os_stats(conn, "test-pkg")
        assert py is not None
        assert py[0]["category"] == "3.12"
        assert os_data is not None
        assert os_data[0]["category"] == "Linux"
        conn.close()

    def test_package_report_uses_cached_env(self, temp_db):
        """generate_package_report should use cached env data instead of live API."""
        import os
        from unittest.mock import patch
        from pkgdb import store_env_stats

        service = PackageStatsService(temp_db)
        service.add_package("test-pkg", verify=False)

        # Store download stats and env data
        recent = json.dumps(
            {"data": {"last_day": 10, "last_week": 70, "last_month": 300}}
        )
        overall = json.dumps(
            {"data": [{"category": "without_mirrors", "downloads": 5000}]}
        )
        py_response = json.dumps({"data": [{"category": "3.12", "downloads": 100}]})
        os_response = json.dumps({"data": [{"category": "Linux", "downloads": 200}]})

        with patch("pkgdb.api.pypistats.recent", return_value=recent):
            with patch("pkgdb.api.pypistats.overall", return_value=overall):
                with patch(
                    "pkgdb.api.pypistats.python_minor", return_value=py_response
                ):
                    with patch("pkgdb.api.pypistats.system", return_value=os_response):
                        service.fetch_all_stats()

        # Generate report -- should NOT call python_minor or system APIs
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
            output = f.name

        with (
            patch("pkgdb.api.pypistats.python_minor") as mock_py,
            patch("pkgdb.api.pypistats.system") as mock_os,
        ):
            service.generate_package_report("test-pkg", output)
            mock_py.assert_not_called()
            mock_os.assert_not_called()

        os.unlink(output)


class TestDatabaseInfo:
    """Tests for database info/stats functionality."""

    def test_get_database_stats_empty_db(self, db_conn):
        """get_database_stats should return zeros for empty database."""
        stats = get_database_stats(db_conn)

        assert stats["package_count"] == 0
        assert stats["record_count"] == 0
        assert stats["first_fetch"] is None
        assert stats["last_fetch"] is None

    def test_get_database_stats_with_data(self, db_conn):
        """get_database_stats should return correct counts."""
        # Add packages
        add_package(db_conn, "pkg-a")
        add_package(db_conn, "pkg-b")

        # Add stats records
        db_conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES ('pkg-a', '2024-01-01', 10, 70, 300, 1000)
        """)
        db_conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES ('pkg-a', '2024-01-02', 15, 75, 310, 1015)
        """)
        db_conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES ('pkg-b', '2024-01-02', 5, 35, 150, 500)
        """)
        db_conn.commit()

        stats = get_database_stats(db_conn)

        assert stats["package_count"] == 2
        assert stats["record_count"] == 3
        assert stats["first_fetch"] == "2024-01-01"
        assert stats["last_fetch"] == "2024-01-02"

    def test_service_get_database_info(self, temp_db):
        """Service.get_database_info should return DatabaseInfo."""
        service = PackageStatsService(temp_db)
        service.add_package("test-pkg", verify=False)

        # Add a stats record
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES ('test-pkg', '2024-01-15', 10, 70, 300, 1000)
        """)
        conn.commit()
        conn.close()

        info = service.get_database_info()

        assert info["package_count"] == 1
        assert info["record_count"] == 1
        assert info["first_fetch"] == "2024-01-15"
        assert info["last_fetch"] == "2024-01-15"
        assert info["db_size_bytes"] > 0

    def test_show_info_flag_exists(self):
        """show command should have --info flag."""
        from pkgdb.cli import create_parser

        parser = create_parser()
        args = parser.parse_args(["show", "--info"])
        assert args.info is True

    def test_show_info_flag_defaults_false(self):
        """show --info should default to False."""
        from pkgdb.cli import create_parser

        parser = create_parser()
        args = parser.parse_args(["show"])
        assert getattr(args, "info", False) is False

    def test_cmd_show_with_info_flag(self, temp_db, capsys):
        """cmd_show with --info should display database info."""
        from unittest.mock import patch
        from pkgdb import main

        service = PackageStatsService(temp_db)
        service.add_package("test-pkg", verify=False)

        # Add stats
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES ('test-pkg', '2024-01-15', 10, 70, 300, 1000)
        """)
        conn.commit()
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "show", "--info"]):
            main()

        captured = capsys.readouterr()
        assert "Database Info" in captured.out
        assert "Packages:" in captured.out
        assert "Records:" in captured.out
        assert "Date range:" in captured.out


class TestGitHubCache:
    """Tests for GitHub API cache functions."""

    def test_store_and_retrieve_cached_data(self, temp_db):
        conn = get_db_connection(temp_db)
        init_db(conn)

        data = _make_github_api_response()
        store_cached_repo_data(conn, "owner", "repo", data)

        cached = get_cached_repo_data(conn, "owner", "repo")
        assert cached is not None
        assert cached["stargazers_count"] == 42
        conn.close()

    def test_cache_miss(self, temp_db):
        conn = get_db_connection(temp_db)
        init_db(conn)

        cached = get_cached_repo_data(conn, "nonexistent", "repo")
        assert cached is None
        conn.close()

    def test_cache_case_insensitive(self, temp_db):
        conn = get_db_connection(temp_db)
        init_db(conn)

        data = _make_github_api_response()
        store_cached_repo_data(conn, "Owner", "Repo", data)

        cached = get_cached_repo_data(conn, "owner", "repo")
        assert cached is not None
        conn.close()

    @pytest.mark.skipif(
        not hasattr(time, "tzset"), reason="TZ manipulation requires a POSIX platform"
    )
    @pytest.mark.parametrize("tz", ["Etc/GMT-3", "Etc/GMT+5", "UTC"])
    def test_ttl_is_24_hours_in_any_timezone(self, temp_db, tz, in_timezone):
        """Expiry is read with SQLite's UTC clock, so it must be written in UTC.

        A local-time expiry stretched the 24-hour TTL by the machine's UTC
        offset east of UTC, and cut it short west of UTC.
        """
        conn = get_db_connection(temp_db)
        init_db(conn)

        store_cached_repo_data(conn, "owner", "repo", _make_github_api_response())

        row = conn.execute(
            """SELECT expires_at, datetime('now', '+24 hours') AS expected
               FROM github_cache"""
        ).fetchone()
        expires_at = datetime.fromisoformat(row["expires_at"])
        expected = datetime.fromisoformat(row["expected"])

        assert abs((expires_at - expected).total_seconds()) < 60
        conn.close()

    def test_clear_expired_cache(self, temp_db):
        conn = get_db_connection(temp_db)
        init_db(conn)

        # Insert an already-expired entry
        _ensure_cache_table(conn)
        conn.execute(
            """INSERT INTO github_cache (repo_key, data, fetched_at, expires_at)
               VALUES (?, ?, datetime('now'), datetime('now', '-1 hour'))""",
            ("expired/repo", json.dumps({"test": True})),
        )
        conn.commit()

        cleared = clear_github_cache(conn, expired_only=True)
        assert cleared == 1
        conn.close()

    def test_clear_all_cache(self, temp_db):
        conn = get_db_connection(temp_db)
        init_db(conn)

        data = _make_github_api_response()
        store_cached_repo_data(conn, "owner1", "repo1", data)
        store_cached_repo_data(conn, "owner2", "repo2", data)

        cleared = clear_github_cache(conn, expired_only=False)
        assert cleared == 2
        conn.close()

    def test_cache_stats(self, temp_db):
        conn = get_db_connection(temp_db)
        init_db(conn)

        data = _make_github_api_response()
        store_cached_repo_data(conn, "owner", "repo", data)

        stats = get_github_cache_stats(conn)
        assert stats["total"] == 1
        assert stats["valid"] == 1
        assert stats["expired"] == 0
        conn.close()

    def test_cache_stats_empty(self, temp_db):
        conn = get_db_connection(temp_db)
        init_db(conn)

        stats = get_github_cache_stats(conn)
        assert stats["total"] == 0
        assert stats["valid"] == 0
        assert stats["expired"] == 0
        conn.close()


class TestGithubDatabaseInit:
    """Tests for GitHub cache table creation in init_db."""

    def test_init_db_creates_github_cache_table(self, temp_db):
        conn = get_db_connection(temp_db)
        init_db(conn)

        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='github_cache'"
        )
        result = cursor.fetchone()
        assert result is not None
        assert result["name"] == "github_cache"
        conn.close()


def _daily(date, dimension, category, downloads):
    """Build a single DailyDownload record."""
    return {
        "date": date,
        "dimension": dimension,
        "category": category,
        "downloads": downloads,
    }


class TestDailyDownloads:
    """Tests for the daily download time-series store."""

    def test_init_db_creates_daily_downloads_table(self, temp_db):
        conn = get_db_connection(temp_db)
        init_db(conn)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='daily_downloads'"
        )
        assert cursor.fetchone() is not None
        conn.close()

    def test_store_and_get_roundtrip(self, db_conn):
        records = [
            _daily("2026-01-01", "overall", "without_mirrors", 100),
            _daily("2026-01-02", "overall", "without_mirrors", 150),
            _daily("2026-01-01", "python", "3.12", 40),
        ]
        written = store_daily_downloads(db_conn, "pkg", records)
        assert written == 3

        overall = get_daily_downloads(db_conn, "pkg", dimension="overall")
        assert [r["date"] for r in overall] == ["2026-01-01", "2026-01-02"]
        assert [r["downloads"] for r in overall] == [100, 150]

        python = get_daily_downloads(db_conn, "pkg", dimension="python")
        assert len(python) == 1
        assert python[0]["category"] == "3.12"

    def test_upsert_is_idempotent(self, db_conn):
        """Re-storing the same day overwrites rather than duplicating."""
        store_daily_downloads(
            db_conn, "pkg", [_daily("2026-01-01", "overall", "without_mirrors", 100)]
        )
        # Same natural key, corrected count (e.g. a later fetch of the same day)
        store_daily_downloads(
            db_conn, "pkg", [_daily("2026-01-01", "overall", "without_mirrors", 120)]
        )
        rows = get_daily_downloads(db_conn, "pkg", dimension="overall")
        assert len(rows) == 1
        assert rows[0]["downloads"] == 120

    def test_store_none_and_empty_are_noops(self, db_conn):
        assert store_daily_downloads(db_conn, "pkg", None) == 0
        assert store_daily_downloads(db_conn, "pkg", []) == 0
        assert get_daily_downloads(db_conn, "pkg") == []

    def test_get_filters_by_category(self, db_conn):
        store_daily_downloads(
            db_conn,
            "pkg",
            [
                _daily("2026-01-01", "os", "Linux", 80),
                _daily("2026-01-01", "os", "Windows", 20),
            ],
        )
        linux = get_daily_downloads(db_conn, "pkg", dimension="os", category="Linux")
        assert len(linux) == 1
        assert linux[0]["downloads"] == 80

    def test_get_filters_by_since(self, db_conn):
        store_daily_downloads(
            db_conn,
            "pkg",
            [
                _daily("2026-01-01", "overall", "without_mirrors", 10),
                _daily("2026-01-05", "overall", "without_mirrors", 20),
                _daily("2026-01-10", "overall", "without_mirrors", 30),
            ],
        )
        recent = get_daily_downloads(db_conn, "pkg", since="2026-01-05")
        assert [r["date"] for r in recent] == ["2026-01-05", "2026-01-10"]

    def test_cleanup_orphaned_removes_daily(self, db_conn):
        add_package(db_conn, "tracked")
        store_daily_downloads(
            db_conn, "tracked", [_daily("2026-01-01", "overall", "without_mirrors", 5)]
        )
        store_daily_downloads(
            db_conn, "orphan", [_daily("2026-01-01", "overall", "without_mirrors", 5)]
        )
        cleanup_orphaned_stats(db_conn)
        assert get_daily_downloads(db_conn, "tracked") != []
        assert get_daily_downloads(db_conn, "orphan") == []

    def test_growth_prefers_daily_series(self, db_conn):
        """get_stats_with_growth should compute exact growth from daily data."""
        from pkgdb import store_stats, get_stats_with_growth

        add_package(db_conn, "pkg")
        # A single snapshot so the package appears in latest stats.
        store_stats(
            db_conn,
            "pkg",
            {"last_day": 10, "last_week": 70, "last_month": 300, "total": 5000},
        )
        # 14 days of daily data: last 7 days sum to 700, prior 7 to 350 -> +100%
        rows = []
        for i in range(1, 8):
            rows.append(_daily(f"2026-06-{i:02d}", "overall", "without_mirrors", 50))
        for i in range(8, 15):
            rows.append(_daily(f"2026-06-{i:02d}", "overall", "without_mirrors", 100))
        store_daily_downloads(db_conn, "pkg", rows)

        stats = get_stats_with_growth(db_conn)
        s = next(x for x in stats if x["package_name"] == "pkg")
        # current week (08-14) = 700, previous week (01-07) = 350
        assert s["week_growth"] == 100.0

    def test_prune_removes_old_daily(self, db_conn):
        from datetime import datetime

        today = datetime.now().strftime("%Y-%m-%d")
        store_daily_downloads(
            db_conn,
            "pkg",
            [
                _daily("2020-01-01", "overall", "without_mirrors", 5),
                _daily(today, "overall", "without_mirrors", 7),
            ],
        )
        prune_old_stats(db_conn, days=30)
        remaining = get_daily_downloads(db_conn, "pkg")
        assert [r["date"] for r in remaining] == [today]


class TestPackageTags:
    """Tests for package tagging / grouping."""

    def test_add_and_get_tags(self, db_conn):
        from pkgdb import add_package_tag, get_package_tags

        add_package(db_conn, "pkg")
        assert add_package_tag(db_conn, "pkg", "web") is True
        assert add_package_tag(db_conn, "pkg", "cli") is True
        assert get_package_tags(db_conn, "pkg") == ["cli", "web"]  # sorted

    def test_tags_normalized(self, db_conn):
        from pkgdb import add_package_tag, get_package_tags

        add_package(db_conn, "pkg")
        add_package_tag(db_conn, "pkg", "  Web  ")
        assert get_package_tags(db_conn, "pkg") == ["web"]

    def test_duplicate_and_empty_tag(self, db_conn):
        from pkgdb import add_package_tag

        add_package(db_conn, "pkg")
        assert add_package_tag(db_conn, "pkg", "web") is True
        assert add_package_tag(db_conn, "pkg", "WEB") is False  # normalized dup
        assert add_package_tag(db_conn, "pkg", "   ") is False  # empty

    def test_remove_tag(self, db_conn):
        from pkgdb import add_package_tag, remove_package_tag, get_package_tags

        add_package(db_conn, "pkg")
        add_package_tag(db_conn, "pkg", "web")
        assert remove_package_tag(db_conn, "pkg", "web") is True
        assert remove_package_tag(db_conn, "pkg", "web") is False
        assert get_package_tags(db_conn, "pkg") == []

    def test_get_packages_for_tag_and_map(self, db_conn):
        from pkgdb import add_package_tag, get_packages_for_tag, get_tags_map

        for p in ("a", "b", "c"):
            add_package(db_conn, p)
        add_package_tag(db_conn, "a", "web")
        add_package_tag(db_conn, "b", "web")
        add_package_tag(db_conn, "c", "cli")
        assert get_packages_for_tag(db_conn, "web") == ["a", "b"]
        assert get_tags_map(db_conn) == {"cli": ["c"], "web": ["a", "b"]}

    def test_remove_package_cascades_tags(self, db_conn):
        from pkgdb import add_package_tag, get_packages_for_tag

        add_package(db_conn, "pkg")
        add_package_tag(db_conn, "pkg", "web")
        remove_package(db_conn, "pkg")
        assert get_packages_for_tag(db_conn, "web") == []

    def test_cleanup_orphaned_removes_tags(self, db_conn):
        from pkgdb import add_package_tag, get_packages_for_tag

        add_package(db_conn, "tracked")
        add_package_tag(db_conn, "tracked", "web")
        # Orphan tag row (package not in packages table)
        db_conn.execute(
            "INSERT INTO package_tags (package_name, tag) VALUES ('orphan', 'web')"
        )
        db_conn.commit()
        cleanup_orphaned_stats(db_conn)
        assert get_packages_for_tag(db_conn, "web") == ["tracked"]


class TestGithubStatsHistory:
    """Tests for the GitHub metrics time-series store."""

    def test_init_creates_table(self, temp_db):
        conn = get_db_connection(temp_db)
        init_db(conn)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='github_stats_history'"
        )
        assert cur.fetchone() is not None
        conn.close()

    def test_store_and_get(self, db_conn):
        from pkgdb import store_github_stats_snapshot, get_github_stats_history

        add_package(db_conn, "pkg")
        store_github_stats_snapshot(db_conn, "pkg", "o/r", 100, 10, 3, 20)
        rows = get_github_stats_history(db_conn, "pkg")
        assert len(rows) == 1
        assert rows[0]["stars"] == 100
        assert rows[0]["forks"] == 10
        assert rows[0]["open_issues"] == 3
        assert rows[0]["watchers"] == 20

    def test_same_day_upsert(self, db_conn):
        from pkgdb import store_github_stats_snapshot, get_github_stats_history

        add_package(db_conn, "pkg")
        store_github_stats_snapshot(db_conn, "pkg", "o/r", 100, 10, 3, 20)
        store_github_stats_snapshot(db_conn, "pkg", "o/r", 105, 11, 2, 21)
        rows = get_github_stats_history(db_conn, "pkg")
        assert len(rows) == 1  # same date -> updated, not duplicated
        assert rows[0]["stars"] == 105

    def test_get_since_filter(self, db_conn):
        from pkgdb import get_github_stats_history

        db_conn.executemany(
            "INSERT INTO github_stats_history "
            "(package_name, repo_key, date, stars, forks, open_issues, watchers) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("pkg", "o/r", "2026-06-01", 100, 1, 1, 1),
                ("pkg", "o/r", "2026-06-05", 110, 1, 1, 1),
                ("pkg", "o/r", "2026-06-10", 120, 1, 1, 1),
            ],
        )
        db_conn.commit()
        rows = get_github_stats_history(db_conn, "pkg", since="2026-06-05")
        assert [r["date"] for r in rows] == ["2026-06-05", "2026-06-10"]

    def test_cleanup_orphaned_removes_history(self, db_conn):
        from pkgdb import store_github_stats_snapshot, get_github_stats_history

        add_package(db_conn, "tracked")
        store_github_stats_snapshot(db_conn, "tracked", "o/r", 1, 1, 1, 1)
        store_github_stats_snapshot(db_conn, "orphan", "o/x", 1, 1, 1, 1)
        cleanup_orphaned_stats(db_conn)
        assert get_github_stats_history(db_conn, "tracked") != []
        assert get_github_stats_history(db_conn, "orphan") == []

    def test_store_and_get_issues_excluding_prs(self, db_conn):
        from pkgdb import store_github_stats_snapshot, get_github_stats_history

        add_package(db_conn, "pkg")
        store_github_stats_snapshot(
            db_conn, "pkg", "o/r", 100, 10, 9, 20, open_issues_excl_prs=4
        )
        rows = get_github_stats_history(db_conn, "pkg")
        assert rows[0]["open_issues"] == 9  # GitHub's PR-inclusive figure
        assert rows[0]["open_issues_excl_prs"] == 4

    def test_issues_excluding_prs_defaults_to_unknown(self, db_conn):
        """An unavailable count is recorded as NULL, not as zero."""
        from pkgdb import store_github_stats_snapshot, get_github_stats_history

        add_package(db_conn, "pkg")
        store_github_stats_snapshot(db_conn, "pkg", "o/r", 100, 10, 9, 20)
        rows = get_github_stats_history(db_conn, "pkg")
        assert rows[0]["open_issues_excl_prs"] is None

    def test_migration_adds_column_to_existing_database(self, tmp_path):
        """A database predating the column gains it, keeping its rows."""
        import sqlite3

        from pkgdb import get_db_connection, get_github_stats_history, init_db

        db_path = str(tmp_path / "old.db")
        old = sqlite3.connect(db_path)
        old.execute("""
            CREATE TABLE github_stats_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                package_name TEXT NOT NULL,
                repo_key TEXT NOT NULL,
                date TEXT NOT NULL,
                stars INTEGER NOT NULL,
                forks INTEGER NOT NULL,
                open_issues INTEGER NOT NULL,
                watchers INTEGER NOT NULL,
                UNIQUE(package_name, date)
            )
        """)
        old.execute(
            "INSERT INTO github_stats_history "
            "(package_name, repo_key, date, stars, forks, open_issues, watchers) "
            "VALUES ('pkg', 'o/r', '2026-06-01', 100, 10, 9, 20)"
        )
        old.commit()
        old.close()

        conn = get_db_connection(db_path)
        init_db(conn)
        rows = get_github_stats_history(conn, "pkg")
        assert len(rows) == 1
        assert rows[0]["open_issues"] == 9
        assert rows[0]["open_issues_excl_prs"] is None
        conn.close()

    def test_migration_is_idempotent(self, db_conn):
        """init_db runs on every connection, so re-running must be a no-op."""
        from pkgdb import init_db

        init_db(db_conn)
        init_db(db_conn)
        cols = {
            row[1] for row in db_conn.execute("PRAGMA table_info(github_stats_history)")
        }
        assert "open_issues_excl_prs" in cols

    def test_prune_removes_old_history(self, db_conn):
        from datetime import datetime
        from pkgdb import get_github_stats_history

        today = datetime.now().strftime("%Y-%m-%d")
        db_conn.executemany(
            "INSERT INTO github_stats_history "
            "(package_name, repo_key, date, stars, forks, open_issues, watchers) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("pkg", "o/r", "2020-01-01", 1, 1, 1, 1),
                ("pkg", "o/r", today, 2, 1, 1, 1),
            ],
        )
        db_conn.commit()
        prune_old_stats(db_conn, days=30)
        rows = get_github_stats_history(db_conn, "pkg")
        assert [r["date"] for r in rows] == [today]


def _make_github_api_response(**overrides):
    """Helper to create a mock GitHub API response dict."""
    defaults = {
        "owner": {"login": "testowner"},
        "name": "testrepo",
        "full_name": "testowner/testrepo",
        "description": "A test repo",
        "stargazers_count": 42,
        "forks_count": 5,
        "open_issues_count": 3,
        "subscribers_count": 10,
        "language": "Python",
        "license": {"spdx_id": "MIT", "name": "MIT License"},
        "created_at": "2023-01-01T00:00:00Z",
        "updated_at": "2024-06-01T00:00:00Z",
        "pushed_at": "2024-06-01T00:00:00Z",
        "archived": False,
        "fork": False,
        "default_branch": "main",
        "topics": ["python", "testing"],
        "homepage": "https://example.com",
    }
    defaults.update(overrides)
    return defaults


class TestMaintenanceReporting:
    """Counts must describe the whole schema, not just `package_stats`.

    Most of a mature database is the daily series and the GitHub history, so
    reporting the snapshot table alone understates both how much is stored and
    how much a maintenance command removed.
    """

    def _seed(self, db_conn):
        """Track one package and orphan another, both with data in every table."""
        from pkgdb import store_daily_downloads, store_github_stats_snapshot

        add_package(db_conn, "kept")
        for pkg in ("kept", "orphan"):
            db_conn.execute(
                "INSERT INTO package_stats (package_name, fetch_date, last_day,"
                " last_week, last_month, total) VALUES (?, '2024-01-01', 1, 1, 1, 1)",
                (pkg,),
            )
            store_daily_downloads(
                db_conn,
                pkg,
                [
                    {
                        "date": f"2024-01-0{i}",
                        "dimension": "overall",
                        "category": "without_mirrors",
                        "downloads": 10,
                    }
                    for i in range(1, 4)
                ],
            )
            store_github_stats_snapshot(db_conn, pkg, "o/r", 1, 1, 1, 1)
        db_conn.commit()

    def test_database_stats_counts_every_history_table(self, db_conn):
        """record_count should cover daily and GitHub rows, not just snapshots."""
        self._seed(db_conn)

        stats = get_database_stats(db_conn)
        assert stats["snapshot_records"] == 2
        assert stats["daily_records"] == 6
        assert stats["github_history_records"] == 2
        assert stats["record_count"] == 10

    def test_database_stats_date_range_spans_daily_series(self, db_conn):
        """The daily series reaches back further than the snapshot table."""
        from pkgdb import store_daily_downloads

        add_package(db_conn, "pkg")
        db_conn.execute(
            "INSERT INTO package_stats (package_name, fetch_date, last_day,"
            " last_week, last_month, total) VALUES ('pkg', '2024-06-01', 1, 1, 1, 1)"
        )
        store_daily_downloads(
            db_conn,
            "pkg",
            [
                {
                    "date": "2024-01-15",
                    "dimension": "overall",
                    "category": "without_mirrors",
                    "downloads": 10,
                }
            ],
        )
        db_conn.commit()

        stats = get_database_stats(db_conn)
        assert stats["first_fetch"] == "2024-01-15"
        assert stats["last_fetch"] == "2024-06-01"

    def test_cleanup_reports_every_table_it_touched(self, db_conn):
        """The return value must account for all of the orphan's rows."""
        self._seed(db_conn)

        deleted = cleanup_orphaned_stats(db_conn)
        assert deleted["package_stats"] == 1
        assert deleted["daily_downloads"] == 3
        assert deleted["github_stats_history"] == 1
        assert deleted["total"] == 5

        # The tracked package keeps everything.
        assert get_database_stats(db_conn)["record_count"] == 5

    def test_cleanup_clears_milestone_state(self, db_conn):
        """A re-added package should not inherit a stale high-water mark."""
        from pkgdb import get_milestone_high_water, set_milestone_high_water

        add_package(db_conn, "kept")
        set_milestone_high_water(db_conn, "kept", 500)
        set_milestone_high_water(db_conn, "orphan", 900)

        cleanup_orphaned_stats(db_conn)

        assert get_milestone_high_water(db_conn, "kept") == 500
        assert get_milestone_high_water(db_conn, "orphan") is None

    def test_prune_reports_every_table_it_touched(self, db_conn):
        """Pruning is dominated by the daily series, so it must be counted."""
        from pkgdb import store_daily_downloads

        add_package(db_conn, "pkg")
        db_conn.execute(
            "INSERT INTO package_stats (package_name, fetch_date, last_day,"
            " last_week, last_month, total) VALUES ('pkg', '2000-01-01', 1, 1, 1, 1)"
        )
        store_daily_downloads(
            db_conn,
            "pkg",
            [
                {
                    "date": f"2000-01-0{i}",
                    "dimension": "overall",
                    "category": "without_mirrors",
                    "downloads": 10,
                }
                for i in range(1, 5)
            ],
        )
        db_conn.commit()

        deleted = prune_old_stats(db_conn, days=30)
        assert deleted["package_stats"] == 1
        assert deleted["daily_downloads"] == 4
        assert deleted["total"] == 5

    # Rows are dated on the local calendar, so the cutoff has to be too. These
    # two zones sit far enough either side of UTC that at any given instant at
    # least one of them is on a different calendar date than UTC, which is what
    # makes a UTC cutoff fail here whatever time the suite runs.
    @pytest.mark.skipif(
        not hasattr(time, "tzset"), reason="TZ manipulation requires a POSIX platform"
    )
    @pytest.mark.parametrize("tz", ["Etc/GMT-14", "Etc/GMT+11"])
    def test_prune_boundary_uses_the_local_calendar(self, db_conn, tz, in_timezone):
        """A row exactly `days` old survives; the day before it does not."""
        today = datetime.now().date()
        boundary = (today - timedelta(days=30)).strftime("%Y-%m-%d")
        day_before = (today - timedelta(days=31)).strftime("%Y-%m-%d")
        track(db_conn, "pkg-a")
        db_conn.executemany(
            "INSERT INTO package_stats (package_name, fetch_date, total) VALUES (?, ?, ?)",
            [("pkg-a", boundary, 100), ("pkg-a", day_before, 90)],
        )
        db_conn.commit()

        deleted = prune_old_stats(db_conn, days=30)

        assert deleted["package_stats"] == 1
        remaining = db_conn.execute(
            "SELECT fetch_date FROM package_stats ORDER BY fetch_date"
        ).fetchall()
        assert [r["fetch_date"] for r in remaining] == [boundary]
