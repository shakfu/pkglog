"""Tests for GitHub integration: URL parsing, repo stats, API interactions."""

import json
import os
import subprocess
import time
from datetime import datetime, timedelta

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from pkgdb import (
    get_db_connection,
    init_db,
    RepoStats,
    RepoResult,
    parse_github_url,
    extract_github_url,
    get_github_token,
)
from pkgdb.utils import utcnow
from pkgdb.github import (
    _parse_repo_data,
    _parse_datetime,
    fetch_package_github_stats,
)


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


class TestParseGithubUrl:
    """Tests for parse_github_url function."""

    def test_parse_https_url(self):
        result = parse_github_url("https://github.com/owner/repo")
        assert result == ("owner", "repo")

    def test_parse_url_with_www(self):
        result = parse_github_url("https://www.github.com/owner/repo")
        assert result == ("owner", "repo")

    def test_parse_http_url(self):
        result = parse_github_url("http://github.com/owner/repo")
        assert result == ("owner", "repo")

    def test_parse_url_with_trailing_slash(self):
        result = parse_github_url("https://github.com/owner/repo/")
        assert result == ("owner", "repo")

    def test_parse_url_with_subpath(self):
        result = parse_github_url("https://github.com/owner/repo/tree/main")
        assert result == ("owner", "repo")

    def test_parse_url_with_git_suffix(self):
        result = parse_github_url("https://github.com/owner/repo.git")
        assert result == ("owner", "repo")

    def test_parse_non_github_url(self):
        result = parse_github_url("https://gitlab.com/owner/repo")
        assert result is None

    def test_parse_empty_url(self):
        result = parse_github_url("")
        assert result is None


class TestRepoStats:
    """Tests for RepoStats dataclass."""

    def test_days_since_push(self):
        yesterday = utcnow() - timedelta(days=1)
        stats = _make_repo_stats(pushed_at=yesterday)
        assert stats.days_since_push == 1

    @pytest.mark.skipif(
        not hasattr(time, "tzset"), reason="TZ manipulation requires a POSIX platform"
    )
    @pytest.mark.parametrize("tz", ["Etc/GMT-3", "Etc/GMT+5", "UTC"])
    def test_days_since_push_ignores_local_timezone(self, tz, in_timezone):
        """`pushed_at` is UTC from the API, so the age must be measured in UTC.

        Measuring it against a local clock shifted the answer by the machine's
        UTC offset, which flips the reported age by a whole day either side of
        the boundary and with it `activity_status`.
        """
        stats = _make_repo_stats(pushed_at=utcnow() - timedelta(days=1, hours=6))
        assert stats.days_since_push == 1

    def test_days_since_push_none(self):
        stats = _make_repo_stats(pushed_at=None)
        assert stats.days_since_push is None

    def test_is_active_recent_push(self):
        stats = _make_repo_stats(pushed_at=utcnow() - timedelta(days=10))
        assert stats.is_active is True

    def test_is_active_old_push(self):
        stats = _make_repo_stats(pushed_at=utcnow() - timedelta(days=400))
        assert stats.is_active is False

    def test_activity_status_very_active(self):
        stats = _make_repo_stats(pushed_at=utcnow() - timedelta(days=5))
        assert stats.activity_status == "very active"

    def test_activity_status_active(self):
        stats = _make_repo_stats(pushed_at=utcnow() - timedelta(days=60))
        assert stats.activity_status == "active"

    def test_activity_status_maintained(self):
        stats = _make_repo_stats(pushed_at=utcnow() - timedelta(days=200))
        assert stats.activity_status == "maintained"

    def test_activity_status_stale(self):
        stats = _make_repo_stats(pushed_at=utcnow() - timedelta(days=400))
        assert stats.activity_status == "stale"

    def test_activity_status_archived(self):
        stats = _make_repo_stats(archived=True)
        assert stats.activity_status == "archived"

    def test_activity_status_no_push_date(self):
        stats = _make_repo_stats(pushed_at=None)
        assert stats.activity_status == "unknown"


