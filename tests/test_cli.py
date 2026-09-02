"""Tests for CLI argument parsing, commands, and CLI-specific behaviors."""

import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import mock_pypistats, track
from pkgdb import (
    get_db_connection,
    get_db,
    init_db,
    add_package,
    get_packages,
    store_stats,
    store_daily_downloads,
    record_fetch_attempt,
    load_packages,
    get_config_dir,
    DEFAULT_DB_FILE,
    DEFAULT_PACKAGES_FILE,
    DEFAULT_REPORT_FILE,
    PackageStatsService,
    RepoStats,
    RepoResult,
    main,
)
from pkgdb.utils import utcnow
from datetime import datetime, timedelta


def _make_repo_stats(**overrides):
    """Helper to create a RepoStats with sensible defaults."""
    defaults = dict(
        owner="test",
        name="repo",
        full_name="test/repo",
        description="Test repo",
        stars=100,
        forks=10,
        open_issues=5,
        watchers=50,
        language="Python",
        license="MIT",
        created_at=utcnow() - timedelta(days=365),
        updated_at=utcnow(),
        pushed_at=utcnow() - timedelta(days=1),
        archived=False,
        fork=False,
        default_branch="main",
        topics=["test"],
    )
    defaults.update(overrides)
    return RepoStats(**defaults)


