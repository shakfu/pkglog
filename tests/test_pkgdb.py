"""Tests for pkgdb - PyPI package download statistics tracker."""

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from pkgdb import (
    get_db_connection,
    get_db,
    init_db,
    load_packages,
    add_package,
    remove_package,
    get_packages,
    import_packages_from_file,
    load_packages_from_file,
    store_stats,
    get_latest_stats,
    get_package_history,
    get_all_history,
    calculate_growth,
    make_sparkline,
    export_csv,
    export_json,
    export_markdown,
    fetch_package_stats,
    fetch_python_versions,
    fetch_os_stats,
    aggregate_env_stats,
    make_svg_pie_chart,
    generate_html_report,
    generate_package_html_report,
    main,
    get_config_dir,
    DEFAULT_DB_FILE,
    DEFAULT_PACKAGES_FILE,
    DEFAULT_REPORT_FILE,
    PackageStatsService,
    PackageInfo,
    FetchResult,
    PackageDetails,
    validate_package_name,
)


@pytest.fixture
def temp_db():
    """Create a temporary database file."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture
def temp_packages_file():
    """Create a temporary packages.yml file."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yml", delete=False
    ) as f:
        yaml.dump({"published": ["package-a", "package-b"]}, f)
        packages_path = f.name
    yield packages_path
    Path(packages_path).unlink(missing_ok=True)


