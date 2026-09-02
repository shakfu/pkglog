"""Tests for CI scanning: workflow runs, the repo registry, and `pkgdb ci`."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pkgdb import (
    CI_STATE_FAIL,
    CI_STATE_NO_RUNS,
    CI_STATE_PASS,
    CI_STATE_RUNNING,
    CI_STATE_UNKNOWN,
    add_package,
    add_repo,
    cleanup_orphaned_stats,
    get_ci_status,
    get_db_connection,
    get_repo_packages,
    get_repos,
    init_db,
    latest_run_per_workflow,
    link_package_repo,
    normalize_repo_key,
    parse_workflow_runs,
    remove_package,
    remove_repo,
    seed_repos_from_cache,
    set_repo_enabled,
    store_ci_status,
    store_github_stats_snapshot,
)
from pkgdb.cli import _humanize_age, create_parser, main
from pkgdb.github import (
    WorkflowRun,
    fetch_default_branch,
    fetch_user_repos,
    fetch_workflow_count,
    fetch_workflow_runs,
    fetch_workflow_runs_raw,
)
from pkgdb.service import CIEntry, PackageStatsService
from pkgdb.utils import utcnow


def _run(**overrides):
    """A raw workflow run entry as the GitHub API returns it."""
    defaults = {
        "id": 1001,
        "workflow_id": 77,
        "name": "build",
        "status": "completed",
        "conclusion": "success",
        "head_branch": "main",
        "event": "push",
        "created_at": "2026-08-01T12:00:00Z",
        "html_url": "https://github.com/o/r/actions/runs/1001",
    }
    defaults.update(overrides)
    return defaults


def _json_response(payload):
    """A urlopen context manager yielding `payload` as JSON."""
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode()
    response.__enter__ = lambda self: self
    response.__exit__ = lambda self, *a: None
    return response


def _no_config():
    return patch(
        "pkgdb.config.get_config_path", return_value=Path("/nonexistent/config.toml")
    )


class TestWorkflowRunState:
    """The normalized state derived from a run's status and conclusion."""

    @pytest.mark.parametrize(
        "status,conclusion,expected",
        [
            ("completed", "success", CI_STATE_PASS),
            ("completed", "failure", CI_STATE_FAIL),
            ("completed", "timed_out", CI_STATE_FAIL),
            ("completed", "startup_failure", CI_STATE_FAIL),
            ("completed", "cancelled", "CANCELLED"),
            ("completed", "skipped", "SKIPPED"),
            ("completed", "action_required", "ACTION_REQUIRED"),
            ("completed", None, CI_STATE_UNKNOWN),
            ("in_progress", None, CI_STATE_RUNNING),
            ("queued", None, CI_STATE_RUNNING),
            ("waiting", None, CI_STATE_RUNNING),
        ],
    )
    def test_state(self, status, conclusion, expected):
        runs = parse_workflow_runs([_run(status=status, conclusion=conclusion)])
        assert runs[0].state == expected

    def test_cancelled_is_not_a_failure(self):
        runs = parse_workflow_runs([_run(conclusion="cancelled")])
        assert not runs[0].is_failure

    def test_timed_out_is_a_failure(self):
        runs = parse_workflow_runs([_run(conclusion="timed_out")])
        assert runs[0].is_failure


class TestParseWorkflowRuns:
    def test_fields(self):
        run = parse_workflow_runs([_run()])[0]
        assert run.workflow_id == 77
        assert run.workflow_name == "build"
        assert run.run_id == 1001
        assert run.branch == "main"
        assert run.url.endswith("/1001")
        assert run.created_at == datetime(2026, 8, 1, 12, 0, 0)

    def test_missing_name_falls_back_to_the_workflow_file(self):
        raw = _run(name=None, path=".github/workflows/build-wheels.yml")
        assert parse_workflow_runs([raw])[0].workflow_name == "build-wheels.yml"

    def test_unnamed_workflow_shows_its_file_not_its_path(self):
        # GitHub reports a workflow with no `name:` key under its own path.
        raw = _run(
            name=".github/workflows/build-wheels.yml",
            path=".github/workflows/build-wheels.yml",
        )
        assert parse_workflow_runs([raw])[0].workflow_name == "build-wheels.yml"

    def test_a_real_name_is_left_alone(self):
        raw = _run(name="build / test", path=".github/workflows/ci.yml")
        assert parse_workflow_runs([raw])[0].workflow_name == "build / test"


class TestLatestRunPerWorkflow:
    def test_picks_most_recent_per_workflow(self):
        runs = parse_workflow_runs(
            [
                _run(id=1, workflow_id=1, created_at="2026-08-01T00:00:00Z"),
                _run(
                    id=2,
                    workflow_id=1,
                    created_at="2026-08-03T00:00:00Z",
                    conclusion="failure",
                ),
                _run(id=3, workflow_id=1, created_at="2026-08-02T00:00:00Z"),
            ]
        )
        latest = latest_run_per_workflow(runs)
        assert len(latest) == 1
        assert latest[0].run_id == 2
        assert latest[0].state == CI_STATE_FAIL

    def test_groups_by_workflow_id_not_name(self):
        # A workflow setting `run-name` reports a different name per run;
        # grouping on the name would split one workflow into several.
        runs = parse_workflow_runs(
            [
                _run(
                    id=1,
                    workflow_id=5,
                    name="deploy v1",
                    created_at="2026-08-01T00:00:00Z",
                ),
                _run(
                    id=2,
                    workflow_id=5,
                    name="deploy v2",
                    created_at="2026-08-02T00:00:00Z",
                ),
            ]
        )
        assert len(latest_run_per_workflow(runs)) == 1

    def test_separate_workflows_sorted_by_name(self):
        runs = parse_workflow_runs(
            [
                _run(id=1, workflow_id=1, name="zeta"),
                _run(id=2, workflow_id=2, name="alpha"),
            ]
        )
        assert [r.workflow_name for r in latest_run_per_workflow(runs)] == [
            "alpha",
            "zeta",
        ]

    def test_ties_break_on_run_id(self):
        runs = parse_workflow_runs(
            [
                _run(id=10, workflow_id=1, created_at="2026-08-01T00:00:00Z"),
                _run(id=11, workflow_id=1, created_at="2026-08-01T00:00:00Z"),
            ]
        )
        assert latest_run_per_workflow(runs)[0].run_id == 11

    def test_empty(self):
        assert latest_run_per_workflow([]) == []


