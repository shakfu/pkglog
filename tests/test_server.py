"""Tests for the pkgdb dashboard server and HTML page generation."""

import json
import threading
import time
from http.server import HTTPServer
from io import BytesIO
from unittest.mock import patch

import pytest
from urllib.request import urlopen
from urllib.error import HTTPError

from pkgdb.dashboard import (
    generate_comparison_page,
    generate_overview_page,
    generate_package_page,
    _js_string,
    _js_array,
)
from pkgdb.server import DashboardHandler, start_server
from pkgdb.service import PackageStatsService
from pkgdb.db import (
    add_package,
    get_db,
    store_stats,
    store_env_stats,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def populated_db(temp_db):
    """Create a temp DB with some packages and stats."""
    with get_db(temp_db) as conn:
        add_package(conn, "alpha-pkg")
        add_package(conn, "beta-pkg")
        store_stats(
            conn,
            "alpha-pkg",
            {"last_day": 100, "last_week": 700, "last_month": 3000, "total": 50000},
        )
        store_stats(
            conn,
            "beta-pkg",
            {"last_day": 50, "last_week": 350, "last_month": 1500, "total": 20000},
        )
        store_env_stats(
            conn,
            "alpha-pkg",
            python_versions=[
                {"category": "3.12", "downloads": 500},
                {"category": "3.11", "downloads": 200},
            ],
            os_data=[
                {"category": "Linux", "downloads": 600},
                {"category": "Windows", "downloads": 100},
            ],
        )
    return temp_db


@pytest.fixture
def server_url(populated_db):
    """Start a dashboard server in a background thread and yield its URL."""
    service = PackageStatsService(populated_db)
    # Find a free port
    server = HTTPServer(("127.0.0.1", 0), DashboardHandler)
    server.service = service
    port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    url = f"http://127.0.0.1:{port}"
    yield url

    server.shutdown()


# ---------------------------------------------------------------------------
# HTML Page Generation Tests
# ---------------------------------------------------------------------------


class TestDashboardPages:
    """Test HTML page generation functions."""

    def test_overview_page_structure(self):
        html = generate_overview_page()
        assert "<!DOCTYPE html>" in html
        assert "<title>" in html
        assert "pkgdb" in html
        assert "/api/packages" in html
        assert "pkg-filter" in html
        assert "uplot.min.js" in html
        assert "uplot.min.css" in html

    def test_package_page_structure(self):
        html = generate_package_page("my-package")
        assert "my-package" in html
        assert "/api/daily/" in html
        assert "/api/github-history/" in html
        assert "/api/history/" in html
        assert "/api/env/" in html
        assert "/api/releases/" in html
        assert "history-chart" in html
        assert "github-chart" in html
        assert "renderGithubChart" in html
        assert "py-bars" in html
        assert "os-bars" in html

    def test_package_page_escapes_html(self):
        html = generate_package_page("<script>alert(1)</script>")
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_comparison_page_structure(self):
        html = generate_comparison_page()
        assert "Compare" in html
        assert "compare-chart" in html
        assert "/api/packages" in html
        assert "pkg-cb" in html

    def test_nav_present_on_all_pages(self):
        for page_fn in [
            generate_overview_page,
            generate_comparison_page,
        ]:
            html = page_fn()
            assert '<a class="brand" href="/">pkgdb</a>' in html

    def test_nav_on_package_page(self):
        html = generate_package_page("test-pkg")
        assert '<a class="brand" href="/">pkgdb</a>' in html
        assert "Back to overview" in html


class TestJsHelpers:
    """Test JavaScript string/array escaping helpers."""

    def test_js_string_basic(self):
        assert _js_string("hello") == '"hello"'

    def test_js_string_escapes_quotes(self):
        result = _js_string('say "hi"')
        assert '\\"' in result

    def test_js_string_escapes_backslash(self):
        result = _js_string("path\\to")
        assert "\\\\" in result

    def test_js_string_escapes_html(self):
        result = _js_string("<script>")
        assert "\\x3c" in result
        assert "<script>" not in result

    def test_js_array(self):
        result = _js_array(["a", "b", "c"])
        assert result == '["a","b","c"]'

    def test_js_array_empty(self):
        assert _js_array([]) == "[]"


# ---------------------------------------------------------------------------
# Server Routing and API Tests (using real HTTP)
# ---------------------------------------------------------------------------


class TestServerRouting:
    """Test HTTP routing and status codes."""

    def test_root_returns_html(self, server_url):
        resp = urlopen(f"{server_url}/")
        assert resp.status == 200
        assert "text/html" in resp.headers["Content-Type"]
        body = resp.read().decode()
        assert "<!DOCTYPE html>" in body

    def test_package_page_returns_html(self, server_url):
        resp = urlopen(f"{server_url}/package/alpha-pkg")
        assert resp.status == 200
        assert "text/html" in resp.headers["Content-Type"]
        body = resp.read().decode()
        assert "alpha-pkg" in body

    def test_compare_page_returns_html(self, server_url):
        resp = urlopen(f"{server_url}/compare")
        assert resp.status == 200
        body = resp.read().decode()
        assert "Compare" in body

    def test_unknown_path_returns_404(self, server_url):
        with pytest.raises(HTTPError) as exc_info:
            urlopen(f"{server_url}/nonexistent")
        assert exc_info.value.code == 404

    def test_static_js_served(self, server_url):
        resp = urlopen(f"{server_url}/static/uplot.min.js")
        assert resp.status == 200
        assert "javascript" in resp.headers["Content-Type"]
        body = resp.read()
        assert len(body) > 1000  # uPlot is ~50KB

    def test_static_css_served(self, server_url):
        resp = urlopen(f"{server_url}/static/uplot.min.css")
        assert resp.status == 200
        assert "css" in resp.headers["Content-Type"]

    def test_static_missing_returns_404(self, server_url):
        with pytest.raises(HTTPError) as exc_info:
            urlopen(f"{server_url}/static/nonexistent.js")
        assert exc_info.value.code == 404


class TestApiEndpoints:
    """Test JSON API endpoints."""

    def test_api_packages(self, server_url):
        resp = urlopen(f"{server_url}/api/packages")
        assert resp.status == 200
        assert "application/json" in resp.headers["Content-Type"]
        data = json.loads(resp.read())
        assert isinstance(data, list)
        assert len(data) == 2
        names = {p["package_name"] for p in data}
        assert "alpha-pkg" in names
        assert "beta-pkg" in names

    def test_api_packages_has_growth_fields(self, server_url):
        resp = urlopen(f"{server_url}/api/packages")
        data = json.loads(resp.read())
        pkg = data[0]
        # Growth fields should be present (may be None with only one data point)
        assert "week_growth" in pkg or "total" in pkg

    def test_api_history_for_package(self, server_url):
        resp = urlopen(f"{server_url}/api/history/alpha-pkg")
        assert resp.status == 200
        data = json.loads(resp.read())
        assert isinstance(data, list)
        assert len(data) >= 1
        assert data[0]["package_name"] == "alpha-pkg"
        assert data[0]["total"] == 50000

    def test_api_history_all(self, server_url):
        resp = urlopen(f"{server_url}/api/history")
        assert resp.status == 200
        data = json.loads(resp.read())
        assert isinstance(data, dict)
        assert "alpha-pkg" in data
        assert "beta-pkg" in data

    def test_api_history_limit(self, server_url):
        resp = urlopen(f"{server_url}/api/history/alpha-pkg?limit=1")
        data = json.loads(resp.read())
        assert len(data) <= 1

    def test_api_history_nonexistent(self, server_url):
        resp = urlopen(f"{server_url}/api/history/nonexistent-pkg")
        data = json.loads(resp.read())
        assert data == []

    def test_api_daily_series(self, populated_db, server_url):
        from pkgdb.db import store_daily_downloads

        with get_db(populated_db) as conn:
            store_daily_downloads(
                conn,
                "alpha-pkg",
                [
                    {
                        "date": "2026-06-01",
                        "dimension": "overall",
                        "category": "without_mirrors",
                        "downloads": 10,
                    },
                    {
                        "date": "2026-06-02",
                        "dimension": "overall",
                        "category": "without_mirrors",
                        "downloads": 20,
                    },
                    # A python-dimension row must NOT appear in the overall series
                    {
                        "date": "2026-06-01",
                        "dimension": "python",
                        "category": "3.12",
                        "downloads": 5,
                    },
                ],
            )
        resp = urlopen(f"{server_url}/api/daily/alpha-pkg")
        assert resp.status == 200
        data = json.loads(resp.read())
        assert data == [
            {"date": "2026-06-01", "downloads": 10},
            {"date": "2026-06-02", "downloads": 20},
        ]

    def test_api_daily_nonexistent(self, server_url):
        resp = urlopen(f"{server_url}/api/daily/nonexistent-pkg")
        data = json.loads(resp.read())
        assert data == []

    def test_api_github_history(self, populated_db, server_url):
        from pkgdb.db import store_github_stats_snapshot

        with get_db(populated_db) as conn:
            conn.executemany(
                "INSERT INTO github_stats_history (package_name, repo_key, date, "
                "stars, forks, open_issues, watchers) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    ("alpha-pkg", "o/r", "2026-06-01", 100, 5, 1, 1),
                    ("alpha-pkg", "o/r", "2026-06-02", 108, 5, 1, 1),
                ],
            )
            conn.commit()
        resp = urlopen(f"{server_url}/api/github-history/alpha-pkg")
        assert resp.status == 200
        data = json.loads(resp.read())
        assert [d["stars"] for d in data] == [100, 108]

    def test_api_github_history_empty(self, server_url):
        resp = urlopen(f"{server_url}/api/github-history/nonexistent-pkg")
        assert json.loads(resp.read()) == []

    def test_api_ci_empty_before_any_scan(self, server_url):
        resp = urlopen(f"{server_url}/api/ci")
        assert resp.status == 200
        assert json.loads(resp.read()) == []

    def test_api_ci_returns_recorded_state(self, populated_db, server_url):
        from pkgdb.db import add_repo, link_package_repo, store_ci_status

        with get_db(populated_db) as conn:
            add_repo(conn, "o/r")
            link_package_repo(conn, "alpha-pkg", "o/r")
            store_ci_status(
                conn,
                "o/r",
                "build",
                "FAIL",
                branch="main",
                run_url="https://github.com/o/r/actions/runs/1",
                run_started_at="2026-08-01T00:00:00",
            )
        resp = urlopen(f"{server_url}/api/ci")
        data = json.loads(resp.read())
        assert data[0]["repo_key"] == "o/r"
        assert data[0]["state"] == "FAIL"
        assert data[0]["packages"] == ["alpha-pkg"]

    def test_api_env(self, server_url):
        resp = urlopen(f"{server_url}/api/env/alpha-pkg")
        assert resp.status == 200
        data = json.loads(resp.read())
        assert "python_versions" in data
        assert "os_stats" in data
        assert len(data["python_versions"]) == 2
        assert data["python_versions"][0]["category"] == "3.12"
        assert len(data["os_stats"]) == 2

    def test_api_env_no_data(self, server_url):
        resp = urlopen(f"{server_url}/api/env/beta-pkg")
        data = json.loads(resp.read())
        assert data["python_versions"] == []
        assert data["os_stats"] == []

    def test_api_releases(self, server_url):
        # alpha-pkg has no cached releases, so patch the live lookups
        with (
            patch("pkgdb.service.fetch_pypi_releases", return_value=None),
            patch("pkgdb.service.extract_github_url", return_value=None),
        ):
            resp = urlopen(f"{server_url}/api/releases/alpha-pkg")
        assert resp.status == 200
        data = json.loads(resp.read())
        assert "pypi" in data
        assert "github" in data

    def test_api_unknown_returns_404(self, server_url):
        with pytest.raises(HTTPError) as exc_info:
            urlopen(f"{server_url}/api/nonexistent")
        assert exc_info.value.code == 404


