"""Database operations for pkgdb."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Generator

from .types import PackageStats
from .utils import calculate_growth


def get_config_dir() -> Path:
    """Get the pkgdb config directory (~/.pkgdb), creating it if needed."""
    config_dir = Path.home() / ".pkgdb"
    config_dir.mkdir(exist_ok=True)
    return config_dir


DEFAULT_DB_FILE = str(get_config_dir() / "pkg.db")
DEFAULT_REPORT_FILE = str(get_config_dir() / "report.html")


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
        CREATE INDEX IF NOT EXISTS idx_package_name
        ON package_stats(package_name)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_fetch_date
        ON package_stats(fetch_date)
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
    conn.commit()
    return cursor.rowcount > 0


def get_packages(conn: sqlite3.Connection) -> list[str]:
    """Get list of tracked package names from the database."""
    cursor = conn.execute("SELECT package_name FROM packages ORDER BY package_name")
    return [row["package_name"] for row in cursor.fetchall()]


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


def get_latest_stats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Get the most recent stats for all packages, ordered by total downloads."""
    cursor = conn.execute("""
        SELECT ps.*
        FROM package_stats ps
        INNER JOIN (
            SELECT package_name, MAX(fetch_date) as max_date
            FROM package_stats
            GROUP BY package_name
        ) latest ON ps.package_name = latest.package_name
                AND ps.fetch_date = latest.max_date
        ORDER BY ps.total DESC
    """)
    return [dict(row) for row in cursor.fetchall()]


def get_package_history(
    conn: sqlite3.Connection, package_name: str, limit: int = 30
) -> list[dict[str, Any]]:
    """Get historical stats for a specific package, ordered by date descending."""
    cursor = conn.execute(
        """
        SELECT * FROM package_stats
        WHERE package_name = ?
        ORDER BY fetch_date DESC
        LIMIT ?
    """,
        (package_name, limit),
    )
    return [dict(row) for row in cursor.fetchall()]


def get_all_history(
    conn: sqlite3.Connection, limit_per_package: int = 30
) -> dict[str, list[dict[str, Any]]]:
    """Get historical stats for all packages, grouped by package name."""
    cursor = conn.execute(
        """
        SELECT * FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY package_name ORDER BY fetch_date DESC) as rn
            FROM package_stats
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


def get_stats_with_growth(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Get latest stats with week-over-week and month-over-month growth metrics.

    Uses a single query to fetch all history, avoiding N+1 query pattern.
    """
    stats = get_latest_stats(conn)
    if not stats:
        return stats

    # Fetch all history in ONE query instead of N queries
    all_history = get_all_history(conn, limit_per_package=31)

    for s in stats:
        pkg = s["package_name"]
        # History is sorted ASC by date, reverse for DESC order
        history = list(reversed(all_history.get(pkg, [])))

        # Find stats from ~7 days ago and ~30 days ago
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

        # Calculate growth
        s["week_growth"] = calculate_growth(
            s["last_month"], week_ago["last_month"] if week_ago else None
        )
        s["month_growth"] = calculate_growth(
            s["total"], month_ago["total"] if month_ago else None
        )

    return stats


def cleanup_orphaned_stats(conn: sqlite3.Connection) -> int:
    """Remove stats for packages that are no longer being tracked.

    Returns the number of orphaned records deleted.
    """
    cursor = conn.execute("""
        DELETE FROM package_stats
        WHERE package_name NOT IN (SELECT package_name FROM packages)
    """)
    conn.commit()
    return cursor.rowcount


def prune_old_stats(conn: sqlite3.Connection, days: int = 365) -> int:
    """Remove stats older than the specified number of days.

    Args:
        conn: Database connection.
        days: Delete stats older than this many days (default: 365).

    Returns:
        Number of records deleted.
    """
    cursor = conn.execute(
        """
        DELETE FROM package_stats
        WHERE fetch_date < date('now', ?)
    """,
        (f"-{days} days",),
    )
    conn.commit()
    return cursor.rowcount