@pytest.fixture
def db_conn(temp_db):
    """Create an initialized database connection."""
    conn = get_db_connection(temp_db)
    init_db(conn)
    yield conn
    conn.close()


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

        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
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
            ("test-package",)
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
            ("test-package",)
        )
        assert cursor.fetchone()["count"] == 1

    def test_remove_package_success(self, db_conn):
        """remove_package should delete a package and return True."""
        add_package(db_conn, "test-package")
        result = remove_package(db_conn, "test-package")
        assert result is True

        cursor = db_conn.execute(
            "SELECT COUNT(*) as count FROM packages WHERE package_name = ?",
            ("test-package",)
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

    def test_import_packages_from_yaml(self, db_conn, temp_packages_file):
        """import_packages_from_file should import packages from YAML."""
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


class TestLoadPackages:
    """Tests for loading packages from YAML."""

    def test_load_packages_returns_list(self, temp_packages_file):
        """load_packages should return a list of package names."""
        packages = load_packages(temp_packages_file)
        assert isinstance(packages, list)
        assert packages == ["package-a", "package-b"]

    def test_load_packages_empty_published(self):
        """load_packages should return empty list if published key is missing."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yml", delete=False
        ) as f:
            yaml.dump({"other_key": ["something"]}, f)
            path = f.name

        try:
            packages = load_packages(path)
            assert packages == [] or packages is None
        finally:
            Path(path).unlink(missing_ok=True)

    def test_load_packages_file_not_found(self):
        """load_packages should raise FileNotFoundError for missing files."""
        with pytest.raises(FileNotFoundError):
            load_packages("/nonexistent/packages.yml")


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
        stats2 = {"last_day": 200, "last_week": 1400, "last_month": 6000, "total": 60000}

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


class TestExportFormats:
    """Tests for export format functions."""

    @pytest.fixture
    def sample_stats(self):
        """Sample stats for export tests."""
        return [
            {"package_name": "pkg-a", "total": 10000, "last_month": 3000, "last_week": 700, "last_day": 100, "fetch_date": "2024-01-15"},
            {"package_name": "pkg-b", "total": 5000, "last_month": 1500, "last_week": 350, "last_day": 50, "fetch_date": "2024-01-15"},
        ]

    def test_export_csv_format(self, sample_stats):
        """export_csv should produce valid CSV output."""
        output = export_csv(sample_stats)

        # Check header
        assert "rank,package_name,total,last_month,last_week,last_day,fetch_date" in output

        # Check data rows
        assert "1,pkg-a,10000,3000,700,100,2024-01-15" in output
        assert "2,pkg-b,5000,1500,350,50,2024-01-15" in output

    def test_export_csv_empty_stats(self):
        """export_csv should handle empty stats."""
        output = export_csv([])
        # Should have header row
        assert "rank,package_name" in output
        # Should not have data rows beyond header
        assert output.count("pkg") == 0

    def test_export_json_format(self, sample_stats):
        """export_json should produce valid JSON output."""
        output = export_json(sample_stats)
        data = json.loads(output)

        assert "generated" in data
        assert "packages" in data
        assert len(data["packages"]) == 2

        pkg_a = data["packages"][0]
        assert pkg_a["rank"] == 1
        assert pkg_a["name"] == "pkg-a"
        assert pkg_a["total"] == 10000

    def test_export_json_empty_stats(self):
        """export_json should handle empty stats."""
        output = export_json([])
        data = json.loads(output)
        assert data["packages"] == []

    def test_export_markdown_format(self, sample_stats):
        """export_markdown should produce valid Markdown table."""
        output = export_markdown(sample_stats)
        lines = output.split("\n")

        # Check header
        assert "| Rank | Package | Total | Month | Week | Day |" in lines[0]
        assert "|------|---------|" in lines[1]

        # Check data rows contain package names
        assert "pkg-a" in output
        assert "pkg-b" in output

    def test_export_markdown_empty_stats(self):
        """export_markdown should handle empty stats."""
        output = export_markdown([])
        lines = output.split("\n")
        assert len(lines) == 2  # Just header and separator


class TestFetchPackageStats:
    """Tests for fetching stats from PyPI API."""

    def test_fetch_package_stats_parses_response(self):
        """fetch_package_stats should parse pypistats responses correctly."""
        recent_response = json.dumps({
            "data": {"last_day": 100, "last_week": 700, "last_month": 3000}
        })
        overall_response = json.dumps({
            "data": [
                {"category": "with_mirrors", "downloads": 100000},
                {"category": "without_mirrors", "downloads": 50000},
            ]
        })

        with patch("pkgdb.api.pypistats.recent", return_value=recent_response):
            with patch("pkgdb.api.pypistats.overall", return_value=overall_response):
                stats = fetch_package_stats("test-package")

        assert stats["last_day"] == 100
        assert stats["last_week"] == 700
        assert stats["last_month"] == 3000
        assert stats["total"] == 50000

    def test_fetch_package_stats_handles_error(self, caplog):
        """fetch_package_stats should return None and log error on failure."""
        with patch("pkgdb.api.pypistats.recent", side_effect=ValueError("API error")):
            stats = fetch_package_stats("nonexistent-package")

        assert stats is None
        assert "Error fetching stats" in caplog.text

    def test_fetch_python_versions_parses_response(self):
        """fetch_python_versions should parse pypistats response correctly."""
        mock_response = json.dumps({
            "data": [
                {"category": "3.10", "downloads": 1000},
                {"category": "3.11", "downloads": 2000},
                {"category": "3.9", "downloads": 500},
            ]
        })

        with patch("pkgdb.api.pypistats.python_minor", return_value=mock_response):
            versions = fetch_python_versions("test-package")

        assert versions is not None
        assert len(versions) == 3
        # Should be sorted by downloads descending
        assert versions[0]["category"] == "3.11"
        assert versions[0]["downloads"] == 2000

    def test_fetch_python_versions_handles_error(self, capsys):
        """fetch_python_versions should return None on error."""
        with patch("pkgdb.api.pypistats.python_minor", side_effect=ValueError("API error")):
            versions = fetch_python_versions("nonexistent-package")

        assert versions is None

    def test_fetch_os_stats_parses_response(self):
        """fetch_os_stats should parse pypistats response correctly."""
        mock_response = json.dumps({
            "data": [
                {"category": "Linux", "downloads": 5000},
                {"category": "Windows", "downloads": 2000},
                {"category": "Darwin", "downloads": 1000},
            ]
        })

        with patch("pkgdb.api.pypistats.system", return_value=mock_response):
            os_stats = fetch_os_stats("test-package")

        assert os_stats is not None
        assert len(os_stats) == 3
        # Should be sorted by downloads descending
        assert os_stats[0]["category"] == "Linux"
        assert os_stats[0]["downloads"] == 5000

    def test_fetch_os_stats_handles_error(self, capsys):
        """fetch_os_stats should return None on error."""
        with patch("pkgdb.api.pypistats.system", side_effect=ValueError("API error")):
            os_stats = fetch_os_stats("nonexistent-package")

        assert os_stats is None


class TestHTMLReportGeneration:
    """Tests for HTML report generation."""

    def test_generate_html_report_creates_file(self):
        """generate_html_report should create a self-contained HTML file with SVG."""
        stats = [
            {
                "package_name": "test-pkg",
                "total": 1000,
                "last_month": 300,
                "last_week": 70,
                "last_day": 10,
            }
        ]

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False
        ) as f:
            output_path = f.name

        try:
            generate_html_report(stats, output_path)
            assert Path(output_path).exists()

            content = Path(output_path).read_text()
            assert "<!DOCTYPE html>" in content
            assert "test-pkg" in content
            assert "<svg" in content
            assert "cdn" not in content.lower()
        finally:
            Path(output_path).unlink(missing_ok=True)

    def test_generate_html_report_includes_all_packages(self):
        """generate_html_report should include all packages in the report."""
        stats = [
            {"package_name": "pkg-a", "total": 1000, "last_month": 300, "last_week": 70, "last_day": 10},
            {"package_name": "pkg-b", "total": 500, "last_month": 150, "last_week": 35, "last_day": 5},
        ]

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False
        ) as f:
            output_path = f.name

        try:
            generate_html_report(stats, output_path)
            content = Path(output_path).read_text()
            assert "pkg-a" in content
            assert "pkg-b" in content
        finally:
            Path(output_path).unlink(missing_ok=True)

    def test_generate_html_report_empty_stats(self, caplog):
        """generate_html_report should handle empty stats gracefully."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False
        ) as f:
            output_path = f.name

        try:
            generate_html_report([], output_path)
            assert "No statistics available" in caplog.text
        finally:
            Path(output_path).unlink(missing_ok=True)

    def test_generate_html_report_with_history(self):
        """generate_html_report should include time-series chart when history provided."""
        stats = [
            {"package_name": "pkg-a", "total": 3000, "last_month": 900, "last_week": 210, "last_day": 30},
        ]
        history = {
            "pkg-a": [
                {"package_name": "pkg-a", "fetch_date": "2024-01-01", "total": 1000, "last_month": 300, "last_week": 70, "last_day": 10},
                {"package_name": "pkg-a", "fetch_date": "2024-01-02", "total": 2000, "last_month": 600, "last_week": 140, "last_day": 20},
                {"package_name": "pkg-a", "fetch_date": "2024-01-03", "total": 3000, "last_month": 900, "last_week": 210, "last_day": 30},
            ]
        }

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False
        ) as f:
            output_path = f.name

        try:
            generate_html_report(stats, output_path, history)
            content = Path(output_path).read_text()
            assert "Downloads Over Time" in content
            assert "time-series-chart" in content
            assert "polyline" in content  # SVG line element
        finally:
            Path(output_path).unlink(missing_ok=True)


