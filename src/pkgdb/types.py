"""Type definitions for pkgdb using TypedDict for known structures."""

from typing import TypedDict


class PackageStats(TypedDict):
    """Download statistics for a package."""

    last_day: int
    last_week: int
    last_month: int
    total: int


class CategoryDownloads(TypedDict):
    """Downloads breakdown by category (Python version or OS)."""

    category: str
    downloads: int


class DailyDownload(TypedDict):
    """A single day's download count for one category of one dimension.

    ``dimension`` is one of ``"overall"`` (categories ``with_mirrors`` /
    ``without_mirrors``), ``"python"`` (categories like ``"3.12"``), or
    ``"os"`` (categories like ``"Linux"``). ``date`` is ``YYYY-MM-DD``.
    """

    date: str
    dimension: str
    category: str
    downloads: int


class EnvSummary(TypedDict):
    """Aggregated environment statistics."""

    python_versions: list[tuple[str, int]]
    os_distribution: list[tuple[str, int]]


class HistoryRecord(TypedDict):
    """Historical stats record from database."""

    id: int
    package_name: str
    fetch_date: str
    last_day: int | None
    last_week: int | None
    last_month: int | None
    total: int | None


class StatsWithGrowth(TypedDict, total=False):
    """Stats record with optional growth metrics."""

    id: int
    package_name: str
    fetch_date: str
    last_day: int | None
    last_week: int | None
    last_month: int | None
    total: int | None
    week_growth: float | None
    month_growth: float | None


class DatabaseInfo(TypedDict):
    """Database statistics and metadata."""

    package_count: int
    record_count: int
    snapshot_records: int
    daily_records: int
    github_history_records: int
    first_fetch: str | None
    last_fetch: str | None
    db_size_bytes: int


class PyPIRelease(TypedDict):
    """A PyPI package release."""

    version: str
    upload_date: str


class GitHubRelease(TypedDict):
    """A GitHub repository release."""

    tag_name: str
    published_at: str
    name: str | None


class CheckEvent(TypedDict, total=False):
    """A noteworthy event surfaced by ``pkgdb check``.

    ``package``, ``kind`` and ``message`` are always present. ``kind`` is one of
    ``"spike"``, ``"drop"`` or ``"milestone"``. The remaining fields carry the
    supporting numbers for whichever kind of event this is.
    """

    package: str
    kind: str
    message: str
    # Anomaly fields (kind: spike/drop)
    value: int
    baseline: float
    change_pct: float
    z_score: float
    period: str
    # Milestone field (kind: milestone)
    milestone: int
    total: int


# Normalized CI states. Derived by the GitHub client from a run's status and
# conclusion, stored verbatim by the database, so both modules share them.
CI_STATE_PASS = "PASS"
CI_STATE_FAIL = "FAIL"
CI_STATE_RUNNING = "RUNNING"
CI_STATE_UNKNOWN = "UNKNOWN"
# No workflow run exists for the repository (or the scanned branch).
CI_STATE_NO_RUNS = "NO_RUNS"


class RepoRecord(TypedDict):
    """A repository in the scan registry.

    ``source`` is ``"package"``, ``"discover"`` or ``"manual"``.
    ``has_workflows`` is None when it has not been probed.
    """

    repo_key: str
    source: str
    has_workflows: int | None
    enabled: int
    default_branch: str | None
    added_date: str


class CIRun(TypedDict):
    """The latest known state of one workflow in one repository.

    ``first_failed_at`` is the start of the run that began the current failing
    streak, and is None whenever the workflow is not failing.
    """

    repo_key: str
    workflow_name: str
    state: str
    branch: str | None
    run_id: int | None
    run_url: str | None
    run_started_at: str | None
    first_failed_at: str | None
