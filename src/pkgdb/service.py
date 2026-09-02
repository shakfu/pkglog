"""Service layer for pkgdb - provides a clean abstraction over database and API operations."""

import fnmatch
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .api import (
    aggregate_env_stats,
    check_package_exists,
    fetch_daily_downloads,
    fetch_os_stats,
    fetch_package_stats,
    fetch_pypi_releases,
    fetch_python_versions,
    fetch_user_packages,
)
from .db import (
    REPO_SOURCE_DISCOVER,
    REPO_SOURCE_MANUAL,
    REPO_SOURCE_PACKAGE,
    add_package,
    add_package_tag,
    add_repo,
    cleanup_orphaned_stats,
    get_ci_status,
    get_repo_packages,
    get_repos,
    link_package_repo,
    normalize_repo_key,
    prune_ci_status,
    remove_repo,
    seed_repos_from_cache,
    set_repo_enabled,
    store_ci_status,
    get_all_history,
    get_all_github_releases,
    get_all_pypi_releases,
    get_database_stats,
    get_db,
    get_github_releases,
    get_latest_stats,
    get_package_history,
    get_package_tags,
    get_packages,
    get_packages_for_tag,
    get_cached_env_summary,
    get_cached_os_stats,
    get_cached_python_versions,
    get_daily_downloads,
    get_github_stats_history,
    get_milestone_high_water,
    get_next_update_seconds,
    get_packages_needing_update,
    set_milestone_high_water,
    get_pypi_releases,
    get_tags_map,
    store_daily_downloads,
    store_env_stats,
    store_github_releases,
    store_github_stats_snapshot,
    store_pypi_releases,
    get_stats_with_growth,
    prune_old_stats,
    record_fetch_attempt,
    remove_package,
    remove_package_tag,
    store_stats,
)
from .badges import generate_downloads_badge
from .checks import (
    DEFAULT_BASELINE_WEEKS,
    DEFAULT_MIN_WEEKLY,
    DEFAULT_Z_THRESHOLD,
    detect_anomaly,
    detect_milestones,
)
from .export import export_csv, export_json, export_markdown
from .github import (
    CI_RUN_WINDOW,
    RepoResult,
    clear_github_cache,
    extract_github_url,
    fetch_default_branch,
    fetch_github_releases,
    fetch_package_github_stats,
    fetch_user_repos,
    fetch_workflow_count,
    fetch_workflow_runs_raw,
    get_cached_workflow_runs,
    get_github_cache_stats,
    latest_run_per_workflow,
    parse_github_url,
    parse_workflow_runs,
    store_cached_workflow_runs,
)
from .reports import (
    generate_ci_html_report,
    generate_html_report,
    generate_package_html_report,
    generate_project_html_report,
)
from .types import (
    CI_STATE_FAIL,
    CI_STATE_NO_RUNS,
    CategoryDownloads,
    CheckEvent,
    DailyDownload,
    DatabaseInfo,
    GitHubRelease,
    PackageStats,
    PyPIRelease,
)
from .utils import (
    daily_window_sums,
    utcnow,
    validate_output_path,
    validate_package_name,
)

# Delay in seconds between fetching each package to avoid hitting API rate limits
_FETCH_DELAY_SECONDS = 1.0

# Placeholder workflow name for a repository with no runs on the scanned
# branch. One row per repository, so the registry and the report agree on how
# many repositories a scan covered.
CI_NO_RUNS_WORKFLOW = "-"

# Repositories scanned concurrently by `ci`. The GitHub API tolerates this
# comfortably; the bound exists so a 140-repo registry does not open 140 sockets.
_CI_MAX_WORKERS = 8


@dataclass
class PackageInfo:
    """Information about a tracked package."""

    name: str
    added_date: str


@dataclass
class FetchResult:
    """Result of a fetch operation."""

    success: int
    failed: int
    skipped: int
    results: dict[str, PackageStats | None]
    next_update_seconds: float | None = None


@dataclass
class PackageDetails:
    """Detailed statistics for a package."""

    name: str
    stats: PackageStats | None
    python_versions: list[CategoryDownloads] | None
    os_stats: list[CategoryDownloads] | None


@dataclass
class SyncResult:
    """Result of syncing packages from a PyPI user."""

    added: list[str]
    already_tracked: list[str]
    not_on_remote: list[str]
    pruned: list[str]


@dataclass
class CIEntry:
    """The latest known state of one workflow in one repository."""

    repo_key: str
    workflow_name: str
    state: str
    branch: str | None = None
    run_id: int | None = None
    run_url: str | None = None
    run_started_at: str | None = None
    first_failed_at: str | None = None

    @property
    def is_failure(self) -> bool:
        return self.state == CI_STATE_FAIL

    @property
    def failing_days(self) -> int | None:
        """Whole days since the current failing streak began."""
        if not self.is_failure or not self.first_failed_at:
            return None
        try:
            started = datetime.fromisoformat(self.first_failed_at.replace("Z", ""))
        except ValueError:
            return None
        if started.tzinfo is not None:
            started = started.replace(tzinfo=None)
        return max((utcnow() - started).days, 0)


@dataclass
class CIResult:
    """The CI scan of one repository."""

    repo_key: str
    entries: list[CIEntry]
    packages: list[str]
    branch: str | None = None
    error: str | None = None

    @property
    def failures(self) -> list[CIEntry]:
        return [e for e in self.entries if e.is_failure]

    @property
    def ok(self) -> bool:
        return self.error is None and not self.failures


