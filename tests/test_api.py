"""Tests for PyPI API interactions: fetching stats, user packages, and package existence checks."""

import json
from unittest.mock import patch

import pytest

from pkgdb import (
    fetch_package_stats,
    fetch_python_versions,
    fetch_os_stats,
    fetch_daily_downloads,
    aggregate_env_stats,
    fetch_user_packages,
    check_package_exists,
)


class TestFetchPackageStats:
    """Tests for fetching stats from PyPI API."""

    def test_fetch_package_stats_parses_response(self):
        """fetch_package_stats should parse pypistats responses correctly."""
        recent_response = json.dumps(
            {"data": {"last_day": 100, "last_week": 700, "last_month": 3000}}
        )
        overall_response = json.dumps(
            {
                "data": [
                    {"category": "with_mirrors", "downloads": 100000},
                    {"category": "without_mirrors", "downloads": 50000},
                ]
            }
        )

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
        mock_response = json.dumps(
            {
                "data": [
                    {"category": "3.10", "downloads": 1000},
                    {"category": "3.11", "downloads": 2000},
                    {"category": "3.9", "downloads": 500},
                ]
            }
        )

        with patch("pkgdb.api.pypistats.python_minor", return_value=mock_response):
            versions = fetch_python_versions("test-package")

        assert versions is not None
        assert len(versions) == 3
        # Should be sorted by downloads descending
        assert versions[0]["category"] == "3.11"
        assert versions[0]["downloads"] == 2000

    def test_fetch_python_versions_handles_error(self, capsys):
        """fetch_python_versions should return None on error."""
        with patch(
            "pkgdb.api.pypistats.python_minor", side_effect=ValueError("API error")
        ):
            versions = fetch_python_versions("nonexistent-package")

        assert versions is None

    def test_fetch_os_stats_parses_response(self):
        """fetch_os_stats should parse pypistats response correctly."""
        mock_response = json.dumps(
            {
                "data": [
                    {"category": "Linux", "downloads": 5000},
                    {"category": "Windows", "downloads": 2000},
                    {"category": "Darwin", "downloads": 1000},
                ]
            }
        )

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


class TestFetchDailyDownloads:
    """Tests for the daily time-series fetch across all dimensions."""

    @staticmethod
    def _daily_response(category_dates):
        """Build a pypistats-style daily response.

        category_dates: list of (category, date, downloads) tuples.
        """
        return json.dumps(
            {
                "data": [
                    {"category": c, "date": d, "downloads": n}
                    for c, d, n in category_dates
                ]
            }
        )

    def test_combines_all_dimensions_and_tags_them(self):
        overall = self._daily_response(
            [
                ("with_mirrors", "2026-01-01", 120),
                ("without_mirrors", "2026-01-01", 100),
            ]
        )
        python = self._daily_response([("3.12", "2026-01-01", 40)])
        system = self._daily_response([("Linux", "2026-01-01", 80)])

        with (
            patch("pkgdb.api.pypistats.overall", return_value=overall),
            patch("pkgdb.api.pypistats.python_minor", return_value=python),
            patch("pkgdb.api.pypistats.system", return_value=system),
        ):
            records = fetch_daily_downloads("pkg")

        assert records is not None
        dims = {r["dimension"] for r in records}
        assert dims == {"overall", "python", "os"}
        assert len(records) == 4
        py = [r for r in records if r["dimension"] == "python"][0]
        assert py["category"] == "3.12"
        assert py["downloads"] == 40

    def test_requests_daily_totals(self):
        """Every dimension must be fetched with total='daily'."""
        empty = json.dumps({"data": []})
        with (
            patch("pkgdb.api.pypistats.overall", return_value=empty) as m_overall,
            patch("pkgdb.api.pypistats.python_minor", return_value=empty),
            patch("pkgdb.api.pypistats.system", return_value=empty),
        ):
            fetch_daily_downloads("pkg")
        _, kwargs = m_overall.call_args
        assert kwargs.get("total") == "daily"

    def test_partial_failure_returns_other_dimensions(self):
        overall = self._daily_response([("without_mirrors", "2026-01-01", 100)])
        with (
            patch("pkgdb.api.pypistats.overall", return_value=overall),
            patch(
                "pkgdb.api.pypistats.python_minor",
                side_effect=ValueError("boom"),
            ),
            patch("pkgdb.api.pypistats.system", side_effect=ValueError("boom")),
        ):
            records = fetch_daily_downloads("pkg")

        assert records is not None
        assert {r["dimension"] for r in records} == {"overall"}

    def test_all_failures_return_none(self):
        with (
            patch("pkgdb.api.pypistats.overall", side_effect=ValueError("boom")),
            patch("pkgdb.api.pypistats.python_minor", side_effect=ValueError("boom")),
            patch("pkgdb.api.pypistats.system", side_effect=ValueError("boom")),
        ):
            assert fetch_daily_downloads("pkg") is None

    def test_skips_rows_missing_date_or_category(self):
        response = json.dumps(
            {
                "data": [
                    {
                        "category": "without_mirrors",
                        "date": "2026-01-01",
                        "downloads": 100,
                    },
                    {"category": None, "date": "2026-01-01", "downloads": 5},
                    {"category": "without_mirrors", "date": None, "downloads": 5},
                ]
            }
        )
        empty = json.dumps({"data": []})
        with (
            patch("pkgdb.api.pypistats.overall", return_value=response),
            patch("pkgdb.api.pypistats.python_minor", return_value=empty),
            patch("pkgdb.api.pypistats.system", return_value=empty),
        ):
            records = fetch_daily_downloads("pkg")

        assert records is not None
        assert len(records) == 1
        assert records[0]["category"] == "without_mirrors"