class TestFetchWorkflowRuns:
    def test_success(self):
        payload = {"workflow_runs": [_run()]}
        with patch("pkgdb.github.urlopen", return_value=_json_response(payload)):
            raw = fetch_workflow_runs_raw("o", "r")
        assert len(raw) == 1

    def test_no_runs_is_empty_not_none(self):
        with patch(
            "pkgdb.github.urlopen", return_value=_json_response({"workflow_runs": []})
        ):
            assert fetch_workflow_runs_raw("o", "r") == []

    def test_error_is_none(self):
        from urllib.error import URLError

        with patch("pkgdb.github.urlopen", side_effect=URLError("boom")):
            assert fetch_workflow_runs_raw("o", "r") is None

    def test_branch_is_url_encoded(self):
        captured = {}

        def capture(req, **kwargs):
            captured["url"] = req.full_url
            return _json_response({"workflow_runs": []})

        with patch("pkgdb.github.urlopen", side_effect=capture):
            fetch_workflow_runs_raw("o", "r", branch="feature/a b")
        assert "branch=feature%2Fa%20b" in captured["url"]

    def test_limit_in_query(self):
        captured = {}

        def capture(req, **kwargs):
            captured["url"] = req.full_url
            return _json_response({"workflow_runs": []})

        with patch("pkgdb.github.urlopen", side_effect=capture):
            fetch_workflow_runs_raw("o", "r", limit=7)
        assert "per_page=7" in captured["url"]

    def test_cache_round_trip(self, db_conn):
        payload = {"workflow_runs": [_run()]}
        with patch(
            "pkgdb.github.urlopen", return_value=_json_response(payload)
        ) as mock:
            fetch_workflow_runs("o", "r", branch="main", conn=db_conn)
            assert mock.call_count == 1
            runs = fetch_workflow_runs("o", "r", branch="main", conn=db_conn)
            assert mock.call_count == 1  # served from cache
        assert runs[0].run_id == 1001

    def test_cache_is_per_branch(self, db_conn):
        payload = {"workflow_runs": [_run()]}
        with patch(
            "pkgdb.github.urlopen", return_value=_json_response(payload)
        ) as mock:
            fetch_workflow_runs("o", "r", branch="main", conn=db_conn)
            fetch_workflow_runs("o", "r", branch="dev", conn=db_conn)
        assert mock.call_count == 2

    def test_no_cache_bypasses(self, db_conn):
        payload = {"workflow_runs": [_run()]}
        with patch(
            "pkgdb.github.urlopen", return_value=_json_response(payload)
        ) as mock:
            fetch_workflow_runs("o", "r", conn=db_conn, use_cache=False)
            fetch_workflow_runs("o", "r", conn=db_conn, use_cache=False)
        assert mock.call_count == 2

    def test_failed_fetch_is_not_cached(self, db_conn):
        from urllib.error import URLError

        with patch("pkgdb.github.urlopen", side_effect=URLError("boom")):
            assert fetch_workflow_runs("o", "r", conn=db_conn) is None
        payload = {"workflow_runs": [_run()]}
        with patch("pkgdb.github.urlopen", return_value=_json_response(payload)):
            assert len(fetch_workflow_runs("o", "r", conn=db_conn)) == 1


class TestWorkflowCountAndBranch:
    def test_workflow_count(self):
        with patch(
            "pkgdb.github.urlopen", return_value=_json_response({"total_count": 3})
        ):
            assert fetch_workflow_count("o", "r") == 3

    def test_workflow_count_error_is_none(self):
        from urllib.error import URLError

        with patch("pkgdb.github.urlopen", side_effect=URLError("boom")):
            assert fetch_workflow_count("o", "r") is None

    def test_default_branch_uses_repo_cache(self, db_conn):
        from pkgdb.github import store_cached_repo_data

        store_cached_repo_data(db_conn, "o", "r", {"default_branch": "trunk"})
        with patch("pkgdb.github.urlopen", side_effect=AssertionError("no request")):
            assert fetch_default_branch("o", "r", conn=db_conn) == "trunk"

    def test_default_branch_fetches_and_caches(self, db_conn):
        payload = {"default_branch": "main", "name": "r"}
        with patch(
            "pkgdb.github.urlopen", return_value=_json_response(payload)
        ) as mock:
            assert fetch_default_branch("o", "r", conn=db_conn) == "main"
            assert fetch_default_branch("o", "r", conn=db_conn) == "main"
        assert mock.call_count == 1


class TestFetchUserRepos:
    @staticmethod
    def _anonymous():
        """No token, so the public /users/{user}/repos endpoint is used."""
        return patch("pkgdb.github.get_github_token", return_value=None)

    def test_filters_forks_and_archived_by_default(self):
        payload = [
            {
                "full_name": "u/a",
                "default_branch": "main",
                "fork": False,
                "archived": False,
            },
            {
                "full_name": "u/b",
                "default_branch": "main",
                "fork": True,
                "archived": False,
            },
            {
                "full_name": "u/c",
                "default_branch": "main",
                "fork": False,
                "archived": True,
            },
        ]
        with self._anonymous():
            with patch("pkgdb.github.urlopen", return_value=_json_response(payload)):
                repos = fetch_user_repos("u")
        assert [r["full_name"] for r in repos] == ["u/a"]

    def test_include_flags(self):
        payload = [
            {"full_name": "u/a", "fork": True, "archived": False},
            {"full_name": "u/b", "fork": False, "archived": True},
        ]
        with self._anonymous():
            with patch("pkgdb.github.urlopen", return_value=_json_response(payload)):
                repos = fetch_user_repos("u", include_forks=True, include_archived=True)
        assert len(repos) == 2

    def test_error_is_none(self):
        from urllib.error import URLError

        with self._anonymous():
            with patch("pkgdb.github.urlopen", side_effect=URLError("boom")):
                assert fetch_user_repos("u") is None

    def test_stops_on_short_page(self):
        payload = [{"full_name": "u/a", "fork": False, "archived": False}]
        with self._anonymous():
            with patch(
                "pkgdb.github.urlopen", return_value=_json_response(payload)
            ) as mock:
                fetch_user_repos("u")
        assert mock.call_count == 1

    def test_own_account_uses_the_authenticated_endpoint(self):
        # /users/{user}/repos returns public repos only, even with a token, so
        # a private repository would be dropped without a word.
        payload = [
            {
                "full_name": "u/private",
                "fork": False,
                "archived": False,
                "private": True,
            }
        ]
        captured = []

        def capture(req, **kwargs):
            captured.append(req.full_url)
            return _json_response(payload)

        with patch("pkgdb.github.fetch_authenticated_login", return_value="u"):
            with patch("pkgdb.github.urlopen", side_effect=capture):
                repos = fetch_user_repos("u")
        assert captured[0].endswith(
            "/user/repos?per_page=100&page=1&affiliation=owner&sort=full_name"
        )
        assert repos[0]["private"] is True

    def test_other_account_uses_the_public_endpoint(self):
        payload = [{"full_name": "other/a", "fork": False, "archived": False}]
        captured = []

        def capture(req, **kwargs):
            captured.append(req.full_url)
            return _json_response(payload)

        with patch("pkgdb.github.fetch_authenticated_login", return_value="u"):
            with patch("pkgdb.github.urlopen", side_effect=capture):
                fetch_user_repos("other")
        assert "/users/other/repos" in captured[0]

    def test_login_lookup_is_skipped_without_a_token(self):
        from pkgdb.github import fetch_authenticated_login

        with self._anonymous():
            with patch(
                "pkgdb.github.urlopen", side_effect=AssertionError("no request")
            ):
                assert fetch_authenticated_login() is None