class TestCLI:
    """Tests for CLI argument parsing and commands."""

    def test_default_values(self):
        """Default values should be set correctly."""
        config_dir = get_config_dir()
        assert DEFAULT_DB_FILE == str(config_dir / "pkg.db")
        assert DEFAULT_PACKAGES_FILE == "packages.yml"
        assert DEFAULT_REPORT_FILE == str(config_dir / "report.html")

    def test_main_no_command_shows_help(self, capsys):
        """main() with no command should print help."""
        with patch("sys.argv", ["pkgdb"]):
            main()
        captured = capsys.readouterr()
        assert "usage:" in captured.out.lower() or "Available commands" in captured.out

    def test_main_add_command(self, temp_db, caplog):
        """add command should add a package to tracking."""
        with patch("sys.argv", ["pkgdb", "-d", temp_db, "add", "requests"]):
            main()

        assert "Added" in caplog.text
        assert "requests" in caplog.text

        conn = get_db_connection(temp_db)
        packages = get_packages(conn)
        conn.close()
        assert "requests" in packages

    def test_main_add_command_duplicate(self, temp_db, caplog):
        """add command should indicate when package already tracked."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "requests")
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "add", "requests"]):
            main()

        assert "already" in caplog.text

    def test_main_remove_command(self, temp_db, caplog):
        """remove command should remove a package from tracking."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "requests")
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "remove", "requests"]):
            main()

        assert "Removed" in caplog.text
        assert "requests" in caplog.text

        conn = get_db_connection(temp_db)
        packages = get_packages(conn)
        conn.close()
        assert "requests" not in packages

    def test_main_remove_command_not_found(self, temp_db, caplog):
        """remove command should indicate when package not tracked."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "remove", "nonexistent"]):
            main()

        assert "was not" in caplog.text

    def test_main_list_command_empty(self, temp_db, caplog):
        """list command should indicate when no packages tracked."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "list"]):
            main()

        assert "No packages" in caplog.text

    def test_main_list_command_with_packages(self, temp_db, capsys, caplog):
        """list command should display tracked packages."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "requests")
        add_package(conn, "flask")
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "list"]):
            main()

        captured = capsys.readouterr()
        # Table data goes to stdout
        assert "requests" in captured.out
        assert "flask" in captured.out
        # Header message goes to logging
        assert "Tracking 2 packages" in caplog.text

    def test_main_import_command(self, temp_db, temp_packages_file, caplog):
        """import command should import packages from YAML file."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "import", temp_packages_file]):
            main()

        assert "Imported 2 packages" in caplog.text

        conn = get_db_connection(temp_db)
        packages = get_packages(conn)
        conn.close()
        assert "package-a" in packages
        assert "package-b" in packages

    def test_main_import_command_file_not_found(self, temp_db, caplog):
        """import command should handle missing file."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "import", "/nonexistent/file.yml"]):
            main()

        assert "File not found" in caplog.text

    def test_main_fetch_command(self, temp_db):
        """fetch command should fetch and store stats for tracked packages."""
        # First add packages to track
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.execute("INSERT INTO packages (package_name, added_date) VALUES ('package-a', '2024-01-01')")
        conn.execute("INSERT INTO packages (package_name, added_date) VALUES ('package-b', '2024-01-01')")
        conn.commit()
        conn.close()

        recent_response = json.dumps({
            "data": {"last_day": 100, "last_week": 700, "last_month": 3000}
        })
        overall_response = json.dumps({
            "data": [{"category": "without_mirrors", "downloads": 50000}]
        })

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "fetch"]):
            with patch("pkgdb.api.pypistats.recent", return_value=recent_response):
                with patch("pkgdb.api.pypistats.overall", return_value=overall_response):
                    main()

        conn = get_db_connection(temp_db)
        cursor = conn.execute("SELECT COUNT(*) as count FROM package_stats")
        assert cursor.fetchone()["count"] == 2
        conn.close()

    def test_main_fetch_command_no_packages(self, temp_db, caplog):
        """fetch command should prompt to add packages when none tracked."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "fetch"]):
            main()

        assert "No packages" in caplog.text
        assert "pkgdb add" in caplog.text or "pkgdb import" in caplog.text

    def test_main_show_command_empty_db(self, temp_db, caplog):
        """show command should indicate when database is empty."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "show"]):
            main()

        assert "No data" in caplog.text or "fetch" in caplog.text.lower()

    def test_main_show_command_with_data(self, temp_db, capsys):
        """show command should display stats from database."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES ('test-pkg', '2024-01-01', 10, 70, 300, 1000)
        """)
        conn.commit()
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "show"]):
            main()

        captured = capsys.readouterr()
        assert "test-pkg" in captured.out

    def test_main_report_command(self, temp_db):
        """report command should generate HTML report."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES ('test-pkg', '2024-01-01', 10, 70, 300, 1000)
        """)
        conn.commit()
        conn.close()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False
        ) as f:
            output_path = f.name

        try:
            with patch("sys.argv", ["pkgdb", "-d", temp_db, "report", "-o", output_path]):
                with patch("webbrowser.open_new_tab"):
                    main()

            assert Path(output_path).exists()
            content = Path(output_path).read_text()
            assert "test-pkg" in content
        finally:
            Path(output_path).unlink(missing_ok=True)

    def test_main_history_command(self, temp_db, capsys):
        """history command should display historical stats for a package."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES
            ('test-pkg', '2024-01-01', 10, 70, 300, 1000),
            ('test-pkg', '2024-01-02', 20, 140, 600, 2000)
        """)
        conn.commit()
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "history", "test-pkg"]):
            main()

        captured = capsys.readouterr()
        assert "test-pkg" in captured.out
        assert "2024-01-01" in captured.out
        assert "2024-01-02" in captured.out

    def test_main_history_command_unknown_package(self, temp_db, caplog):
        """history command should indicate when no data found."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "history", "nonexistent"]):
            main()

        assert "No data found" in caplog.text

    def test_main_export_csv(self, temp_db, capsys):
        """export command should output CSV to stdout."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES ('test-pkg', '2024-01-01', 10, 70, 300, 1000)
        """)
        conn.commit()
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "export", "-f", "csv"]):
            main()

        captured = capsys.readouterr()
        assert "test-pkg" in captured.out
        assert "1000" in captured.out

    def test_main_export_json(self, temp_db, capsys):
        """export command should output JSON."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES ('test-pkg', '2024-01-01', 10, 70, 300, 1000)
        """)
        conn.commit()
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "export", "-f", "json"]):
            main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["packages"][0]["name"] == "test-pkg"

    def test_main_export_markdown(self, temp_db, capsys):
        """export command should output Markdown."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES ('test-pkg', '2024-01-01', 10, 70, 300, 1000)
        """)
        conn.commit()
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "export", "-f", "markdown"]):
            main()

        captured = capsys.readouterr()
        assert "| Rank |" in captured.out
        assert "test-pkg" in captured.out

    def test_main_export_to_file(self, temp_db):
        """export command should write to file when -o specified."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES ('test-pkg', '2024-01-01', 10, 70, 300, 1000)
        """)
        conn.commit()
        conn.close()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            output_path = f.name

        try:
            with patch("sys.argv", ["pkgdb", "-d", temp_db, "export", "-f", "csv", "-o", output_path]):
                main()

            assert Path(output_path).exists()
            content = Path(output_path).read_text()
            assert "test-pkg" in content
        finally:
            Path(output_path).unlink(missing_ok=True)

    def test_main_stats_command(self, capsys):
        """stats command should display Python versions and OS breakdown."""
        recent_response = json.dumps({
            "data": {"last_day": 100, "last_week": 700, "last_month": 3000}
        })
        overall_response = json.dumps({
            "data": [{"category": "without_mirrors", "downloads": 50000}]
        })
        python_response = json.dumps({
            "data": [
                {"category": "3.11", "downloads": 2000},
                {"category": "3.10", "downloads": 1000},
            ]
        })
        system_response = json.dumps({
            "data": [
                {"category": "Linux", "downloads": 4000},
                {"category": "Windows", "downloads": 1000},
            ]
        })

        with patch("sys.argv", ["pkgdb", "stats", "test-package"]):
            with patch("pkgdb.api.pypistats.recent", return_value=recent_response):
                with patch("pkgdb.api.pypistats.overall", return_value=overall_response):
                    with patch("pkgdb.api.pypistats.python_minor", return_value=python_response):
                        with patch("pkgdb.api.pypistats.system", return_value=system_response):
                            main()

        captured = capsys.readouterr()
        assert "Download Summary" in captured.out
        assert "Python Version Distribution" in captured.out
        assert "Operating System Distribution" in captured.out
        assert "3.11" in captured.out
        assert "Linux" in captured.out

    def test_main_report_command_single_package(self, temp_db):
        """report command with package arg should generate single-package report."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES
            ('test-pkg', '2024-01-01', 10, 70, 300, 1000),
            ('test-pkg', '2024-01-02', 20, 140, 600, 2000)
        """)
        conn.commit()
        conn.close()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False
        ) as f:
            output_path = f.name

        # Mock API calls for environment data
        python_response = json.dumps({
            "data": [{"category": "3.11", "downloads": 2000}]
        })
        system_response = json.dumps({
            "data": [{"category": "Linux", "downloads": 4000}]
        })

        try:
            with patch("sys.argv", ["pkgdb", "-d", temp_db, "report", "test-pkg", "-o", output_path]):
                with patch("webbrowser.open_new_tab"):
                    with patch("pkgdb.api.pypistats.python_minor", return_value=python_response):
                        with patch("pkgdb.api.pypistats.system", return_value=system_response):
                            main()

            assert Path(output_path).exists()
            content = Path(output_path).read_text()
            # Single package report should have package name as title
            assert "test-pkg" in content
            assert "Environment Distribution" in content
            assert "Python Versions" in content
            assert "Operating Systems" in content
        finally:
            Path(output_path).unlink(missing_ok=True)

    def test_main_report_command_with_env_flag(self, temp_db):
        """report command with --env should include environment summary."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES ('test-pkg', '2024-01-01', 10, 70, 300, 1000)
        """)
        conn.commit()
        conn.close()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False
        ) as f:
            output_path = f.name

        # Mock API calls for environment data
        python_response = json.dumps({
            "data": [
                {"category": "3.11", "downloads": 2000},
                {"category": "3.10", "downloads": 1000},
            ]
        })
        system_response = json.dumps({
            "data": [
                {"category": "Linux", "downloads": 4000},
                {"category": "Windows", "downloads": 1000},
            ]
        })

        try:
            with patch("sys.argv", ["pkgdb", "-d", temp_db, "report", "-e", "-o", output_path]):
                with patch("webbrowser.open_new_tab"):
                    with patch("pkgdb.api.pypistats.python_minor", return_value=python_response):
                        with patch("pkgdb.api.pypistats.system", return_value=system_response):
                            main()

            assert Path(output_path).exists()
            content = Path(output_path).read_text()
            assert "Environment Summary" in content
            assert "py-version-chart" in content or "os-chart" in content
        finally:
            Path(output_path).unlink(missing_ok=True)

    def test_main_report_command_no_browser(self, temp_db, capsys):
        """report command with --no-browser should not open browser."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES ('test-pkg', '2024-01-01', 10, 70, 300, 1000)
        """)
        conn.commit()
        conn.close()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False
        ) as f:
            output_path = f.name

        try:
            with patch("sys.argv", ["pkgdb", "-d", temp_db, "report", "--no-browser", "-o", output_path]):
                with patch("webbrowser.open_new_tab") as mock_browser:
                    main()
                    # Browser should NOT be called
                    mock_browser.assert_not_called()

            assert Path(output_path).exists()
            captured = capsys.readouterr()
            # Should not contain "Opening" message
            assert "Opening" not in captured.out
        finally:
            Path(output_path).unlink(missing_ok=True)