class TestCLI:
    """Tests for CLI argument parsing and commands."""

    def test_default_values(self):
        """Default values should be set correctly."""
        config_dir = get_config_dir()
        assert DEFAULT_DB_FILE == str(config_dir / "pkg.db")
        assert DEFAULT_PACKAGES_FILE == str(config_dir / "packages.json")
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
            with patch("pkgdb.service.check_package_exists", return_value=(True, None)):
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
            with patch("pkgdb.service.check_package_exists", return_value=(True, None)):
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

    def test_main_packages_command_empty(self, temp_db, caplog):
        """packages command should indicate when no packages tracked."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "packages"]):
            main()

        assert "No packages" in caplog.text

    def test_main_packages_command_with_packages(self, temp_db, capsys, caplog):
        """packages command should display tracked packages."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "requests")
        add_package(conn, "flask")
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "packages"]):
            main()

        captured = capsys.readouterr()
        # Table data goes to stdout
        assert "requests" in captured.out
        assert "flask" in captured.out
        # Header message goes to logging
        assert "Tracking 2 packages" in caplog.text

    def test_main_import_command(self, temp_db, temp_packages_file, caplog):
        """import command should import packages from file."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.close()

        with patch(
            "sys.argv",
            ["pkgdb", "-d", temp_db, "import", temp_packages_file, "--no-verify"],
        ):
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

        with patch(
            "sys.argv", ["pkgdb", "-d", temp_db, "import", "/nonexistent/file.json"]
        ):
            main()

        assert "File not found" in caplog.text

    def test_main_fetch_command(self, temp_db):
        """fetch command should fetch and store stats for tracked packages."""
        # First add packages to track
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.execute(
            "INSERT INTO packages (package_name, added_date) VALUES ('package-a', '2024-01-01')"
        )
        conn.execute(
            "INSERT INTO packages (package_name, added_date) VALUES ('package-b', '2024-01-01')"
        )
        conn.commit()
        conn.close()

        recent_response = json.dumps(
            {"data": {"last_day": 100, "last_week": 700, "last_month": 3000}}
        )
        overall_response = json.dumps(
            {"data": [{"category": "without_mirrors", "downloads": 50000}]}
        )

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "fetch"]):
            with mock_pypistats(recent=recent_response, overall=overall_response):
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
        track(conn, "test-pkg")
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
        track(conn, "test-pkg")
        conn.close()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
            output_path = f.name

        try:
            with patch(
                "sys.argv", ["pkgdb", "-d", temp_db, "report", "-o", output_path]
            ):
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
        track(conn, "test-pkg")
        conn.close()

        with patch(
            "sys.argv", ["pkgdb", "-d", temp_db, "history", "test-pkg", "--text"]
        ):
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
        track(conn, "test-pkg")
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
        track(conn, "test-pkg")
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
        track(conn, "test-pkg")
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
        track(conn, "test-pkg")
        conn.close()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            output_path = f.name

        try:
            with patch(
                "sys.argv",
                ["pkgdb", "-d", temp_db, "export", "-f", "csv", "-o", output_path],
            ):
                main()

            assert Path(output_path).exists()
            content = Path(output_path).read_text()
            assert "test-pkg" in content
        finally:
            Path(output_path).unlink(missing_ok=True)

    def test_main_stats_command(self, capsys):
        """stats command should display Python versions and OS breakdown."""
        recent_response = json.dumps(
            {"data": {"last_day": 100, "last_week": 700, "last_month": 3000}}
        )
        overall_response = json.dumps(
            {"data": [{"category": "without_mirrors", "downloads": 50000}]}
        )
        python_response = json.dumps(
            {
                "data": [
                    {"category": "3.11", "downloads": 2000},
                    {"category": "3.10", "downloads": 1000},
                ]
            }
        )
        system_response = json.dumps(
            {
                "data": [
                    {"category": "Linux", "downloads": 4000},
                    {"category": "Windows", "downloads": 1000},
                ]
            }
        )

        with patch("sys.argv", ["pkgdb", "stats", "test-package"]):
            with patch("pkgdb.api.pypistats.recent", return_value=recent_response):
                with patch(
                    "pkgdb.api.pypistats.overall", return_value=overall_response
                ):
                    with patch(
                        "pkgdb.api.pypistats.python_minor", return_value=python_response
                    ):
                        with patch(
                            "pkgdb.api.pypistats.system", return_value=system_response
                        ):
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
        # Report only reads history for tracked packages; without this the
        # stats lookup falls through to a live API call.
        track(conn, "test-pkg")
        conn.close()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
            output_path = f.name

        # Mock API calls for environment data
        python_response = json.dumps(
            {"data": [{"category": "3.11", "downloads": 2000}]}
        )
        system_response = json.dumps(
            {"data": [{"category": "Linux", "downloads": 4000}]}
        )
        recent_response = json.dumps(
            {"data": {"last_day": 20, "last_week": 140, "last_month": 600}}
        )
        overall_response = json.dumps(
            {"data": [{"category": "without_mirrors", "downloads": 2000}]}
        )

        try:
            with patch(
                "sys.argv",
                ["pkgdb", "-d", temp_db, "report", "test-pkg", "-o", output_path],
            ):
                with patch("webbrowser.open_new_tab"):
                    with patch(
                        "pkgdb.api.pypistats.recent", return_value=recent_response
                    ):
                        with patch(
                            "pkgdb.api.pypistats.overall", return_value=overall_response
                        ):
                            with patch(
                                "pkgdb.api.pypistats.python_minor",
                                return_value=python_response,
                            ):
                                with patch(
                                    "pkgdb.api.pypistats.system",
                                    return_value=system_response,
                                ):
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
        track(conn, "test-pkg")
        conn.close()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
            output_path = f.name

        # Mock API calls for environment data
        python_response = json.dumps(
            {
                "data": [
                    {"category": "3.11", "downloads": 2000},
                    {"category": "3.10", "downloads": 1000},
                ]
            }
        )
        system_response = json.dumps(
            {
                "data": [
                    {"category": "Linux", "downloads": 4000},
                    {"category": "Windows", "downloads": 1000},
                ]
            }
        )

        try:
            with patch(
                "sys.argv", ["pkgdb", "-d", temp_db, "report", "-e", "-o", output_path]
            ):
                with patch("webbrowser.open_new_tab"):
                    with patch(
                        "pkgdb.api.pypistats.python_minor", return_value=python_response
                    ):
                        with patch(
                            "pkgdb.api.pypistats.system", return_value=system_response
                        ):
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

        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
            output_path = f.name

        try:
            with patch(
                "sys.argv",
                ["pkgdb", "-d", temp_db, "report", "--no-browser", "-o", output_path],
            ):
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

    def test_main_version_command(self, capsys):
        """version command should display package version."""
        from pkgdb import __version__

        with patch("sys.argv", ["pkgdb", "version"]):
            main()

        captured = capsys.readouterr()
        assert "pkgdb" in captured.out
        assert __version__ in captured.out

    def test_main_show_command_with_limit(self, temp_db, capsys):
        """show command with --limit should limit output."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        # Add 5 packages
        for i in range(5):
            conn.execute(f"""
                INSERT INTO package_stats
                (package_name, fetch_date, last_day, last_week, last_month, total)
                VALUES ('pkg-{i}', '2024-01-01', {i * 10}, {i * 70}, {i * 300}, {i * 1000 + 1000})
            """)
        conn.commit()
        track(conn, *[f"pkg-{i}" for i in range(5)])
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "show", "--limit", "3"]):
            main()

        captured = capsys.readouterr()
        # Should show only top 3 (highest totals)
        assert "pkg-4" in captured.out  # 5000 total
        assert "pkg-3" in captured.out  # 4000 total
        assert "pkg-2" in captured.out  # 3000 total
        assert "pkg-0" not in captured.out  # 1000 total - should be excluded

    def test_main_show_command_with_sort_by(self, temp_db, capsys):
        """show command with --sort-by should sort by specified field."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        # Add packages with different stats profiles
        conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES
            ('high-total', '2024-01-01', 10, 70, 300, 10000),
            ('high-month', '2024-01-01', 10, 70, 9000, 5000),
            ('high-day', '2024-01-01', 500, 70, 300, 3000)
        """)
        conn.commit()
        track(conn, "high-total", "high-month", "high-day")
        conn.close()

        # Sort by month
        with patch("sys.argv", ["pkgdb", "-d", temp_db, "show", "--sort-by", "month"]):
            main()

        captured = capsys.readouterr()
        lines = [l for l in captured.out.split("\n") if l.strip()]
        # First data line (after headers) should be high-month
        data_lines = [l for l in lines if "high-" in l]
        assert "high-month" in data_lines[0]

    def test_main_show_command_with_json(self, temp_db, capsys):
        """show command with --json should output JSON."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES ('test-pkg', '2024-01-01', 10, 70, 300, 1000)
        """)
        conn.commit()
        track(conn, "test-pkg")
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "show", "--json"]):
            main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["package"] == "test-pkg"
        assert data[0]["total"] == 1000
        assert data[0]["last_month"] == 300
        assert data[0]["last_week"] == 70
        assert data[0]["last_day"] == 10

    def test_main_history_command_with_since(self, temp_db, capsys):
        """history command with --since should filter by date."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES
            ('test-pkg', '2024-01-01', 10, 70, 300, 1000),
            ('test-pkg', '2024-01-05', 20, 140, 600, 2000),
            ('test-pkg', '2024-01-10', 30, 210, 900, 3000)
        """)
        conn.commit()
        track(conn, "test-pkg")
        conn.close()

        with patch(
            "sys.argv",
            [
                "pkgdb",
                "-d",
                temp_db,
                "history",
                "test-pkg",
                "--text",
                "--since",
                "2024-01-05",
            ],
        ):
            main()

        captured = capsys.readouterr()
        assert "test-pkg" in captured.out
        assert "2024-01-01" not in captured.out  # Should be filtered out
        assert "2024-01-05" in captured.out
        assert "2024-01-10" in captured.out

    def test_main_history_command_since_no_data(self, temp_db, caplog):
        """history command with --since should handle no data in range."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES ('test-pkg', '2024-01-01', 10, 70, 300, 1000)
        """)
        conn.commit()
        conn.close()

        with patch(
            "sys.argv",
            ["pkgdb", "-d", temp_db, "history", "test-pkg", "--since", "2024-06-01"],
        ):
            main()

        assert "No data found" in caplog.text
        assert "since 2024-06-01" in caplog.text

    def test_main_sync_command_adds_new_packages(self, temp_db, caplog):
        """sync command should add new packages from PyPI user."""
        # First add an existing package
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "existing-pkg")
        conn.close()

        mock_packages = [["Owner", "existing-pkg"], ["Owner", "new-pkg"]]

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "sync", "--user", "testuser"]):
            with patch("pkgdb.api.xmlrpc.client.ServerProxy") as mock_proxy:
                mock_proxy.return_value.user_packages.return_value = mock_packages
                main()

        assert "Added 1 new packages" in caplog.text
        assert "new-pkg" in caplog.text

        # Verify new package was added
        conn = get_db_connection(temp_db)
        packages = get_packages(conn)
        conn.close()
        assert "existing-pkg" in packages
        assert "new-pkg" in packages

    def test_main_sync_command_no_new_packages(self, temp_db, caplog):
        """sync command should report when no new packages to add."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "pkg-a")
        add_package(conn, "pkg-b")
        conn.close()

        mock_packages = [["Owner", "pkg-a"], ["Owner", "pkg-b"]]

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "sync", "--user", "testuser"]):
            with patch("pkgdb.api.xmlrpc.client.ServerProxy") as mock_proxy:
                mock_proxy.return_value.user_packages.return_value = mock_packages
                main()

        assert "No new packages to add" in caplog.text

    def test_main_sync_command_warns_not_on_remote(self, temp_db, caplog):
        """sync command should warn about packages not on remote."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "local-only-pkg")
        add_package(conn, "common-pkg")
        conn.close()

        mock_packages = [["Owner", "common-pkg"]]

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "sync", "--user", "testuser"]):
            with patch("pkgdb.api.xmlrpc.client.ServerProxy") as mock_proxy:
                mock_proxy.return_value.user_packages.return_value = mock_packages
                main()

        assert "locally tracked packages not found" in caplog.text
        assert "local-only-pkg" in caplog.text

    def test_main_sync_command_user_not_found(self, temp_db, caplog):
        """sync command should handle API error."""
        import xmlrpc.client

        with patch(
            "sys.argv", ["pkgdb", "-d", temp_db, "sync", "--user", "nonexistent"]
        ):
            with patch("pkgdb.api.xmlrpc.client.ServerProxy") as mock_proxy:
                mock_proxy.return_value.user_packages.side_effect = xmlrpc.client.Fault(
                    1, "User not found"
                )
                main()

        assert "Could not fetch" in caplog.text

    def test_main_sync_command_with_prune(self, temp_db, caplog):
        """sync command with --prune should remove packages not on remote."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "local-only-pkg")
        add_package(conn, "common-pkg")
        conn.close()

        mock_packages = [["Owner", "common-pkg"]]

        with patch(
            "sys.argv",
            ["pkgdb", "-d", temp_db, "sync", "--user", "testuser", "--prune"],
        ):
            with patch("pkgdb.api.xmlrpc.client.ServerProxy") as mock_proxy:
                mock_proxy.return_value.user_packages.return_value = mock_packages
                main()

        assert "Pruned 1 packages" in caplog.text
        assert "local-only-pkg" in caplog.text

        # Verify package was removed from database
        conn = get_db_connection(temp_db)
        packages = get_packages(conn)
        conn.close()
        assert "local-only-pkg" not in packages
        assert "common-pkg" in packages


class TestCLINoVerifyFlag:
    """Tests for --no-verify CLI flag."""

    def test_add_parser_has_no_verify_flag(self):
        """add command should have --no-verify flag."""
        from pkgdb.cli import create_parser

        parser = create_parser()
        args = parser.parse_args(["add", "test-pkg", "--no-verify"])
        assert args.no_verify is True

    def test_add_parser_no_verify_defaults_false(self):
        """add command --no-verify should default to False."""
        from pkgdb.cli import create_parser

        parser = create_parser()
        args = parser.parse_args(["add", "test-pkg"])
        assert getattr(args, "no_verify", False) is False

    def test_import_parser_has_no_verify_flag(self):
        """import command should have --no-verify flag."""
        from pkgdb.cli import create_parser

        parser = create_parser()
        args = parser.parse_args(["import", "packages.txt", "--no-verify"])
        assert args.no_verify is True

    def test_history_since_help_mentions_relative(self):
        """history --since help should mention relative formats."""
        from pkgdb.cli import create_parser

        parser = create_parser()
        # Get help text for history command
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            parser.parse_args(["history", "--help"])
        except SystemExit:
            pass
        help_text = sys.stdout.getvalue()
        sys.stdout = old_stdout

        assert "7d" in help_text or "relative" in help_text.lower()


class TestLoadPackages:
    """Tests for loading packages from JSON."""

    def test_load_packages_returns_list(self, temp_packages_file):
        """load_packages should return a list of package names."""
        packages = load_packages(temp_packages_file)
        assert isinstance(packages, list)
        assert packages == ["package-a", "package-b"]

    def test_load_packages_empty_published(self):
        """load_packages should return empty list if published key is missing."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"other_key": ["something"]}, f)
            path = f.name

        try:
            packages = load_packages(path)
            assert packages == [] or packages is None
        finally:
            Path(path).unlink(missing_ok=True)

    def test_load_packages_file_not_found(self):
        """load_packages should raise FileNotFoundError for missing files."""
        with pytest.raises(FileNotFoundError):
            load_packages("/nonexistent/packages.json")