class TestNormalizeRepoKey:
    @pytest.mark.parametrize(
        "given,expected",
        [
            ("Owner/Repo", "owner/repo"),
            ("https://github.com/Owner/Repo", "owner/repo"),
            ("https://github.com/Owner/Repo.git", "owner/repo"),
            ("github.com/Owner/Repo/", "owner/repo"),
            ("  owner/repo  ", "owner/repo"),
        ],
    )
    def test_normalize(self, given, expected):
        assert normalize_repo_key(given) == expected


class TestRepoRegistry:
    def test_add_is_idempotent(self, db_conn):
        assert add_repo(db_conn, "o/r") is True
        assert add_repo(db_conn, "O/R") is False
        assert len(get_repos(db_conn)) == 1

    def test_add_does_not_erase_known_values(self, db_conn):
        add_repo(db_conn, "o/r", has_workflows=3, default_branch="trunk")
        add_repo(db_conn, "o/r")  # a cheaper caller with nothing to add
        repo = get_repos(db_conn)[0]
        assert repo["has_workflows"] == 3
        assert repo["default_branch"] == "trunk"

    def test_add_fills_in_learned_values(self, db_conn):
        add_repo(db_conn, "o/r")
        add_repo(db_conn, "o/r", default_branch="main")
        assert get_repos(db_conn)[0]["default_branch"] == "main"

    def test_remove(self, db_conn):
        add_repo(db_conn, "o/r")
        store_ci_status(db_conn, "o/r", "build", CI_STATE_FAIL)
        assert remove_repo(db_conn, "o/r") is True
        assert get_repos(db_conn) == []
        assert get_ci_status(db_conn) == []
        assert remove_repo(db_conn, "o/r") is False

    def test_enabled_filter(self, db_conn):
        add_repo(db_conn, "o/a")
        add_repo(db_conn, "o/b")
        set_repo_enabled(db_conn, "o/b", False)
        assert [r["repo_key"] for r in get_repos(db_conn)] == ["o/a"]
        assert len(get_repos(db_conn, enabled_only=False)) == 2

    def test_with_workflows_filter_keeps_unprobed(self, db_conn):
        add_repo(db_conn, "o/probed", has_workflows=2)
        add_repo(db_conn, "o/none", has_workflows=0)
        add_repo(db_conn, "o/unknown")
        keys = [r["repo_key"] for r in get_repos(db_conn, with_workflows_only=True)]
        assert keys == ["o/probed", "o/unknown"]


class TestPackageRepoLinks:
    def test_many_packages_to_one_repo(self, db_conn):
        for pkg in ("cyllama", "cyllama-cuda12", "cyllama-rocm"):
            add_package(db_conn, pkg)
            link_package_repo(db_conn, pkg, "shakfu/cyllama")
        mapping = get_repo_packages(db_conn)
        assert mapping["shakfu/cyllama"] == [
            "cyllama",
            "cyllama-cuda12",
            "cyllama-rocm",
        ]

    def test_untracked_packages_excluded(self, db_conn):
        add_package(db_conn, "kept")
        link_package_repo(db_conn, "kept", "o/r")
        link_package_repo(db_conn, "gone", "o/r")
        assert get_repo_packages(db_conn) == {"o/r": ["kept"]}


class TestCIStatusStreak:
    """`first_failed_at` is the one value a scan cannot recompute from the API."""

    def test_failure_records_the_run_start(self, db_conn):
        first = store_ci_status(
            db_conn,
            "o/r",
            "build",
            CI_STATE_FAIL,
            run_started_at="2026-08-01T00:00:00",
        )
        assert first == "2026-08-01T00:00:00"

    def test_streak_start_is_kept_across_failures(self, db_conn):
        store_ci_status(
            db_conn, "o/r", "build", CI_STATE_FAIL, run_started_at="2026-08-01T00:00:00"
        )
        second = store_ci_status(
            db_conn, "o/r", "build", CI_STATE_FAIL, run_started_at="2026-08-05T00:00:00"
        )
        assert second == "2026-08-01T00:00:00"

    def test_pass_clears_the_streak(self, db_conn):
        store_ci_status(
            db_conn, "o/r", "build", CI_STATE_FAIL, run_started_at="2026-08-01T00:00:00"
        )
        assert store_ci_status(db_conn, "o/r", "build", CI_STATE_PASS) is None

    def test_running_carries_the_streak_forward(self, db_conn):
        # A fix that is still building does not end the failure; resetting here
        # would report a month-old break as new when the run fails again.
        store_ci_status(
            db_conn, "o/r", "build", CI_STATE_FAIL, run_started_at="2026-08-01T00:00:00"
        )
        store_ci_status(db_conn, "o/r", "build", CI_STATE_RUNNING)
        again = store_ci_status(
            db_conn, "o/r", "build", CI_STATE_FAIL, run_started_at="2026-08-09T00:00:00"
        )
        assert again == "2026-08-01T00:00:00"

    def test_cancelled_carries_the_streak_forward(self, db_conn):
        store_ci_status(
            db_conn, "o/r", "build", CI_STATE_FAIL, run_started_at="2026-08-01T00:00:00"
        )
        store_ci_status(db_conn, "o/r", "build", "CANCELLED")
        again = store_ci_status(db_conn, "o/r", "build", CI_STATE_FAIL)
        assert again == "2026-08-01T00:00:00"

    def test_new_streak_after_a_pass(self, db_conn):
        store_ci_status(
            db_conn, "o/r", "build", CI_STATE_FAIL, run_started_at="2026-08-01T00:00:00"
        )
        store_ci_status(db_conn, "o/r", "build", CI_STATE_PASS)
        again = store_ci_status(
            db_conn, "o/r", "build", CI_STATE_FAIL, run_started_at="2026-08-20T00:00:00"
        )
        assert again == "2026-08-20T00:00:00"

    def test_failure_without_a_run_time_falls_back_to_now(self, db_conn):
        first = store_ci_status(db_conn, "o/r", "build", CI_STATE_FAIL)
        assert first is not None

    def test_workflows_are_independent(self, db_conn):
        store_ci_status(
            db_conn, "o/r", "build", CI_STATE_FAIL, run_started_at="2026-08-01T00:00:00"
        )
        store_ci_status(db_conn, "o/r", "test", CI_STATE_PASS)
        rows = {r["workflow_name"]: r for r in get_ci_status(db_conn, "o/r")}
        assert rows["build"]["first_failed_at"] == "2026-08-01T00:00:00"
        assert rows["test"]["first_failed_at"] is None

    def test_upsert_does_not_duplicate(self, db_conn):
        for _ in range(3):
            store_ci_status(db_conn, "o/r", "build", CI_STATE_PASS)
        assert len(get_ci_status(db_conn)) == 1