class TestPieChart:
    """Tests for SVG pie chart generation."""

    def test_make_svg_pie_chart_creates_svg(self):
        """make_svg_pie_chart should create valid SVG."""
        data = [("Linux", 5000), ("Windows", 2000), ("Darwin", 1000)]
        svg = make_svg_pie_chart(data, "test-chart")

        assert "<svg" in svg
        assert "test-chart" in svg
        assert "</svg>" in svg
        assert "path" in svg  # Pie slices are paths

    def test_make_svg_pie_chart_includes_legend(self):
        """make_svg_pie_chart should include legend with percentages."""
        data = [("Linux", 5000), ("Windows", 2500), ("Darwin", 2500)]
        svg = make_svg_pie_chart(data, "test-chart")

        assert "Linux" in svg
        assert "Windows" in svg
        assert "Darwin" in svg
        assert "%" in svg  # Should show percentages

    def test_make_svg_pie_chart_empty_data(self):
        """make_svg_pie_chart should handle empty data."""
        svg = make_svg_pie_chart([], "test-chart")
        assert svg == ""

    def test_make_svg_pie_chart_zero_total(self):
        """make_svg_pie_chart should handle zero total."""
        data = [("Linux", 0), ("Windows", 0)]
        svg = make_svg_pie_chart(data, "test-chart")
        assert "No data" in svg

    def test_make_svg_pie_chart_groups_others(self):
        """make_svg_pie_chart should group items beyond top 5 as 'Other'."""
        data = [
            ("A", 100), ("B", 90), ("C", 80), ("D", 70), ("E", 60),
            ("F", 50), ("G", 40), ("H", 30),
        ]
        svg = make_svg_pie_chart(data, "test-chart")

        # Should have "Other" in legend
        assert "Other" in svg
        # Should not have all individual items beyond top 5
        assert "H" not in svg


