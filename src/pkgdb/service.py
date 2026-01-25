"""Service layer for pkgdb - provides a clean abstraction over database and API operations."""

from dataclasses import dataclass
from typing import Any, Callable

from .api import (
    aggregate_env_stats,
    fetch_os_stats,
    fetch_package_stats,
    fetch_python_versions,
)
from .db import (
    add_package,
    cleanup_orphaned_stats,
    get_all_history,
    get_db,
    get_latest_stats,
    get_package_history,
    get_packages,
    get_stats_with_growth,
    prune_old_stats,
    remove_package,
    store_stats,
)
from .export import export_csv, export_json, export_markdown
from .reports import generate_html_report, generate_package_html_report
from .types import CategoryDownloads, PackageStats
from .utils import validate_output_path, validate_package_name


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
    results: dict[str, PackageStats | None]


@dataclass
class PackageDetails:
    """Detailed statistics for a package."""

    name: str
    stats: PackageStats | None
    python_versions: list[CategoryDownloads] | None
    os_stats: list[CategoryDownloads] | None


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

    def add_package(self, name: str) -> bool:
        """Add a package to tracking.

        Args:
            name: Package name to add.

        Returns:
            True if package was added, False if it already exists.

        Raises:
            ValueError: If package name is invalid.
        """
        is_valid, error_msg = validate_package_name(name)
        if not is_valid:
            raise ValueError(error_msg)

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

    def import_packages(self, file_path: str) -> tuple[int, int, list[str]]:
        """Import packages from a file.

        Args:
            file_path: Path to file (YAML, JSON, or plain text).

        Returns:
            Tuple of (added_count, skipped_count, invalid_names).

        Raises:
            FileNotFoundError: If file doesn't exist.
        """
        from .cli import load_packages_from_file

        packages = load_packages_from_file(file_path)
        added = 0
        skipped = 0
        invalid: list[str] = []

        with get_db(self.db_path) as conn:
            for pkg in packages:
                is_valid, _ = validate_package_name(pkg)
                if not is_valid:
                    invalid.append(pkg)
                    continue
                if add_package(conn, pkg):
                    added += 1
                else:
                    skipped += 1
        return added, skipped, invalid

    # -------------------------------------------------------------------------
    # Data Fetching
    # -------------------------------------------------------------------------

    def fetch_all_stats(
        self,
        progress_callback: Callable[[int, int, str, PackageStats | None], None]
        | None = None,
    ) -> FetchResult:
        """Fetch and store stats for all tracked packages.

        Uses batch commits for better performance when storing multiple packages.

        Args:
            progress_callback: Optional callback called for each package with
                (current_index, total_count, package_name, stats_or_none).

        Returns:
            FetchResult with success/failure counts and results.
        """
        with get_db(self.db_path) as conn:
            packages = get_packages(conn)
            if not packages:
                return FetchResult(success=0, failed=0, results={})

            results: dict[str, PackageStats | None] = {}
            success = 0
            failed = 0

            for i, package in enumerate(packages, 1):
                stats = fetch_package_stats(package)
                results[package] = stats

                if stats:
                    # Use commit=False for batch operation
                    store_stats(conn, package, stats, commit=False)
                    success += 1
                else:
                    failed += 1

                if progress_callback:
                    progress_callback(i, len(packages), package, stats)

            # Single commit for all successful stores
            conn.commit()

            return FetchResult(success=success, failed=failed, results=results)

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

    def get_stats(self, with_growth: bool = False) -> list[dict[str, Any]]:
        """Get latest stats for all packages.

        Args:
            with_growth: If True, include growth metrics.

        Returns:
            List of stats dictionaries ordered by total downloads.
        """
        with get_db(self.db_path) as conn:
            if with_growth:
                return get_stats_with_growth(conn)
            return get_latest_stats(conn)

    def get_history(self, package: str, limit: int = 30) -> list[dict[str, Any]]:
        """Get historical stats for a package.

        Args:
            package: Package name.
            limit: Maximum number of days to return.

        Returns:
            List of historical stats ordered by date descending.
        """
        with get_db(self.db_path) as conn:
            return get_package_history(conn, package, limit=limit)

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
            return get_all_history(conn, limit_per_package=limit_per_package)

    # -------------------------------------------------------------------------
    # Reporting
    # -------------------------------------------------------------------------

    def generate_report(
        self,
        output_file: str,
        include_env: bool = False,
    ) -> bool:
        """Generate HTML report for all packages.

        Args:
            output_file: Path to write HTML file.
            include_env: If True, include Python/OS distribution summary.

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
            stats = get_latest_stats(conn)
            if not stats:
                return False

            all_history = get_all_history(conn, limit_per_package=30)
            packages = [s["package_name"] for s in stats]

        env_summary = None
        if include_env:
            env_summary = aggregate_env_stats(packages)

        generate_html_report(stats, output_file, all_history, packages, env_summary)
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
            history = get_package_history(conn, package, limit=30)

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

        generate_package_html_report(
            package, output_file, stats=pkg_stats, history=history
        )
        return True

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

    # -------------------------------------------------------------------------
    # Maintenance
    # -------------------------------------------------------------------------

    def cleanup(self) -> tuple[int, int]:
        """Clean up orphaned stats and return counts.

        Removes stats for packages that are no longer being tracked.

        Returns:
            Tuple of (orphaned_deleted, packages_remaining).
        """
        with get_db(self.db_path) as conn:
            orphaned = cleanup_orphaned_stats(conn)
            packages = get_packages(conn)
            return orphaned, len(packages)

    def prune(self, days: int = 365) -> int:
        """Remove stats older than the specified number of days.

        Args:
            days: Delete stats older than this many days.

        Returns:
            Number of records deleted.
        """
        with get_db(self.db_path) as conn:
            return prune_old_stats(conn, days)