class TestSeedReposFromCache:
    def test_seeds_from_history_and_cache(self, db_conn):
        from pkgdb.github import store_cached_repo_data

        add_package(db_conn, "pkg")
        store_github_stats_snapshot(db_conn, "pkg", "o/hist", 1, 1, 1, 1)
        store_cached_repo_data(db_conn, "o", "cached", {"default_branch": "trunk"})
        assert seed_repos_from_cache(db_conn) == 2
        repos = {r["repo_key"]: r for r in get_repos(db_conn)}
        assert set(repos) == {"o/hist", "o/cached"}
        assert repos["o/cached"]["default_branch"] == "trunk"

    def test_suffixed_cache_keys_are_not_repos(self, db_conn):
        # The issues-only count and the run listings share github_cache under
        # suffixed keys; treating those as repo keys yields phantom scans.
        from pkgdb.github import _store_cached_json

        _store_cached_json(db_conn, "o/r#open-issues", {"open_issues": 4})
        _store_cached_json(db_conn, "o/r#ci:main", {"workflow_runs": []})
        assert seed_repos_from_cache(db_conn) == 0
        assert get_repos(db_conn) == []

    def test_seeding_is_idempotent(self, db_conn):
        from pkgdb.github import store_cached_repo_data

        store_cached_repo_data(db_conn, "o", "r", {"default_branch": "main"})
        assert seed_repos_from_cache(db_conn) == 1
        assert seed_repos_from_cache(db_conn) == 0


class TestCleanup:
    def test_removes_links_for_untracked_packages(self, db_conn):
        add_package(db_conn, "pkg")
        link_package_repo(db_conn, "pkg", "o/r")
        remove_package(db_conn, "pkg")
        counts = cleanup_orphaned_stats(db_conn)
        assert counts["package_repos"] == 1

    def test_removes_ci_rows_for_unregistered_repos(self, db_conn):
        add_repo(db_conn, "o/kept")
        store_ci_status(db_conn, "o/kept", "build", CI_STATE_PASS)
        store_ci_status(db_conn, "o/gone", "build", CI_STATE_FAIL)
        counts = cleanup_orphaned_stats(db_conn)
        assert counts["github_ci_status"] == 1
        assert [r["repo_key"] for r in get_ci_status(db_conn)] == ["o/kept"]


class TestCIEntry:
    def test_failing_days(self):
        started = (utcnow() - timedelta(days=6, hours=1)).isoformat()
        entry = CIEntry("o/r", "build", CI_STATE_FAIL, first_failed_at=started)
        assert entry.failing_days == 6

    def test_no_days_when_passing(self):
        entry = CIEntry("o/r", "build", CI_STATE_PASS, first_failed_at=None)
        assert entry.failing_days is None

    def test_unparseable_timestamp(self):
        entry = CIEntry("o/r", "build", CI_STATE_FAIL, first_failed_at="nonsense")
        assert entry.failing_days is None


class TestHumanizeAge:
    @pytest.mark.parametrize(
        "delta,expected",
        [
            (timedelta(minutes=30), "30m"),
            (timedelta(hours=5), "5h"),
            (timedelta(days=3), "3d"),
        ],
    )
    def test_ages(self, delta, expected):
        assert _humanize_age((utcnow() - delta).isoformat()) == expected

    def test_missing_and_invalid(self):
        assert _humanize_age(None) == "-"
        assert _humanize_age("nonsense") == "-"