class TestAggregateEnvStats:
    """Tests for aggregating environment stats across packages."""

    def test_aggregate_env_stats_combines_packages(self):
        """aggregate_env_stats should combine stats from multiple packages."""
        python_response_1 = json.dumps({
            "data": [{"category": "3.11", "downloads": 2000}]
        })
        python_response_2 = json.dumps({
            "data": [{"category": "3.11", "downloads": 1000}]
        })
        system_response_1 = json.dumps({
            "data": [{"category": "Linux", "downloads": 4000}]
        })
        system_response_2 = json.dumps({
            "data": [{"category": "Linux", "downloads": 2000}]
        })

        call_count = {"python": 0, "system": 0}

        def mock_python_minor(pkg, format=None):
            call_count["python"] += 1
            return python_response_1 if call_count["python"] == 1 else python_response_2

        def mock_system(pkg, format=None):
            call_count["system"] += 1
            return system_response_1 if call_count["system"] == 1 else system_response_2

        with patch("pkgdb.api.pypistats.python_minor", side_effect=mock_python_minor):
            with patch("pkgdb.api.pypistats.system", side_effect=mock_system):
                result = aggregate_env_stats(["pkg-a", "pkg-b"])

        # Should aggregate downloads
        py_versions = dict(result["python_versions"])
        assert py_versions.get("3.11") == 3000  # 2000 + 1000

        os_dist = dict(result["os_distribution"])
        assert os_dist.get("Linux") == 6000  # 4000 + 2000

    def test_aggregate_env_stats_handles_errors(self):
        """aggregate_env_stats should handle API errors gracefully."""
        with patch("pkgdb.api.pypistats.python_minor", side_effect=ValueError("API error")):
            with patch("pkgdb.api.pypistats.system", side_effect=ValueError("API error")):
                result = aggregate_env_stats(["pkg-a"])

        assert result["python_versions"] == []
        assert result["os_distribution"] == []

    def test_aggregate_env_stats_empty_packages(self):
        """aggregate_env_stats should handle empty package list."""
        result = aggregate_env_stats([])
        assert result["python_versions"] == []
        assert result["os_distribution"] == []


class TestPackageHTMLReport:
    """Tests for single-package HTML report generation."""

    def test_generate_package_html_report_creates_file(self):
        """generate_package_html_report should create HTML file."""
        stats = {"total": 1000, "last_month": 300, "last_week": 70, "last_day": 10}

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False
        ) as f:
            output_path = f.name

        python_response = json.dumps({
            "data": [{"category": "3.11", "downloads": 2000}]
        })
        system_response = json.dumps({
            "data": [{"category": "Linux", "downloads": 4000}]
        })

        try:
            with patch("pkgdb.api.pypistats.python_minor", return_value=python_response):
                with patch("pkgdb.api.pypistats.system", return_value=system_response):
                    generate_package_html_report("test-pkg", output_path, stats=stats)

            assert Path(output_path).exists()
            content = Path(output_path).read_text()
            assert "<!DOCTYPE html>" in content
            assert "test-pkg" in content
        finally:
            Path(output_path).unlink(missing_ok=True)

    def test_generate_package_html_report_includes_stats_cards(self):
        """generate_package_html_report should include download stat cards."""
        stats = {"total": 1000, "last_month": 300, "last_week": 70, "last_day": 10}

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False
        ) as f:
            output_path = f.name

        python_response = json.dumps({"data": []})
        system_response = json.dumps({"data": []})

        try:
            with patch("pkgdb.api.pypistats.python_minor", return_value=python_response):
                with patch("pkgdb.api.pypistats.system", return_value=system_response):
                    generate_package_html_report("test-pkg", output_path, stats=stats)

            content = Path(output_path).read_text()
            assert "Total Downloads" in content
            assert "Last Month" in content
            assert "Last Week" in content
            assert "Last Day" in content
            assert "1,000" in content  # Formatted total
        finally:
            Path(output_path).unlink(missing_ok=True)

    def test_generate_package_html_report_includes_env_charts(self):
        """generate_package_html_report should include environment pie charts."""
        stats = {"total": 1000, "last_month": 300, "last_week": 70, "last_day": 10}

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False
        ) as f:
            output_path = f.name

        python_response = json.dumps({
            "data": [
                {"category": "3.11", "downloads": 2000},
                {"category": "3.10", "downloads": 1000},
            ]
        })
        system_response = json.dumps({
            "data": [
                {"category": "Linux", "downloads": 4000},
                {"category": "Windows", "downloads": 1000},
            ]
        })

        try:
            with patch("pkgdb.api.pypistats.python_minor", return_value=python_response):
                with patch("pkgdb.api.pypistats.system", return_value=system_response):
                    generate_package_html_report("test-pkg", output_path, stats=stats)

            content = Path(output_path).read_text()
            assert "Environment Distribution" in content
            assert "py-version-chart" in content
            assert "os-chart" in content
        finally:
            Path(output_path).unlink(missing_ok=True)

    def test_generate_package_html_report_with_history(self):
        """generate_package_html_report should include history chart when available."""
        stats = {"total": 3000, "last_month": 900, "last_week": 210, "last_day": 30}
        history = [
            {"package_name": "test-pkg", "fetch_date": "2024-01-01", "total": 1000, "last_month": 300, "last_week": 70, "last_day": 10},
            {"package_name": "test-pkg", "fetch_date": "2024-01-02", "total": 2000, "last_month": 600, "last_week": 140, "last_day": 20},
            {"package_name": "test-pkg", "fetch_date": "2024-01-03", "total": 3000, "last_month": 900, "last_week": 210, "last_day": 30},
        ]

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False
        ) as f:
            output_path = f.name

        python_response = json.dumps({"data": []})
        system_response = json.dumps({"data": []})

        try:
            with patch("pkgdb.api.pypistats.python_minor", return_value=python_response):
                with patch("pkgdb.api.pypistats.system", return_value=system_response):
                    generate_package_html_report("test-pkg", output_path, stats=stats, history=history)

            content = Path(output_path).read_text()
            assert "Downloads Over Time" in content
            assert "polyline" in content  # SVG line element
        finally:
            Path(output_path).unlink(missing_ok=True)