class TestGithubCLI:
    """Tests for GitHub CLI commands."""

    def test_github_command_no_packages(self, temp_db, capsys):
        with patch("sys.argv", ["pkgdb", "-d", temp_db, "github"]):
            main()
        # Should warn about no packages

    def test_github_cache_command(self, temp_db, capsys):
        # Add a package so it doesn't exit early
        with get_db(temp_db) as conn:
            add_package(conn, "test-pkg")

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "github", "cache"]):
            main()

        captured = capsys.readouterr()
        assert "GitHub Cache Statistics:" in captured.out
        assert "Total entries:" in captured.out

    def test_github_clear_command(self, temp_db):
        with get_db(temp_db) as conn:
            add_package(conn, "test-pkg")

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "github", "clear", "--all"]):
            main()

    def test_github_fetch_command_with_results(self, temp_db, capsys):
        with get_db(temp_db) as conn:
            add_package(conn, "test-pkg")

        stats = _make_repo_stats(stars=100, forks=10, language="Python")
        result = RepoResult(
            package_name="test-pkg",
            repo_url="https://github.com/test/repo",
            stats=stats,
        )

        with patch("pkgdb.service.fetch_package_github_stats", return_value=result):
            with patch("sys.argv", ["pkgdb", "-d", temp_db, "github", "fetch"]):
                main()

        captured = capsys.readouterr()
        assert "test-pkg" in captured.out
        assert "100" in captured.out

    def test_github_fetch_shows_star_growth(self, temp_db, capsys):
        from datetime import datetime, timedelta
        from pkgdb import store_github_stats_snapshot

        with get_db(temp_db) as conn:
            add_package(conn, "test-pkg")
            # A snapshot ~40 days old at 90 stars
            old = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
            conn.execute(
                "INSERT INTO github_stats_history (package_name, repo_key, date, "
                "stars, forks, open_issues, watchers) VALUES "
                "('test-pkg', 'test/repo', ?, 90, 5, 1, 1)",
                (old,),
            )
            conn.commit()

        stats = _make_repo_stats(stars=100, forks=10)
        result = RepoResult(
            package_name="test-pkg",
            repo_url="https://github.com/test/repo",
            stats=stats,
        )
        with patch("pkgdb.service.fetch_package_github_stats", return_value=result):
            with patch("sys.argv", ["pkgdb", "-d", temp_db, "github", "fetch"]):
                main()

        out = capsys.readouterr().out
        assert "Stars" in out and "+10" in out  # today 100 vs 40d-ago 90

    def test_github_fetch_json_includes_star_growth(self, temp_db, capsys):
        with get_db(temp_db) as conn:
            add_package(conn, "test-pkg")

        stats = _make_repo_stats(stars=100)
        result = RepoResult(
            package_name="test-pkg",
            repo_url="https://github.com/test/repo",
            stats=stats,
        )
        with patch("pkgdb.service.fetch_package_github_stats", return_value=result):
            # --json is a flag on the parent `github` parser
            with patch("sys.argv", ["pkgdb", "-d", temp_db, "github", "--json"]):
                main()

        data = json.loads(capsys.readouterr().out)
        assert data[0]["package"] == "test-pkg"
        # Only one snapshot (today) -> growth not yet computable
        assert data[0]["star_growth"] is None