class TestServiceFetchCIStatus:
    @staticmethod
    def _service(temp_db, repos, branch="main"):
        service = PackageStatsService(temp_db)
        for key in repos:
            service.add_repo(key)
        return service

    def test_scan_records_failures(self, temp_db):
        service = self._service(temp_db, ["o/r"])
        payload = {
            "workflow_runs": [
                _run(id=1, workflow_id=1, name="build", conclusion="failure"),
                _run(id=2, workflow_id=2, name="docs", conclusion="success"),
            ]
        }
        with patch("pkgdb.github.urlopen", return_value=_json_response(payload)):
            results = service.fetch_ci_status(branch="main")
        assert len(results) == 1
        assert [e.workflow_name for e in results[0].failures] == ["build"]
        assert not results[0].ok

    def test_scan_persists_state(self, temp_db):
        service = self._service(temp_db, ["o/r"])
        payload = {"workflow_runs": [_run(conclusion="failure")]}
        with patch("pkgdb.github.urlopen", return_value=_json_response(payload)):
            service.fetch_ci_status(branch="main")
        stored = service.get_ci_status("o/r")
        assert stored[0]["state"] == CI_STATE_FAIL
        assert stored[0]["first_failed_at"] == "2026-08-01T12:00:00"

    def test_green_repo(self, temp_db):
        service = self._service(temp_db, ["o/r"])
        payload = {"workflow_runs": [_run()]}
        with patch("pkgdb.github.urlopen", return_value=_json_response(payload)):
            results = service.fetch_ci_status(branch="main")
        assert results[0].ok

    def test_repo_with_no_runs(self, temp_db):
        service = self._service(temp_db, ["o/r"])
        with patch(
            "pkgdb.github.urlopen", return_value=_json_response({"workflow_runs": []})
        ):
            results = service.fetch_ci_status(branch="main")
        assert results[0].entries[0].state == CI_STATE_NO_RUNS
        assert results[0].ok

    def test_fetch_error_is_not_a_failure(self, temp_db):
        from urllib.error import URLError

        service = self._service(temp_db, ["o/r"])
        with patch("pkgdb.github.urlopen", side_effect=URLError("boom")):
            results = service.fetch_ci_status(branch="main")
        assert results[0].error is not None
        assert results[0].failures == []

    def test_explicit_repos_bypass_the_registry(self, temp_db):
        service = PackageStatsService(temp_db)
        payload = {"workflow_runs": [_run(conclusion="failure")]}
        with patch("pkgdb.github.urlopen", return_value=_json_response(payload)):
            results = service.fetch_ci_status(repos=["O/Other"], branch="main")
        assert [r.repo_key for r in results] == ["o/other"]

    def test_ignore_patterns(self, temp_db):
        service = self._service(temp_db, ["o/r"])
        payload = {
            "workflow_runs": [
                _run(id=1, workflow_id=1, name="build", conclusion="failure"),
                _run(id=2, workflow_id=2, name="nightly fuzz", conclusion="failure"),
            ]
        }
        with patch("pkgdb.github.urlopen", return_value=_json_response(payload)):
            results = service.fetch_ci_status(
                branch="main", ignore_workflows=["nightly*"]
            )
        assert [e.workflow_name for e in results[0].entries] == ["build"]

    def test_default_branch_is_resolved_and_stored(self, temp_db):
        service = PackageStatsService(temp_db)
        service.add_repo("o/r")

        def respond(req, **kwargs):
            if "/actions/runs" in req.full_url:
                assert "branch=trunk" in req.full_url
                return _json_response({"workflow_runs": [_run()]})
            return _json_response({"default_branch": "trunk", "name": "r"})

        with patch("pkgdb.github.urlopen", side_effect=respond):
            service.fetch_ci_status()
        assert service.list_repos()[0]["default_branch"] == "trunk"

    def test_registry_seeds_itself_when_empty(self, temp_db):
        service = PackageStatsService(temp_db)
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "pkg")
        store_github_stats_snapshot(conn, "pkg", "o/seeded", 1, 1, 1, 1)
        conn.close()
        with patch(
            "pkgdb.github.urlopen", return_value=_json_response({"workflow_runs": []})
        ):
            results = service.fetch_ci_status(branch="main")
        assert [r.repo_key for r in results] == ["o/seeded"]

    def test_linked_packages_are_reported(self, temp_db):
        service = PackageStatsService(temp_db)
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "pkg")
        add_repo(conn, "o/r")
        link_package_repo(conn, "pkg", "o/r")
        conn.close()
        with patch(
            "pkgdb.github.urlopen", return_value=_json_response({"workflow_runs": []})
        ):
            results = service.fetch_ci_status(branch="main")
        assert results[0].packages == ["pkg"]

    def test_second_scan_uses_the_cache(self, temp_db):
        service = self._service(temp_db, ["o/r"])
        payload = {"workflow_runs": [_run()]}
        with patch(
            "pkgdb.github.urlopen", return_value=_json_response(payload)
        ) as mock:
            service.fetch_ci_status(branch="main")
            service.fetch_ci_status(branch="main")
        assert mock.call_count == 1

    def test_empty_registry_returns_nothing(self, temp_db):
        service = PackageStatsService(temp_db)
        with patch("pkgdb.github.urlopen", side_effect=AssertionError("no request")):
            assert service.fetch_ci_status() == []


class TestServiceDiscoverRepos:
    def test_registers_repos_with_workflow_counts(self, temp_db):
        service = PackageStatsService(temp_db)
        listing = [
            {
                "full_name": "u/a",
                "default_branch": "main",
                "fork": False,
                "archived": False,
            },
            {
                "full_name": "u/b",
                "default_branch": "dev",
                "fork": False,
                "archived": False,
            },
        ]
        with patch("pkgdb.service.fetch_user_repos", return_value=listing):
            with patch("pkgdb.service.fetch_workflow_count", side_effect=[2, 0]):
                summary = service.discover_repos("u")
        assert summary["found"] == 2
        assert summary["added"] == 2
        assert summary["with_workflows"] == 1
        repos = {r["repo_key"]: r for r in service.list_repos()}
        assert repos["u/a"]["has_workflows"] == 2
        assert repos["u/b"]["has_workflows"] == 0
        assert repos["u/b"]["default_branch"] == "dev"

    def test_no_probe_leaves_counts_unknown(self, temp_db):
        service = PackageStatsService(temp_db)
        listing = [{"full_name": "u/a", "default_branch": "main"}]
        with patch("pkgdb.service.fetch_user_repos", return_value=listing):
            with patch(
                "pkgdb.service.fetch_workflow_count",
                side_effect=AssertionError("probed"),
            ):
                service.discover_repos("u", probe_workflows=False)
        assert service.list_repos()[0]["has_workflows"] is None

    def test_known_counts_are_not_reprobed(self, temp_db):
        service = PackageStatsService(temp_db)
        listing = [{"full_name": "u/a", "default_branch": "main"}]
        with patch("pkgdb.service.fetch_user_repos", return_value=listing):
            with patch("pkgdb.service.fetch_workflow_count", return_value=3) as probe:
                service.discover_repos("u")
                service.discover_repos("u")
        assert probe.call_count == 1

    def test_links_packages_by_name(self, temp_db):
        # Recovers packages whose PyPI metadata carries no repository URL.
        service = PackageStatsService(temp_db)
        conn = get_db_connection(temp_db)
        init_db(conn)
        for pkg in ("py2max_server", "unrelated"):
            add_package(conn, pkg)
        conn.close()
        listing = [{"full_name": "u/py2max-server", "default_branch": "main"}]
        with patch("pkgdb.service.fetch_user_repos", return_value=listing):
            with patch("pkgdb.service.fetch_workflow_count", return_value=1):
                summary = service.discover_repos("u")
        assert summary["linked"] == 1
        assert service.list_repos()[0]["packages"] == ["py2max_server"]

    def test_listing_failure_reports_an_error(self, temp_db):
        service = PackageStatsService(temp_db)
        with patch("pkgdb.service.fetch_user_repos", return_value=None):
            summary = service.discover_repos("u")
        assert "error" in summary