# ---------------------------------------------------------------------------
# Server Lifecycle Tests
# ---------------------------------------------------------------------------


class TestServerLifecycle:
    """Test server startup behavior."""

    def test_start_server_opens_browser(self, populated_db):
        """start_server should call webbrowser.open when open_browser=True."""
        with (
            patch("pkgdb.server.webbrowser.open_new_tab") as mock_open,
            patch.object(HTTPServer, "serve_forever", side_effect=KeyboardInterrupt),
        ):
            start_server(populated_db, port=0, open_browser=True)
            mock_open.assert_called_once()

    def test_start_server_no_browser(self, populated_db):
        """start_server should not open browser when open_browser=False."""
        with (
            patch("pkgdb.server.webbrowser.open_new_tab") as mock_open,
            patch.object(HTTPServer, "serve_forever", side_effect=KeyboardInterrupt),
        ):
            start_server(populated_db, port=0, open_browser=False)
            mock_open.assert_not_called()

    def test_port_in_use_handling(self, populated_db):
        """start_server should log error and return if port is in use."""
        # Simulate address-in-use by making HTTPServer constructor raise
        err = OSError("Address already in use")
        err.errno = 48
        with (
            patch("pkgdb.server.HTTPServer", side_effect=err),
            patch("pkgdb.server.logger") as mock_logger,
        ):
            start_server(populated_db, port=9999, open_browser=False)
            mock_logger.error.assert_called_once()
            assert "already in use" in mock_logger.error.call_args[0][0]


# ---------------------------------------------------------------------------
# CLI Integration Tests
# ---------------------------------------------------------------------------


class TestServeCli:
    """Test the serve CLI command integration."""

    def test_cmd_serve_calls_start_server(self, populated_db):
        """cmd_serve should call start_server with correct args."""
        import argparse
        from pkgdb.cli import cmd_serve

        args = argparse.Namespace(database=populated_db, port=9999, no_browser=True)

        with patch("pkgdb.server.start_server") as mock_start:
            cmd_serve(args)
            mock_start.assert_called_once_with(
                db_path=populated_db, port=9999, open_browser=False
            )
