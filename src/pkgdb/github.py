"""GitHub API client for fetching repository statistics."""

import json
import logging
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .types import (
    CI_STATE_FAIL,
    CI_STATE_PASS,
    CI_STATE_RUNNING,
    CI_STATE_UNKNOWN,
)
from .utils import utcnow

logger = logging.getLogger("pkgdb")

GITHUB_API = "https://api.github.com"
GITHUB_REPO_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/.*)?$"
)
PYPI_JSON_API = "https://pypi.org/pypi"

# Cache TTL: 24 hours
GITHUB_CACHE_TTL_HOURS = 24

# Cache-key suffix for the issues-only count, which is a separate API call.
OPEN_ISSUES_CACHE_SUFFIX = "#open-issues"

# Cache-key prefix for workflow run listings. The branch is appended, because
# the same repository is scanned per branch.
CI_CACHE_SUFFIX = "#ci"

# CI state changes on every push, so it gets a far shorter TTL than the repo
# metadata sharing the table.
CI_CACHE_TTL_MINUTES = 60

# Runs requested per repository. Scoped to a single branch this covers the
# latest run of every workflow unless one workflow fires far more than the rest.
CI_RUN_WINDOW = 30

# Conclusions that mean the workflow is broken, as opposed to merely not green.
# `cancelled`, `skipped`, `neutral` and `action_required` are reported under
# their own names so that a cancelled run is not chased as a failure.
CI_FAILURE_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})

# GitHub's per-IP limit without a token. Named so the warning can quote it.
UNAUTHENTICATED_RATE_LIMIT = 60

# One-shot warning flags: every request would otherwise repeat the message.
_warned_unauthenticated = False
_warned_rate_limited = False

GITHUB_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS github_cache (
    repo_key TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    expires_at DATETIME NOT NULL
)
"""


@dataclass
class RepoStats:
    """Statistics for a GitHub repository."""

    owner: str
    name: str
    full_name: str
    description: str | None
    stars: int
    forks: int
    # GitHub's open_issues_count, which counts open pull requests as issues.
    open_issues: int
    watchers: int
    language: str | None
    license: str | None
    created_at: datetime | None
    updated_at: datetime | None
    pushed_at: datetime | None
    archived: bool
    fork: bool
    default_branch: str
    topics: list[str]
    homepage: str | None = None
    # Open issues with pull requests excluded, from the search API. None when
    # the count could not be fetched; see fetch_open_issue_count().
    open_issues_excl_prs: int | None = None

    @property
    def repo_url(self) -> str:
        """Canonical HTTPS URL of the git repository."""
        return f"https://github.com/{self.full_name}"

    @property
    def issues_url(self) -> str:
        """URL of the repository's open issue list."""
        return f"{self.repo_url}/issues"

    @property
    def days_since_push(self) -> int | None:
        if self.pushed_at:
            # pushed_at comes from the API as UTC with the tzinfo stripped.
            return (utcnow() - self.pushed_at).days
        return None

    @property
    def is_active(self) -> bool:
        days = self.days_since_push
        return days is not None and days < 365

    @property
    def activity_status(self) -> str:
        if self.archived:
            return "archived"
        days = self.days_since_push
        if days is None:
            return "unknown"
        if days < 30:
            return "very active"
        if days < 90:
            return "active"
        if days < 365:
            return "maintained"
        return "stale"


@dataclass
class WorkflowRun:
    """One GitHub Actions workflow run."""

    workflow_id: int
    workflow_name: str
    run_id: int
    status: str
    conclusion: str | None
    branch: str | None
    event: str | None
    created_at: datetime | None
    url: str | None

    @property
    def state(self) -> str:
        """Normalized state: PASS, FAIL, RUNNING, or the conclusion upcased.

        A run that has not completed is RUNNING whatever its queue state, and a
        conclusion outside CI_FAILURE_CONCLUSIONS keeps its own name so that
        CANCELLED and SKIPPED stay distinguishable from a real break.
        """
        if self.status != "completed":
            return CI_STATE_RUNNING
        if self.conclusion is None:
            return CI_STATE_UNKNOWN
        if self.conclusion == "success":
            return CI_STATE_PASS
        if self.conclusion in CI_FAILURE_CONCLUSIONS:
            return CI_STATE_FAIL
        return self.conclusion.upper()

    @property
    def is_failure(self) -> bool:
        return self.state == CI_STATE_FAIL