class TestCICommand:
    def test_parser(self):
        args = create_parser().parse_args(["ci", "--repo", "o/r", "--json"])
        assert args.command == "ci"
        assert args.repo == ["o/r"]

    def test_failures_exit_nonzero(self, temp_db, capsys):
        payload = {"workflow_runs": [_run(name="build", conclusion="failure")]}
        argv = ["pkgdb", "-d", temp_db, "ci", "--repo", "o/r", "--branch", "main"]
        with patch("sys.argv", argv), _no_config():
            with patch("pkgdb.github.urlopen", return_value=_json_response(payload)):
                with pytest.raises(SystemExit) as exc:
                    main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "o/r" in out and "build" in out and "FAIL" in out

    def test_green_exits_zero(self, temp_db):
        payload = {"workflow_runs": [_run()]}
        argv = ["pkgdb", "-d", temp_db, "ci", "--repo", "o/r", "--branch", "main"]
        with patch("sys.argv", argv), _no_config():
            with patch("pkgdb.github.urlopen", return_value=_json_response(payload)):
                main()  # must not raise

    def test_exit_zero_flag(self, temp_db):
        payload = {"workflow_runs": [_run(conclusion="failure")]}
        argv = [
            "pkgdb",
            "-d",
            temp_db,
            "ci",
            "--repo",
            "o/r",
            "--branch",
            "main",
            "--exit-zero",
        ]
        with patch("sys.argv", argv), _no_config():
            with patch("pkgdb.github.urlopen", return_value=_json_response(payload)):
                main()  # must not raise

    def test_passing_workflows_are_hidden_by_default(self, temp_db, capsys):
        payload = {
            "workflow_runs": [
                _run(id=1, workflow_id=1, name="build", conclusion="failure"),
                _run(id=2, workflow_id=2, name="docs", conclusion="success"),
            ]
        }
        argv = ["pkgdb", "-d", temp_db, "ci", "--repo", "o/r", "--branch", "main"]
        with patch("sys.argv", argv), _no_config():
            with patch("pkgdb.github.urlopen", return_value=_json_response(payload)):
                with pytest.raises(SystemExit):
                    main()
        out = capsys.readouterr().out
        assert "build" in out
        assert "docs" not in out

    def test_all_flag_shows_passing(self, temp_db, capsys):
        payload = {"workflow_runs": [_run(name="docs")]}
        argv = [
            "pkgdb",
            "-d",
            temp_db,
            "ci",
            "--repo",
            "o/r",
            "--branch",
            "main",
            "--all",
        ]
        with patch("sys.argv", argv), _no_config():
            with patch("pkgdb.github.urlopen", return_value=_json_response(payload)):
                main()
        assert "docs" in capsys.readouterr().out

    def test_json_output(self, temp_db, capsys):
        payload = {"workflow_runs": [_run(name="build", conclusion="failure")]}
        argv = [
            "pkgdb",
            "-d",
            temp_db,
            "ci",
            "--repo",
            "o/r",
            "--branch",
            "main",
            "--json",
        ]
        with patch("sys.argv", argv), _no_config():
            with patch("pkgdb.github.urlopen", return_value=_json_response(payload)):
                with pytest.raises(SystemExit):
                    main()
        data = json.loads(capsys.readouterr().out)
        assert data[0]["repo"] == "o/r"
        assert data[0]["workflows"][0]["state"] == CI_STATE_FAIL

    def test_no_repositories_warns(self, temp_db, caplog):
        argv = ["pkgdb", "-d", temp_db, "ci"]
        with patch("sys.argv", argv), _no_config():
            main()
        assert "No repositories to scan" in caplog.text


class TestRepoCommand:
    def test_parser(self):
        args = create_parser().parse_args(["repo", "add", "o/r"])
        assert args.command == "repo"
        assert args.repo_command == "add"

    def test_add_and_list(self, temp_db, capsys):
        with patch("sys.argv", ["pkgdb", "-d", temp_db, "repo", "add", "o/r"]):
            with _no_config():
                main()
        with patch("sys.argv", ["pkgdb", "-d", temp_db, "repo", "list"]):
            with _no_config():
                main()
        assert "o/r" in capsys.readouterr().out

    def test_remove(self, temp_db, caplog):
        with patch("sys.argv", ["pkgdb", "-d", temp_db, "repo", "add", "o/r"]):
            with _no_config():
                main()
        with patch("sys.argv", ["pkgdb", "-d", temp_db, "repo", "remove", "o/r"]):
            with _no_config():
                main()
        assert "Removed o/r" in caplog.text

    def test_list_json(self, temp_db, capsys):
        with patch("sys.argv", ["pkgdb", "-d", temp_db, "repo", "add", "o/r"]):
            with _no_config():
                main()
        with patch("sys.argv", ["pkgdb", "-d", temp_db, "repo", "--json", "list"]):
            with _no_config():
                main()
        data = json.loads(capsys.readouterr().out)
        assert data[0]["repo_key"] == "o/r"

    def test_discover_requires_a_user(self, temp_db, caplog):
        with patch("sys.argv", ["pkgdb", "-d", temp_db, "repo", "discover"]):
            with _no_config():
                main()
        assert "No GitHub user given" in caplog.text

    def test_discover(self, temp_db, caplog):
        listing = [{"full_name": "u/a", "default_branch": "main"}]
        argv = ["pkgdb", "-d", temp_db, "repo", "discover", "--user", "u"]
        with patch("sys.argv", argv), _no_config():
            with patch("pkgdb.service.fetch_user_repos", return_value=listing):
                with patch("pkgdb.service.fetch_workflow_count", return_value=1):
                    main()
        assert "Found 1 repositories" in caplog.text