class PackageStatsService:
    """High-level service for managing package statistics.

    Provides a clean abstraction over database and API operations,
    making it easier to test, mock, and extend.
    """

    def __init__(self, db_path: str):
        """Initialize the service with a database path.

        Args:
            db_path: Path to the SQLite database file.
        """
        self.db_path = db_path

    # -------------------------------------------------------------------------
    # Package Management
    # -------------------------------------------------------------------------

    def add_package(self, name: str, verify: bool = True) -> bool:
        """Add a package to tracking.

        Args:
            name: Package name to add.
            verify: If True, verify package exists on PyPI before adding.
                    Network errors are logged as warnings but don't block addition.

        Returns:
            True if package was added, False if it already exists.

        Raises:
            ValueError: If package name is invalid or package not found on PyPI
                        (when verify=True).
        """
        import logging

        logger = logging.getLogger("pkgdb")

        is_valid, error_msg = validate_package_name(name)
        if not is_valid:
            raise ValueError(error_msg)

        if verify:
            exists, error = check_package_exists(name)
            if exists is False:
                raise ValueError(f"Package '{name}' not found on PyPI")
            if exists is None and error:
                # Network error - warn but allow (fail open)
                logger.warning("Could not verify package '%s': %s", name, error)

        with get_db(self.db_path) as conn:
            return add_package(conn, name)

    def remove_package(self, name: str) -> bool:
        """Remove a package from tracking.

        Args:
            name: Package name to remove.

        Returns:
            True if package was removed, False if it didn't exist.
        """
        with get_db(self.db_path) as conn:
            return remove_package(conn, name)

    def list_packages(self) -> list[PackageInfo]:
        """Get list of tracked packages with their added dates.

        Returns:
            List of PackageInfo objects.
        """
        with get_db(self.db_path) as conn:
            packages = get_packages(conn)
            if not packages:
                return []

            cursor = conn.execute(
                "SELECT package_name, added_date FROM packages ORDER BY package_name"
            )
            return [
                PackageInfo(name=row["package_name"], added_date=row["added_date"])
                for row in cursor.fetchall()
            ]

    def import_packages(
        self, file_path: str, verify: bool = True
    ) -> tuple[int, int, list[str], list[str]]:
        """Import packages from a file.

        Args:
            file_path: Path to file (JSON or plain text).
            verify: If True, verify each package exists on PyPI before adding.

        Returns:
            Tuple of (added_count, skipped_count, invalid_names, not_found_names).

        Raises:
            FileNotFoundError: If file doesn't exist.
        """
        import logging

        from .cli import load_packages_from_file

        logger = logging.getLogger("pkgdb")
        packages = load_packages_from_file(file_path)
        added = 0
        skipped = 0
        invalid: list[str] = []
        not_found: list[str] = []

        with get_db(self.db_path) as conn:
            for pkg in packages:
                is_valid, _ = validate_package_name(pkg)
                if not is_valid:
                    invalid.append(pkg)
                    continue

                if verify:
                    exists, error = check_package_exists(pkg)
                    if exists is False:
                        not_found.append(pkg)
                        continue
                    if exists is None and error:
                        logger.warning("Could not verify package '%s': %s", pkg, error)

                if add_package(conn, pkg):
                    added += 1
                else:
                    skipped += 1
        return added, skipped, invalid, not_found

    def sync_packages_from_user(
        self, username: str, prune: bool = False
    ) -> SyncResult | None:
        """Sync tracked packages with a PyPI user's current packages.

        Fetches the user's packages from PyPI and adds any that aren't
        already being tracked. Optionally removes packages no longer
        associated with the user.

        Args:
            username: PyPI username to fetch packages from.
            prune: If True, remove locally tracked packages not in user's
                PyPI account.

        Returns:
            SyncResult with lists of added, already tracked, packages
            not on remote, and pruned packages.
            Returns None if unable to fetch from PyPI.
        """
        remote_packages = fetch_user_packages(username)
        if remote_packages is None:
            return None

        remote_set = set(remote_packages)
        local_packages = [p.name for p in self.list_packages()]
        local_set = set(local_packages)

        # Packages on remote but not locally tracked
        to_add = remote_set - local_set
        # Packages both remote and local
        already_tracked = remote_set & local_set
        # Packages tracked locally but not on remote
        not_on_remote = local_set - remote_set

        added: list[str] = []
        for pkg in sorted(to_add):
            # Skip verification since packages come from PyPI's user_packages API
            if self.add_package(pkg, verify=False):
                added.append(pkg)

        pruned: list[str] = []
        if prune:
            for pkg in sorted(not_on_remote):
                if self.remove_package(pkg):
                    pruned.append(pkg)

        return SyncResult(
            added=added,
            already_tracked=sorted(already_tracked),
            not_on_remote=sorted(not_on_remote),
            pruned=pruned,
        )

    # -------------------------------------------------------------------------
    # Data Fetching
    # -------------------------------------------------------------------------

    def fetch_all_stats(
        self,
        progress_callback: Callable[[int, int, str, PackageStats | None], None]
        | None = None,
        delay: float = _FETCH_DELAY_SECONDS,
    ) -> FetchResult:
        """Fetch and store stats for all tracked packages.

        Skips packages that have been attempted within the last 24 hours.
        Uses batch commits for better performance when storing multiple packages.

        Args:
            progress_callback: Optional callback called for each package with
                (current_index, total_count, package_name, stats_or_none).

        Returns:
            FetchResult with success/failure/skipped counts and results.
        """
        with get_db(self.db_path) as conn:
            all_packages = get_packages(conn)
            if not all_packages:
                return FetchResult(success=0, failed=0, skipped=0, results={})

            packages_to_fetch = get_packages_needing_update(conn)
            skipped = len(all_packages) - len(packages_to_fetch)

            if not packages_to_fetch:
                return FetchResult(
                    success=0,
                    failed=0,
                    skipped=skipped,
                    results={},
                    next_update_seconds=get_next_update_seconds(conn),
                )

            results: dict[str, PackageStats | None] = {}
            success = 0
            failed = 0

            for i, package in enumerate(packages_to_fetch, 1):
                # Throttle requests to avoid HTTP 429 from pypistats API
                if i > 1 and delay > 0:
                    time.sleep(delay)

                stats = fetch_package_stats(package)
                results[package] = stats

                if stats:
                    # Use commit=False for batch operation
                    store_stats(conn, package, stats, commit=False)
                    py_versions = fetch_python_versions(package)
                    os_data = fetch_os_stats(package)
                    store_env_stats(conn, package, py_versions, os_data, commit=False)
                    daily = fetch_daily_downloads(package)
                    store_daily_downloads(conn, package, daily, commit=False)
                    record_fetch_attempt(conn, package, success=True, commit=False)
                    success += 1
                else:
                    record_fetch_attempt(conn, package, success=False, commit=False)
                    failed += 1

                if progress_callback:
                    progress_callback(i, len(packages_to_fetch), package, stats)

            # Single commit for all stores and attempts
            conn.commit()

            return FetchResult(
                success=success, failed=failed, skipped=skipped, results=results
            )

    def fetch_package_details(self, package: str) -> PackageDetails:
        """Fetch detailed statistics for a single package.

        Args:
            package: Package name.

        Returns:
            PackageDetails with stats, Python versions, and OS breakdown.
        """
        return PackageDetails(
            name=package,
            stats=fetch_package_stats(package),
            python_versions=fetch_python_versions(package),
            os_stats=fetch_os_stats(package),
        )

    # -------------------------------------------------------------------------
    # Data Retrieval
    # -------------------------------------------------------------------------

    def get_stats(
        self, with_growth: bool = False, tag: str | None = None
    ) -> list[dict[str, Any]]:
        """Get latest stats for all packages.

        Args:
            with_growth: If True, include growth metrics.
            tag: If given, restrict to packages carrying this tag.

        Returns:
            List of stats dictionaries ordered by total downloads.
        """
        with get_db(self.db_path) as conn:
            stats = (
                get_stats_with_growth(conn, tracked_only=True)
                if with_growth
                else get_latest_stats(conn, tracked_only=True)
            )
            if tag is not None:
                members = set(get_packages_for_tag(conn, tag))
                stats = [s for s in stats if s["package_name"] in members]
            return stats

    # -------------------------------------------------------------------------
    # Tags / groups
    # -------------------------------------------------------------------------

    def add_tag(self, package: str, tag: str) -> bool:
        """Tag a package. Returns True if added, False if empty/duplicate.

        Raises:
            ValueError: If the package is not tracked.
        """
        with get_db(self.db_path) as conn:
            return add_package_tag(conn, package, tag)

    def remove_tag(self, package: str, tag: str) -> bool:
        """Remove a tag from a package. Returns True if a tag was removed."""
        with get_db(self.db_path) as conn:
            return remove_package_tag(conn, package, tag)

    def get_package_tags(self, package: str) -> list[str]:
        """Get the sorted tags for a package."""
        with get_db(self.db_path) as conn:
            return get_package_tags(conn, package)

    def get_tag_summary(self) -> list[dict[str, Any]]:
        """Aggregate latest download stats per tag (a portfolio rollup).

        Returns one entry per tag with its member count and the summed
        ``total``/``last_month``/``last_week``/``last_day`` across members,
        ordered by total downloads descending.
        """
        with get_db(self.db_path) as conn:
            tags_map = get_tags_map(conn)
            latest = {
                s["package_name"]: s for s in get_latest_stats(conn, tracked_only=True)
            }

        summary: list[dict[str, Any]] = []
        for tag, members in tags_map.items():
            agg = {"total": 0, "last_month": 0, "last_week": 0, "last_day": 0}
            for pkg in members:
                s = latest.get(pkg)
                if not s:
                    continue
                for key in agg:
                    agg[key] += s.get(key) or 0
            summary.append(
                {
                    "tag": tag,
                    "package_count": len(members),
                    "packages": members,
                    **agg,
                }
            )
        summary.sort(key=lambda e: e["total"], reverse=True)
        return summary

    def get_history(self, package: str, limit: int = 30) -> list[dict[str, Any]]:
        """Get historical stats for a package.

        Args:
            package: Package name.
            limit: Maximum number of days to return.

        Returns:
            List of historical stats ordered by date descending.
        """
        with get_db(self.db_path) as conn:
            return get_package_history(conn, package, limit=limit, tracked_only=True)

    def get_all_history(
        self, limit_per_package: int = 30
    ) -> dict[str, list[dict[str, Any]]]:
        """Get historical stats for all packages.

        Args:
            limit_per_package: Maximum days per package.

        Returns:
            Dict mapping package names to their history.
        """
        with get_db(self.db_path) as conn:
            return get_all_history(
                conn, limit_per_package=limit_per_package, tracked_only=True
            )

    def get_env_data(
        self, package: str
    ) -> tuple[list[CategoryDownloads] | None, list[CategoryDownloads] | None]:
        """Get cached environment breakdown for a package.

        Args:
            package: Package name.

        Returns:
            Tuple of (python_versions, os_stats), either may be None.
        """
        with get_db(self.db_path) as conn:
            return (
                get_cached_python_versions(conn, package),
                get_cached_os_stats(conn, package),
            )

    def get_daily_downloads(
        self,
        package: str,
        dimension: str = "overall",
        category: str | None = None,
        since: str | None = None,
    ) -> list[DailyDownload]:
        """Get the stored daily download time series for a package.

        Args:
            package: Package name.
            dimension: One of ``"overall"``, ``"python"``, or ``"os"``.
            category: Restrict to a single category (e.g. ``"without_mirrors"``).
            since: Only include rows on or after this ``YYYY-MM-DD`` date.

        Returns:
            List of daily records ordered by date (possibly empty).
        """
        with get_db(self.db_path) as conn:
            return get_daily_downloads(
                conn,
                package,
                dimension=dimension,
                category=category,
                since=since,
                tracked_only=True,
            )

    def get_daily_totals(
        self, package: str, since: str | None = None
    ) -> list[dict[str, Any]]:
        """Get the true per-day download series for a package.

        Returns the ``overall`` / ``without_mirrors`` daily counts (the same
        figure PyPI reports as "downloads", excluding mirror traffic) as a list
        of ``{"date", "downloads"}`` dicts ordered by date. Unlike the snapshot
        history, this is populated from a single fetch (~180 days backfilled),
        so it renders a real trend even before repeated fetches accumulate.

        Args:
            package: Package name.
            since: Only include rows on or after this ``YYYY-MM-DD`` date.

        Returns:
            List of ``{"date", "downloads"}`` dicts (possibly empty).
        """
        rows = self.get_daily_downloads(
            package, dimension="overall", category="without_mirrors", since=since
        )
        return [{"date": r["date"], "downloads": r["downloads"]} for r in rows]

    def get_period_comparison(self, package: str, days: int) -> tuple[int, int] | None:
        """Compare two adjacent ``days``-long windows of true daily downloads.

        Returns ``(current, previous)`` download totals for the most recent
        window versus the one before it, or None when no daily data exists for
        the package. Both windows are derived from a single fetch, so this gives
        exact period-over-period figures without waiting for repeat fetches.

        Args:
            package: Package name.
            days: Window width in calendar days (e.g. 7 for week, 30 for month).
        """
        series = [
            (row["date"], row["downloads"]) for row in self.get_daily_totals(package)
        ]
        return daily_window_sums(series, days)

    def run_checks(
        self,
        milestones: list[int] | None = None,
        baseline_weeks: int = DEFAULT_BASELINE_WEEKS,
        z_threshold: float = DEFAULT_Z_THRESHOLD,
        min_weekly: float = DEFAULT_MIN_WEEKLY,
    ) -> list[CheckEvent]:
        """Scan tracked packages for download anomalies and milestone crossings.

        For each package this flags a weekly spike/drop against its trailing
        baseline (from the daily series) and any configured download milestone
        newly reached. Returns the events ordered by package name.

        Milestones are measured against *observed downloads*: the sum of the
        locally stored daily series, which keeps accumulating past the roughly
        180-day window pypistats serves. That is deliberately not the snapshot
        ``total`` column, which is itself a rolling window and so can fall back
        below a threshold as old days age out. Each package also carries a
        high-water mark, so a metric that dips (through pruning, or through the
        snapshot fallback's window rolling over) and later recovers cannot
        announce the same milestone a second time.

        Args:
            milestones: Download thresholds to watch (empty/None to skip).
            baseline_weeks: Number of prior weeks used as the anomaly baseline.
            z_threshold: Standard deviations from baseline to flag an anomaly.
            min_weekly: Skip packages averaging fewer weekly downloads than this.
        """
        milestones = milestones or []
        events: list[CheckEvent] = []

        with get_db(self.db_path) as conn:
            for package in get_packages(conn):
                daily = get_daily_downloads(
                    conn, package, dimension="overall", category="without_mirrors"
                )
                series = [(r["date"], r["downloads"]) for r in daily]

                anomaly = detect_anomaly(
                    series,
                    baseline_weeks=baseline_weeks,
                    z_threshold=z_threshold,
                    min_weekly=min_weekly,
                )
                if anomaly is not None:
                    anomaly["package"] = package
                    events.append(anomaly)

                if milestones:
                    current, previous = self._observed_downloads(conn, package, series)
                    # Before the first check there is no mark to compare
                    # against, so fall back to the prior observation and report
                    # the crossing this run actually witnessed.
                    baseline = get_milestone_high_water(conn, package)
                    if baseline is None:
                        baseline = previous

                    if current is not None:
                        for m in detect_milestones(baseline, current, milestones):
                            events.append(
                                {
                                    "package": package,
                                    "kind": "milestone",
                                    "milestone": m,
                                    "total": current,
                                    "message": (
                                        f"crossed {m:,} observed downloads "
                                        f"(now {current:,})"
                                    ),
                                }
                            )
                        set_milestone_high_water(conn, package, current)

        events.sort(key=lambda e: (e["package"], e["kind"]))
        return events

    @staticmethod
    def _observed_downloads(
        conn: sqlite3.Connection,
        package: str,
        series: list[tuple[str, int]],
    ) -> tuple[int | None, int | None]:
        """Return ``(current, previous)`` observed download totals for a package.

        "Observed" means every download pkgdb has stored locally, so the figure
        accumulates across fetches instead of rolling like the pypistats window.
        It is the running sum of the daily series; ``previous`` is the same sum
        one day earlier, which gives the first-ever check something to compare
        against.

        Databases predating the daily series have no such sum, so those fall
        back to the two most recent snapshot totals as before.

        Returns ``(None, None)`` when the package has no usable data at all.
        """
        if series:
            total = sum(downloads for _, downloads in series)
            last_day = max(series, key=lambda point: point[0])[1]
            return total, total - last_day

        rows = conn.execute(
            "SELECT total FROM package_stats WHERE package_name = ? "
            "ORDER BY fetch_date DESC LIMIT 2",
            (package,),
        ).fetchall()
        if not rows:
            return None, None
        current = rows[0]["total"]
        previous = rows[1]["total"] if len(rows) >= 2 else None
        return current, previous

    # -------------------------------------------------------------------------
    # Reporting
    # -------------------------------------------------------------------------

    def generate_report(
        self,
        output_file: str,
        include_env: bool = False,
        include_github: bool = False,
        include_ci: bool = False,
    ) -> bool:
        """Generate HTML report for all packages.

        Args:
            output_file: Path to write HTML file.
            include_env: If True, include Python/OS distribution summary.
            include_github: If True, include GitHub stats (stars, forks, etc.)
                from cache. Packages without cached data are skipped.
            include_ci: If True, scan registered repositories and include a CI
                status section. Run listings are cached for an hour, so a repeat
                report costs no requests.

        Returns:
            True if report was generated, False if no data available.

        Raises:
            ValueError: If output path is invalid or not writable.
        """
        # Validate output path
        is_valid, error_msg = validate_output_path(
            output_file, allowed_extensions=[".html", ".htm"]
        )
        if not is_valid:
            raise ValueError(error_msg)

        with get_db(self.db_path) as conn:
            stats = get_stats_with_growth(conn, tracked_only=True)
            if not stats:
                return False

            all_history = get_all_history(conn, limit_per_package=30, tracked_only=True)
            packages = [s["package_name"] for s in stats]

            env_summary = get_cached_env_summary(conn) if include_env else None
            if include_env and env_summary is None:
                env_summary = aggregate_env_stats(packages)

            github_stats = None
            if include_github:
                from .github import fetch_package_github_stats

                github_stats = {}
                for pkg in packages:
                    result = fetch_package_github_stats(pkg, conn=conn, use_cache=True)
                    if result.success and result.stats is not None:
                        github_stats[pkg] = result.stats

        ci_rows = None
        if include_ci:
            ci_rows = self.get_ci_rows(self.fetch_ci_status())

        generate_html_report(
            stats,
            output_file,
            all_history,
            packages,
            env_summary,
            github_stats,
            ci_rows,
        )
        return True

    def generate_package_report(self, package: str, output_file: str) -> bool:
        """Generate detailed HTML report for a single package.

        Args:
            package: Package name.
            output_file: Path to write HTML file.

        Returns:
            True if report was generated.

        Raises:
            ValueError: If output path is invalid or not writable.
        """
        # Validate output path
        is_valid, error_msg = validate_output_path(
            output_file, allowed_extensions=[".html", ".htm"]
        )
        if not is_valid:
            raise ValueError(error_msg)

        with get_db(self.db_path) as conn:
            history = get_package_history(conn, package, limit=30, tracked_only=True)
            py_versions = get_cached_python_versions(conn, package)
            os_data = get_cached_os_stats(conn, package)

        # Find stats in history or fetch fresh
        pkg_stats: PackageStats | None = None
        for h in history:
            if h["package_name"] == package:
                pkg_stats = {
                    "total": h["total"] or 0,
                    "last_month": h["last_month"] or 0,
                    "last_week": h["last_week"] or 0,
                    "last_day": h["last_day"] or 0,
                }
                break

        return generate_package_html_report(
            package,
            output_file,
            stats=pkg_stats,
            history=history,
            python_versions=py_versions,
            os_stats=os_data,
        )

    def fetch_package_releases(
        self, package: str
    ) -> tuple[list[PyPIRelease], list[GitHubRelease]]:
        """Fetch PyPI and GitHub releases for a package.

        Uses cached data when available (24h TTL).

        Args:
            package: Package name.

        Returns:
            Tuple of (pypi_releases, github_releases).
        """
        import logging

        logger = logging.getLogger("pkgdb")

        with get_db(self.db_path) as conn:
            # PyPI releases
            pypi = get_pypi_releases(conn, package)
            if pypi is None:
                fetched = fetch_pypi_releases(package)
                if fetched:
                    store_pypi_releases(conn, package, fetched)
                    pypi = fetched
                else:
                    pypi = get_all_pypi_releases(conn, package)

            # GitHub releases
            gh: list[GitHubRelease] = []
            github_url = extract_github_url(package)
            if github_url:
                parsed = parse_github_url(github_url)
                if parsed:
                    owner, repo = parsed
                    repo_key = f"{owner}/{repo}".lower()
                    cached_gh = get_github_releases(conn, repo_key)
                    if cached_gh is not None:
                        gh = cached_gh
                    else:
                        fetched_gh = fetch_github_releases(owner, repo)
                        if fetched_gh is not None:
                            gh_typed: list[GitHubRelease] = [
                                GitHubRelease(
                                    tag_name=r["tag_name"],
                                    published_at=r["published_at"],
                                    name=r.get("name"),
                                )
                                for r in fetched_gh
                            ]
                            store_github_releases(conn, repo_key, gh_typed)
                            gh = gh_typed
                        else:
                            gh = get_all_github_releases(conn, repo_key)
            else:
                logger.debug("No GitHub repository found for %s.", package)

        return pypi, gh

    def generate_project_report(
        self,
        package: str,
        output_file: str,
        since: str | None = None,
        limit: int = 90,
    ) -> bool:
        """Generate a project view HTML report for a single package.

        Shows download history with release markers, release timeline,
        and environment distribution.

        Args:
            package: Package name.
            output_file: Path to write HTML file.
            since: Restrict both the daily series and the snapshot history to
                dates on or after this ``YYYY-MM-DD`` date. None charts
                everything stored.
            limit: Maximum number of snapshots in the fallback history table.

        Returns:
            True if report was generated.

        Raises:
            ValueError: If output path is invalid.
        """
        is_valid, error_msg = validate_output_path(
            output_file, allowed_extensions=[".html", ".htm"]
        )
        if not is_valid:
            raise ValueError(error_msg)

        with get_db(self.db_path) as conn:
            history = get_package_history(
                conn, package, limit=limit, since=since, tracked_only=True
            )
            daily_series = get_daily_downloads(
                conn,
                package,
                dimension="overall",
                category="without_mirrors",
                since=since,
                tracked_only=True,
            )
            py_versions = get_cached_python_versions(conn, package)
            os_data = get_cached_os_stats(conn, package)

        pkg_stats: PackageStats | None = None
        for h in history:
            if h["package_name"] == package:
                pkg_stats = {
                    "total": h["total"] or 0,
                    "last_month": h["last_month"] or 0,
                    "last_week": h["last_week"] or 0,
                    "last_day": h["last_day"] or 0,
                }
                break

        pypi_releases, github_releases = self.fetch_package_releases(package)

        return generate_project_html_report(
            package,
            output_file,
            stats=pkg_stats,
            history=history,
            daily_series=daily_series,
            pypi_releases=pypi_releases,
            github_releases=github_releases,
            python_versions=py_versions,
            os_stats=os_data,
        )

    # -------------------------------------------------------------------------
    # Export
    # -------------------------------------------------------------------------

    def export(self, format: str, output_file: str | None = None) -> str | None:
        """Export stats in the specified format.

        Args:
            format: One of 'csv', 'json', 'markdown', 'md'.
            output_file: Optional path to write output. If None, returns string.

        Returns:
            Exported string, or None if no data available.

        Raises:
            ValueError: If format is unknown or output path is invalid.
        """
        # Validate output path if specified
        if output_file:
            ext_map = {
                "csv": [".csv"],
                "json": [".json"],
                "markdown": [".md", ".markdown", ".txt"],
                "md": [".md", ".markdown", ".txt"],
            }
            allowed_ext = ext_map.get(format, [])
            is_valid, error_msg = validate_output_path(
                output_file, allowed_extensions=allowed_ext if allowed_ext else None
            )
            if not is_valid:
                raise ValueError(error_msg)

        stats = self.get_stats()
        if not stats:
            return None

        if format == "csv":
            return export_csv(stats)
        elif format == "json":
            return export_json(stats)
        elif format in ("markdown", "md"):
            return export_markdown(stats)
        else:
            raise ValueError(f"Unknown format: {format}")

    def generate_badge(
        self,
        package: str,
        period: str = "total",
        color: str | None = None,
    ) -> str | None:
        """Generate an SVG badge for a package's download count.

        Args:
            package: Package name.
            period: One of "total", "month", "week", "day".
            color: Badge color (default: auto-select based on count).

        Returns:
            SVG string for the badge, or None if no stats available.
        """
        stats = self.get_stats()
        if not stats:
            return None

        # Find the package
        pkg_stats = None
        for s in stats:
            if s["package_name"] == package:
                pkg_stats = s
                break

        if pkg_stats is None:
            return None

        # Get the appropriate count
        count_map = {
            "total": pkg_stats.get("total") or 0,
            "month": pkg_stats.get("last_month") or 0,
            "week": pkg_stats.get("last_week") or 0,
            "day": pkg_stats.get("last_day") or 0,
        }
        count = count_map.get(period, count_map["total"])

        return generate_downloads_badge(count, period=period, color=color)

    # -------------------------------------------------------------------------
    # GitHub Stats
    # -------------------------------------------------------------------------

    def fetch_github_stats(
        self,
        packages: list[str] | None = None,
        use_cache: bool = True,
    ) -> list[RepoResult]:
        """Fetch GitHub repository stats for tracked packages.

        Args:
            packages: Specific packages to fetch. If None, fetches all tracked.
            use_cache: Whether to use cached GitHub API responses (24h TTL).

        Returns:
            List of RepoResult with stats or error for each package.
        """
        if packages is None:
            pkg_list = [p.name for p in self.list_packages()]
        else:
            pkg_list = packages

        results: list[RepoResult] = []
        with get_db(self.db_path) as conn:
            for pkg in pkg_list:
                result = fetch_package_github_stats(pkg, conn=conn, use_cache=use_cache)
                results.append(result)
                # Record a daily snapshot so star/fork history accumulates.
                if result.stats is not None:
                    s = result.stats
                    repo_key = f"{s.owner}/{s.name}".lower()
                    # Keep the scan registry current for free: a package that
                    # resolves to a repo here never needs discovering.
                    add_repo(
                        conn,
                        repo_key,
                        source=REPO_SOURCE_PACKAGE,
                        default_branch=s.default_branch,
                        commit=False,
                    )
                    link_package_repo(conn, pkg, repo_key, commit=False)
                    store_github_stats_snapshot(
                        conn,
                        pkg,
                        repo_key,
                        s.stars,
                        s.forks,
                        s.open_issues,
                        s.watchers,
                        open_issues_excl_prs=s.open_issues_excl_prs,
                        commit=False,
                    )
            conn.commit()

        return results

    def get_github_history(
        self, package: str, since: str | None = None
    ) -> list[dict[str, Any]]:
        """Get a package's recorded GitHub metric snapshots (date-ordered).

        Args:
            package: Package name.
            since: Only include rows on or after this ``YYYY-MM-DD`` date.
        """
        with get_db(self.db_path) as conn:
            return get_github_stats_history(conn, package, since=since)

    def get_star_growth(self, package: str, days: int = 30) -> int | None:
        """Return the change in stars over roughly the last ``days`` days.

        GitHub exposes no star history, so this compares the latest snapshot to
        the newest snapshot at least ``days`` old (or the oldest recorded, if the
        series is younger than that). Returns None until at least two snapshots
        exist -- growth only becomes visible as history accumulates.
        """
        history = self.get_github_history(package)
        if len(history) < 2:
            return None

        latest = history[-1]
        cutoff = (
            datetime.strptime(latest["date"], "%Y-%m-%d") - timedelta(days=days)
        ).strftime("%Y-%m-%d")
        older = [h for h in history[:-1] if h["date"] <= cutoff]
        baseline = older[-1] if older else history[0]
        return int(latest["stars"]) - int(baseline["stars"])

    def clear_github_cache(self, expired_only: bool = True) -> int:
        """Clear GitHub API cache.

        Args:
            expired_only: If True, only clear expired entries.

        Returns:
            Number of entries cleared.
        """
        with get_db(self.db_path) as conn:
            return clear_github_cache(conn, expired_only=expired_only)

    def get_github_cache_stats(self) -> dict[str, int]:
        """Get GitHub cache statistics.

        Returns:
            Dict with 'total', 'valid', and 'expired' counts.
        """
        with get_db(self.db_path) as conn:
            return get_github_cache_stats(conn)

    # -------------------------------------------------------------------------
    # Repository registry
    # -------------------------------------------------------------------------

    def list_repos(
        self, enabled_only: bool = True, with_workflows_only: bool = False
    ) -> list[dict[str, Any]]:
        """List registered repositories, with their linked packages."""
        with get_db(self.db_path) as conn:
            repos = get_repos(
                conn,
                enabled_only=enabled_only,
                with_workflows_only=with_workflows_only,
            )
            packages = get_repo_packages(conn)
        for repo in repos:
            repo["packages"] = packages.get(repo["repo_key"], [])
        return repos

    def add_repo(self, repo_key: str) -> bool:
        """Register a repository by ``owner/name`` (or its GitHub URL)."""
        with get_db(self.db_path) as conn:
            return add_repo(conn, repo_key, source=REPO_SOURCE_MANUAL)

    def remove_repo(self, repo_key: str) -> bool:
        """Unregister a repository and drop its CI rows."""
        with get_db(self.db_path) as conn:
            return remove_repo(conn, repo_key)

    def set_repo_enabled(self, repo_key: str, enabled: bool) -> bool:
        """Include or exclude a repository from scans without forgetting it."""
        with get_db(self.db_path) as conn:
            return set_repo_enabled(conn, repo_key, enabled)

    def seed_repos(self) -> int:
        """Fill an empty registry from GitHub data already stored locally."""
        with get_db(self.db_path) as conn:
            return seed_repos_from_cache(conn)

    def discover_repos(
        self,
        user: str,
        include_forks: bool = False,
        include_archived: bool = False,
        probe_workflows: bool = True,
    ) -> dict[str, Any]:
        """Register a user's repositories, and link them to tracked packages.

        Probing asks each new repository how many workflows it defines, which
        is one request per repository but only on the run that first sees it --
        the answer is stored. Pass ``probe_workflows=False`` to register
        everything unprobed and let the scan find out.

        Returns counts under ``found``, ``added``, ``with_workflows``, ``linked``
        and the ``repos`` listing, or an ``error`` key when the listing failed.
        """
        listing = fetch_user_repos(
            user,
            include_forks=include_forks,
            include_archived=include_archived,
        )
        if listing is None:
            return {"error": f"Could not list repositories for '{user}'"}

        known: dict[str, dict[str, Any]] = {}
        with get_db(self.db_path) as conn:
            for row in get_repos(conn, enabled_only=False):
                known[row["repo_key"]] = row

        # Probe only repositories whose workflow count is not already known.
        to_probe = [
            r["full_name"]
            for r in listing
            if probe_workflows
            and known.get(normalize_repo_key(r["full_name"]), {}).get("has_workflows")
            is None
        ]
        counts: dict[str, int | None] = {}
        if to_probe:
            with ThreadPoolExecutor(max_workers=_CI_MAX_WORKERS) as executor:
                counts = dict(
                    zip(to_probe, executor.map(self._probe_workflows, to_probe))
                )

        added = 0
        with_workflows = 0
        with get_db(self.db_path) as conn:
            for repo in listing:
                key = normalize_repo_key(repo["full_name"])
                count = counts.get(repo["full_name"])
                if count is None:
                    count = known.get(key, {}).get("has_workflows")
                if add_repo(
                    conn,
                    key,
                    source=REPO_SOURCE_DISCOVER,
                    has_workflows=count,
                    default_branch=repo.get("default_branch"),
                    commit=False,
                ):
                    added += 1
                if count:
                    with_workflows += 1
            conn.commit()
            linked = self._link_packages_by_name(conn)

        return {
            "found": len(listing),
            "added": added,
            "with_workflows": with_workflows,
            "linked": linked,
            "repos": [r["full_name"] for r in listing],
        }

    @staticmethod
    def _probe_workflows(full_name: str) -> int | None:
        owner, _, name = full_name.partition("/")
        if not name:
            return None
        return fetch_workflow_count(owner, name)

    @staticmethod
    def _link_packages_by_name(conn: sqlite3.Connection) -> int:
        """Link tracked packages to same-named repositories.

        The package-to-repo lookup goes through PyPI metadata, so a package that
        is unpublished, or published without a repository URL, resolves to
        nothing. Matching on the normalized name recovers those, which on a
        typical account is most of the gap.
        """

        def canonical(name: str) -> str:
            return name.lower().replace("_", "-").replace(".", "-")

        by_name = {
            canonical(row["repo_key"].split("/", 1)[-1]): row["repo_key"]
            for row in get_repos(conn, enabled_only=False)
        }
        linked_keys = {
            (row["package_name"], row["repo_key"])
            for row in conn.execute("SELECT package_name, repo_key FROM package_repos")
        }
        linked = 0
        for row in conn.execute("SELECT package_name FROM packages"):
            package = row["package_name"]
            repo_key = by_name.get(canonical(package))
            if repo_key and (package, repo_key) not in linked_keys:
                link_package_repo(conn, package, repo_key, commit=False)
                linked += 1
        conn.commit()
        return linked

    # -------------------------------------------------------------------------
    # CI status
    # -------------------------------------------------------------------------

    def fetch_ci_status(
        self,
        repos: list[str] | None = None,
        branch: str | None = None,
        use_cache: bool = True,
        limit: int = CI_RUN_WINDOW,
        ignore_workflows: list[str] | None = None,
    ) -> list[CIResult]:
        """Scan repositories for the latest run of each of their workflows.

        Repositories come from the registry unless ``repos`` names them. With
        ``branch`` unset each repository is scanned on its own default branch,
        so a red run on someone's feature branch is not reported as broken CI.

        Cache reads and all writes happen on this thread and only the network
        calls are parallel, because a SQLite connection cannot cross threads.
        """
        ignore = ignore_workflows or []

        with get_db(self.db_path) as conn:
            if repos is None:
                registry = get_repos(conn, enabled_only=True, with_workflows_only=True)
                if not registry:
                    seed_repos_from_cache(conn)
                    registry = get_repos(
                        conn, enabled_only=True, with_workflows_only=True
                    )
                targets = [
                    (r["repo_key"], branch or r["default_branch"]) for r in registry
                ]
            else:
                known = {
                    r["repo_key"]: r["default_branch"]
                    for r in get_repos(conn, enabled_only=False)
                }
                targets = []
                for repo_key in repos:
                    key = normalize_repo_key(repo_key)
                    targets.append((key, branch or known.get(key)))

            package_map = get_repo_packages(conn)

            # Phase 1: serve what the cache already holds.
            pending: list[tuple[str, str | None]] = []
            raw_runs: dict[str, list[dict[str, Any]] | None] = {}
            for repo_key, repo_branch in targets:
                owner, _, name = repo_key.partition("/")
                cached = (
                    get_cached_workflow_runs(conn, owner, name, repo_branch)
                    if use_cache and name
                    else None
                )
                if cached is not None:
                    raw_runs[repo_key] = cached
                else:
                    pending.append((repo_key, repo_branch))

            # Phase 2: fetch the misses in parallel, network only.
            fetched: list[tuple[str, str | None, list[dict[str, Any]] | None]] = []
            if pending:
                with ThreadPoolExecutor(max_workers=_CI_MAX_WORKERS) as executor:
                    fetched = list(
                        executor.map(
                            lambda t: self._scan_repo(t[0], t[1], limit),
                            pending,
                        )
                    )

            # Phase 3: persist cache entries, branches and CI state.
            branches = dict(targets)
            for repo_key, resolved_branch, raw in fetched:
                branches[repo_key] = resolved_branch
                raw_runs[repo_key] = raw
                owner, _, name = repo_key.partition("/")
                if resolved_branch:
                    add_repo(
                        conn,
                        repo_key,
                        source=REPO_SOURCE_PACKAGE,
                        default_branch=resolved_branch,
                        commit=False,
                    )
                if use_cache and raw is not None and name:
                    store_cached_workflow_runs(conn, owner, name, resolved_branch, raw)

            results: list[CIResult] = []
            for repo_key, _ in targets:
                raw = raw_runs.get(repo_key)
                repo_branch = branches.get(repo_key)
                packages = package_map.get(repo_key, [])
                if raw is None:
                    results.append(
                        CIResult(
                            repo_key=repo_key,
                            entries=[],
                            packages=packages,
                            branch=repo_branch,
                            error="Could not fetch workflow runs",
                        )
                    )
                    continue

                entries: list[CIEntry] = []
                for run in latest_run_per_workflow(parse_workflow_runs(raw)):
                    if any(
                        fnmatch.fnmatch(run.workflow_name, pattern)
                        for pattern in ignore
                    ):
                        continue
                    started = run.created_at.isoformat() if run.created_at else None
                    first_failed = store_ci_status(
                        conn,
                        repo_key,
                        run.workflow_name,
                        run.state,
                        branch=run.branch,
                        run_id=run.run_id,
                        run_url=run.url,
                        run_started_at=started,
                        commit=False,
                    )
                    entries.append(
                        CIEntry(
                            repo_key=repo_key,
                            workflow_name=run.workflow_name,
                            state=run.state,
                            branch=run.branch,
                            run_id=run.run_id,
                            run_url=run.url,
                            run_started_at=started,
                            first_failed_at=first_failed,
                        )
                    )

                if not entries:
                    # Recorded, not just displayed: a repository with workflows
                    # but no runs on this branch is a scan result, and leaving
                    # it out would make the report count fewer repositories
                    # than the scan reports.
                    store_ci_status(
                        conn,
                        repo_key,
                        CI_NO_RUNS_WORKFLOW,
                        CI_STATE_NO_RUNS,
                        branch=repo_branch,
                        commit=False,
                    )
                    entries = [
                        CIEntry(
                            repo_key=repo_key,
                            workflow_name=CI_NO_RUNS_WORKFLOW,
                            state=CI_STATE_NO_RUNS,
                            branch=repo_branch,
                        )
                    ]

                # Forget workflows this scan did not see: renamed and deleted
                # ones would otherwise keep reporting their last failure.
                prune_ci_status(
                    conn,
                    repo_key,
                    [e.workflow_name for e in entries],
                    commit=False,
                )
                results.append(
                    CIResult(
                        repo_key=repo_key,
                        entries=entries,
                        packages=packages,
                        branch=repo_branch,
                    )
                )
            conn.commit()

        return results

    @staticmethod
    def _scan_repo(
        repo_key: str, branch: str | None, limit: int
    ) -> tuple[str, str | None, list[dict[str, Any]] | None]:
        """Network half of a repository scan. Runs off the main thread."""
        owner, _, name = repo_key.partition("/")
        if not name:
            return repo_key, branch, None
        if branch is None:
            branch = fetch_default_branch(owner, name)
        return (
            repo_key,
            branch,
            fetch_workflow_runs_raw(owner, name, branch=branch, limit=limit),
        )

    def get_ci_rows(
        self, results: list[CIResult] | None = None
    ) -> list[dict[str, Any]]:
        """Flatten CI state into report rows, failures first.

        Reads the last scan from the database when ``results`` is not given, so
        a report can render CI without touching the network.
        """
        if results is not None:
            rows = [
                {
                    "repo_key": result.repo_key,
                    "workflow_name": entry.workflow_name,
                    "state": entry.state,
                    "branch": entry.branch or result.branch,
                    "run_url": entry.run_url,
                    "run_started_at": entry.run_started_at,
                    "first_failed_at": entry.first_failed_at,
                    "failing_days": entry.failing_days,
                    "packages": result.packages,
                }
                for result in results
                for entry in result.entries
            ]
        else:
            with get_db(self.db_path) as conn:
                stored = get_ci_status(conn)
                package_map = get_repo_packages(conn)
            rows = []
            for row in stored:
                entry = CIEntry(
                    repo_key=row["repo_key"],
                    workflow_name=row["workflow_name"],
                    state=row["state"],
                    branch=row["branch"],
                    run_url=row["run_url"],
                    run_started_at=row["run_started_at"],
                    first_failed_at=row["first_failed_at"],
                )
                rows.append(
                    {
                        "repo_key": entry.repo_key,
                        "workflow_name": entry.workflow_name,
                        "state": entry.state,
                        "branch": entry.branch,
                        "run_url": entry.run_url,
                        "run_started_at": entry.run_started_at,
                        "first_failed_at": entry.first_failed_at,
                        "failing_days": entry.failing_days,
                        "packages": package_map.get(entry.repo_key, []),
                    }
                )

        def order(row: dict[str, Any]) -> tuple[bool, int, str, str]:
            days = row["failing_days"]
            return (
                row["state"] != CI_STATE_FAIL,
                -int(days) if days is not None else 0,
                str(row["repo_key"]),
                str(row["workflow_name"]),
            )

        rows.sort(key=order)
        return rows

    def generate_ci_report(
        self,
        output_file: str,
        scan: bool = True,
        show_all: bool = False,
        branch: str | None = None,
        use_cache: bool = True,
    ) -> bool:
        """Write a standalone CI status report.

        Scans first unless ``scan`` is False, in which case the last recorded
        state is rendered without any request.

        Raises:
            ValueError: If the output path is invalid or not writable.
        """
        is_valid, error_msg = validate_output_path(
            output_file, allowed_extensions=[".html", ".htm"]
        )
        if not is_valid:
            raise ValueError(error_msg)

        results = (
            self.fetch_ci_status(branch=branch, use_cache=use_cache) if scan else None
        )
        generate_ci_html_report(
            self.get_ci_rows(results), output_file, show_all=show_all
        )
        return True

    def get_ci_status(self, repo_key: str | None = None) -> list[dict[str, Any]]:
        """Return the CI state recorded by the last scan, without fetching."""
        with get_db(self.db_path) as conn:
            return get_ci_status(conn, repo_key)

    # -------------------------------------------------------------------------
    # Maintenance
    # -------------------------------------------------------------------------

    def cleanup(self) -> tuple[dict[str, int], int]:
        """Clean up orphaned stats and return counts.

        Removes every stored row belonging to packages that are no longer
        tracked. This is the physical purge that `remove_package` defers.

        Returns:
            Tuple of (rows deleted per table including a ``total`` entry,
            packages_remaining).
        """
        with get_db(self.db_path) as conn:
            orphaned = cleanup_orphaned_stats(conn)
            packages = get_packages(conn)
            return orphaned, len(packages)

    def prune(self, days: int = 365) -> dict[str, int]:
        """Remove stats older than the specified number of days.

        Args:
            days: Delete stats older than this many days.

        Returns:
            Rows deleted per table, plus a ``total`` entry summing them.
        """
        with get_db(self.db_path) as conn:
            return prune_old_stats(conn, days)

    def get_database_info(self) -> DatabaseInfo:
        """Get database statistics and metadata.

        Returns:
            DatabaseInfo with package count, record count, date range, and file size.
        """
        with get_db(self.db_path) as conn:
            stats = get_database_stats(conn)

        # Get file size
        db_path = Path(self.db_path)
        db_size = db_path.stat().st_size if db_path.exists() else 0

        return DatabaseInfo(
            package_count=stats["package_count"],
            record_count=stats["record_count"],
            snapshot_records=stats["snapshot_records"],
            daily_records=stats["daily_records"],
            github_history_records=stats["github_history_records"],
            first_fetch=stats["first_fetch"],
            last_fetch=stats["last_fetch"],
            db_size_bytes=db_size,
        )