class TestInitCommand:
    """Tests for the init command."""

    def test_init_parser_exists(self):
        """init command should be in the parser."""
        from pkgdb.cli import create_parser

        parser = create_parser()
        # Parse a valid init command
        args = parser.parse_args(["init"])
        assert args.command == "init"

    def test_init_parser_user_flag(self):
        """init command should accept --user flag."""
        from pkgdb.cli import create_parser

        parser = create_parser()
        args = parser.parse_args(["init", "--user", "testuser"])
        assert args.pypi_user == "testuser"

    def test_init_parser_short_user_flag(self):
        """init command should accept -u flag."""
        from pkgdb.cli import create_parser

        parser = create_parser()
        args = parser.parse_args(["init", "-u", "testuser"])
        assert args.pypi_user == "testuser"

    def test_init_parser_no_browser(self):
        """init command should accept --no-browser flag."""
        from pkgdb.cli import create_parser

        parser = create_parser()
        args = parser.parse_args(["init", "--no-browser"])
        assert args.no_browser is True

    def test_init_parser_output(self):
        """init command should accept -o/--output flag."""
        from pkgdb.cli import create_parser

        parser = create_parser()
        args = parser.parse_args(["init", "-o", "/tmp/custom.html"])
        assert args.output == "/tmp/custom.html"

    def _no_config(self):
        """Patch helper to skip config file loading."""
        return patch(
            "pkgdb.config.get_config_path",
            return_value=Path("/nonexistent/config.toml"),
        )

    def test_init_with_existing_packages_decline(self, temp_db, caplog):
        """init should ask to continue when packages exist, and exit on 'n'."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "requests")
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "init"]):
            with patch("pkgdb.cli.input", return_value="n"):
                with self._no_config():
                    main()

        assert "Already tracking 1 packages" in caplog.text

    def test_init_with_username_syncs_and_fetches(self, temp_db, caplog):
        """init with --user should sync packages and attempt fetch."""
        mock_stats = {
            "last_day": 100,
            "last_week": 700,
            "last_month": 3000,
            "total": 50000,
        }

        with patch(
            "sys.argv",
            ["pkgdb", "-d", temp_db, "init", "-u", "testuser", "--no-browser"],
        ):
            with patch("pkgdb.service.fetch_user_packages", return_value=["test-pkg"]):
                with patch(
                    "pkgdb.service.fetch_package_stats", return_value=mock_stats
                ):
                    with patch(
                        "pkgdb.service.fetch_python_versions", return_value=None
                    ):
                        with patch("pkgdb.service.fetch_os_stats", return_value=None):
                            with patch(
                                "pkgdb.service.fetch_daily_downloads", return_value=None
                            ):
                                with self._no_config():
                                    main()

        assert "Syncing packages" in caplog.text
        assert "Added 1 packages" in caplog.text
        assert "Fetch complete" in caplog.text

    def test_init_manual_entry(self, temp_db, caplog):
        """init without username should prompt for manual package entry."""
        mock_stats = {
            "last_day": 10,
            "last_week": 70,
            "last_month": 300,
            "total": 5000,
        }

        # Simulate: blank username, then enter "test-pkg", then blank to finish
        inputs = iter(["", "test-pkg", ""])

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "init", "--no-browser"]):
            with patch("pkgdb.cli.input", side_effect=inputs):
                with patch(
                    "pkgdb.service.check_package_exists", return_value=(True, None)
                ):
                    with patch(
                        "pkgdb.service.fetch_package_stats", return_value=mock_stats
                    ):
                        with patch(
                            "pkgdb.service.fetch_python_versions", return_value=None
                        ):
                            with patch(
                                "pkgdb.service.fetch_os_stats", return_value=None
                            ):
                                with patch(
                                    "pkgdb.service.fetch_daily_downloads",
                                    return_value=None,
                                ):
                                    with self._no_config():
                                        main()

        assert "Added 'test-pkg'" in caplog.text
        assert "Fetch complete" in caplog.text

    def test_init_no_packages_added(self, temp_db, caplog):
        """init should exit gracefully when no packages are added."""
        # Simulate: blank username, then blank immediately (no packages)
        inputs = iter(["", ""])

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "init", "--no-browser"]):
            with patch("pkgdb.cli.input", side_effect=inputs):
                with self._no_config():
                    main()

        assert "No packages added" in caplog.text

    def test_init_user_not_found(self, temp_db, caplog):
        """init should handle nonexistent PyPI user."""
        with patch("sys.argv", ["pkgdb", "-d", temp_db, "init", "-u", "nonexistent"]):
            with patch("pkgdb.service.fetch_user_packages", return_value=None):
                with self._no_config():
                    main()

        assert "Could not fetch packages" in caplog.text

    def test_init_user_no_packages(self, temp_db, caplog):
        """init should handle user with no packages."""
        with patch("sys.argv", ["pkgdb", "-d", temp_db, "init", "-u", "emptyuser"]):
            with patch("pkgdb.service.fetch_user_packages", return_value=[]):
                with self._no_config():
                    main()

        assert "No packages found" in caplog.text


class TestConfigIntegration:
    """Tests for config file integration with CLI main()."""

    def test_main_loads_config(self, tmp_path, temp_db, capsys):
        """main() should load and apply config file."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[defaults]\nsort_by = "name"\n')

        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "alpha-pkg")
        add_package(conn, "zeta-pkg")
        store_stats(
            conn,
            "alpha-pkg",
            {
                "last_day": 10,
                "last_week": 70,
                "last_month": 300,
                "total": 5000,
            },
        )
        store_stats(
            conn,
            "zeta-pkg",
            {
                "last_day": 100,
                "last_week": 700,
                "last_month": 3000,
                "total": 50000,
            },
        )
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "show"]):
            with patch("pkgdb.config.get_config_path", return_value=config_path):
                main()

        captured = capsys.readouterr()
        # With sort_by=name, alpha-pkg should come first
        alpha_pos = captured.out.index("alpha-pkg")
        zeta_pos = captured.out.index("zeta-pkg")
        assert alpha_pos < zeta_pos

    def test_cli_flag_overrides_config(self, tmp_path, temp_db, capsys):
        """CLI --sort-by should override config sort_by."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[defaults]\nsort_by = "name"\n')

        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "alpha-pkg")
        add_package(conn, "zeta-pkg")
        store_stats(
            conn,
            "alpha-pkg",
            {
                "last_day": 10,
                "last_week": 70,
                "last_month": 300,
                "total": 50000,
            },
        )
        store_stats(
            conn,
            "zeta-pkg",
            {
                "last_day": 100,
                "last_week": 700,
                "last_month": 3000,
                "total": 5000,
            },
        )
        conn.close()

        # --sort-by total should override config's sort_by=name
        with patch("sys.argv", ["pkgdb", "-d", temp_db, "show", "--sort-by", "total"]):
            with patch("pkgdb.config.get_config_path", return_value=config_path):
                main()

        captured = capsys.readouterr()
        # With sort_by=total descending, alpha-pkg (50000) should come first
        alpha_pos = captured.out.index("alpha-pkg")
        zeta_pos = captured.out.index("zeta-pkg")
        assert alpha_pos < zeta_pos


class TestAdaptiveShowOutput:
    """Tests for R3: show command adapts when history is insufficient."""

    def _no_config(self):
        return patch(
            "pkgdb.config.get_config_path",
            return_value=Path("/nonexistent/config.toml"),
        )

    def test_show_hides_trend_growth_with_single_data_point(self, temp_db, capsys):
        """With only one fetch, Trend and Growth columns should not appear."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "my-pkg")
        store_stats(
            conn,
            "my-pkg",
            {
                "last_day": 100,
                "last_week": 700,
                "last_month": 3000,
                "total": 50000,
            },
        )
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "show"]):
            with self._no_config():
                main()

        captured = capsys.readouterr()
        assert "my-pkg" in captured.out
        assert "50,000" in captured.out
        # Should NOT have Trend or Growth headers
        assert "Trend" not in captured.out
        assert "Growth" not in captured.out

    def test_show_includes_trend_growth_with_multiple_data_points(
        self, temp_db, capsys
    ):
        """With multiple fetches, Trend and Growth columns should appear."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "my-pkg")
        # Insert two historical data points with different dates
        conn.execute(
            """INSERT INTO package_stats
               (package_name, fetch_date, last_day, last_week, last_month, total)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("my-pkg", "2026-03-01", 90, 600, 2800, 48000),
        )
        conn.execute(
            """INSERT INTO package_stats
               (package_name, fetch_date, last_day, last_week, last_month, total)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("my-pkg", "2026-04-01", 100, 700, 3000, 50000),
        )
        conn.commit()
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "show"]):
            with self._no_config():
                main()

        captured = capsys.readouterr()
        assert "Trend" in captured.out
        assert "Growth" in captured.out

    def test_show_next_update_message(self, temp_db, capsys):
        """Show should display next update time when packages are throttled."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "my-pkg")
        store_stats(
            conn,
            "my-pkg",
            {
                "last_day": 100,
                "last_week": 700,
                "last_month": 3000,
                "total": 50000,
            },
        )
        # Record a recent fetch attempt so next_update_seconds is set
        record_fetch_attempt(conn, "my-pkg", success=True)
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "show"]):
            with self._no_config():
                main()

        captured = capsys.readouterr()
        assert "Next update available in" in captured.out