class TestFetchUserPackages:
    """Tests for fetch_user_packages function."""

    def test_fetch_user_packages_success(self):
        """fetch_user_packages should return list of package names."""
        mock_packages = [["Owner", "pkg-c"], ["Owner", "pkg-a"], ["Owner", "pkg-b"]]

        with patch("pkgdb.api.xmlrpc.client.ServerProxy") as mock_proxy:
            mock_proxy.return_value.user_packages.return_value = mock_packages
            result = fetch_user_packages("testuser")

        assert result == ["pkg-a", "pkg-b", "pkg-c"]  # Should be sorted

    def test_fetch_user_packages_deduplicates(self):
        """fetch_user_packages should deduplicate packages."""
        # User might have multiple roles for same package
        mock_packages = [
            ["Owner", "pkg-a"],
            ["Maintainer", "pkg-a"],
            ["Owner", "pkg-b"],
        ]

        with patch("pkgdb.api.xmlrpc.client.ServerProxy") as mock_proxy:
            mock_proxy.return_value.user_packages.return_value = mock_packages
            result = fetch_user_packages("testuser")

        assert result == ["pkg-a", "pkg-b"]

    def test_fetch_user_packages_empty(self):
        """fetch_user_packages should return empty list for user with no packages."""
        with patch("pkgdb.api.xmlrpc.client.ServerProxy") as mock_proxy:
            mock_proxy.return_value.user_packages.return_value = []
            result = fetch_user_packages("testuser")

        assert result == []

    def test_fetch_user_packages_api_error(self):
        """fetch_user_packages should return None on API error."""
        import xmlrpc.client

        with patch("pkgdb.api.xmlrpc.client.ServerProxy") as mock_proxy:
            mock_proxy.return_value.user_packages.side_effect = xmlrpc.client.Fault(
                1, "Error"
            )
            result = fetch_user_packages("testuser")

        assert result is None

    def test_fetch_user_packages_network_error(self):
        """fetch_user_packages should return None on network error."""
        with patch("pkgdb.api.xmlrpc.client.ServerProxy") as mock_proxy:
            mock_proxy.return_value.user_packages.side_effect = OSError(
                "Connection refused"
            )
            result = fetch_user_packages("testuser")

        assert result is None


class TestAggregateEnvStats:
    """Tests for aggregating environment stats across packages."""

    def test_aggregate_env_stats_combines_packages(self):
        """aggregate_env_stats should combine stats from multiple packages."""
        python_response_1 = json.dumps(
            {"data": [{"category": "3.11", "downloads": 2000}]}
        )
        python_response_2 = json.dumps(
            {"data": [{"category": "3.11", "downloads": 1000}]}
        )
        system_response_1 = json.dumps(
            {"data": [{"category": "Linux", "downloads": 4000}]}
        )
        system_response_2 = json.dumps(
            {"data": [{"category": "Linux", "downloads": 2000}]}
        )

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
        with patch(
            "pkgdb.api.pypistats.python_minor", side_effect=ValueError("API error")
        ):
            with patch(
                "pkgdb.api.pypistats.system", side_effect=ValueError("API error")
            ):
                result = aggregate_env_stats(["pkg-a"])

        assert result["python_versions"] == []
        assert result["os_distribution"] == []

    def test_aggregate_env_stats_empty_packages(self):
        """aggregate_env_stats should handle empty package list."""
        result = aggregate_env_stats([])
        assert result["python_versions"] == []
        assert result["os_distribution"] == []


class TestCheckPackageExists:
    """Tests for check_package_exists function."""

    def test_existing_package_returns_true(self):
        """check_package_exists returns (True, None) for existing package."""
        # Mock a successful HEAD request
        with patch("pkgdb.api.urlopen") as mock_urlopen:
            mock_response = type(
                "MockResponse",
                (),
                {
                    "status": 200,
                    "__enter__": lambda s: s,
                    "__exit__": lambda s, *a: None,
                },
            )()
            mock_urlopen.return_value = mock_response

            exists, error = check_package_exists("requests")
            assert exists is True
            assert error is None

    def test_nonexistent_package_returns_false(self):
        """check_package_exists returns (False, None) for 404."""
        from urllib.error import HTTPError

        with patch("pkgdb.api.urlopen") as mock_urlopen:
            error = HTTPError("url", 404, "Not Found", {}, None)
            error.code = 404
            mock_urlopen.side_effect = error

            exists, err = check_package_exists("nonexistent-pkg-xyz123")
            assert exists is False
            assert err is None

    def test_network_error_returns_none_with_message(self):
        """check_package_exists returns (None, message) on network error."""
        from urllib.error import URLError

        with patch("pkgdb.api.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = URLError("Connection refused")

            exists, error = check_package_exists("some-package")
            assert exists is None
            assert error is not None
            assert "Network error" in error

    def test_timeout_returns_none_with_message(self):
        """check_package_exists returns (None, message) on timeout."""
        with patch("pkgdb.api.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = TimeoutError()

            exists, error = check_package_exists("some-package")
            assert exists is None
            assert error is not None
            assert "timed out" in error.lower()