class TestRepoResult:
    """Tests for RepoResult dataclass."""

    def test_success_with_stats(self):
        stats = _make_repo_stats()
        result = RepoResult(
            package_name="test-pkg",
            repo_url="https://github.com/test/repo",
            stats=stats,
        )
        assert result.success is True

    def test_failure_with_error(self):
        result = RepoResult(package_name="test-pkg", repo_url=None, error="Not found")
        assert result.success is False

    def test_no_github_repo(self):
        result = RepoResult(
            package_name="test-pkg", repo_url=None, error="No GitHub repository found"
        )
        assert result.success is False
        assert result.repo_url is None


class TestParseRepoData:
    """Tests for _parse_repo_data function."""

    def test_parse_full_response(self):
        data = _make_github_api_response()
        stats = _parse_repo_data(data)
        assert stats.owner == "testowner"
        assert stats.name == "testrepo"
        assert stats.full_name == "testowner/testrepo"
        assert stats.stars == 42
        assert stats.forks == 5
        assert stats.open_issues == 3
        assert stats.watchers == 10
        assert stats.language == "Python"
        assert stats.license == "MIT"
        assert stats.archived is False
        assert stats.fork is False
        assert stats.default_branch == "main"
        assert "python" in stats.topics

    def test_parse_response_no_license(self):
        data = _make_github_api_response(license=None)
        stats = _parse_repo_data(data)
        assert stats.license is None

    def test_parse_response_no_homepage(self):
        data = _make_github_api_response(homepage="")
        stats = _parse_repo_data(data)
        assert stats.homepage is None

    def test_parse_response_missing_optional_fields(self):
        data = _make_github_api_response()
        del data["language"]
        del data["topics"]
        stats = _parse_repo_data(data)
        assert stats.language is None
        assert stats.topics == []


class TestParseDatetime:
    """Tests for _parse_datetime helper."""

    def test_parse_iso_with_z(self):
        result = _parse_datetime("2024-01-15T10:30:00Z")
        assert result is not None
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15
        assert result.tzinfo is None

    def test_parse_none(self):
        result = _parse_datetime(None)
        assert result is None


class TestExtractGithubUrl:
    """Tests for extract_github_url function."""

    def test_extract_from_project_urls_repository(self):
        mock_response = json.dumps(
            {
                "info": {
                    "project_urls": {
                        "Repository": "https://github.com/owner/repo",
                        "Homepage": "https://example.com",
                    }
                }
            }
        ).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = mock_response
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("pkgdb.github.urlopen", return_value=mock_resp):
            url = extract_github_url("test-pkg")
        assert url == "https://github.com/owner/repo"

    def test_extract_from_project_urls_source(self):
        mock_response = json.dumps(
            {
                "info": {
                    "project_urls": {
                        "Source": "https://github.com/owner/repo",
                    }
                }
            }
        ).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = mock_response
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("pkgdb.github.urlopen", return_value=mock_resp):
            url = extract_github_url("test-pkg")
        assert url == "https://github.com/owner/repo"

    def test_extract_from_home_page_fallback(self):
        mock_response = json.dumps(
            {
                "info": {
                    "home_page": "https://github.com/owner/repo",
                    "project_urls": {
                        "Documentation": "https://docs.example.com",
                    },
                }
            }
        ).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = mock_response
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("pkgdb.github.urlopen", return_value=mock_resp):
            url = extract_github_url("test-pkg")
        assert url == "https://github.com/owner/repo"

    def test_extract_no_github_url(self):
        mock_response = json.dumps(
            {
                "info": {
                    "home_page": "https://example.com",
                    "project_urls": {
                        "Homepage": "https://example.com",
                    },
                }
            }
        ).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = mock_response
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("pkgdb.github.urlopen", return_value=mock_resp):
            url = extract_github_url("test-pkg")
        assert url is None

    def test_extract_network_error(self):
        from urllib.error import URLError

        with patch("pkgdb.github.urlopen", side_effect=URLError("fail")):
            url = extract_github_url("test-pkg")
        assert url is None