@dataclass
class RepoResult:
    """Result of fetching repo stats for a package."""

    package_name: str
    repo_url: str | None
    stats: RepoStats | None = None
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.stats is not None


def parse_github_url(url: str) -> tuple[str, str] | None:
    """Extract owner and repo name from a GitHub URL.

    Returns (owner, repo) tuple or None if not a GitHub URL.
    """
    if not url:
        return None
    match = GITHUB_REPO_PATTERN.match(url)
    if match:
        return match.group(1), match.group(2)
    return None


# `gh auth token` spawns a subprocess and the answer cannot change mid-run, so
# it is resolved once. None is a real answer here, hence the separate flag.
_gh_cli_token_cached: str | None = None
_gh_cli_token_resolved = False


def _gh_cli_token() -> str | None:
    """Read the github.com token held by an authenticated ``gh`` CLI.

    Returns None when gh is absent, not logged in, or slow to answer. Neither
    the token nor gh's stderr is logged: this is a credential.
    """
    global _gh_cli_token_cached, _gh_cli_token_resolved
    if _gh_cli_token_resolved:
        return _gh_cli_token_cached
    _gh_cli_token_resolved = True
    try:
        result = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    _gh_cli_token_cached = result.stdout.strip() or None
    return _gh_cli_token_cached


def get_github_token() -> str | None:
    """Get a GitHub token.

    Prefers GITHUB_TOKEN, then GH_TOKEN, then the token an authenticated ``gh``
    CLI already holds. The gh fallback exists because the unauthenticated limit
    of 60 requests/hour is too low to scan an account's CI, and requiring a
    hand-made token to do what gh is already logged in for is friction with no
    security benefit. Setting either variable overrides gh.
    """
    return (
        os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or _gh_cli_token()
    )


def extract_github_url(package_name: str) -> str | None:
    """Extract GitHub repository URL from PyPI package metadata.

    Queries the PyPI JSON API and looks for GitHub URLs in project_urls
    and home_page fields.

    Returns the GitHub URL or None if not found.
    """
    url = f"{PYPI_JSON_API}/{package_name}/json"
    try:
        req = Request(url, headers={"Accept": "application/json"})
        with urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
    except (HTTPError, URLError, TimeoutError, OSError) as e:
        logger.warning("Could not fetch PyPI metadata for '%s': %s", package_name, e)
        return None

    info = data.get("info", {})

    # Check project_urls first (most reliable)
    project_urls = info.get("project_urls") or {}
    for key in ("Repository", "Source", "Source Code", "Code", "GitHub", "Homepage"):
        val = project_urls.get(key, "")
        if val and "github.com" in val.lower():
            return str(val)

    # Fallback to home_page
    home_page = info.get("home_page") or ""
    if "github.com" in home_page.lower():
        return str(home_page)

    # Check all project_urls values
    for val in project_urls.values():
        if val and "github.com" in val.lower():
            return str(val)

    return None


# ---------------------------------------------------------------------------
# GitHub API Cache
# ---------------------------------------------------------------------------


def _get_cache_key(owner: str, repo: str) -> str:
    return f"{owner.lower()}/{repo.lower()}"


def _ensure_cache_table(conn: sqlite3.Connection) -> None:
    conn.execute(GITHUB_CACHE_SCHEMA)


def _get_cached_json(conn: sqlite3.Connection, cache_key: str) -> dict[str, Any] | None:
    """Get a cached API response by key if still valid."""
    _ensure_cache_table(conn)
    cursor = conn.execute(
        "SELECT data FROM github_cache WHERE repo_key = ? AND expires_at > datetime('now')",
        (cache_key,),
    )
    row = cursor.fetchone()
    if row:
        try:
            result: dict[str, Any] = json.loads(row["data"])
            return result
        except json.JSONDecodeError:
            return None
    return None