class TestCIRows:
    """The flattened row shape the report and dashboard consume."""

    @staticmethod
    def _scanned(temp_db, payload, repos=("o/r",)):
        service = PackageStatsService(temp_db)
        for key in repos:
            service.add_repo(key)
        with patch("pkgdb.github.urlopen", return_value=_json_response(payload)):
            service.fetch_ci_status(branch="main")
        return service

    def test_rows_from_the_database(self, temp_db):
        payload = {"workflow_runs": [_run(name="build", conclusion="failure")]}
        service = self._scanned(temp_db, payload)
        rows = service.get_ci_rows()
        assert rows[0]["repo_key"] == "o/r"
        assert rows[0]["workflow_name"] == "build"
        assert rows[0]["state"] == CI_STATE_FAIL
        assert rows[0]["failing_days"] is not None

    def test_rows_need_no_network(self, temp_db):
        payload = {"workflow_runs": [_run(conclusion="failure")]}
        service = self._scanned(temp_db, payload)
        with patch("pkgdb.github.urlopen", side_effect=AssertionError("no request")):
            assert len(service.get_ci_rows()) == 1

    def test_failures_sort_first_and_longest_first(self, temp_db):
        service = PackageStatsService(temp_db)
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_repo(conn, "o/r")
        store_ci_status(conn, "o/r", "passing", CI_STATE_PASS)
        store_ci_status(
            conn,
            "o/r",
            "recent",
            CI_STATE_FAIL,
            run_started_at=(utcnow() - timedelta(days=2)).isoformat(),
        )
        store_ci_status(
            conn,
            "o/r",
            "ancient",
            CI_STATE_FAIL,
            run_started_at=(utcnow() - timedelta(days=200)).isoformat(),
        )
        conn.close()
        assert [r["workflow_name"] for r in service.get_ci_rows()] == [
            "ancient",
            "recent",
            "passing",
        ]

    def test_linked_packages_travel_with_the_row(self, temp_db):
        payload = {"workflow_runs": [_run(conclusion="failure")]}
        service = PackageStatsService(temp_db)
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "pkg")
        add_repo(conn, "o/r")
        link_package_repo(conn, "pkg", "o/r")
        conn.close()
        with patch("pkgdb.github.urlopen", return_value=_json_response(payload)):
            service.fetch_ci_status(branch="main")
        assert service.get_ci_rows()[0]["packages"] == ["pkg"]

    def test_empty_when_never_scanned(self, temp_db):
        assert PackageStatsService(temp_db).get_ci_rows() == []


class TestCIReportSection:
    """The HTML section shared by the standalone and combined reports."""

    def test_no_scan_explains_how_to_start(self):
        from pkgdb.reports import build_ci_section

        html = build_ci_section([])
        assert "pkgdb repo discover" in html
        assert "pkgdb ci" in html

    def test_all_green(self):
        from pkgdb.reports import build_ci_section

        html = build_ci_section(
            [{"repo_key": "o/r", "workflow_name": "build", "state": CI_STATE_PASS}]
        )
        assert "All 1 repositories are green." in html
        assert "build" not in html

    def test_failures_are_listed(self):
        from pkgdb.reports import build_ci_section

        html = build_ci_section(
            [
                {
                    "repo_key": "o/r",
                    "workflow_name": "build",
                    "state": CI_STATE_FAIL,
                    "branch": "main",
                    "run_url": "https://github.com/o/r/actions/runs/1",
                    "failing_days": 6,
                    "packages": ["pkg"],
                }
            ]
        )
        assert "1 of 1 repositories have failing workflows." in html
        assert "ci-fail" in html
        assert "6d" in html
        assert 'href="https://github.com/o/r"' in html
        assert "https://github.com/o/r/actions/runs/1" in html

    def test_show_all_includes_passing(self):
        from pkgdb.reports import build_ci_section

        rows = [{"repo_key": "o/r", "workflow_name": "docs", "state": CI_STATE_PASS}]
        assert "docs" in build_ci_section(rows, show_all=True)

    def test_workflow_names_are_escaped(self):
        # Workflow names come from arbitrary repository YAML.
        from pkgdb.reports import build_ci_section

        html = build_ci_section(
            [
                {
                    "repo_key": "o/r",
                    "workflow_name": "<script>alert(1)</script>",
                    "state": CI_STATE_FAIL,
                }
            ]
        )
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html


class TestCIHtmlReport:
    def test_standalone_report(self, temp_db, tmp_path):
        service = PackageStatsService(temp_db)
        service.add_repo("o/r")
        payload = {"workflow_runs": [_run(name="build", conclusion="failure")]}
        out = tmp_path / "ci.html"
        with patch("pkgdb.github.urlopen", return_value=_json_response(payload)):
            assert service.generate_ci_report(str(out), branch="main") is True
        html = out.read_text()
        assert "<title>CI Status</title>" in html
        assert "o/r" in html and "build" in html

    def test_report_without_scanning(self, temp_db, tmp_path):
        service = PackageStatsService(temp_db)
        service.add_repo("o/r")
        payload = {"workflow_runs": [_run(conclusion="failure")]}
        with patch("pkgdb.github.urlopen", return_value=_json_response(payload)):
            service.fetch_ci_status(branch="main")
        out = tmp_path / "ci.html"
        with patch("pkgdb.github.urlopen", side_effect=AssertionError("no request")):
            service.generate_ci_report(str(out), scan=False)
        assert "build" in out.read_text()

    def test_rejects_a_bad_output_path(self, temp_db, tmp_path):
        service = PackageStatsService(temp_db)
        with pytest.raises(ValueError):
            service.generate_ci_report(str(tmp_path / "ci.txt"), scan=False)

    def test_ci_command_writes_a_report(self, temp_db, tmp_path):
        out = tmp_path / "ci.html"
        payload = {"workflow_runs": [_run(name="build", conclusion="failure")]}
        argv = [
            "pkgdb",
            "-d",
            temp_db,
            "ci",
            "--repo",
            "o/r",
            "--branch",
            "main",
            "-o",
            str(out),
            "--no-browser",
        ]
        with patch("sys.argv", argv), _no_config():
            with patch("pkgdb.github.urlopen", return_value=_json_response(payload)):
                with pytest.raises(SystemExit):
                    main()
        assert "build" in out.read_text()

    def test_ci_command_does_not_write_by_default(self, temp_db, tmp_path):
        payload = {"workflow_runs": [_run()]}
        argv = ["pkgdb", "-d", temp_db, "ci", "--repo", "o/r", "--branch", "main"]
        with patch("sys.argv", argv), _no_config():
            with patch("pkgdb.github.urlopen", return_value=_json_response(payload)):
                main()
        assert list(tmp_path.iterdir()) == []