class TestJSONOutput:
    """Tests for R4: --json flag on packages, history, stats, cleanup, github."""

    def _no_config(self):
        return patch(
            "pkgdb.config.get_config_path",
            return_value=Path("/nonexistent/config.toml"),
        )

    def test_packages_json(self, temp_db, capsys):
        """packages --json should output valid JSON."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "alpha-pkg")
        add_package(conn, "beta-pkg")
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "packages", "--json"]):
            with self._no_config():
                main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert len(data) == 2
        names = {p["package"] for p in data}
        assert "alpha-pkg" in names
        assert "beta-pkg" in names
        assert "added_date" in data[0]

    def test_history_json(self, temp_db, capsys):
        """history --json should output valid JSON."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "my-pkg")
        conn.execute(
            """INSERT INTO package_stats
               (package_name, fetch_date, last_day, last_week, last_month, total)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("my-pkg", "2026-04-01", 100, 700, 3000, 50000),
        )
        conn.execute(
            """INSERT INTO package_stats
               (package_name, fetch_date, last_day, last_week, last_month, total)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("my-pkg", "2026-04-02", 110, 750, 3100, 50110),
        )
        conn.commit()
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "history", "my-pkg", "--json"]):
            with self._no_config():
                main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert len(data) == 2
        # Should be oldest first (reversed from DESC)
        assert data[0]["date"] == "2026-04-01"
        assert data[1]["date"] == "2026-04-02"
        assert data[1]["total"] == 50110

    def test_history_html_default(self, temp_db, caplog):
        """history should generate HTML report by default."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "my-pkg")
        conn.execute(
            """INSERT INTO package_stats
               (package_name, fetch_date, last_day, last_week, last_month, total)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("my-pkg", "2026-04-01", 100, 700, 3000, 50000),
        )
        conn.execute(
            """INSERT INTO package_stats
               (package_name, fetch_date, last_day, last_week, last_month, total)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("my-pkg", "2026-04-02", 110, 750, 3100, 50110),
        )
        conn.commit()
        conn.close()

        output = os.path.join(os.path.dirname(temp_db), "history_report.html")
        with patch(
            "sys.argv",
            ["pkgdb", "-d", temp_db, "history", "my-pkg", "-o", output, "--no-browser"],
        ):
            with patch("pkgdb.service.fetch_pypi_releases", return_value=[]):
                with patch("pkgdb.service.extract_github_url", return_value=None):
                    # No env stats are cached for this package, so the report
                    # would otherwise fetch them from pypistats.
                    with patch(
                        "pkgdb.reports.fetch_python_versions", return_value=None
                    ):
                        with patch("pkgdb.reports.fetch_os_stats", return_value=None):
                            with self._no_config():
                                main()

        assert "Project report generated" in caplog.text
        assert Path(output).exists()
        with open(output) as f:
            html = f.read()
        assert "my-pkg" in html
        assert "50,110" in html
        Path(output).unlink(missing_ok=True)

    def test_stats_json(self, temp_db, capsys):
        """stats --json should output valid JSON."""
        mock_stats = {
            "last_day": 100,
            "last_week": 700,
            "last_month": 3000,
            "total": 50000,
        }
        mock_py_versions = [
            {"category": "3.12", "downloads": 20000},
            {"category": "3.11", "downloads": 15000},
        ]
        mock_os = [
            {"category": "Linux", "downloads": 30000},
            {"category": "Windows", "downloads": 10000},
        ]

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "stats", "my-pkg", "--json"]):
            with patch("pkgdb.service.fetch_package_stats", return_value=mock_stats):
                with patch(
                    "pkgdb.service.fetch_python_versions", return_value=mock_py_versions
                ):
                    with patch("pkgdb.service.fetch_os_stats", return_value=mock_os):
                        with self._no_config():
                            main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["package"] == "my-pkg"
        assert data["downloads"]["total"] == 50000
        assert len(data["python_versions"]) == 2
        assert data["python_versions"][0]["version"] == "3.12"
        assert len(data["os"]) == 2
        assert data["os"][0]["name"] == "Linux"

    def test_cleanup_json(self, temp_db, capsys):
        """cleanup --json should output valid JSON."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "tracked-pkg")
        # Add orphaned stats for a package not in the packages table
        conn.execute(
            """INSERT INTO package_stats
               (package_name, fetch_date, last_day, last_week, last_month, total)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("orphaned-pkg", "2026-04-01", 10, 70, 300, 5000),
        )
        conn.commit()
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "cleanup", "--json"]):
            with self._no_config():
                main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["orphaned_removed"] == 1
        assert data["packages_remaining"] == 1

    def test_cleanup_json_with_prune(self, temp_db, capsys):
        """cleanup --json --days should include prune info."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "my-pkg")
        conn.execute(
            """INSERT INTO package_stats
               (package_name, fetch_date, last_day, last_week, last_month, total)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("my-pkg", "2020-01-01", 10, 70, 300, 5000),
        )
        conn.commit()
        conn.close()

        with patch(
            "sys.argv", ["pkgdb", "-d", temp_db, "cleanup", "--days", "30", "--json"]
        ):
            with self._no_config():
                main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "pruned" in data
        assert data["pruned"] == 1
        assert data["prune_days"] == 30

    def test_github_fetch_json(self, temp_db, capsys):
        """github --json should output valid JSON for fetch results."""
        from pkgdb.github import RepoResult, RepoStats

        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "my-pkg")
        conn.close()

        mock_stats = RepoStats(
            owner="owner",
            name="my-pkg",
            full_name="owner/my-pkg",
            description="A test package",
            stars=42,
            forks=5,
            open_issues=3,
            watchers=42,
            language="Python",
            license="MIT",
            created_at=None,
            updated_at=None,
            pushed_at=None,
            archived=False,
            fork=False,
            default_branch="main",
            topics=[],
        )
        mock_result = RepoResult(
            package_name="my-pkg",
            repo_url="https://github.com/owner/my-pkg",
            stats=mock_stats,
        )

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "github", "--json"]):
            with patch.object(
                PackageStatsService, "fetch_github_stats", return_value=[mock_result]
            ):
                with self._no_config():
                    main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert len(data) == 1
        assert data[0]["package"] == "my-pkg"
        assert data[0]["stars"] == 42
        assert data[0]["forks"] == 5
        assert data[0]["language"] == "Python"

    def test_github_json_reports_issues_excluding_prs(self, temp_db, capsys):
        """open_issues keeps its GitHub meaning; the issues-only count is extra."""
        from pkgdb.github import RepoResult, RepoStats

        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "my-pkg")
        conn.close()

        mock_stats = RepoStats(
            owner="owner",
            name="my-pkg",
            full_name="owner/my-pkg",
            description="A test package",
            stars=42,
            forks=5,
            open_issues=11,
            watchers=42,
            language="Python",
            license="MIT",
            created_at=None,
            updated_at=None,
            pushed_at=None,
            archived=False,
            fork=False,
            default_branch="main",
            topics=[],
            open_issues_excl_prs=4,
        )
        mock_result = RepoResult(
            package_name="my-pkg",
            repo_url="https://github.com/owner/my-pkg",
            stats=mock_stats,
        )

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "github", "--json"]):
            with patch.object(
                PackageStatsService, "fetch_github_stats", return_value=[mock_result]
            ):
                with self._no_config():
                    main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data[0]["open_issues"] == 11
        assert data[0]["open_issues_excl_prs"] == 4

    def test_github_table_shows_issues_column(self, temp_db, capsys):
        """The text table carries the issues-only count, or a dash if unknown."""
        from pkgdb.github import RepoResult, RepoStats

        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "my-pkg")
        add_package(conn, "other-pkg")
        conn.close()

        def _stats(name, excl_prs):
            return RepoStats(
                owner="owner",
                name=name,
                full_name=f"owner/{name}",
                description=None,
                stars=42,
                forks=5,
                open_issues=11,
                watchers=42,
                language="Python",
                license="MIT",
                created_at=None,
                updated_at=None,
                pushed_at=None,
                archived=False,
                fork=False,
                default_branch="main",
                topics=[],
                open_issues_excl_prs=excl_prs,
            )

        results = [
            RepoResult(
                package_name="my-pkg",
                repo_url="https://github.com/owner/my-pkg",
                stats=_stats("my-pkg", 4),
            ),
            RepoResult(
                package_name="other-pkg",
                repo_url="https://github.com/owner/other-pkg",
                stats=_stats("other-pkg", None),
            ),
        ]

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "github"]):
            with patch.object(
                PackageStatsService, "fetch_github_stats", return_value=results
            ):
                with self._no_config():
                    main()

        captured = capsys.readouterr()
        assert "Issues" in captured.out
        my_row = next(ln for ln in captured.out.splitlines() if "my-pkg" in ln)
        other_row = next(ln for ln in captured.out.splitlines() if "other-pkg" in ln)
        assert "4" in my_row.split()
        assert "-" in other_row.split()
        assert "11" not in captured.out

    def test_github_cache_json(self, temp_db, capsys):
        """github cache --json should output valid JSON."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "my-pkg")
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "github", "--json", "cache"]):
            with self._no_config():
                main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "total" in data
        assert "valid" in data
        assert "expired" in data

    def test_github_clear_json(self, temp_db, capsys):
        """github clear --json should output valid JSON."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "my-pkg")
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "github", "--json", "clear"]):
            with self._no_config():
                main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "cleared" in data
        assert "scope" in data


class TestDiffCommand:
    """Tests for the diff command."""

    def _no_config(self):
        return patch(
            "pkgdb.config.get_config_path",
            return_value=Path("/nonexistent/config.toml"),
        )

    def _setup_history(self, temp_db):
        """Insert two data points for two packages."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "alpha-pkg")
        add_package(conn, "beta-pkg")
        # Day 1
        conn.execute(
            """INSERT INTO package_stats
               (package_name, fetch_date, last_day, last_week, last_month, total)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("alpha-pkg", "2026-03-01", 80, 560, 2400, 40000),
        )
        conn.execute(
            """INSERT INTO package_stats
               (package_name, fetch_date, last_day, last_week, last_month, total)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("beta-pkg", "2026-03-01", 20, 140, 600, 10000),
        )
        # Day 2
        conn.execute(
            """INSERT INTO package_stats
               (package_name, fetch_date, last_day, last_week, last_month, total)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("alpha-pkg", "2026-04-01", 100, 700, 3000, 50000),
        )
        conn.execute(
            """INSERT INTO package_stats
               (package_name, fetch_date, last_day, last_week, last_month, total)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("beta-pkg", "2026-04-01", 15, 105, 450, 10450),
        )
        conn.commit()
        conn.close()

    def test_diff_parser_exists(self):
        """diff command should be in the parser."""
        from pkgdb.cli import create_parser

        parser = create_parser()
        args = parser.parse_args(["diff"])
        assert args.command == "diff"

    def test_diff_parser_period_flag(self):
        """diff command should accept --period flag."""
        from pkgdb.cli import create_parser

        parser = create_parser()
        args = parser.parse_args(["diff", "--period", "week"])
        assert args.period == "week"

    def test_diff_default_latest(self, temp_db, capsys):
        """diff with no period should compare latest to previous fetch."""
        self._setup_history(temp_db)

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "diff"]):
            with self._no_config():
                main()

        captured = capsys.readouterr()
        assert "alpha-pkg" in captured.out
        assert "beta-pkg" in captured.out
        # alpha-pkg total went from 40000 to 50000 = +10,000
        assert "+10,000" in captured.out
        # Should show the comparison dates
        assert "2026-03-01" in captured.out
        assert "2026-04-01" in captured.out

    def test_diff_week_period(self, temp_db, capsys):
        """diff --period week should compare to ~7 days ago."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "my-pkg")
        # 10 days ago
        conn.execute(
            """INSERT INTO package_stats
               (package_name, fetch_date, last_day, last_week, last_month, total)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("my-pkg", "2026-03-22", 80, 560, 2400, 40000),
        )
        # Today
        conn.execute(
            """INSERT INTO package_stats
               (package_name, fetch_date, last_day, last_week, last_month, total)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("my-pkg", "2026-04-01", 100, 700, 3000, 50000),
        )
        conn.commit()
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "diff", "--period", "week"]):
            with self._no_config():
                main()

        captured = capsys.readouterr()
        assert "Week-over-Week" in captured.out
        assert "my-pkg" in captured.out

    def test_diff_month_period(self, temp_db, capsys):
        """diff --period month should compare to ~30 days ago."""
        self._setup_history(temp_db)

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "diff", "--period", "month"]):
            with self._no_config():
                main()

        captured = capsys.readouterr()
        assert "Month-over-Month" in captured.out
        assert "alpha-pkg" in captured.out

    def test_diff_no_data(self, temp_db, caplog):
        """diff should warn when no data exists."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "diff"]):
            with self._no_config():
                main()

        assert "No data in database" in caplog.text

    def test_diff_single_data_point(self, temp_db, caplog):
        """diff should warn when only one data point exists."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "my-pkg")
        store_stats(
            conn,
            "my-pkg",
            {
                "last_day": 100,
                "last_week": 700,
                "last_month": 3000,
                "total": 50000,
            },
        )
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "diff"]):
            with self._no_config():
                main()

        assert "Need at least 2 data points" in caplog.text

    def test_diff_json_output(self, temp_db, capsys):
        """diff --json should output valid JSON."""
        self._setup_history(temp_db)

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "diff", "--json"]):
            with self._no_config():
                main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert len(data) == 2

        # Find alpha-pkg entry
        alpha = next(d for d in data if d["package"] == "alpha-pkg")
        assert alpha["total"]["current"] == 50000
        assert alpha["total"]["previous"] == 40000
        assert alpha["total"]["change"] == 10000
        assert alpha["current_date"] == "2026-04-01"
        assert alpha["previous_date"] == "2026-03-01"

    def test_diff_sort_by_change(self, temp_db, capsys):
        """diff --sort-by change should sort by absolute total change."""
        self._setup_history(temp_db)

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "diff", "--sort-by", "change"]):
            with self._no_config():
                main()

        captured = capsys.readouterr()
        # alpha-pkg had +10000 change, beta-pkg had +450 change
        # With descending sort, alpha should come first
        alpha_pos = captured.out.index("alpha-pkg")
        beta_pos = captured.out.index("beta-pkg")
        assert alpha_pos < beta_pos

    def test_diff_sort_by_name(self, temp_db, capsys):
        """diff --sort-by name should sort alphabetically."""
        self._setup_history(temp_db)

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "diff", "--sort-by", "name"]):
            with self._no_config():
                main()

        captured = capsys.readouterr()
        alpha_pos = captured.out.index("alpha-pkg")
        beta_pos = captured.out.index("beta-pkg")
        assert alpha_pos < beta_pos

    def test_diff_percentage_change(self, temp_db, capsys):
        """diff should show percentage changes."""
        self._setup_history(temp_db)

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "diff"]):
            with self._no_config():
                main()

        captured = capsys.readouterr()
        # alpha total: 40000 -> 50000 = +25.0%
        assert "+25.0%" in captured.out

    def test_diff_negative_change(self, temp_db, capsys):
        """diff should show negative changes correctly."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "declining-pkg")
        conn.execute(
            """INSERT INTO package_stats
               (package_name, fetch_date, last_day, last_week, last_month, total)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("declining-pkg", "2026-03-01", 100, 700, 3000, 50000),
        )
        conn.execute(
            """INSERT INTO package_stats
               (package_name, fetch_date, last_day, last_week, last_month, total)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("declining-pkg", "2026-04-01", 80, 560, 2400, 50080),
        )
        conn.commit()
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "diff"]):
            with self._no_config():
                main()

        captured = capsys.readouterr()
        # Week went from 700 to 560 = -140, -20.0%
        assert "-140" in captured.out
        assert "-20.0%" in captured.out

    def test_diff_insufficient_history_for_period(self, temp_db, caplog):
        """diff --period month should warn when not enough history."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "my-pkg")
        # Two records only 2 days apart
        conn.execute(
            """INSERT INTO package_stats
               (package_name, fetch_date, last_day, last_week, last_month, total)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("my-pkg", "2026-03-30", 80, 560, 2400, 40000),
        )
        conn.execute(
            """INSERT INTO package_stats
               (package_name, fetch_date, last_day, last_week, last_month, total)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("my-pkg", "2026-04-01", 100, 700, 3000, 50000),
        )
        conn.commit()
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "diff", "--period", "month"]):
            with self._no_config():
                main()

        assert "Not enough history for month comparison" in caplog.text

    @staticmethod
    def _seed_daily(temp_db, package, rows):
        from pkgdb import store_daily_downloads

        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, package)
        store_daily_downloads(
            conn,
            package,
            [
                {
                    "date": d,
                    "dimension": "overall",
                    "category": "without_mirrors",
                    "downloads": n,
                }
                for d, n in rows
            ],
        )
        conn.close()

    def test_diff_week_uses_daily_when_available(self, temp_db, capsys):
        """diff --period week should use exact daily sums when daily data exists."""
        # 14 days: prior week (01-07) = 7*10=70, current week (08-14) = 7*30=210
        rows = [(f"2026-06-{i:02d}", 10) for i in range(1, 8)]
        rows += [(f"2026-06-{i:02d}", 30) for i in range(8, 15)]
        self._seed_daily(temp_db, "my-pkg", rows)

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "diff", "--period", "week"]):
            with self._no_config():
                main()
        out = capsys.readouterr().out
        assert "Week-over-Week (daily downloads)" in out
        assert "This Week" in out and "Last Week" in out
        assert "210" in out  # current week total
        assert "70" in out  # previous week total

    def test_diff_week_daily_json(self, temp_db, capsys):
        """diff --period week --json should emit exact daily period sums."""
        rows = [(f"2026-06-{i:02d}", 10) for i in range(1, 8)]
        rows += [(f"2026-06-{i:02d}", 30) for i in range(8, 15)]
        self._seed_daily(temp_db, "my-pkg", rows)

        with patch(
            "sys.argv", ["pkgdb", "-d", temp_db, "diff", "--period", "week", "--json"]
        ):
            with self._no_config():
                main()
        data = json.loads(capsys.readouterr().out)
        assert data == [
            {
                "package": "my-pkg",
                "period": "week",
                "current": 210,
                "previous": 70,
                "change": 140,
            },
        ]

    def test_diff_latest_still_uses_snapshots(self, temp_db, capsys):
        """diff (latest) must remain snapshot-based even with daily data present."""
        self._setup_history(temp_db)  # snapshot rows for alpha/beta
        self._seed_daily(
            temp_db, "alpha-pkg", [(f"2026-06-{i:02d}", 5) for i in range(1, 15)]
        )

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "diff"]):
            with self._no_config():
                main()
        out = capsys.readouterr().out
        # Snapshot latest comparison shows the +10,000 total delta and dates
        assert "+10,000" in out
        assert "daily downloads" not in out


class TestCheckCommand:
    """Tests for the `check` command (anomaly + milestone detection)."""

    def _no_config(self):
        return patch(
            "pkgdb.config.get_config_path",
            return_value=Path("/nonexistent/config.toml"),
        )

    @staticmethod
    def _seed_daily(temp_db, package, daily_values, start="2026-01-05"):
        from datetime import datetime, timedelta
        from pkgdb import store_daily_downloads

        d0 = datetime.strptime(start, "%Y-%m-%d").date()
        rows = [
            {
                "date": (d0 + timedelta(days=i)).isoformat(),
                "dimension": "overall",
                "category": "without_mirrors",
                "downloads": v,
            }
            for i, v in enumerate(daily_values)
        ]
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, package)
        store_daily_downloads(conn, package, rows)
        conn.close()

    def test_check_parser_exists(self):
        from pkgdb.cli import create_parser

        args = create_parser().parse_args(["check"])
        assert args.command == "check"

    def test_check_no_events_exits_zero(self, temp_db, caplog):
        self._seed_daily(temp_db, "steady", [100] * 63)  # flat -> no anomaly
        with patch("sys.argv", ["pkgdb", "-d", temp_db, "check"]):
            with self._no_config():
                main()  # must NOT raise SystemExit
        assert "No anomalies" in caplog.text

    def test_check_spike_exits_nonzero(self, temp_db, capsys):
        self._seed_daily(temp_db, "spiky", [100] * 56 + [220] * 7)
        with patch("sys.argv", ["pkgdb", "-d", temp_db, "check"]):
            with self._no_config():
                with pytest.raises(SystemExit) as exc:
                    main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "spiky" in out
        assert "SPIKE" in out

    def test_check_exit_zero_flag(self, temp_db):
        self._seed_daily(temp_db, "spiky", [100] * 56 + [220] * 7)
        with patch("sys.argv", ["pkgdb", "-d", temp_db, "check", "--exit-zero"]):
            with self._no_config():
                main()  # must NOT raise despite an event

    def test_check_json_output(self, temp_db, capsys):
        self._seed_daily(temp_db, "spiky", [100] * 56 + [220] * 7)
        with patch("sys.argv", ["pkgdb", "-d", temp_db, "check", "--json"]):
            with self._no_config():
                with pytest.raises(SystemExit):
                    main()
        data = json.loads(capsys.readouterr().out)
        assert len(data) == 1
        assert data[0]["package"] == "spiky"
        assert data[0]["kind"] == "spike"

    def test_check_milestone_crossing(self, temp_db, capsys):
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "my-pkg")
        conn.execute(
            "INSERT INTO package_stats (package_name, fetch_date, last_day, "
            "last_week, last_month, total) VALUES ('my-pkg', '2026-03-01', 1, 1, 1, 900)"
        )
        conn.execute(
            "INSERT INTO package_stats (package_name, fetch_date, last_day, "
            "last_week, last_month, total) VALUES ('my-pkg', '2026-04-01', 1, 1, 1, 1100)"
        )
        conn.commit()
        conn.close()

        with patch(
            "sys.argv",
            ["pkgdb", "-d", temp_db, "check", "--milestone", "1000", "--json"],
        ):
            with self._no_config():
                with pytest.raises(SystemExit) as exc:
                    main()
        assert exc.value.code == 1
        data = json.loads(capsys.readouterr().out)
        assert data == [
            {
                "package": "my-pkg",
                "kind": "milestone",
                "milestone": 1000,
                "total": 1100,
                "message": "crossed 1,000 observed downloads (now 1,100)",
            }
        ]


class TestTagCommands:
    """Tests for tag / untag / tags commands and show --tag."""

    def _no_config(self):
        return patch(
            "pkgdb.config.get_config_path",
            return_value=Path("/nonexistent/config.toml"),
        )

    @staticmethod
    def _seed(temp_db, packages_with_totals):
        conn = get_db_connection(temp_db)
        init_db(conn)
        for pkg, total in packages_with_totals:
            add_package(conn, pkg)
            store_stats(
                conn,
                pkg,
                {"last_day": 1, "last_week": 7, "last_month": 10, "total": total},
            )
        conn.close()

    def test_tag_and_untag(self, temp_db, caplog):
        self._seed(temp_db, [("a", 100)])
        with patch("sys.argv", ["pkgdb", "-d", temp_db, "tag", "a", "web", "cli"]):
            with self._no_config():
                main()
        assert PackageStatsService(temp_db).get_package_tags("a") == ["cli", "web"]

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "untag", "a", "cli"]):
            with self._no_config():
                main()
        assert PackageStatsService(temp_db).get_package_tags("a") == ["web"]

    def test_untag_all(self, temp_db):
        self._seed(temp_db, [("a", 100)])
        svc = PackageStatsService(temp_db)
        svc.add_tag("a", "web")
        svc.add_tag("a", "cli")
        with patch("sys.argv", ["pkgdb", "-d", temp_db, "untag", "a", "--all"]):
            with self._no_config():
                main()
        assert svc.get_package_tags("a") == []

    def test_tag_untracked_package_warns(self, temp_db, caplog):
        self._seed(temp_db, [("a", 100)])
        with patch("sys.argv", ["pkgdb", "-d", temp_db, "tag", "ghost", "web"]):
            with self._no_config():
                main()
        assert "not tracked" in caplog.text
        assert PackageStatsService(temp_db).get_package_tags("ghost") == []

    def test_tags_rollup_table(self, temp_db, capsys):
        self._seed(temp_db, [("a", 100), ("b", 250), ("c", 40)])
        svc = PackageStatsService(temp_db)
        svc.add_tag("a", "web")
        svc.add_tag("b", "web")
        svc.add_tag("c", "cli")
        with patch("sys.argv", ["pkgdb", "-d", temp_db, "tags"]):
            with self._no_config():
                main()
        out = capsys.readouterr().out
        assert "web" in out
        assert "350" in out  # aggregated total for web (100 + 250)

    def test_tags_json(self, temp_db, capsys):
        self._seed(temp_db, [("a", 100), ("b", 250)])
        svc = PackageStatsService(temp_db)
        svc.add_tag("a", "web")
        svc.add_tag("b", "web")
        with patch("sys.argv", ["pkgdb", "-d", temp_db, "tags", "--json"]):
            with self._no_config():
                main()
        data = json.loads(capsys.readouterr().out)
        assert data[0]["tag"] == "web"
        assert data[0]["package_count"] == 2
        assert data[0]["total"] == 350
        assert set(data[0]["packages"]) == {"a", "b"}

    def test_show_filtered_by_tag(self, temp_db, capsys):
        self._seed(temp_db, [("a", 100), ("b", 250), ("c", 40)])
        svc = PackageStatsService(temp_db)
        svc.add_tag("a", "web")
        svc.add_tag("b", "web")
        with patch("sys.argv", ["pkgdb", "-d", temp_db, "show", "--tag", "web"]):
            with self._no_config():
                main()
        out = capsys.readouterr().out
        assert "a" in out and "b" in out
        assert "40" not in out  # package c (total 40) is excluded
        assert "Group 'web' (2 packages)" in out


class TestReleasesCommand:
    """Tests for the releases CLI command."""

    def _no_config(self):
        return patch(
            "pkgdb.config.get_config_path",
            return_value=Path("/nonexistent/config.toml"),
        )

    def test_releases_parser_exists(self):
        """releases command should be in the parser."""
        from pkgdb.cli import create_parser
        from pkgdb import PyPIRelease, GitHubRelease

        parser = create_parser()
        args = parser.parse_args(["releases", "my-pkg"])
        assert args.command == "releases"
        assert args.package == "my-pkg"

    def test_releases_json_output(self, temp_db, capsys):
        """releases --json should output valid JSON."""
        from pkgdb import PyPIRelease

        mock_pypi = [
            PyPIRelease(version="0.1.0", upload_date="2025-01-15"),
        ]

        with patch(
            "sys.argv", ["pkgdb", "-d", temp_db, "releases", "my-pkg", "--json"]
        ):
            with patch.object(
                PackageStatsService,
                "fetch_package_releases",
                return_value=(mock_pypi, []),
            ):
                with self._no_config():
                    main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["package"] == "my-pkg"
        assert len(data["pypi_releases"]) == 1
        assert data["pypi_releases"][0]["version"] == "0.1.0"

    def test_releases_table_output(self, temp_db, capsys):
        """releases should show a table of merged releases."""
        from pkgdb import PyPIRelease, GitHubRelease

        mock_pypi = [
            PyPIRelease(version="0.1.0", upload_date="2025-01-15"),
        ]
        mock_gh = [
            GitHubRelease(tag_name="v0.1.0", published_at="2025-01-16", name=None),
        ]

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "releases", "my-pkg"]):
            with patch.object(
                PackageStatsService,
                "fetch_package_releases",
                return_value=(mock_pypi, mock_gh),
            ):
                with self._no_config():
                    main()

        captured = capsys.readouterr()
        assert "0.1.0" in captured.out
        assert "v0.1.0" in captured.out
        assert "PyPI" in captured.out
        assert "GitHub" in captured.out

    def test_releases_no_releases(self, temp_db, caplog):
        """releases should warn when no releases found."""
        with patch("sys.argv", ["pkgdb", "-d", temp_db, "releases", "my-pkg"]):
            with patch.object(
                PackageStatsService, "fetch_package_releases", return_value=([], [])
            ):
                with self._no_config():
                    main()

        assert "No releases found" in caplog.text

    def test_report_project_flag(self):
        """report command should accept --project flag."""
        from pkgdb.cli import create_parser

        parser = create_parser()
        args = parser.parse_args(["report", "my-pkg", "--project", "--no-browser"])
        assert args.project is True
        assert args.package == "my-pkg"


class TestHistoryDailySeries:
    """Tests for history rendering the true daily download series (Step 2)."""

    def _no_config(self):
        return patch(
            "pkgdb.config.get_config_path",
            return_value=Path("/nonexistent/config.toml"),
        )

    @staticmethod
    def _seed_daily(
        temp_db, package, rows, dimension="overall", category="without_mirrors"
    ):
        """Seed daily_downloads rows: rows is a list of (date, downloads)."""
        from pkgdb import store_daily_downloads

        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, package)
        store_daily_downloads(
            conn,
            package,
            [
                {
                    "date": d,
                    "dimension": dimension,
                    "category": category,
                    "downloads": n,
                }
                for d, n in rows
            ],
        )
        conn.close()

    def test_text_prefers_daily_series(self, temp_db, capsys):
        """--text should show the per-day series when daily data exists."""
        self._seed_daily(
            temp_db,
            "my-pkg",
            [("2026-06-01", 100), ("2026-06-02", 120), ("2026-06-03", 90)],
        )
        with patch("sys.argv", ["pkgdb", "-d", temp_db, "history", "my-pkg", "--text"]):
            with self._no_config():
                main()
        out = capsys.readouterr().out
        assert "Daily downloads for my-pkg" in out
        assert "Downloads" in out
        # Per-day figures, not rolling snapshot columns
        assert "Month" not in out
        assert "120" in out

    def test_json_prefers_daily_series(self, temp_db, capsys):
        """--json should emit the per-day series when daily data exists."""
        self._seed_daily(
            temp_db,
            "my-pkg",
            [("2026-06-01", 100), ("2026-06-02", 120)],
        )
        with patch("sys.argv", ["pkgdb", "-d", temp_db, "history", "my-pkg", "--json"]):
            with self._no_config():
                main()
        data = json.loads(capsys.readouterr().out)
        assert data == [
            {"date": "2026-06-01", "downloads": 100},
            {"date": "2026-06-02", "downloads": 120},
        ]

    def test_json_filters_daily_by_since(self, temp_db, capsys):
        """--since should filter the daily series."""
        self._seed_daily(
            temp_db,
            "my-pkg",
            [("2026-06-01", 100), ("2026-06-05", 120), ("2026-06-10", 90)],
        )
        with patch(
            "sys.argv",
            [
                "pkgdb",
                "-d",
                temp_db,
                "history",
                "my-pkg",
                "--json",
                "--since",
                "2026-06-05",
            ],
        ):
            with self._no_config():
                main()
        data = json.loads(capsys.readouterr().out)
        assert [r["date"] for r in data] == ["2026-06-05", "2026-06-10"]

    def test_text_falls_back_to_snapshot_without_daily(self, temp_db, capsys):
        """Without daily data, --text shows the snapshot progression."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "my-pkg")
        conn.execute(
            """INSERT INTO package_stats
               (package_name, fetch_date, last_day, last_week, last_month, total)
               VALUES ('my-pkg', '2024-01-01', 10, 70, 300, 1000)""",
        )
        conn.commit()
        conn.close()
        with patch("sys.argv", ["pkgdb", "-d", temp_db, "history", "my-pkg", "--text"]):
            with self._no_config():
                main()
        out = capsys.readouterr().out
        assert "Historical stats for my-pkg" in out
        assert "Month" in out  # snapshot columns

    def test_html_report_uses_daily_chart_title(self, temp_db):
        """The HTML project report labels the chart 'Daily' when daily data exists."""
        rows = [(f"2026-06-{d:02d}", 100 + d) for d in range(1, 11)]
        self._seed_daily(temp_db, "my-pkg", rows)
        # A snapshot row supplies the stat cards, avoiding a live stats fetch.
        conn = get_db_connection(temp_db)
        conn.execute(
            """INSERT INTO package_stats
               (package_name, fetch_date, last_day, last_week, last_month, total)
               VALUES ('my-pkg', '2026-06-10', 110, 700, 3000, 50000)""",
        )
        conn.commit()
        conn.close()

        output = os.path.join(os.path.dirname(temp_db), "daily_report.html")
        service = PackageStatsService(temp_db)
        with (
            patch.object(
                PackageStatsService, "fetch_package_releases", return_value=([], [])
            ),
            patch("pkgdb.reports.fetch_python_versions", return_value=None),
            patch("pkgdb.reports.fetch_os_stats", return_value=None),
        ):
            assert service.generate_project_report("my-pkg", output) is True

        html = Path(output).read_text()
        assert (
            "Daily Downloads &amp; Releases" in html
            or "Daily Downloads & Releases" in html
        )
        Path(output).unlink(missing_ok=True)


class TestHistoryHtmlFiltering:
    """`history` defaults to HTML, so --since/--limit must reach that path.

    The text and JSON paths filter in the command itself; the HTML path renders
    through generate_project_report, which previously charted every stored day
    regardless of the flags the user passed.
    """

    def _seed(self, temp_db, days=12):
        """Track a package with one daily row per day from 2024-01-01."""
        conn = get_db_connection(temp_db)
        init_db(conn)
        rows = []
        for i in range(days):
            date = f"2024-01-{i + 1:02d}"
            rows.append(
                {
                    "date": date,
                    "dimension": "overall",
                    "category": "without_mirrors",
                    "downloads": 100 + i,
                }
            )
            conn.execute(
                "INSERT INTO package_stats (package_name, fetch_date, last_day,"
                " last_week, last_month, total) VALUES (?, ?, ?, ?, ?, ?)",
                ("test-pkg", date, 10, 70, 300, 1000 + i),
            )
        store_daily_downloads(conn, "test-pkg", rows)
        track(conn, "test-pkg")
        conn.close()

    def _run_history(self, temp_db, extra_args):
        """Run `history` in its default HTML mode and return the HTML."""
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
            output_path = f.name
        try:
            argv = [
                "pkgdb",
                "-d",
                temp_db,
                "history",
                "test-pkg",
                "-o",
                output_path,
                "--no-browser",
            ] + extra_args
            with patch("sys.argv", argv):
                with patch(
                    "pkgdb.service.PackageStatsService.fetch_package_releases",
                    return_value=([], []),
                ):
                    with patch(
                        "pkgdb.reports.fetch_python_versions", return_value=None
                    ):
                        with patch("pkgdb.reports.fetch_os_stats", return_value=None):
                            main()
            return Path(output_path).read_text()
        finally:
            Path(output_path).unlink(missing_ok=True)

    def test_html_history_honors_since(self, temp_db):
        """--since should trim the charted daily series, not just the guard."""
        self._seed(temp_db)
        content = self._run_history(temp_db, ["--since", "2024-01-10"])

        assert "2024-01-10" in content
        assert "2024-01-12" in content
        assert "2024-01-01" not in content
        assert "2024-01-09" not in content

    def test_html_history_honors_relative_since(self, temp_db):
        """Relative forms like 7d are what the README documents."""
        self._seed(temp_db)
        # Everything seeded is well before today, so a 7d window charts nothing
        # and the command should report no data rather than a full-range report.
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
            output_path = f.name
        try:
            argv = [
                "pkgdb",
                "-d",
                temp_db,
                "history",
                "test-pkg",
                "-o",
                output_path,
                "--no-browser",
                "--since",
                "7d",
            ]
            with patch("sys.argv", argv):
                main()
            assert not Path(output_path).read_text()
        finally:
            Path(output_path).unlink(missing_ok=True)

    def test_html_history_without_since_charts_everything(self, temp_db):
        """Absent --since the report keeps its full-range behavior."""
        self._seed(temp_db)
        content = self._run_history(temp_db, [])

        assert "2024-01-01" in content
        assert "2024-01-12" in content

    def test_html_history_honors_limit_for_snapshots(self, temp_db):
        """--limit caps the snapshot series the report falls back to."""
        self._seed(temp_db)
        # Drop the daily series so the report must use snapshot history.
        conn = get_db_connection(temp_db)
        conn.execute("DELETE FROM daily_downloads")
        conn.commit()
        conn.close()

        content = self._run_history(temp_db, ["--limit", "3"])

        assert "2024-01-12" in content
        assert "2024-01-10" in content
        assert "2024-01-01" not in content