def _store_cached_json(
    conn: sqlite3.Connection,
    cache_key: str,
    data: dict[str, Any],
    ttl_hours: float = GITHUB_CACHE_TTL_HOURS,
) -> None:
    """Store an API response under a cache key."""
    _ensure_cache_table(conn)
    # UTC: compared against SQLite's datetime('now') in _get_cached_json.
    expires_at = utcnow() + timedelta(hours=ttl_hours)
    conn.execute(
        """INSERT OR REPLACE INTO github_cache (repo_key, data, fetched_at, expires_at)
           VALUES (?, ?, datetime('now'), ?)""",
        (cache_key, json.dumps(data), expires_at.isoformat()),
    )
    conn.commit()


def get_cached_repo_data(
    conn: sqlite3.Connection, owner: str, repo: str
) -> dict[str, Any] | None:
    """Get cached GitHub API response if still valid."""
    return _get_cached_json(conn, _get_cache_key(owner, repo))


def store_cached_repo_data(
    conn: sqlite3.Connection,
    owner: str,
    repo: str,
    data: dict[str, Any],
    ttl_hours: int = GITHUB_CACHE_TTL_HOURS,
) -> None:
    """Store GitHub API response in cache."""
    _store_cached_json(conn, _get_cache_key(owner, repo), data, ttl_hours)


def clear_github_cache(conn: sqlite3.Connection, expired_only: bool = True) -> int:
    """Clear GitHub API cache entries.

    Returns number of entries cleared.
    """
    _ensure_cache_table(conn)
    if expired_only:
        conn.execute("DELETE FROM github_cache WHERE expires_at <= datetime('now')")
    else:
        conn.execute("DELETE FROM github_cache")
    deleted: int = conn.execute("SELECT changes()").fetchone()[0]
    conn.commit()
    return deleted


def get_github_cache_stats(conn: sqlite3.Connection) -> dict[str, int]:
    """Get statistics about the GitHub cache."""
    _ensure_cache_table(conn)
    total = conn.execute("SELECT COUNT(*) FROM github_cache").fetchone()[0]
    valid = conn.execute(
        "SELECT COUNT(*) FROM github_cache WHERE expires_at > datetime('now')"
    ).fetchone()[0]
    return {"total": total, "valid": valid, "expired": total - valid}


# ---------------------------------------------------------------------------
# Exponential Backoff
# ---------------------------------------------------------------------------


def _rate_limit_exhausted(error: HTTPError) -> bool:
    """True when a 403 says no requests are left in the window.

    A 403 without this header is a permission error or a secondary limit, both
    of which the backoff path still handles.
    """
    return str(error.headers.get("x-ratelimit-remaining", "")) == "0"


def _warn_rate_limited(error: HTTPError) -> None:
    """Report an exhausted quota once per process, with its reset time."""
    global _warned_rate_limited
    if _warned_rate_limited:
        return
    _warned_rate_limited = True
    reset = error.headers.get("x-ratelimit-reset")
    when = ""
    if reset:
        try:
            when = datetime.fromtimestamp(int(reset)).strftime(" (resets at %H:%M)")
        except (ValueError, OSError, OverflowError):
            when = ""
    logger.warning(
        "GitHub rate limit exhausted%s. Results are incomplete; repositories "
        "that could not be reached are reported as unknown, not as passing.",
        when,
    )


def _exponential_backoff(
    attempt: int,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
) -> float:
    """Calculate delay for exponential backoff."""
    import random

    delay: float = min(base_delay * (2**attempt), max_delay)
    delay = delay * (0.5 + random.random())
    return delay


def _fetch_json(
    url: str,
    headers: dict[str, str],
    max_retries: int = 3,
    timeout: float = 30.0,
) -> Any:
    """Fetch URL with exponential backoff on rate limiting (403).

    Returns the decoded JSON, which is an object for most endpoints and an
    array for the list ones. Raises HTTPError on non-retryable errors or max
    retries exceeded.
    """
    last_error: HTTPError | None = None

    for attempt in range(max_retries + 1):
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode())
        except HTTPError as e:
            if e.code == 403 and _rate_limit_exhausted(e):
                # The quota is gone until the reset, which is up to an hour
                # away. Sleeping through that inside one run is not retrying,
                # it is hanging, and it multiplies by every repository scanned.
                _warn_rate_limited(e)
                raise
            if e.code == 403:
                last_error = e
                if attempt < max_retries:
                    delay = _exponential_backoff(attempt)
                    retry_after = e.headers.get("Retry-After")
                    if retry_after:
                        try:
                            delay = max(delay, float(retry_after))
                        except ValueError:
                            pass
                    time.sleep(delay)
                    continue
            raise

    if last_error:
        raise last_error
    raise RuntimeError("Unexpected state in backoff loop")