class TestPackageStatsService:
    """Tests for the PackageStatsService abstraction layer."""

    def test_service_add_and_remove_package(self, temp_db):
        """Service should add and remove packages."""
        service = PackageStatsService(temp_db)

        # Add package
        assert service.add_package("test-package") is True
        assert service.add_package("test-package") is False  # Already exists

        # List packages
        packages = service.list_packages()
        assert len(packages) == 1
        assert packages[0].name == "test-package"
        assert isinstance(packages[0], PackageInfo)

        # Remove package
        assert service.remove_package("test-package") is True
        assert service.remove_package("test-package") is False  # Already removed

        assert service.list_packages() == []

    def test_service_import_packages(self, temp_db, temp_packages_file):
        """Service should import packages from file."""
        service = PackageStatsService(temp_db)

        added, skipped, invalid = service.import_packages(temp_packages_file)
        assert added == 2
        assert skipped == 0
        assert invalid == []

        packages = service.list_packages()
        assert len(packages) == 2

    def test_service_fetch_all_stats(self, temp_db):
        """Service should fetch and store stats for all packages."""
        service = PackageStatsService(temp_db)
        service.add_package("test-pkg")

        recent_response = json.dumps({
            "data": {"last_day": 100, "last_week": 700, "last_month": 3000}
        })
        overall_response = json.dumps({
            "data": [{"category": "without_mirrors", "downloads": 50000}]
        })

        progress_calls = []

        def on_progress(current, total, package, stats):
            progress_calls.append((current, total, package, stats))

        with patch("pkgdb.api.pypistats.recent", return_value=recent_response):
            with patch("pkgdb.api.pypistats.overall", return_value=overall_response):
                result = service.fetch_all_stats(progress_callback=on_progress)

        assert isinstance(result, FetchResult)
        assert result.success == 1
        assert result.failed == 0
        assert "test-pkg" in result.results
        assert result.results["test-pkg"]["total"] == 50000

        # Progress callback should have been called
        assert len(progress_calls) == 1
        assert progress_calls[0][0] == 1  # current
        assert progress_calls[0][1] == 1  # total
        assert progress_calls[0][2] == "test-pkg"  # package

    def test_service_get_stats(self, temp_db):
        """Service should retrieve stats."""
        service = PackageStatsService(temp_db)

        # Empty initially
        assert service.get_stats() == []

        # Add some data
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES ('test-pkg', '2024-01-01', 10, 70, 300, 1000)
        """)
        conn.commit()
        conn.close()

        stats = service.get_stats()
        assert len(stats) == 1
        assert stats[0]["package_name"] == "test-pkg"

    def test_service_get_history(self, temp_db):
        """Service should retrieve package history."""
        service = PackageStatsService(temp_db)

        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES
            ('test-pkg', '2024-01-01', 10, 70, 300, 1000),
            ('test-pkg', '2024-01-02', 20, 140, 600, 2000)
        """)
        conn.commit()
        conn.close()

        history = service.get_history("test-pkg", limit=10)
        assert len(history) == 2

    def test_service_export(self, temp_db):
        """Service should export stats in various formats."""
        service = PackageStatsService(temp_db)

        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES ('test-pkg', '2024-01-01', 10, 70, 300, 1000)
        """)
        conn.commit()
        conn.close()

        # CSV
        csv_output = service.export("csv")
        assert csv_output is not None
        assert "test-pkg" in csv_output

        # JSON
        json_output = service.export("json")
        assert json_output is not None
        data = json.loads(json_output)
        assert data["packages"][0]["name"] == "test-pkg"

        # Markdown
        md_output = service.export("markdown")
        assert md_output is not None
        assert "| Rank |" in md_output

    def test_service_export_empty(self, temp_db):
        """Service should return None for empty export."""
        service = PackageStatsService(temp_db)
        assert service.export("csv") is None

    def test_service_export_invalid_format(self, temp_db):
        """Service should raise ValueError for invalid format."""
        service = PackageStatsService(temp_db)

        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES ('test-pkg', '2024-01-01', 10, 70, 300, 1000)
        """)
        conn.commit()
        conn.close()

        with pytest.raises(ValueError, match="Unknown format"):
            service.export("invalid")

    def test_service_fetch_package_details(self, temp_db):
        """Service should fetch detailed package info."""
        service = PackageStatsService(temp_db)

        recent_response = json.dumps({
            "data": {"last_day": 100, "last_week": 700, "last_month": 3000}
        })
        overall_response = json.dumps({
            "data": [{"category": "without_mirrors", "downloads": 50000}]
        })
        python_response = json.dumps({
            "data": [{"category": "3.11", "downloads": 2000}]
        })
        system_response = json.dumps({
            "data": [{"category": "Linux", "downloads": 4000}]
        })

        with patch("pkgdb.api.pypistats.recent", return_value=recent_response):
            with patch("pkgdb.api.pypistats.overall", return_value=overall_response):
                with patch("pkgdb.api.pypistats.python_minor", return_value=python_response):
                    with patch("pkgdb.api.pypistats.system", return_value=system_response):
                        details = service.fetch_package_details("test-pkg")

        assert isinstance(details, PackageDetails)
        assert details.name == "test-pkg"
        assert details.stats is not None
        assert details.stats["total"] == 50000
        assert details.python_versions is not None
        assert len(details.python_versions) == 1
        assert details.os_stats is not None
        assert len(details.os_stats) == 1

    def test_service_generate_report(self, temp_db):
        """Service should generate HTML report."""
        service = PackageStatsService(temp_db)

        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES ('test-pkg', '2024-01-01', 10, 70, 300, 1000)
        """)
        conn.commit()
        conn.close()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False
        ) as f:
            output_path = f.name

        try:
            result = service.generate_report(output_path)
            assert result is True
            assert Path(output_path).exists()
            content = Path(output_path).read_text()
            assert "test-pkg" in content
        finally:
            Path(output_path).unlink(missing_ok=True)

    def test_service_generate_report_empty(self, temp_db):
        """Service should return False for empty report."""
        service = PackageStatsService(temp_db)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False
        ) as f:
            output_path = f.name

        try:
            result = service.generate_report(output_path)
            assert result is False
        finally:
            Path(output_path).unlink(missing_ok=True)


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
        service = PackageStatsService(temp_db)

        # Create a temp file with mix of valid and invalid names
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("valid-pkg\n")
            f.write("-invalid\n")
            f.write("another-valid\n")
            f.write("also invalid spaces\n")
            temp_file = f.name

        try:
            added, skipped, invalid = service.import_packages(temp_file)
            assert added == 2
            assert skipped == 0
            assert len(invalid) == 2
            assert "-invalid" in invalid
            assert "also invalid spaces" in invalid
        finally:
            Path(temp_file).unlink()


# =============================================================================
# Integration Tests (require network, skipped by default)
# =============================================================================


@pytest.mark.integration
class TestIntegration:
    """Integration tests that make real API calls.

    Run with: pytest -m integration
    Or set environment variable: RUN_INTEGRATION=1 pytest
    """

    @pytest.mark.skipif(
        os.environ.get("RUN_INTEGRATION") != "1",
        reason="Integration tests disabled (set RUN_INTEGRATION=1 to enable)"
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
        reason="Integration tests disabled (set RUN_INTEGRATION=1 to enable)"
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
        reason="Integration tests disabled (set RUN_INTEGRATION=1 to enable)"
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
        reason="Integration tests disabled (set RUN_INTEGRATION=1 to enable)"
    )
    def test_fetch_nonexistent_package(self):
        """fetch_package_stats should return None for nonexistent package."""
        stats = fetch_package_stats("this-package-definitely-does-not-exist-12345")
        assert stats is None


# =============================================================================
# Edge Case Tests for Chart Generation
# =============================================================================


class TestChartEdgeCases:
    """Tests for edge cases in chart generation."""

    def test_pie_chart_exactly_six_items(self):
        """Pie chart with exactly PIE_CHART_MAX_ITEMS should not group into Other."""
        data = [
            ("A", 100), ("B", 90), ("C", 80),
            ("D", 70), ("E", 60), ("F", 50),
        ]
        svg = make_svg_pie_chart(data, "test-chart")

        # All items should be present (no "Other")
        assert "A" in svg
        assert "B" in svg
        assert "C" in svg
        assert "D" in svg
        assert "E" in svg
        assert "F" in svg
        assert "Other" not in svg

    def test_pie_chart_seven_items_groups_other(self):
        """Pie chart with 7 items should group last items into Other."""
        data = [
            ("A", 100), ("B", 90), ("C", 80),
            ("D", 70), ("E", 60), ("F", 50), ("G", 40),
        ]
        svg = make_svg_pie_chart(data, "test-chart")

        # Should have "Other" for items beyond limit
        assert "Other" in svg
        # Last item should not appear individually
        assert "G" not in svg

    def test_pie_chart_single_item(self):
        """Pie chart with single item should render correctly."""
        data = [("Only", 100)]
        svg = make_svg_pie_chart(data, "test-chart")

        assert "<svg" in svg
        assert "Only" in svg
        assert "100.0%" in svg

    def test_pie_chart_very_small_slice(self):
        """Pie chart should handle very small percentage slices."""
        data = [("Big", 99999), ("Tiny", 1)]
        svg = make_svg_pie_chart(data, "test-chart")

        assert "Big" in svg
        assert "Tiny" in svg
        # Small slice should still render (even if thin)
        assert svg.count("<path") >= 2

    def test_line_chart_single_data_point(self):
        """Line chart with single data point should return empty."""
        from pkgdb.reports import _make_single_line_chart

        dates = ["2024-01-01"]
        values = [1000]
        svg = _make_single_line_chart(dates, values)

        # Single point cannot form a line
        assert svg == ""

    def test_line_chart_two_data_points(self):
        """Line chart with two data points should render."""
        from pkgdb.reports import _make_single_line_chart

        dates = ["2024-01-01", "2024-01-02"]
        values = [1000, 2000]
        svg = _make_single_line_chart(dates, values)

        assert "<svg" in svg
        assert "polyline" in svg

    def test_line_chart_constant_values(self):
        """Line chart with all same values should render flat line."""
        from pkgdb.reports import _make_single_line_chart

        dates = ["2024-01-01", "2024-01-02", "2024-01-03"]
        values = [1000, 1000, 1000]
        svg = _make_single_line_chart(dates, values)

        assert "<svg" in svg
        assert "polyline" in svg

    def test_line_chart_zero_values(self):
        """Line chart with zero values should render."""
        from pkgdb.reports import _make_single_line_chart

        dates = ["2024-01-01", "2024-01-02", "2024-01-03"]
        values = [0, 0, 0]
        svg = _make_single_line_chart(dates, values)

        assert "<svg" in svg

    def test_bar_chart_very_large_numbers(self):
        """Bar chart should handle very large download numbers."""
        from pkgdb.reports import _make_svg_bar_chart

        data = [
            ("huge-pkg", 1_000_000_000),  # 1 billion
            ("big-pkg", 100_000_000),     # 100 million
        ]
        svg = _make_svg_bar_chart(data, "Downloads", "test-chart")

        assert "<svg" in svg
        assert "huge-pkg" in svg
        assert "1,000,000,000" in svg

    def test_bar_chart_single_item(self):
        """Bar chart with single item should render."""
        from pkgdb.reports import _make_svg_bar_chart

        data = [("only-pkg", 1000)]
        svg = _make_svg_bar_chart(data, "Downloads", "test-chart")

        assert "<svg" in svg
        assert "only-pkg" in svg

    def test_bar_chart_empty_data(self):
        """Bar chart with empty data should return empty string."""
        from pkgdb.reports import _make_svg_bar_chart

        svg = _make_svg_bar_chart([], "Downloads", "test-chart")
        assert svg == ""

    def test_multi_line_chart_single_package(self):
        """Multi-line chart with single package should render."""
        from pkgdb.reports import _make_multi_line_chart

        history = {
            "pkg-a": [
                {"fetch_date": "2024-01-01", "total": 1000},
                {"fetch_date": "2024-01-02", "total": 2000},
            ]
        }
        svg = _make_multi_line_chart(history, "test-chart")

        assert "<svg" in svg
        assert "pkg-a" in svg

    def test_multi_line_chart_empty_history(self):
        """Multi-line chart with empty history should return empty."""
        from pkgdb.reports import _make_multi_line_chart

        svg = _make_multi_line_chart({}, "test-chart")
        assert svg == ""

    def test_multi_line_chart_single_date(self):
        """Multi-line chart with single date should show message."""
        from pkgdb.reports import _make_multi_line_chart

        history = {
            "pkg-a": [{"fetch_date": "2024-01-01", "total": 1000}]
        }
        svg = _make_multi_line_chart(history, "test-chart")

        assert "Not enough" in svg


# =============================================================================
# Error Path Tests
# =============================================================================


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
            "SELECT * FROM package_stats WHERE package_name = ?",
            ("test-pkg",)
        )
        row = cursor.fetchone()
        assert row is not None
        assert row["last_day"] is None
        assert row["total"] is None

    def test_load_packages_invalid_yaml(self):
        """load_packages should handle invalid YAML gracefully."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("invalid: yaml: content: [unclosed")
            path = f.name

        try:
            with pytest.raises(yaml.YAMLError):
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

        recent_success = json.dumps({
            "data": {"last_day": 100, "last_week": 700, "last_month": 3000}
        })
        overall_success = json.dumps({
            "data": [{"category": "without_mirrors", "downloads": 50000}]
        })

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
                results = fetch_all_package_stats(["good-pkg", "fail-pkg", "another-good"])

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
            {"package_name": "pkg", "total": 1000, "last_month": 300, "last_week": 70, "last_day": 10}
        ]
        history = {
            "pkg": [
                {"fetch_date": "2024-01-01", "total": None, "last_month": None, "last_week": None, "last_day": None}
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
        assert orphaned == 0
        assert remaining == 0

    def test_service_prune_no_old_data(self, temp_db):
        """Service prune should work when no old data exists."""
        service = PackageStatsService(temp_db)
        deleted = service.prune(days=30)
        assert deleted == 0


# =============================================================================
# Performance Tests (skipped by default)
# =============================================================================


@pytest.mark.slow
class TestPerformance:
    """Performance tests for large datasets.

    Run with: pytest -m slow
    """

    @pytest.mark.skipif(
        os.environ.get("RUN_SLOW_TESTS") != "1",
        reason="Slow tests disabled (set RUN_SLOW_TESTS=1 to enable)"
    )
    def test_large_number_of_packages(self, temp_db):
        """Database should handle 100+ packages efficiently."""
        import time

        service = PackageStatsService(temp_db)

        # Add 100 packages
        start = time.time()
        for i in range(100):
            service.add_package(f"test-package-{i:03d}")
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
        reason="Slow tests disabled (set RUN_SLOW_TESTS=1 to enable)"
    )
    def test_large_historical_data(self, temp_db):
        """Database should handle 1000+ days of history efficiently."""
        import time
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
                    (pkg_name, date, day * 10, day * 70, day * 300, day * 1000)
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
        reason="Slow tests disabled (set RUN_SLOW_TESTS=1 to enable)"
    )
    def test_report_generation_performance(self, temp_db):
        """Report generation should complete in reasonable time."""
        import time

        conn = get_db_connection(temp_db)
        init_db(conn)

        # Create test data: 50 packages with 30 days history each
        from datetime import datetime, timedelta
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
                    (pkg_name, date, 100 + day, 700 + day * 7, 3000 + day * 30, 50000 + day * 100)
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
        reason="Slow tests disabled (set RUN_SLOW_TESTS=1 to enable)"
    )
    def test_get_stats_with_growth_performance(self, temp_db):
        """get_stats_with_growth should be efficient with many packages."""
        import time
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
                    (pkg_name, date, 100, 700, 3000 + day * 10, 50000 + day * 100)
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