class TestCombinedReport:
    """`pkgdb report --ci` adds the section to the main report."""

    @staticmethod
    def _seed_downloads(temp_db):
        from pkgdb import store_stats

        conn = get_db_connection(temp_db)
        init_db(conn)
        add_package(conn, "pkg")
        store_stats(
            conn,
            "pkg",
            {"last_day": 1, "last_week": 7, "last_month": 30, "total": 100},
        )
        add_repo(conn, "o/r")
        conn.close()

    def test_ci_section_is_included(self, temp_db, tmp_path):
        self._seed_downloads(temp_db)
        service = PackageStatsService(temp_db)
        out = tmp_path / "report.html"
        payload = {"workflow_runs": [_run(name="build", conclusion="failure")]}
        with patch("pkgdb.github.urlopen", return_value=_json_response(payload)):
            assert service.generate_report(str(out), include_ci=True) is True
        html = out.read_text()
        assert "CI Status" in html
        assert "build" in html

    def test_ci_section_is_absent_by_default(self, temp_db, tmp_path):
        self._seed_downloads(temp_db)
        service = PackageStatsService(temp_db)
        out = tmp_path / "report.html"
        with patch("pkgdb.github.urlopen", side_effect=AssertionError("no request")):
            service.generate_report(str(out))
        assert "CI Status" not in out.read_text()

    def test_report_parser_accepts_ci(self):
        args = create_parser().parse_args(["report", "--ci"])
        assert args.ci is True

    def test_update_parser_accepts_ci(self):
        args = create_parser().parse_args(["update", "--ci"])
        assert args.ci is True


class TestDashboardCIPanel:
    def test_api_endpoint_returns_rows(self, temp_db):
        service = PackageStatsService(temp_db)
        service.add_repo("o/r")
        payload = {"workflow_runs": [_run(name="build", conclusion="failure")]}
        with patch("pkgdb.github.urlopen", return_value=_json_response(payload)):
            service.fetch_ci_status(branch="main")
        rows = service.get_ci_rows()
        assert rows[0]["state"] == CI_STATE_FAIL

    def test_overview_page_has_the_panel(self):
        from pkgdb.dashboard import generate_overview_page

        page = generate_overview_page()
        assert 'id="ci-card"' in page
        assert "/api/ci" in page
        # Hidden until a scan has run, so the panel never nags.
        assert 'id="ci-card" style="display:none"' in page


class TestPruneStaleWorkflows:
    """A workflow this scan did not see must not keep reporting its failure."""

    def test_renamed_workflow_is_dropped(self, temp_db):
        service = PackageStatsService(temp_db)
        service.add_repo("o/r")
        before = {
            "workflow_runs": [
                _run(workflow_id=1, name="old-name", conclusion="failure")
            ]
        }
        after = {
            "workflow_runs": [
                _run(workflow_id=1, name="new-name", conclusion="failure")
            ]
        }
        with patch("pkgdb.github.urlopen", return_value=_json_response(before)):
            service.fetch_ci_status(branch="main")
        with patch("pkgdb.github.urlopen", return_value=_json_response(after)):
            service.fetch_ci_status(branch="main", use_cache=False)
        assert [r["workflow_name"] for r in service.get_ci_status("o/r")] == [
            "new-name"
        ]

    def test_deleted_workflow_is_dropped(self, temp_db):
        service = PackageStatsService(temp_db)
        service.add_repo("o/r")
        before = {
            "workflow_runs": [
                _run(id=1, workflow_id=1, name="build", conclusion="failure"),
                _run(id=2, workflow_id=2, name="gone", conclusion="failure"),
            ]
        }
        after = {
            "workflow_runs": [
                _run(id=1, workflow_id=1, name="build", conclusion="failure")
            ]
        }
        with patch("pkgdb.github.urlopen", return_value=_json_response(before)):
            service.fetch_ci_status(branch="main")
        with patch("pkgdb.github.urlopen", return_value=_json_response(after)):
            service.fetch_ci_status(branch="main", use_cache=False)
        assert [r["workflow_name"] for r in service.get_ci_status("o/r")] == ["build"]

    def test_a_failed_fetch_does_not_erase_state(self, temp_db):
        # A repository that could not be reached is unknown, not empty.
        from urllib.error import URLError

        service = PackageStatsService(temp_db)
        service.add_repo("o/r")
        payload = {"workflow_runs": [_run(name="build", conclusion="failure")]}
        with patch("pkgdb.github.urlopen", return_value=_json_response(payload)):
            service.fetch_ci_status(branch="main")
        with patch("pkgdb.github.urlopen", side_effect=URLError("boom")):
            service.fetch_ci_status(branch="main", use_cache=False)
        assert len(service.get_ci_status("o/r")) == 1

    def test_other_repositories_are_untouched(self, temp_db):
        service = PackageStatsService(temp_db)
        conn = get_db_connection(temp_db)
        init_db(conn)
        add_repo(conn, "o/a")
        add_repo(conn, "o/b")
        store_ci_status(conn, "o/b", "keep-me", CI_STATE_FAIL)
        conn.close()
        payload = {"workflow_runs": [_run(name="build")]}
        with patch("pkgdb.github.urlopen", return_value=_json_response(payload)):
            service.fetch_ci_status(repos=["o/a"], branch="main")
        assert [r["workflow_name"] for r in service.get_ci_status("o/b")] == ["keep-me"]


class TestNoRunsIsRecorded:
    """The scan and the report must agree on how many repositories were covered."""

    def test_no_runs_is_stored(self, temp_db):
        service = PackageStatsService(temp_db)
        service.add_repo("o/r")
        with patch(
            "pkgdb.github.urlopen", return_value=_json_response({"workflow_runs": []})
        ):
            service.fetch_ci_status(branch="main")
        stored = service.get_ci_status("o/r")
        assert stored[0]["state"] == CI_STATE_NO_RUNS

    def test_counts_agree_between_scan_and_report(self, temp_db):
        service = PackageStatsService(temp_db)
        for key in ("o/runs", "o/empty"):
            service.add_repo(key)

        def respond(req, **kwargs):
            if "o/empty" in req.full_url:
                return _json_response({"workflow_runs": []})
            return _json_response({"workflow_runs": [_run(conclusion="failure")]})

        with patch("pkgdb.github.urlopen", side_effect=respond):
            results = service.fetch_ci_status(branch="main")
        scanned = len(results)
        reported = len({r["repo_key"] for r in service.get_ci_rows()})
        assert scanned == reported == 2

    def test_a_repo_that_gains_runs_loses_the_placeholder(self, temp_db):
        service = PackageStatsService(temp_db)
        service.add_repo("o/r")
        with patch(
            "pkgdb.github.urlopen", return_value=_json_response({"workflow_runs": []})
        ):
            service.fetch_ci_status(branch="main")
        payload = {"workflow_runs": [_run(name="build")]}
        with patch("pkgdb.github.urlopen", return_value=_json_response(payload)):
            service.fetch_ci_status(branch="main", use_cache=False)
        assert [r["workflow_name"] for r in service.get_ci_status("o/r")] == ["build"]