def _fetch_with_backoff(
    url: str,
    headers: dict[str, str],
    max_retries: int = 3,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Fetch a JSON object endpoint. See _fetch_json()."""
    result: dict[str, Any] = _fetch_json(url, headers, max_retries, timeout)
    return result


# ---------------------------------------------------------------------------
# Parsing & Fetching
# ---------------------------------------------------------------------------


def _parse_datetime(date_str: str | None) -> datetime | None:
    if date_str:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    return None


def _parse_repo_data(data: dict[str, Any]) -> RepoStats:
    """Parse GitHub API response into RepoStats."""
    license_name = None
    if data.get("license"):
        license_name = data["license"].get("spdx_id") or data["license"].get("name")

    return RepoStats(
        owner=data["owner"]["login"],
        name=data["name"],
        full_name=data["full_name"],
        description=data.get("description"),
        stars=data.get("stargazers_count", 0),
        forks=data.get("forks_count", 0),
        open_issues=data.get("open_issues_count", 0),
        watchers=data.get("subscribers_count", 0),
        language=data.get("language"),
        license=license_name,
        created_at=_parse_datetime(data.get("created_at")),
        updated_at=_parse_datetime(data.get("updated_at")),
        pushed_at=_parse_datetime(data.get("pushed_at")),
        archived=data.get("archived", False),
        fork=data.get("fork", False),
        default_branch=data.get("default_branch", "main"),
        topics=data.get("topics", []),
        homepage=data.get("homepage") or None,
    )


def _github_headers() -> dict[str, str]:
    """Build request headers, adding the token when one is configured.

    Warns once per process when there is no token. An unauthenticated scan of
    any real account runs out of quota partway through and reports the repos it
    could not reach as warnings, which looks like a GitHub problem unless the
    cause is named.
    """
    global _warned_unauthenticated
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "pkgdb",
    }
    token = get_github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif not _warned_unauthenticated:
        _warned_unauthenticated = True
        logger.warning(
            "No GitHub token found: limited to %d requests/hour, public "
            "repositories only. Run 'gh auth login', or set GITHUB_TOKEN.",
            UNAUTHENTICATED_RATE_LIMIT,
        )
    return headers


def fetch_open_issue_count(
    owner: str,
    repo: str,
    conn: sqlite3.Connection | None = None,
    use_cache: bool = True,
) -> int | None:
    """Count a repository's open issues, excluding pull requests.

    The repository endpoint's ``open_issues_count`` counts open pull requests
    as issues, so this asks the search API for ``is:issue is:open`` instead.
    Cached alongside the repo response with the same TTL.

    Returns None when the count could not be determined, which callers should
    render as unknown rather than as zero. The search API has its own, tighter
    rate limit (30 requests/minute authenticated, 10 unauthenticated), so this
    is the part of a GitHub fetch most likely to come back empty.
    """
    cache_key = _get_cache_key(owner, repo) + OPEN_ISSUES_CACHE_SUFFIX
    if use_cache and conn is not None:
        cached = _get_cached_json(conn, cache_key)
        if cached is not None:
            count = cached.get("open_issues")
            return int(count) if isinstance(count, int) else None

    query = f"repo:{owner}/{repo}+is:issue+is:open"
    url = f"{GITHUB_API}/search/issues?q={query}&per_page=1"
    try:
        data = _fetch_with_backoff(url, _github_headers(), max_retries=2, timeout=30.0)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
        logger.debug("Open issue count unavailable for %s/%s: %s", owner, repo, e)
        return None

    total = data.get("total_count")
    if not isinstance(total, int):
        return None

    if use_cache and conn is not None:
        _store_cached_json(conn, cache_key, {"open_issues": total})
    return total


def fetch_repo_stats(
    owner: str,
    repo: str,
    conn: sqlite3.Connection | None = None,
    use_cache: bool = True,
) -> RepoStats:
    """Fetch repository statistics from GitHub API.

    Uses cached responses when available (24h TTL).
    Authenticates via GITHUB_TOKEN/GH_TOKEN or the `gh` CLI; see
    get_github_token().
    Uses exponential backoff on rate limiting (403).

    Raises HTTPError on API errors.
    """
    # Check cache first
    cached = (
        get_cached_repo_data(conn, owner, repo)
        if use_cache and conn is not None
        else None
    )
    if cached:
        stats = _parse_repo_data(cached)
    else:
        url = f"{GITHUB_API}/repos/{owner}/{repo}"
        data = _fetch_with_backoff(url, _github_headers(), max_retries=3, timeout=30.0)

        # Cache the response
        if use_cache and conn is not None:
            store_cached_repo_data(conn, owner, repo, data)

        stats = _parse_repo_data(data)

    # open_issues_count counts pull requests too; get the issues-only figure
    # separately. Cached independently, so a warm cache costs no requests.
    stats.open_issues_excl_prs = fetch_open_issue_count(
        owner, repo, conn=conn, use_cache=use_cache
    )
    return stats


def fetch_package_github_stats(
    package_name: str,
    conn: sqlite3.Connection | None = None,
    use_cache: bool = True,
) -> RepoResult:
    """Fetch GitHub stats for a PyPI package.

    Looks up the GitHub repo URL from PyPI metadata, then fetches
    repository statistics from the GitHub API.
    """
    github_url = extract_github_url(package_name)
    if not github_url:
        return RepoResult(
            package_name=package_name,
            repo_url=None,
            error="No GitHub repository found in PyPI metadata",
        )

    parsed = parse_github_url(github_url)
    if not parsed:
        return RepoResult(
            package_name=package_name,
            repo_url=github_url,
            error="Could not parse GitHub URL",
        )

    owner, repo = parsed
    try:
        stats = fetch_repo_stats(owner, repo, conn=conn, use_cache=use_cache)
        return RepoResult(package_name=package_name, repo_url=github_url, stats=stats)
    except HTTPError as e:
        if e.code == 404:
            error = "Repository not found"
        elif e.code == 403:
            error = "Rate limited (retries exhausted)"
        else:
            error = f"HTTP {e.code}"
        return RepoResult(package_name=package_name, repo_url=github_url, error=error)
    except (URLError, TimeoutError, OSError) as e:
        return RepoResult(package_name=package_name, repo_url=github_url, error=str(e))


def fetch_github_releases(owner: str, repo: str) -> list[dict[str, Any]] | None:
    """Fetch release list from the GitHub Releases API.

    Args:
        owner: Repository owner.
        repo: Repository name.

    Returns:
        List of release dicts with tag_name, published_at, and name,
        sorted by published_at ascending. Returns None on API error,
        or an empty list if the repo has no releases.
    """
    token = get_github_token()
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "pkgdb",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{GITHUB_API}/repos/{owner}/{repo}/releases?per_page=100"
    try:
        req = Request(url, headers=headers)
        with urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode())
    except HTTPError as e:
        if e.code == 404:
            return []
        logger.warning(
            "Error fetching GitHub releases for %s/%s: HTTP %d", owner, repo, e.code
        )
        return None
    except (URLError, TimeoutError, OSError) as e:
        logger.warning("Error fetching GitHub releases for %s/%s: %s", owner, repo, e)
        return None

    if not isinstance(data, list):
        return []

    releases = []
    for r in data:
        published_at = r.get("published_at")
        if not published_at or r.get("draft"):
            continue
        releases.append(
            {
                "tag_name": r.get("tag_name", ""),
                "published_at": published_at[:10],  # YYYY-MM-DD
                "name": r.get("name"),
            }
        )

    releases.sort(key=lambda x: x["published_at"])
    return releases


# ---------------------------------------------------------------------------
# GitHub Actions
# ---------------------------------------------------------------------------


def _ci_cache_key(owner: str, repo: str, branch: str | None) -> str:
    return f"{_get_cache_key(owner, repo)}{CI_CACHE_SUFFIX}:{branch or '*'}"


def _parse_workflow_run(data: dict[str, Any]) -> WorkflowRun:
    """Parse one entry of the workflow runs listing.

    Keyed on ``workflow_id`` rather than the run's ``name``: a workflow that
    sets ``run-name`` reports a different name per run, which would otherwise
    split one workflow's history into several.

    A workflow file with no ``name:`` key is reported by GitHub under its own
    path, so ``name`` comes back as ``.github/workflows/build.yml``. That is
    shown as ``build.yml``, which is still unique within the repository.
    """
    path = str(data.get("path") or "")
    name = str(data.get("name") or path or "workflow")
    if name == path:
        name = path.rsplit("/", 1)[-1]
    return WorkflowRun(
        workflow_id=int(data.get("workflow_id") or 0),
        workflow_name=name,
        run_id=int(data.get("id") or 0),
        status=str(data.get("status") or "unknown"),
        conclusion=data.get("conclusion"),
        branch=data.get("head_branch"),
        event=data.get("event"),
        created_at=_parse_datetime(data.get("created_at")),
        url=data.get("html_url"),
    )


def parse_workflow_runs(raw: list[dict[str, Any]]) -> list[WorkflowRun]:
    """Parse a raw workflow runs listing."""
    return [_parse_workflow_run(r) for r in raw]


def latest_run_per_workflow(runs: list[WorkflowRun]) -> list[WorkflowRun]:
    """Reduce a run listing to the most recent run of each workflow.

    Ordered by workflow name. Ties on ``created_at`` fall back to the run id,
    which increases monotonically.
    """

    def key(run: WorkflowRun) -> tuple[datetime, int]:
        return (run.created_at or datetime.min, run.run_id)

    latest: dict[int, WorkflowRun] = {}
    for run in runs:
        current = latest.get(run.workflow_id)
        if current is None or key(run) > key(current):
            latest[run.workflow_id] = run
    return sorted(latest.values(), key=lambda r: r.workflow_name.lower())


def get_cached_workflow_runs(
    conn: sqlite3.Connection, owner: str, repo: str, branch: str | None = None
) -> list[dict[str, Any]] | None:
    """Return a cached raw run listing for a repo and branch, if still valid."""
    cached = _get_cached_json(conn, _ci_cache_key(owner, repo, branch))
    if cached is None:
        return None
    runs = cached.get("workflow_runs")
    return runs if isinstance(runs, list) else None


def store_cached_workflow_runs(
    conn: sqlite3.Connection,
    owner: str,
    repo: str,
    branch: str | None,
    raw: list[dict[str, Any]],
) -> None:
    """Cache a raw run listing under the repo/branch key."""
    _store_cached_json(
        conn,
        _ci_cache_key(owner, repo, branch),
        {"workflow_runs": raw},
        ttl_hours=CI_CACHE_TTL_MINUTES / 60,
    )


def fetch_workflow_runs_raw(
    owner: str,
    repo: str,
    branch: str | None = None,
    limit: int = CI_RUN_WINDOW,
) -> list[dict[str, Any]] | None:
    """Fetch a repository's recent workflow runs, without cache or parsing.

    Returns the raw listing, an empty list when the repository has no runs on
    the branch, or None when the request failed. The empty and failed cases are
    distinct: one means green-by-absence, the other means unknown.
    """
    params = f"per_page={limit}"
    if branch:
        params += f"&branch={quote(branch, safe='')}"
    url = f"{GITHUB_API}/repos/{owner}/{repo}/actions/runs?{params}"
    try:
        data = _fetch_with_backoff(url, _github_headers(), max_retries=2, timeout=30.0)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        logger.debug("Workflow runs unavailable for %s/%s: %s", owner, repo, e)
        return None
    runs = data.get("workflow_runs")
    return runs if isinstance(runs, list) else []


def fetch_workflow_runs(
    owner: str,
    repo: str,
    branch: str | None = None,
    limit: int = CI_RUN_WINDOW,
    conn: sqlite3.Connection | None = None,
    use_cache: bool = True,
) -> list[WorkflowRun] | None:
    """Fetch a repository's recent workflow runs, serving the cache first.

    Returns None when the listing could not be fetched. Callers scanning many
    repositories should use the cache/network/store functions directly so the
    network half can run in parallel; a SQLite connection cannot cross threads.
    """
    if use_cache and conn is not None:
        cached = get_cached_workflow_runs(conn, owner, repo, branch)
        if cached is not None:
            return parse_workflow_runs(cached)

    raw = fetch_workflow_runs_raw(owner, repo, branch=branch, limit=limit)
    if raw is None:
        return None
    if use_cache and conn is not None:
        store_cached_workflow_runs(conn, owner, repo, branch, raw)
    return parse_workflow_runs(raw)


def fetch_workflow_count(owner: str, repo: str) -> int | None:
    """Count the workflows defined in a repository.

    Used by discovery to skip repositories with no CI. Returns None when the
    request failed, which callers should treat as unknown rather than zero.
    Disabled workflows are included in the count.
    """
    url = f"{GITHUB_API}/repos/{owner}/{repo}/actions/workflows?per_page=1"
    try:
        data = _fetch_with_backoff(url, _github_headers(), max_retries=2, timeout=30.0)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        logger.debug("Workflow count unavailable for %s/%s: %s", owner, repo, e)
        return None
    count = data.get("total_count")
    return int(count) if isinstance(count, int) else None


def fetch_default_branch(
    owner: str,
    repo: str,
    conn: sqlite3.Connection | None = None,
    use_cache: bool = True,
) -> str | None:
    """Return a repository's default branch.

    Shares the repository cache with fetch_repo_stats(), so a repo already
    fetched for its stars costs no request. Deliberately does not call
    fetch_open_issue_count(): the branch name does not need the search API.
    """
    if use_cache and conn is not None:
        cached = get_cached_repo_data(conn, owner, repo)
        if cached is not None:
            branch = cached.get("default_branch")
            return str(branch) if branch else None

    url = f"{GITHUB_API}/repos/{owner}/{repo}"
    try:
        data = _fetch_with_backoff(url, _github_headers(), max_retries=2, timeout=30.0)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        logger.debug("Default branch unavailable for %s/%s: %s", owner, repo, e)
        return None
    if use_cache and conn is not None:
        store_cached_repo_data(conn, owner, repo, data)
    branch = data.get("default_branch")
    return str(branch) if branch else None


def fetch_authenticated_login() -> str | None:
    """Return the login of the token's owner, or None without a usable token."""
    if not get_github_token():
        return None
    try:
        data = _fetch_with_backoff(
            f"{GITHUB_API}/user", _github_headers(), max_retries=1, timeout=30.0
        )
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        logger.debug("Could not identify the authenticated user: %s", e)
        return None
    login = data.get("login")
    return str(login) if login else None


def fetch_user_repos(
    user: str,
    include_forks: bool = False,
    include_archived: bool = False,
    max_pages: int = 10,
) -> list[dict[str, Any]] | None:
    """List a user's own repositories.

    Returns ``{full_name, default_branch, fork, archived, private}`` dicts, or
    None when the listing failed. Paginates at 100 per page up to ``max_pages``.

    ``/users/{user}/repos`` returns public repositories only, even with a token,
    so listing your own account goes through ``/user/repos`` instead -- otherwise
    a private repository's CI is invisible with no indication it was skipped.
    """
    own_account = user.lower() == (fetch_authenticated_login() or "").lower()

    repos: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        if own_account:
            url = (
                f"{GITHUB_API}/user/repos"
                f"?per_page=100&page={page}&affiliation=owner&sort=full_name"
            )
        else:
            url = (
                f"{GITHUB_API}/users/{quote(user, safe='')}/repos"
                f"?per_page=100&page={page}&type=owner&sort=full_name"
            )
        try:
            data = _fetch_json(url, _github_headers(), max_retries=2, timeout=30.0)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            logger.warning("Could not list repositories for '%s': %s", user, e)
            return None if page == 1 else repos
        if not isinstance(data, list) or not data:
            break
        for r in data:
            if not include_forks and r.get("fork"):
                continue
            if not include_archived and r.get("archived"):
                continue
            repos.append(
                {
                    "full_name": r.get("full_name", ""),
                    "default_branch": r.get("default_branch"),
                    "fork": bool(r.get("fork")),
                    "archived": bool(r.get("archived")),
                    "private": bool(r.get("private")),
                }
            )
        if len(data) < 100:
            break
    return repos