class TestGetGithubToken:
    """Tests for get_github_token function."""

    def test_github_token_env(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "test-token"}, clear=False):
            # Remove GH_TOKEN if present to avoid interference
            os.environ.pop("GH_TOKEN", None)
            assert get_github_token() == "test-token"

    def test_gh_token_env(self):
        with patch.dict(os.environ, {"GH_TOKEN": "gh-token"}, clear=False):
            os.environ.pop("GITHUB_TOKEN", None)
            assert get_github_token() == "gh-token"

    def test_no_token(self):
        with patch.dict(os.environ, {}, clear=True):
            assert get_github_token() is None

    def test_falls_back_to_the_gh_cli(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("pkgdb.github._gh_cli_token", return_value="gh-cli-token"):
                assert get_github_token() == "gh-cli-token"

    def test_env_overrides_the_gh_cli(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "env-token"}, clear=True):
            with patch(
                "pkgdb.github._gh_cli_token",
                side_effect=AssertionError("gh must not be consulted"),
            ):
                assert get_github_token() == "env-token"


class TestGhCliToken:
    """The `gh auth token` fallback.

    The autouse fixture in conftest makes the subprocess unavailable and clears
    the resolution cache around every test, so each of these starts cold.
    """

    def test_reads_the_token(self):
        from pkgdb.github import _gh_cli_token

        completed = MagicMock(returncode=0, stdout="gho_secret\n")
        with patch("pkgdb.github.subprocess.run", return_value=completed):
            assert _gh_cli_token() == "gho_secret"

    def test_queries_github_com_explicitly(self):
        from pkgdb.github import _gh_cli_token

        completed = MagicMock(returncode=0, stdout="gho_secret")
        with patch("pkgdb.github.subprocess.run", return_value=completed) as run:
            _gh_cli_token()
        assert run.call_args[0][0] == [
            "gh",
            "auth",
            "token",
            "--hostname",
            "github.com",
        ]

    def test_not_logged_in(self):
        from pkgdb.github import _gh_cli_token

        completed = MagicMock(returncode=1, stdout="")
        with patch("pkgdb.github.subprocess.run", return_value=completed):
            assert _gh_cli_token() is None

    def test_empty_output(self):
        from pkgdb.github import _gh_cli_token

        completed = MagicMock(returncode=0, stdout="\n")
        with patch("pkgdb.github.subprocess.run", return_value=completed):
            assert _gh_cli_token() is None

    def test_gh_not_installed(self):
        from pkgdb.github import _gh_cli_token

        assert _gh_cli_token() is None  # the fixture removes the subprocess

    def test_gh_hangs(self):
        from pkgdb.github import _gh_cli_token

        with patch(
            "pkgdb.github.subprocess.run",
            side_effect=subprocess.TimeoutExpired("gh", 10),
        ):
            assert _gh_cli_token() is None

    def test_resolved_only_once(self):
        from pkgdb.github import _gh_cli_token

        completed = MagicMock(returncode=0, stdout="gho_secret")
        with patch("pkgdb.github.subprocess.run", return_value=completed) as run:
            _gh_cli_token()
            _gh_cli_token()
        assert run.call_count == 1


class TestFetchPackageGithubStats:
    """Tests for fetch_package_github_stats function."""

    def test_fetch_success(self, temp_db):
        conn = get_db_connection(temp_db)
        init_db(conn)

        api_data = _make_github_api_response()
        mock_api_resp = json.dumps(api_data).encode()
        pypi_data = json.dumps(
            {
                "info": {
                    "project_urls": {
                        "Repository": "https://github.com/testowner/testrepo"
                    },
                }
            }
        ).encode()

        def mock_urlopen(req, **kwargs):
            mock_resp = MagicMock()
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "pypi.org" in url:
                mock_resp.read.return_value = pypi_data
            else:
                mock_resp.read.return_value = mock_api_resp
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        with patch("pkgdb.github.urlopen", side_effect=mock_urlopen):
            result = fetch_package_github_stats("test-pkg", conn=conn)

        assert result.success is True
        assert result.stats is not None
        assert result.stats.stars == 42
        assert result.stats.forks == 5
        conn.close()

    def test_fetch_no_github_repo(self, temp_db):
        conn = get_db_connection(temp_db)
        init_db(conn)

        pypi_data = json.dumps(
            {"info": {"project_urls": {"Homepage": "https://example.com"}}}
        ).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = pypi_data
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("pkgdb.github.urlopen", return_value=mock_resp):
            result = fetch_package_github_stats("test-pkg", conn=conn)

        assert result.success is False
        assert "No GitHub repository" in result.error
        conn.close()


