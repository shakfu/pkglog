"""Database operations for pkgdb."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Generator

from .types import (
    CI_STATE_FAIL,
    CI_STATE_PASS,
    CategoryDownloads,
    DailyDownload,
    EnvSummary,
    GitHubRelease,
    PackageStats,
    PyPIRelease,
)
from .utils import calculate_growth, daily_window_sums, utcnow


def get_config_dir() -> Path:
    """Get the pkgdb config directory (~/.pkgdb), creating it if needed."""
    config_dir = Path.home() / ".pkgdb"
    config_dir.mkdir(exist_ok=True)
    return config_dir


DEFAULT_DB_FILE = str(get_config_dir() / "pkg.db")
DEFAULT_REPORT_FILE = str(get_config_dir() / "report.html")

# Membership predicate used by the `tracked_only` read options. Package-owned
# data outlives `remove_package()` so that `cleanup` stays the single physical
# purge, which means tracked-package views have to filter on this explicitly.
_TRACKED_SCOPE = "package_name IN (SELECT package_name FROM packages)"


def get_db_connection(db_path: str) -> sqlite3.Connection:
    """Create and return a database connection."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_db(db_path: str) -> Generator[sqlite3.Connection, None, None]:
    """Context manager for database connections with automatic init and cleanup."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        init_db(conn)
        yield conn
    finally:
        conn.close()


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, decl: str
) -> None:
    """Add a column to an existing table if it is not already present.

    SQLite has no ``ADD COLUMN IF NOT EXISTS``, and ``init_db()`` runs on every
    connection, so the column list is checked first. Only additive migrations
    belong here: the new column must be nullable or carry a default.
    """
    # Positional access: init_db may run on a connection with no row factory.
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db(conn: sqlite3.Connection) -> None:
    """Initialize the database schema."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS package_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            package_name TEXT NOT NULL,
            fetch_date TEXT NOT NULL,
            last_day INTEGER,
            last_week INTEGER,
            last_month INTEGER,
            total INTEGER,
            UNIQUE(package_name, fetch_date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS packages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            package_name TEXT NOT NULL UNIQUE,
            added_date TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fetch_attempts (
            package_name TEXT PRIMARY KEY,
            attempt_time TEXT NOT NULL,
            success INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS python_version_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            package_name TEXT NOT NULL,
            fetch_date TEXT NOT NULL,
            category TEXT NOT NULL,
            downloads INTEGER NOT NULL,
            UNIQUE(package_name, fetch_date, category)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS os_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            package_name TEXT NOT NULL,
            fetch_date TEXT NOT NULL,
            category TEXT NOT NULL,
            downloads INTEGER NOT NULL,
            UNIQUE(package_name, fetch_date, category)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS github_cache (
            repo_key TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            expires_at DATETIME NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_package_name
        ON package_stats(package_name)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pyver_package_name
        ON python_version_stats(package_name)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_os_package_name
        ON os_stats(package_name)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_fetch_date
        ON package_stats(fetch_date)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pypi_releases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            package_name TEXT NOT NULL,
            version TEXT NOT NULL,
            upload_date TEXT NOT NULL,
            fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(package_name, version)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS github_releases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_key TEXT NOT NULL,
            tag_name TEXT NOT NULL,
            published_at TEXT NOT NULL,
            fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(repo_key, tag_name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS release_cache (
            cache_key TEXT PRIMARY KEY,
            fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            expires_at DATETIME NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            package_name TEXT NOT NULL,
            date TEXT NOT NULL,
            dimension TEXT NOT NULL,
            category TEXT NOT NULL,
            downloads INTEGER NOT NULL,
            UNIQUE(package_name, date, dimension, category)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_daily_downloads_lookup
        ON daily_downloads(package_name, dimension, date)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pypi_releases_package
        ON pypi_releases(package_name)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_github_releases_repo
        ON github_releases(repo_key)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS github_stats_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            package_name TEXT NOT NULL,
            repo_key TEXT NOT NULL,
            date TEXT NOT NULL,
            stars INTEGER NOT NULL,
            forks INTEGER NOT NULL,
            open_issues INTEGER NOT NULL,
            open_issues_excl_prs INTEGER,
            watchers INTEGER NOT NULL,
            UNIQUE(package_name, date)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_github_stats_history_package
        ON github_stats_history(package_name, date)
    """)
    # Databases created before the issues-only count lack the column. It is
    # nullable because the count comes from a separately rate-limited API that
    # may not answer; rows written before it existed are NULL for good.
    _add_column_if_missing(
        conn, "github_stats_history", "open_issues_excl_prs", "INTEGER"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS package_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            package_name TEXT NOT NULL,
            tag TEXT NOT NULL,
            UNIQUE(package_name, tag)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_package_tags_tag
        ON package_tags(tag)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS milestone_state (
            package_name TEXT PRIMARY KEY,
            high_water INTEGER NOT NULL,
            updated_date TEXT NOT NULL
        )
    """)
    # The repository registry. Package-derived repos cover only the projects
    # published to PyPI with a GitHub URL in their metadata, so repos reach the
    # registry from three directions: the package fetch, `repo discover`, and
    # `repo add`.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS github_repos (
            repo_key TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            has_workflows INTEGER,
            enabled INTEGER NOT NULL DEFAULT 1,
            default_branch TEXT,
            added_date TEXT NOT NULL
        )
    """)
    # Package to repo is many-to-one: several published packages can be built
    # from one repository, so the link cannot live on either table's row.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS package_repos (
            package_name TEXT NOT NULL,
            repo_key TEXT NOT NULL,
            PRIMARY KEY (package_name, repo_key)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_package_repos_repo
        ON package_repos(repo_key)
    """)
    # Latest state only. `first_failed_at` is the one thing a scan cannot
    # recompute from the API, and is what separates "broke minutes ago" from
    # "broken for a month".
    conn.execute("""
        CREATE TABLE IF NOT EXISTS github_ci_status (
            repo_key TEXT NOT NULL,
            workflow_name TEXT NOT NULL,
            state TEXT NOT NULL,
            branch TEXT,
            run_id INTEGER,
            run_url TEXT,
            run_started_at TEXT,
            first_failed_at TEXT,
            checked_at TEXT NOT NULL,
            PRIMARY KEY (repo_key, workflow_name)
        )
    """)
    conn.commit()


def add_package(conn: sqlite3.Connection, name: str) -> bool:
    """Add a package to the tracking database.

    Returns True if package was added, False if it already exists.
    """
    added_date = datetime.now().strftime("%Y-%m-%d")
    try:
        conn.execute(
            "INSERT INTO packages (package_name, added_date) VALUES (?, ?)",
            (name, added_date),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def remove_package(conn: sqlite3.Connection, name: str) -> bool:
    """Remove a package from the tracking database.

    Returns True if package was removed, False if it didn't exist.
    """
    cursor = conn.execute(
        "DELETE FROM packages WHERE package_name = ?",
        (name,),
    )
    if cursor.rowcount > 0:
        conn.execute(
            "DELETE FROM fetch_attempts WHERE package_name = ?",
            (name,),
        )
        conn.execute(
            "DELETE FROM package_tags WHERE package_name = ?",
            (name,),
        )
    conn.commit()
    return cursor.rowcount > 0


def get_packages(conn: sqlite3.Connection) -> list[str]:
    """Get list of tracked package names from the database."""
    cursor = conn.execute("SELECT package_name FROM packages ORDER BY package_name")
    return [row["package_name"] for row in cursor.fetchall()]


def normalize_tag(tag: str) -> str:
    """Normalize a tag: trimmed and lowercased for case-insensitive grouping."""
    return tag.strip().lower()


def is_tracked(conn: sqlite3.Connection, package_name: str) -> bool:
    """Return whether a package is currently tracked."""
    cursor = conn.execute(
        "SELECT 1 FROM packages WHERE package_name = ?",
        (package_name,),
    )
    return cursor.fetchone() is not None


def add_package_tag(conn: sqlite3.Connection, package_name: str, tag: str) -> bool:
    """Tag a package for grouping.

    Returns True if the tag was added, False if it was empty or already present.

    Raises:
        ValueError: If the package is not tracked. `package_tags` carries no
            foreign key, so membership is enforced here rather than by SQLite;
            without it a tag can be attached to a package that has no stats,
            and it then shows up in `get_tag_summary` contributing nothing.
    """
    normalized = normalize_tag(tag)
    if not normalized:
        return False
    if not is_tracked(conn, package_name):
        raise ValueError(f"Package '{package_name}' is not tracked")
    try:
        conn.execute(
            "INSERT INTO package_tags (package_name, tag) VALUES (?, ?)",
            (package_name, normalized),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def remove_package_tag(conn: sqlite3.Connection, package_name: str, tag: str) -> bool:
    """Remove a tag from a package. Returns True if a tag was removed."""
    cursor = conn.execute(
        "DELETE FROM package_tags WHERE package_name = ? AND tag = ?",
        (package_name, normalize_tag(tag)),
    )
    conn.commit()
    return cursor.rowcount > 0


def get_package_tags(conn: sqlite3.Connection, package_name: str) -> list[str]:
    """Get the sorted list of tags for a package."""
    cursor = conn.execute(
        "SELECT tag FROM package_tags WHERE package_name = ? ORDER BY tag",
        (package_name,),
    )
    return [row["tag"] for row in cursor.fetchall()]


def get_packages_for_tag(conn: sqlite3.Connection, tag: str) -> list[str]:
    """Get the sorted list of packages carrying a tag."""
    cursor = conn.execute(
        "SELECT package_name FROM package_tags WHERE tag = ? ORDER BY package_name",
        (normalize_tag(tag),),
    )
    return [row["package_name"] for row in cursor.fetchall()]


def get_tags_map(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Return a mapping of tag -> sorted list of member package names."""
    cursor = conn.execute(
        "SELECT tag, package_name FROM package_tags ORDER BY tag, package_name"
    )
    result: dict[str, list[str]] = {}
    for row in cursor.fetchall():
        result.setdefault(row["tag"], []).append(row["package_name"])
    return result


def record_fetch_attempt(
    conn: sqlite3.Connection,
    package_name: str,
    success: bool,
    commit: bool = True,
) -> None:
    """Record a fetch attempt for a package.

    Args:
        conn: Database connection.
        package_name: Name of the package.
        success: Whether the fetch was successful.
        commit: If True, commit the transaction.
    """
    # UTC: compared against SQLite's datetime('now') in get_packages_needing_update.
    attempt_time = utcnow().isoformat()
    conn.execute(
        """
        INSERT OR REPLACE INTO fetch_attempts (package_name, attempt_time, success)
        VALUES (?, ?, ?)
        """,
        (package_name, attempt_time, 1 if success else 0),
    )
    if commit:
        conn.commit()


def get_packages_needing_update(conn: sqlite3.Connection, hours: int = 24) -> list[str]:
    """Get packages that haven't been fetched in the last N hours.

    Args:
        conn: Database connection.
        hours: Number of hours since last attempt to consider stale.

    Returns:
        List of package names that need updating.
    """
    # Get all tracked packages
    all_packages = get_packages(conn)
    if not all_packages:
        return []

    # Get packages with recent attempts
    cursor = conn.execute(
        """
        SELECT package_name FROM fetch_attempts
        WHERE datetime(attempt_time) > datetime('now', ?) AND success = 1
        """,
        (f"-{hours} hours",),
    )
    recent_attempts = {row["package_name"] for row in cursor.fetchall()}

    # Return packages without recent attempts
    return [p for p in all_packages if p not in recent_attempts]


def get_next_update_seconds(conn: sqlite3.Connection, hours: int = 24) -> float | None:
    """Get seconds until the next package becomes eligible for update.

    Finds the oldest successful attempt within the cooldown window and computes
    how many seconds remain until it expires.

    Returns:
        Seconds until the next package is eligible, or None if no packages are throttled.
    """
    cursor = conn.execute(
        """
        SELECT MIN(attempt_time) as earliest
        FROM fetch_attempts
        WHERE datetime(attempt_time) > datetime('now', ?) AND success = 1
        """,
        (f"-{hours} hours",),
    )
    row = cursor.fetchone()
    if not row or not row["earliest"]:
        return None

    earliest = datetime.fromisoformat(row["earliest"])
    expires_at = earliest + timedelta(hours=hours)
    remaining = (expires_at - utcnow()).total_seconds()
    return max(0.0, remaining)


def store_env_stats(
    conn: sqlite3.Connection,
    package_name: str,
    python_versions: list[CategoryDownloads] | None = None,
    os_data: list[CategoryDownloads] | None = None,
    commit: bool = True,
) -> None:
    """Store environment stats (Python versions, OS distribution) in the database.

    Args:
        conn: Database connection.
        package_name: Name of the package.
        python_versions: Python version download breakdown, or None.
        os_data: OS distribution download breakdown, or None.
        commit: If True, commit the transaction.
    """
    fetch_date = datetime.now().strftime("%Y-%m-%d")
    if python_versions:
        for item in python_versions:
            conn.execute(
                """
                INSERT OR REPLACE INTO python_version_stats
                (package_name, fetch_date, category, downloads)
                VALUES (?, ?, ?, ?)
                """,
                (package_name, fetch_date, item["category"], item["downloads"]),
            )
    if os_data:
        for item in os_data:
            conn.execute(
                """
                INSERT OR REPLACE INTO os_stats
                (package_name, fetch_date, category, downloads)
                VALUES (?, ?, ?, ?)
                """,
                (package_name, fetch_date, item["category"], item["downloads"]),
            )
    if commit:
        conn.commit()


def store_daily_downloads(
    conn: sqlite3.Connection,
    package_name: str,
    records: list[DailyDownload] | None,
    commit: bool = True,
) -> int:
    """Store a package's daily download time series.

    Each record is upserted on ``(package_name, date, dimension, category)`` so
    re-fetching an already-captured day overwrites its count rather than
    duplicating it. This is what lets repeated fetches refine the trailing
    window while preserving days that have since aged out of the API.

    Args:
        conn: Database connection.
        package_name: Name of the package.
        records: Daily records to store, or None (a no-op).
        commit: If True, commit the transaction.

    Returns:
        Number of records written.
    """
    if not records:
        return 0
    conn.executemany(
        """
        INSERT INTO daily_downloads
            (package_name, date, dimension, category, downloads)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(package_name, date, dimension, category)
        DO UPDATE SET downloads = excluded.downloads
        """,
        [
            (
                package_name,
                rec["date"],
                rec["dimension"],
                rec["category"],
                rec["downloads"],
            )
            for rec in records
        ],
    )
    if commit:
        conn.commit()
    return len(records)


def get_daily_downloads(
    conn: sqlite3.Connection,
    package_name: str,
    dimension: str = "overall",
    category: str | None = None,
    since: str | None = None,
    tracked_only: bool = False,
) -> list[DailyDownload]:
    """Return a package's stored daily download series, ordered by date.

    Args:
        conn: Database connection.
        package_name: Name of the package.
        dimension: One of ``"overall"``, ``"python"``, or ``"os"``.
        category: Restrict to a single category (e.g. ``"without_mirrors"`` or
            ``"3.12"``). If None, all categories in the dimension are returned.
        since: Only include rows on or after this ``YYYY-MM-DD`` date.
        tracked_only: Return nothing when the package is no longer tracked; see
            :func:`get_latest_stats` for why that is not the default.

    Returns:
        List of daily records (possibly empty), ordered by date then category.
    """
    query = [
        "SELECT date, dimension, category, downloads FROM daily_downloads",
        "WHERE package_name = ? AND dimension = ?",
    ]
    params: list[Any] = [package_name, dimension]
    if tracked_only:
        query.append(f"AND {_TRACKED_SCOPE}")
    if category is not None:
        query.append("AND category = ?")
        params.append(category)
    if since is not None:
        query.append("AND date >= ?")
        params.append(since)
    query.append("ORDER BY date, category")

    cursor = conn.execute("\n".join(query), params)
    return [
        DailyDownload(
            date=row["date"],
            dimension=row["dimension"],
            category=row["category"],
            downloads=row["downloads"],
        )
        for row in cursor.fetchall()
    ]


def get_cached_python_versions(
    conn: sqlite3.Connection, package_name: str
) -> list[CategoryDownloads] | None:
    """Get cached Python version stats for a package.

    Returns the most recent fetch date's data, sorted by downloads descending.
    Returns None if no cached data exists.
    """
    cursor = conn.execute(
        """
        SELECT category, downloads FROM python_version_stats
        WHERE package_name = ? AND fetch_date = (
            SELECT MAX(fetch_date) FROM python_version_stats WHERE package_name = ?
        )
        ORDER BY downloads DESC
        """,
        (package_name, package_name),
    )
    rows = cursor.fetchall()
    if not rows:
        return None
    return [
        {"category": row["category"], "downloads": row["downloads"]} for row in rows
    ]


def get_cached_os_stats(
    conn: sqlite3.Connection, package_name: str
) -> list[CategoryDownloads] | None:
    """Get cached OS distribution stats for a package.

    Returns the most recent fetch date's data, sorted by downloads descending.
    Returns None if no cached data exists.
    """
    cursor = conn.execute(
        """
        SELECT category, downloads FROM os_stats
        WHERE package_name = ? AND fetch_date = (
            SELECT MAX(fetch_date) FROM os_stats WHERE package_name = ?
        )
        ORDER BY downloads DESC
        """,
        (package_name, package_name),
    )
    rows = cursor.fetchall()
    if not rows:
        return None
    return [
        {"category": row["category"], "downloads": row["downloads"]} for row in rows
    ]


def get_cached_env_summary(
    conn: sqlite3.Connection,
) -> EnvSummary | None:
    """Aggregate cached environment stats across all packages.

    Returns dict with 'python_versions' and 'os_distribution' keys,
    each mapping to a list of (category, total_downloads) tuples sorted descending.
    Returns None if no cached data exists.
    """
    # Aggregate Python versions from most recent fetch per package
    py_cursor = conn.execute(
        """
        SELECT pv.category, SUM(pv.downloads) as total
        FROM python_version_stats pv
        INNER JOIN (
            SELECT package_name, MAX(fetch_date) as max_date
            FROM python_version_stats GROUP BY package_name
        ) latest ON pv.package_name = latest.package_name
            AND pv.fetch_date = latest.max_date
        GROUP BY pv.category
        ORDER BY total DESC
        """
    )
    py_rows = py_cursor.fetchall()

    os_cursor = conn.execute(
        """
        SELECT os.category, SUM(os.downloads) as total
        FROM os_stats os
        INNER JOIN (
            SELECT package_name, MAX(fetch_date) as max_date
            FROM os_stats GROUP BY package_name
        ) latest ON os.package_name = latest.package_name
            AND os.fetch_date = latest.max_date
        GROUP BY os.category
        ORDER BY total DESC
        """
    )
    os_rows = os_cursor.fetchall()

    if not py_rows and not os_rows:
        return None

    return {
        "python_versions": [(row["category"], row["total"]) for row in py_rows],
        "os_distribution": [(row["category"], row["total"]) for row in os_rows],
    }


def store_stats(
    conn: sqlite3.Connection,
    package_name: str,
    stats: PackageStats,
    commit: bool = True,
) -> None:
    """Store package statistics in the database.

    Args:
        conn: Database connection.
        package_name: Name of the package.
        stats: Package statistics to store.
        commit: If True, commit the transaction. Set to False for batch operations.
    """
    fetch_date = datetime.now().strftime("%Y-%m-%d")
    conn.execute(
        """
        INSERT OR REPLACE INTO package_stats
        (package_name, fetch_date, last_day, last_week, last_month, total)
        VALUES (?, ?, ?, ?, ?, ?)
    """,
        (
            package_name,
            fetch_date,
            stats.get("last_day"),
            stats.get("last_week"),
            stats.get("last_month"),
            stats.get("total"),
        ),
    )
    if commit:
        conn.commit()


def store_stats_batch(
    conn: sqlite3.Connection, stats_list: list[tuple[str, PackageStats]]
) -> int:
    """Store multiple package statistics in a single transaction.

    More efficient than calling store_stats() multiple times as it uses
    a single commit for all inserts.

    Args:
        conn: Database connection.
        stats_list: List of (package_name, stats) tuples to store.

    Returns:
        Number of packages stored.
    """
    fetch_date = datetime.now().strftime("%Y-%m-%d")
    count = 0

    for package_name, stats in stats_list:
        conn.execute(
            """
            INSERT OR REPLACE INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (
                package_name,
                fetch_date,
                stats.get("last_day"),
                stats.get("last_week"),
                stats.get("last_month"),
                stats.get("total"),
            ),
        )
        count += 1

    conn.commit()  # Single commit for all
    return count


def get_latest_stats(
    conn: sqlite3.Connection, tracked_only: bool = False
) -> list[dict[str, Any]]:
    """Get the most recent stats for all packages, ordered by total downloads.

    Args:
        conn: Database connection.
        tracked_only: Restrict results to packages still listed in ``packages``.
            Removing or pruning a package deliberately retains its collected
            statistics until ``cleanup`` runs, so tracked-package views must set
            this to avoid showing untracked leftovers. Defaults to False, which
            reads the stored rows as-is.
    """
    scope = f"WHERE ps.{_TRACKED_SCOPE}" if tracked_only else ""
    cursor = conn.execute(f"""
        SELECT ps.*
        FROM package_stats ps
        INNER JOIN (
            SELECT package_name, MAX(fetch_date) as max_date
            FROM package_stats
            GROUP BY package_name
        ) latest ON ps.package_name = latest.package_name
                AND ps.fetch_date = latest.max_date
        {scope}
        ORDER BY ps.total DESC
    """)
    return [dict(row) for row in cursor.fetchall()]


def get_package_history(
    conn: sqlite3.Connection,
    package_name: str,
    limit: int = 30,
    since: str | None = None,
    tracked_only: bool = False,
) -> list[dict[str, Any]]:
    """Get historical stats for a specific package, ordered by date descending.

    Args:
        conn: Database connection.
        package_name: Name of the package.
        limit: Maximum number of snapshots to return.
        since: Only include snapshots fetched on or after this ``YYYY-MM-DD``
            date, matching the filter :func:`get_daily_downloads` applies to
            the daily series.
        tracked_only: Return nothing for a package that is no longer tracked;
            see :func:`get_latest_stats` for why that is not the default.
    """
    scope = f"AND {_TRACKED_SCOPE}" if tracked_only else ""
    date_filter = "AND fetch_date >= ?" if since is not None else ""
    params: list[Any] = [package_name]
    if since is not None:
        params.append(since)
    params.append(limit)

    cursor = conn.execute(
        f"""
        SELECT * FROM package_stats
        WHERE package_name = ?
        {scope}
        {date_filter}
        ORDER BY fetch_date DESC
        LIMIT ?
    """,
        params,
    )
    return [dict(row) for row in cursor.fetchall()]


def get_all_history(
    conn: sqlite3.Connection, limit_per_package: int = 30, tracked_only: bool = False
) -> dict[str, list[dict[str, Any]]]:
    """Get historical stats for all packages, grouped by package name.

    Set ``tracked_only`` to omit packages that are no longer tracked; see
    :func:`get_latest_stats` for why that is not the default.
    """
    scope = f"WHERE {_TRACKED_SCOPE}" if tracked_only else ""
    cursor = conn.execute(
        f"""
        SELECT * FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY package_name ORDER BY fetch_date DESC) as rn
            FROM package_stats
            {scope}
        ) WHERE rn <= ?
        ORDER BY package_name, fetch_date ASC
    """,
        (limit_per_package,),
    )

    history: dict[str, list[dict[str, Any]]] = {}
    for row in cursor.fetchall():
        row_dict = dict(row)
        pkg = row_dict["package_name"]
        if pkg not in history:
            history[pkg] = []
        history[pkg].append(row_dict)
    return history


def get_stats_with_growth(
    conn: sqlite3.Connection, tracked_only: bool = False
) -> list[dict[str, Any]]:
    """Get latest stats with week-over-week and month-over-month growth metrics.

    Uses a single query to fetch all history, avoiding N+1 query pattern.
    ``tracked_only`` is forwarded to the underlying reads.
    """
    stats = get_latest_stats(conn, tracked_only=tracked_only)
    if not stats:
        return stats

    # Fetch all history in ONE query instead of N queries
    all_history = get_all_history(conn, limit_per_package=31, tracked_only=tracked_only)

    for s in stats:
        pkg = s["package_name"]

        # Prefer exact growth from the true daily series (available from a
        # single fetch): last 7/30 days vs the 7/30 days before.
        daily_rows = get_daily_downloads(
            conn, pkg, dimension="overall", category="without_mirrors"
        )
        if daily_rows:
            series = [(r["date"], r["downloads"]) for r in daily_rows]
            week = daily_window_sums(series, 7)
            month = daily_window_sums(series, 30)
            s["week_growth"] = calculate_growth(week[0], week[1]) if week else None
            s["month_growth"] = calculate_growth(month[0], month[1]) if month else None
            continue

        # Fallback for pre-daily databases: compare snapshot rolling windows
        # across fetches (~7 and ~28 days apart).
        history = list(reversed(all_history.get(pkg, [])))

        week_ago = None
        month_ago = None

        for h in history[1:]:  # Skip the first (current) entry
            days_diff = (
                datetime.strptime(s["fetch_date"], "%Y-%m-%d")
                - datetime.strptime(h["fetch_date"], "%Y-%m-%d")
            ).days
            if week_ago is None and days_diff >= 7:
                week_ago = h
            if month_ago is None and days_diff >= 28:
                month_ago = h
                break

        s["week_growth"] = calculate_growth(
            s["last_week"], week_ago["last_week"] if week_ago else None
        )
        s["month_growth"] = calculate_growth(
            s["total"], month_ago["total"] if month_ago else None
        )

    return stats


def get_milestone_high_water(conn: sqlite3.Connection, package_name: str) -> int | None:
    """Return the highest download total a package has been checked at.

    Milestone alerts fire on the way past this mark rather than on the way past
    the previous snapshot, so a metric that dips and recovers cannot announce
    the same milestone twice. Returns None before the first check.
    """
    cursor = conn.execute(
        "SELECT high_water FROM milestone_state WHERE package_name = ?",
        (package_name,),
    )
    row = cursor.fetchone()
    return int(row["high_water"]) if row is not None else None


def set_milestone_high_water(
    conn: sqlite3.Connection, package_name: str, value: int, commit: bool = True
) -> None:
    """Raise a package's milestone high-water mark, never lowering it."""
    conn.execute(
        """
        INSERT INTO milestone_state (package_name, high_water, updated_date)
        VALUES (?, ?, ?)
        ON CONFLICT(package_name) DO UPDATE SET
            high_water = MAX(high_water, excluded.high_water),
            updated_date = excluded.updated_date
        """,
        (package_name, value, datetime.now().strftime("%Y-%m-%d")),
    )
    if commit:
        conn.commit()


def _run_deletions(
    conn: sqlite3.Connection, deletions: list[tuple[str, str, tuple[Any, ...]]]
) -> dict[str, int]:
    """Run table deletions and report how many rows each one removed.

    Returns a mapping of table name to deleted row count, plus a ``total``
    entry. Counting every table matters because these operations span the whole
    schema, and reporting only one table's count understates them badly once
    the daily series dwarfs the snapshot table.
    """
    counts: dict[str, int] = {}
    for table, where, params in deletions:
        cursor = conn.execute(f"DELETE FROM {table} WHERE {where}", params)
        counts[table] = cursor.rowcount
    conn.commit()
    counts["total"] = sum(counts.values())
    return counts


# Tables holding package-owned rows that `cleanup` purges once a package is no
# longer tracked. `milestone_state` is included so that re-adding a package
# starts its milestone history over rather than inheriting a stale high-water
# mark.
_PACKAGE_OWNED_TABLES = (
    "package_stats",
    "python_version_stats",
    "os_stats",
    "fetch_attempts",
    "daily_downloads",
    "github_stats_history",
    "package_tags",
    "milestone_state",
    "package_repos",
)


def cleanup_orphaned_stats(conn: sqlite3.Connection) -> dict[str, int]:
    """Remove stored data for packages that are no longer being tracked.

    Returns:
        Rows deleted per table, plus a ``total`` entry summing them.
    """
    untracked = "package_name NOT IN (SELECT package_name FROM packages)"
    deletions: list[tuple[str, str, tuple[Any, ...]]] = [
        (table, untracked, ()) for table in _PACKAGE_OWNED_TABLES
    ]
    # CI rows are keyed by repository, so they orphan when a repo leaves the
    # registry rather than when a package stops being tracked.
    deletions.append(
        (
            "github_ci_status",
            "repo_key NOT IN (SELECT repo_key FROM github_repos)",
            (),
        )
    )
    return _run_deletions(conn, deletions)


def prune_old_stats(conn: sqlite3.Connection, days: int = 365) -> dict[str, int]:
    """Remove stats older than the specified number of days.

    Args:
        conn: Database connection.
        days: Delete stats older than this many days (default: 365).

    Returns:
        Rows deleted per table, plus a ``total`` entry summing them. The daily
        series is usually the bulk of it, so the per-table split is what makes
        the figure interpretable.
    """
    # Rows are dated on the local calendar -- `fetch_date` is stamped with the
    # local date, and the daily series is dated by day rather than by instant --
    # so the cutoff is computed on that same calendar. SQLite's `date('now')` is
    # UTC, which puts the boundary a day out for the part of each day when the
    # two calendars disagree, pruning a day early or a day late.
    cutoff = ((datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d"),)
    # The snapshot and environment tables date rows by fetch, the time series
    # tables by the day the downloads happened.
    return _run_deletions(
        conn,
        [
            ("package_stats", "fetch_date < ?", cutoff),
            ("python_version_stats", "fetch_date < ?", cutoff),
            ("os_stats", "fetch_date < ?", cutoff),
            ("daily_downloads", "date < ?", cutoff),
            ("github_stats_history", "date < ?", cutoff),
        ],
    )


def get_database_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Get database statistics.

    Counts and the date range span every table that stores collected history,
    not just the snapshot table. Most of a mature database is the daily series
    and the GitHub history, so reporting `package_stats` alone understates both
    how much is stored and how far back it reaches.

    Returns:
        Dict with package_count, the per-table snapshot_records / daily_records
        / github_history_records counts, their sum as record_count, and the
        first_fetch / last_fetch range across all three.
    """
    cursor = conn.execute("SELECT COUNT(*) as count FROM packages")
    package_count = cursor.fetchone()["count"]

    def count(table: str) -> int:
        row = conn.execute(f"SELECT COUNT(*) as count FROM {table}").fetchone()
        return int(row["count"])

    snapshot_records = count("package_stats")
    daily_records = count("daily_downloads")
    github_history_records = count("github_stats_history")

    # Each table names its date column differently; union them for the range.
    row = conn.execute("""
        SELECT MIN(date) as first, MAX(date) as last FROM (
            SELECT fetch_date AS date FROM package_stats
            UNION ALL SELECT date FROM daily_downloads
            UNION ALL SELECT date FROM github_stats_history
        )
    """).fetchone()

    return {
        "package_count": package_count,
        "record_count": snapshot_records + daily_records + github_history_records,
        "snapshot_records": snapshot_records,
        "daily_records": daily_records,
        "github_history_records": github_history_records,
        "first_fetch": row["first"],
        "last_fetch": row["last"],
    }


# ---------------------------------------------------------------------------
# Release data
# ---------------------------------------------------------------------------

RELEASE_CACHE_TTL_HOURS = 24


def _is_release_cache_valid(conn: sqlite3.Connection, cache_key: str) -> bool:
    """Check if a release cache entry is still valid."""
    cursor = conn.execute(
        "SELECT 1 FROM release_cache WHERE cache_key = ? AND expires_at > datetime('now')",
        (cache_key,),
    )
    return cursor.fetchone() is not None


def _update_release_cache(
    conn: sqlite3.Connection, cache_key: str, ttl_hours: int = RELEASE_CACHE_TTL_HOURS
) -> None:
    """Update the release cache timestamp."""
    conn.execute(
        """INSERT OR REPLACE INTO release_cache (cache_key, fetched_at, expires_at)
           VALUES (?, datetime('now'), datetime('now', ?))""",
        (cache_key, f"+{ttl_hours} hours"),
    )


def store_pypi_releases(
    conn: sqlite3.Connection,
    package_name: str,
    releases: list[PyPIRelease],
) -> None:
    """Store PyPI release data and update cache timestamp."""
    for r in releases:
        conn.execute(
            """INSERT OR REPLACE INTO pypi_releases
               (package_name, version, upload_date, fetched_at)
               VALUES (?, ?, ?, datetime('now'))""",
            (package_name, r["version"], r["upload_date"]),
        )
    _update_release_cache(conn, f"pypi:{package_name}")
    conn.commit()


def get_pypi_releases(
    conn: sqlite3.Connection, package_name: str
) -> list[PyPIRelease] | None:
    """Get cached PyPI releases if cache is valid.

    Returns None if cache has expired or no data exists.
    """
    if not _is_release_cache_valid(conn, f"pypi:{package_name}"):
        return None
    cursor = conn.execute(
        """SELECT version, upload_date FROM pypi_releases
           WHERE package_name = ? ORDER BY upload_date ASC""",
        (package_name,),
    )
    rows = cursor.fetchall()
    if not rows:
        return None
    return [
        PyPIRelease(version=row["version"], upload_date=row["upload_date"])
        for row in rows
    ]


def get_all_pypi_releases(
    conn: sqlite3.Connection, package_name: str
) -> list[PyPIRelease]:
    """Get all stored PyPI releases regardless of cache validity."""
    cursor = conn.execute(
        """SELECT version, upload_date FROM pypi_releases
           WHERE package_name = ? ORDER BY upload_date ASC""",
        (package_name,),
    )
    return [
        PyPIRelease(version=row["version"], upload_date=row["upload_date"])
        for row in cursor.fetchall()
    ]


def store_github_releases(
    conn: sqlite3.Connection,
    repo_key: str,
    releases: list[GitHubRelease],
) -> None:
    """Store GitHub release data and update cache timestamp."""
    for r in releases:
        conn.execute(
            """INSERT OR REPLACE INTO github_releases
               (repo_key, tag_name, published_at, fetched_at)
               VALUES (?, ?, ?, datetime('now'))""",
            (repo_key, r["tag_name"], r["published_at"]),
        )
    _update_release_cache(conn, f"github:{repo_key}")
    conn.commit()


def get_github_releases(
    conn: sqlite3.Connection, repo_key: str
) -> list[GitHubRelease] | None:
    """Get cached GitHub releases if cache is valid.

    Returns None if cache has expired or no data exists.
    """
    if not _is_release_cache_valid(conn, f"github:{repo_key}"):
        return None
    cursor = conn.execute(
        """SELECT tag_name, published_at FROM github_releases
           WHERE repo_key = ? ORDER BY published_at ASC""",
        (repo_key,),
    )
    rows = cursor.fetchall()
    if not rows:
        return None
    return [
        GitHubRelease(
            tag_name=row["tag_name"], published_at=row["published_at"], name=None
        )
        for row in rows
    ]


def get_all_github_releases(
    conn: sqlite3.Connection, repo_key: str
) -> list[GitHubRelease]:
    """Get all stored GitHub releases regardless of cache validity."""
    cursor = conn.execute(
        """SELECT tag_name, published_at FROM github_releases
           WHERE repo_key = ? ORDER BY published_at ASC""",
        (repo_key,),
    )
    return [
        GitHubRelease(
            tag_name=row["tag_name"], published_at=row["published_at"], name=None
        )
        for row in cursor.fetchall()
    ]


# ---------------------------------------------------------------------------
# GitHub stats history (time series)
# ---------------------------------------------------------------------------


def store_github_stats_snapshot(
    conn: sqlite3.Connection,
    package_name: str,
    repo_key: str,
    stars: int,
    forks: int,
    open_issues: int,
    watchers: int,
    open_issues_excl_prs: int | None = None,
    commit: bool = True,
) -> None:
    """Record a daily snapshot of a package's GitHub repo metrics.

    Upserts on ``(package_name, date)`` so repeated fetches on the same day
    refresh the day's values rather than duplicating. Unlike download data,
    GitHub gives no history, so this series accumulates going forward.

    ``open_issues`` is GitHub's ``open_issues_count``, which counts open pull
    requests as issues; ``open_issues_excl_prs`` is the issues-only figure and
    is None when it could not be fetched.
    """
    date = datetime.now().strftime("%Y-%m-%d")
    conn.execute(
        """
        INSERT INTO github_stats_history
            (package_name, repo_key, date, stars, forks, open_issues,
             open_issues_excl_prs, watchers)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(package_name, date) DO UPDATE SET
            repo_key = excluded.repo_key,
            stars = excluded.stars,
            forks = excluded.forks,
            open_issues = excluded.open_issues,
            open_issues_excl_prs = excluded.open_issues_excl_prs,
            watchers = excluded.watchers
        """,
        (
            package_name,
            repo_key,
            date,
            stars,
            forks,
            open_issues,
            open_issues_excl_prs,
            watchers,
        ),
    )
    if commit:
        conn.commit()


def get_github_stats_history(
    conn: sqlite3.Connection, package_name: str, since: str | None = None
) -> list[dict[str, Any]]:
    """Return a package's GitHub metric snapshots, ordered by date ascending.

    Args:
        conn: Database connection.
        package_name: Name of the package.
        since: Only include rows on or after this ``YYYY-MM-DD`` date.

    Returns:
        List of ``{date, stars, forks, open_issues, open_issues_excl_prs,
        watchers}`` dicts. ``open_issues_excl_prs`` is None for rows recorded
        before the issues-only count existed.
    """
    query = [
        "SELECT date, stars, forks, open_issues, open_issues_excl_prs, watchers",
        "FROM github_stats_history WHERE package_name = ?",
    ]
    params: list[Any] = [package_name]
    if since is not None:
        query.append("AND date >= ?")
        params.append(since)
    query.append("ORDER BY date ASC")

    cursor = conn.execute("\n".join(query), params)
    return [dict(row) for row in cursor.fetchall()]


# ---------------------------------------------------------------------------
# Repository registry
# ---------------------------------------------------------------------------

REPO_SOURCE_PACKAGE = "package"
REPO_SOURCE_DISCOVER = "discover"
REPO_SOURCE_MANUAL = "manual"


def normalize_repo_key(repo_key: str) -> str:
    """Canonicalize an ``owner/name`` key: lowercased, no ``.git``, no scheme."""
    key = repo_key.strip().rstrip("/")
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if key.lower().startswith(prefix):
            key = key[len(prefix) :]
            break
    if key.lower().endswith(".git"):
        key = key[:-4]
    return key.lower()


def add_repo(
    conn: sqlite3.Connection,
    repo_key: str,
    source: str = REPO_SOURCE_MANUAL,
    has_workflows: int | None = None,
    default_branch: str | None = None,
    commit: bool = True,
) -> bool:
    """Add a repository to the scan registry.

    Returns True when the row is new. An existing row keeps its source and
    enabled flag; ``has_workflows`` and ``default_branch`` are only overwritten
    when a value is supplied, so a cheap caller cannot erase what an expensive
    one learned.
    """
    key = normalize_repo_key(repo_key)
    existing = conn.execute(
        "SELECT repo_key FROM github_repos WHERE repo_key = ?", (key,)
    ).fetchone()
    if existing is None:
        conn.execute(
            """INSERT INTO github_repos
                   (repo_key, source, has_workflows, enabled, default_branch,
                    added_date)
               VALUES (?, ?, ?, 1, ?, ?)""",
            (
                key,
                source,
                has_workflows,
                default_branch,
                datetime.now().strftime("%Y-%m-%d"),
            ),
        )
    else:
        conn.execute(
            """UPDATE github_repos
                  SET has_workflows = COALESCE(?, has_workflows),
                      default_branch = COALESCE(?, default_branch)
                WHERE repo_key = ?""",
            (has_workflows, default_branch, key),
        )
    if commit:
        conn.commit()
    return existing is None


def remove_repo(conn: sqlite3.Connection, repo_key: str) -> bool:
    """Remove a repository from the registry, along with its CI rows."""
    key = normalize_repo_key(repo_key)
    cursor = conn.execute("DELETE FROM github_repos WHERE repo_key = ?", (key,))
    conn.execute("DELETE FROM github_ci_status WHERE repo_key = ?", (key,))
    conn.execute("DELETE FROM package_repos WHERE repo_key = ?", (key,))
    conn.commit()
    return cursor.rowcount > 0


def set_repo_enabled(conn: sqlite3.Connection, repo_key: str, enabled: bool) -> bool:
    """Include or exclude a repository from scans without forgetting it."""
    cursor = conn.execute(
        "UPDATE github_repos SET enabled = ? WHERE repo_key = ?",
        (1 if enabled else 0, normalize_repo_key(repo_key)),
    )
    conn.commit()
    return cursor.rowcount > 0


def get_repos(
    conn: sqlite3.Connection,
    enabled_only: bool = True,
    with_workflows_only: bool = False,
) -> list[dict[str, Any]]:
    """List registry rows, ordered by key.

    ``with_workflows_only`` keeps repositories not yet probed: ``has_workflows``
    is NULL until discovery looks, and dropping those would hide every repo
    added by hand.
    """
    where = []
    if enabled_only:
        where.append("enabled = 1")
    if with_workflows_only:
        where.append("(has_workflows IS NULL OR has_workflows > 0)")
    query = "SELECT * FROM github_repos"
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY repo_key"
    return [dict(row) for row in conn.execute(query).fetchall()]


def link_package_repo(
    conn: sqlite3.Connection, package_name: str, repo_key: str, commit: bool = True
) -> None:
    """Record that a package is built from a repository."""
    conn.execute(
        "INSERT OR IGNORE INTO package_repos (package_name, repo_key) VALUES (?, ?)",
        (package_name, normalize_repo_key(repo_key)),
    )
    if commit:
        conn.commit()


def get_repo_packages(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Map each repository to the tracked packages built from it."""
    rows = conn.execute(
        """SELECT repo_key, package_name FROM package_repos
           WHERE package_name IN (SELECT package_name FROM packages)
           ORDER BY repo_key, package_name"""
    ).fetchall()
    mapping: dict[str, list[str]] = {}
    for row in rows:
        mapping.setdefault(row["repo_key"], []).append(row["package_name"])
    return mapping


def seed_repos_from_cache(conn: sqlite3.Connection) -> int:
    """Populate an empty registry from data already on disk.

    Reads the repo keys that previous GitHub fetches left in ``github_cache``
    and ``github_stats_history``, so a first ``pkgdb ci`` has something to scan
    without any network call. Suffixed cache keys (the issues-only count, the
    run listings) are excluded -- they are not repository keys.

    Returns the number of repositories added.
    """
    rows = conn.execute("""
        SELECT DISTINCT repo_key FROM github_stats_history
        UNION
        SELECT repo_key FROM github_cache WHERE repo_key NOT LIKE '%#%'
    """).fetchall()

    added = 0
    for row in rows:
        key = normalize_repo_key(row["repo_key"])
        if "/" not in key:
            continue
        cached = conn.execute(
            "SELECT data FROM github_cache WHERE repo_key = ?", (key,)
        ).fetchone()
        default_branch = None
        if cached is not None:
            try:
                default_branch = json.loads(cached["data"]).get("default_branch")
            except (json.JSONDecodeError, TypeError):
                default_branch = None
        if add_repo(
            conn,
            key,
            source=REPO_SOURCE_PACKAGE,
            default_branch=default_branch,
            commit=False,
        ):
            added += 1
    conn.commit()
    return added


# ---------------------------------------------------------------------------
# CI status
# ---------------------------------------------------------------------------


def store_ci_status(
    conn: sqlite3.Connection,
    repo_key: str,
    workflow_name: str,
    state: str,
    branch: str | None = None,
    run_id: int | None = None,
    run_url: str | None = None,
    run_started_at: str | None = None,
    commit: bool = True,
) -> str | None:
    """Record a workflow's latest state and return its ``first_failed_at``.

    The streak start is set from the run that broke the workflow, carried
    forward while it stays broken, and cleared only on a pass. A cancelled or
    still-running run therefore does not reset the clock: neither says the
    failure is over, and resetting on one would report a month-old break as new.
    """
    key = normalize_repo_key(repo_key)
    prev = conn.execute(
        """SELECT state, first_failed_at FROM github_ci_status
           WHERE repo_key = ? AND workflow_name = ?""",
        (key, workflow_name),
    ).fetchone()
    prev_first_failed = prev["first_failed_at"] if prev is not None else None

    if state == CI_STATE_PASS:
        first_failed_at = None
    elif state == CI_STATE_FAIL:
        first_failed_at = prev_first_failed or run_started_at or utcnow().isoformat()
    else:
        first_failed_at = prev_first_failed

    conn.execute(
        """
        INSERT INTO github_ci_status
            (repo_key, workflow_name, state, branch, run_id, run_url,
             run_started_at, first_failed_at, checked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo_key, workflow_name) DO UPDATE SET
            state = excluded.state,
            branch = excluded.branch,
            run_id = excluded.run_id,
            run_url = excluded.run_url,
            run_started_at = excluded.run_started_at,
            first_failed_at = excluded.first_failed_at,
            checked_at = excluded.checked_at
        """,
        (
            key,
            workflow_name,
            state,
            branch,
            run_id,
            run_url,
            run_started_at,
            first_failed_at,
            utcnow().isoformat(),
        ),
    )
    if commit:
        conn.commit()
    return first_failed_at


def get_ci_status(
    conn: sqlite3.Connection, repo_key: str | None = None
) -> list[dict[str, Any]]:
    """Return stored CI rows, for one repository or all of them."""
    query = "SELECT * FROM github_ci_status"
    params: list[Any] = []
    if repo_key is not None:
        query += " WHERE repo_key = ?"
        params.append(normalize_repo_key(repo_key))
    query += " ORDER BY repo_key, workflow_name"
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def prune_ci_status(
    conn: sqlite3.Connection,
    repo_key: str,
    keep: list[str],
    commit: bool = True,
) -> int:
    """Drop a repository's CI rows for workflows not in ``keep``.

    A workflow that is renamed or deleted keeps its last row forever otherwise,
    so a report would show a failure for a workflow that no longer exists.
    Only call this after a successful scan: pruning on a failed fetch would
    erase state the scan simply could not see.

    Returns the number of rows removed.
    """
    key = normalize_repo_key(repo_key)
    placeholders = ",".join("?" for _ in keep)
    query = f"DELETE FROM github_ci_status WHERE repo_key = ? AND workflow_name NOT IN ({placeholders})"
    cursor = conn.execute(query, [key, *keep])
    if commit:
        conn.commit()
    return cursor.rowcount


def clear_ci_status(conn: sqlite3.Connection, repo_key: str | None = None) -> int:
    """Delete stored CI rows. Returns the number removed."""
    if repo_key is None:
        cursor = conn.execute("DELETE FROM github_ci_status")
    else:
        cursor = conn.execute(
            "DELETE FROM github_ci_status WHERE repo_key = ?",
            (normalize_repo_key(repo_key),),
        )
    conn.commit()
    return cursor.rowcount