class TestOpenIssueCount:
    """Tests for the issues-only count from the search API."""

    def _mock_search(self, payload, captured_urls=None):
        def mock_urlopen(req, **kwargs):
            if captured_urls is not None:
                captured_urls.append(req.full_url)
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(payload).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        return mock_urlopen

    def test_counts_issues_excluding_pull_requests(self, temp_db):
        from pkgdb.github import fetch_open_issue_count

        conn = get_db_connection(temp_db)
        init_db(conn)
        urls = []

        with patch(
            "pkgdb.github.urlopen",
            side_effect=self._mock_search({"total_count": 12}, urls),
        ):
            count = fetch_open_issue_count("owner", "repo", conn=conn)

        assert count == 12
        assert "is:issue" in urls[0] and "is:open" in urls[0]
        assert "repo:owner/repo" in urls[0]
        conn.close()

    def test_second_call_is_served_from_cache(self, temp_db):
        from pkgdb.github import fetch_open_issue_count

        conn = get_db_connection(temp_db)
        init_db(conn)
        urls = []
        mock = self._mock_search({"total_count": 4}, urls)

        with patch("pkgdb.github.urlopen", side_effect=mock):
            assert fetch_open_issue_count("owner", "repo", conn=conn) == 4
            assert fetch_open_issue_count("owner", "repo", conn=conn) == 4

        assert len(urls) == 1
        conn.close()

    def test_zero_is_cached_and_not_confused_with_unknown(self, temp_db):
        from pkgdb.github import fetch_open_issue_count

        conn = get_db_connection(temp_db)
        init_db(conn)
        urls = []

        with patch(
            "pkgdb.github.urlopen",
            side_effect=self._mock_search({"total_count": 0}, urls),
        ):
            assert fetch_open_issue_count("owner", "repo", conn=conn) == 0
            assert fetch_open_issue_count("owner", "repo", conn=conn) == 0

        assert len(urls) == 1
        conn.close()

    def test_network_error_returns_none(self, temp_db):
        from urllib.error import URLError

        from pkgdb.github import fetch_open_issue_count

        conn = get_db_connection(temp_db)
        init_db(conn)

        with patch("pkgdb.github.urlopen", side_effect=URLError("fail")):
            assert fetch_open_issue_count("owner", "repo", conn=conn) is None
        conn.close()

    def test_rate_limited_search_does_not_fail_the_repo_fetch(self, temp_db):
        """A 403 on search leaves the count unknown, not the whole fetch."""
        from urllib.error import HTTPError

        from pkgdb.github import fetch_repo_stats

        conn = get_db_connection(temp_db)
        init_db(conn)
        api_data = _make_github_api_response()

        def mock_urlopen(req, **kwargs):
            if "/search/" in req.full_url:
                raise HTTPError(req.full_url, 403, "rate limited", {}, None)
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(api_data).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        with patch("pkgdb.github.urlopen", side_effect=mock_urlopen):
            with patch("pkgdb.github.time.sleep"):
                stats = fetch_repo_stats("testowner", "testrepo", conn=conn)

        assert stats.stars == 42
        assert stats.open_issues == 3  # GitHub's PR-inclusive figure
        assert stats.open_issues_excl_prs is None
        conn.close()

    def test_cached_repo_still_gets_an_issue_count(self, temp_db):
        """A warm repo cache must not skip the separate issues lookup."""
        from pkgdb.github import fetch_repo_stats, store_cached_repo_data

        conn = get_db_connection(temp_db)
        init_db(conn)
        store_cached_repo_data(
            conn, "testowner", "testrepo", _make_github_api_response()
        )
        urls = []

        with patch(
            "pkgdb.github.urlopen",
            side_effect=self._mock_search({"total_count": 8}, urls),
        ):
            stats = fetch_repo_stats("testowner", "testrepo", conn=conn)

        assert stats.open_issues_excl_prs == 8
        # Only the search call went out; the repo body came from the cache.
        assert len(urls) == 1
        assert "/search/" in urls[0]
        conn.close()


class TestUnauthenticatedWarning:
    """An unauthenticated run must say so, or its gaps look like GitHub's fault."""

    def test_warns_when_there_is_no_token(self, caplog):
        from pkgdb.github import _github_headers

        with patch.dict(os.environ, {}, clear=True):
            with caplog.at_level("WARNING"):
                _github_headers()
        assert "No GitHub token found" in caplog.text
        assert "60 requests/hour" in caplog.text

    def test_warns_only_once(self, caplog):
        from pkgdb.github import _github_headers

        with patch.dict(os.environ, {}, clear=True):
            with caplog.at_level("WARNING"):
                for _ in range(5):
                    _github_headers()
        assert caplog.text.count("No GitHub token found") == 1

    def test_silent_with_a_token(self, caplog):
        from pkgdb.github import _github_headers

        with patch.dict(os.environ, {"GITHUB_TOKEN": "t"}, clear=True):
            with caplog.at_level("WARNING"):
                headers = _github_headers()
        assert headers["Authorization"] == "Bearer t"
        assert "No GitHub token" not in caplog.text


class TestRateLimitExhaustion:
    """An exhausted quota is not a retryable 403."""

    @staticmethod
    def _http_error(headers):
        from urllib.error import HTTPError

        return HTTPError("https://api.github.com/x", 403, "forbidden", headers, None)

    def test_exhausted_is_not_retried(self):
        from urllib.error import HTTPError

        from pkgdb.github import _fetch_json

        error = self._http_error({"x-ratelimit-remaining": "0"})
        with patch("pkgdb.github.urlopen", side_effect=error) as mock:
            with patch("pkgdb.github.time.sleep") as sleep:
                with pytest.raises(HTTPError):
                    _fetch_json("https://api.github.com/x", {}, max_retries=3)
        assert mock.call_count == 1
        sleep.assert_not_called()

    def test_a_403_with_quota_left_still_retries(self):
        from urllib.error import HTTPError

        from pkgdb.github import _fetch_json

        error = self._http_error({"x-ratelimit-remaining": "42"})
        with patch("pkgdb.github.urlopen", side_effect=error) as mock:
            with patch("pkgdb.github.time.sleep"):
                with pytest.raises(HTTPError):
                    _fetch_json("https://api.github.com/x", {}, max_retries=2)
        assert mock.call_count == 3

    def test_a_403_without_the_header_still_retries(self):
        from urllib.error import HTTPError

        from pkgdb.github import _fetch_json

        with patch("pkgdb.github.urlopen", side_effect=self._http_error({})) as mock:
            with patch("pkgdb.github.time.sleep"):
                with pytest.raises(HTTPError):
                    _fetch_json("https://api.github.com/x", {}, max_retries=2)
        assert mock.call_count == 3

    def test_exhaustion_is_reported_with_its_reset_time(self, caplog):
        from urllib.error import HTTPError

        from pkgdb.github import _fetch_json

        reset = int(datetime(2026, 9, 2, 14, 30).timestamp())
        error = self._http_error(
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)}
        )
        with patch("pkgdb.github.urlopen", side_effect=error):
            with caplog.at_level("WARNING"):
                with pytest.raises(HTTPError):
                    _fetch_json("https://api.github.com/x", {})
        assert "rate limit exhausted" in caplog.text
        assert "resets at 14:30" in caplog.text

    def test_exhaustion_is_reported_once(self, caplog):
        from urllib.error import HTTPError

        from pkgdb.github import _fetch_json

        error = self._http_error({"x-ratelimit-remaining": "0"})
        with patch("pkgdb.github.urlopen", side_effect=error):
            with caplog.at_level("WARNING"):
                for _ in range(3):
                    with pytest.raises(HTTPError):
                        _fetch_json("https://api.github.com/x", {})
        assert caplog.text.count("rate limit exhausted") == 1

    def test_a_scan_reports_exhaustion_as_unknown_not_passing(self, temp_db):
        # The whole point of not retrying: the repo comes back unknown, and
        # the exit code stays clean because unreachable is not broken.
        from pkgdb.service import PackageStatsService

        error = self._http_error({"x-ratelimit-remaining": "0"})
        service = PackageStatsService(temp_db)
        service.add_repo("o/r")
        with patch("pkgdb.github.urlopen", side_effect=error):
            with patch("pkgdb.github.time.sleep") as sleep:
                results = service.fetch_ci_status(branch="main")
        assert results[0].error is not None
        assert results[0].failures == []
        sleep.assert_not_called()
