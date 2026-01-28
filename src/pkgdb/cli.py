"""CLI argument parsing and command implementations."""

import argparse
import json
import logging
from pathlib import Path
from typing import Any
import webbrowser

from tabulate import tabulate

from .db import DEFAULT_DB_FILE, DEFAULT_REPORT_FILE, get_config_dir
from .logging import setup_logging
from .service import PackageStatsService
from .types import PackageStats
from .utils import make_sparkline, parse_date_arg
from . import __version__

logger = logging.getLogger("pkgdb")


DEFAULT_PACKAGES_FILE = str(get_config_dir() / "packages.json")


def load_packages(packages_file: str) -> list[str]:
    """Load published packages from JSON file."""
    with open(packages_file) as f:
        data = json.load(f)
    if isinstance(data, list):
        return [str(p) for p in data]
    if isinstance(data, dict):
        return data.get("published", []) or data.get("packages", []) or []
    return []


def load_packages_from_file(file_path: str) -> list[str]:
    """Load package names from a file (JSON or plain text).

    Supports:
    - JSON (.json): list of strings or object with 'packages'/'published' key
    - Plain text (.txt, other): one package name per line (comments with # supported)
    """
    path = Path(file_path)
    suffix = path.suffix.lower()

    with open(file_path) as f:
        content = f.read()

    if suffix == ".json":
        data = json.loads(content)
        if isinstance(data, list):
            return [str(p) for p in data]
        if isinstance(data, dict):
            return data.get("packages", []) or data.get("published", []) or []
        return []

    # Plain text: one package per line, strip whitespace, skip empty/comments
    packages = []
    for line in content.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            packages.append(line)
    return packages


def import_packages_from_file(conn: Any, file_path: str) -> tuple[int, int]:
    """Import packages from a file into the database.

    Supports YAML, JSON, and plain text formats.
    Returns tuple of (added_count, skipped_count).

    Note: This function is kept for backward compatibility.
    Prefer using PackageStatsService.import_packages() instead.
    """
    from .db import add_package

    packages = load_packages_from_file(file_path)
    added = 0
    skipped = 0
    for pkg in packages:
        if add_package(conn, pkg):
            added += 1
        else:
            skipped += 1
    return added, skipped


def cmd_fetch(args: argparse.Namespace) -> None:
    """Fetch command: download stats and store in database."""
    service = PackageStatsService(args.database)
    packages = service.list_packages()

    if not packages:
        logger.warning("No packages are being tracked.")
        logger.info(
            "Add packages with 'pkgdb add <name>' or import from YAML with 'pkgdb import'."
        )
        return

    total = len(packages)
    logger.info("Fetching stats for %d tracked packages...", total)

    def on_progress(
        current: int, total: int, package: str, stats: PackageStats | None
    ) -> None:
        logger.info("[%d/%d] Fetching stats for %s...", current, total, package)
        if stats:
            logger.debug(
                "  Total: %s | Month: %s | Week: %s | Day: %s",
                f"{stats['total']:,}",
                f"{stats['last_month']:,}",
                f"{stats['last_week']:,}",
                f"{stats['last_day']:,}",
            )

    result = service.fetch_all_stats(progress_callback=on_progress)
    logger.info("Done. (%d succeeded, %d failed)", result.success, result.failed)


def cmd_report(args: argparse.Namespace) -> None:
    """Report command: generate HTML report from stored data."""
    service = PackageStatsService(args.database)
    package = getattr(args, "package", None)
    no_browser = getattr(args, "no_browser", False)

    if package:
        service.generate_package_report(package, args.output)
    else:
        include_env = getattr(args, "env", False)
        if include_env:
            logger.info("Fetching environment data (this may take a moment)...")

        if not service.generate_report(args.output, include_env=include_env):
            logger.warning("No data in database. Run 'fetch' first.")
            return

    if not no_browser:
        logger.info("Opening report in browser...")
        webbrowser.open_new_tab(Path(args.output).resolve().as_uri())


def cmd_update(args: argparse.Namespace) -> None:
    """Sync command: fetch stats then generate report."""
    cmd_fetch(args)
    if not hasattr(args, "env"):
        args.env = False
    cmd_report(args)


def cmd_show(args: argparse.Namespace) -> None:
    """Show command: display stored statistics in terminal."""
    service = PackageStatsService(args.database)
    stats = service.get_stats(with_growth=True)

    if not stats:
        logger.warning("No data in database. Run 'fetch' first.")
        return

    # Sort by specified field
    sort_by = getattr(args, "sort_by", "total")
    sort_keys = {
        "total": lambda s: s.get("total") or 0,
        "month": lambda s: s.get("last_month") or 0,
        "week": lambda s: s.get("last_week") or 0,
        "day": lambda s: s.get("last_day") or 0,
        "growth": lambda s: s.get("month_growth") or 0,
        "name": lambda s: s.get("package_name", ""),
    }
    reverse = sort_by != "name"  # Ascending for name, descending for numbers
    stats = sorted(
        stats, key=sort_keys.get(sort_by, sort_keys["total"]), reverse=reverse
    )

    # Apply limit
    limit = getattr(args, "limit", None)
    if limit:
        stats = stats[:limit]

    # JSON output
    if getattr(args, "json", False):
        output = []
        for s in stats:
            output.append(
                {
                    "package": s["package_name"],
                    "total": s.get("total") or 0,
                    "last_month": s.get("last_month") or 0,
                    "last_week": s.get("last_week") or 0,
                    "last_day": s.get("last_day") or 0,
                    "month_growth": s.get("month_growth"),
                }
            )
        print(json.dumps(output, indent=2))
        return

    history = service.get_all_history(limit_per_package=14)

    rows = []
    for i, s in enumerate(stats, 1):
        pkg = s["package_name"]
        pkg_history = history.get(pkg, [])
        totals = [h["total"] or 0 for h in pkg_history]
        sparkline = make_sparkline(totals, width=7)

        growth_str = ""
        if s.get("month_growth") is not None:
            g = s["month_growth"]
            sign = "+" if g >= 0 else ""
            growth_str = f"{sign}{g:.1f}%"

        rows.append(
            [
                i,
                pkg,
                f"{s['total'] or 0:,}",
                f"{s['last_month'] or 0:,}",
                f"{s['last_week'] or 0:,}",
                f"{s['last_day'] or 0:,}",
                sparkline,
                growth_str,
            ]
        )

    headers = ["#", "Package", "Total", "Month", "Week", "Day", "Trend", "Growth"]
    print(tabulate(rows, headers=headers, tablefmt="simple"))


def cmd_packages(args: argparse.Namespace) -> None:
    """Packages command: show tracked packages."""
    service = PackageStatsService(args.database)
    packages = service.list_packages()

    if not packages:
        logger.warning("No packages are being tracked.")
        logger.info(
            "Add packages with 'pkgdb add <name>' or import with 'pkgdb import'."
        )
        return

    logger.info("Tracking %d packages:\n", len(packages))

    rows = [[pkg.name, pkg.added_date] for pkg in packages]
    headers = ["Package", "Added"]
    print(tabulate(rows, headers=headers, tablefmt="simple"))


def cmd_add(args: argparse.Namespace) -> None:
    """Add command: add a package to tracking."""
    service = PackageStatsService(args.database)
    verify = not getattr(args, "no_verify", False)
    try:
        if service.add_package(args.name, verify=verify):
            logger.info("Added '%s' to tracking.", args.name)
        else:
            logger.warning("Package '%s' is already being tracked.", args.name)
    except ValueError as e:
        logger.error("Error adding package '%s': %s", args.name, e)


def cmd_remove(args: argparse.Namespace) -> None:
    """Remove command: remove a package from tracking."""
    service = PackageStatsService(args.database)
    if service.remove_package(args.name):
        logger.info("Removed '%s' from tracking.", args.name)
    else:
        logger.warning("Package '%s' was not being tracked.", args.name)


def cmd_import(args: argparse.Namespace) -> None:
    """Import command: import packages from file (YAML, JSON, or text)."""
    service = PackageStatsService(args.database)
    verify = not getattr(args, "no_verify", False)
    try:
        added, skipped, invalid, not_found = service.import_packages(
            args.file, verify=verify
        )
        logger.info("Imported %d packages (%d already tracked).", added, skipped)
        if invalid:
            logger.warning(
                "Skipped %d invalid package names: %s", len(invalid), ", ".join(invalid)
            )
        if not_found:
            logger.warning(
                "Skipped %d packages not found on PyPI: %s",
                len(not_found),
                ", ".join(not_found),
            )
    except FileNotFoundError:
        logger.error("File not found: %s", args.file)


def cmd_sync(args: argparse.Namespace) -> None:
    """Sync command: refresh package list from PyPI user account."""
    username = args.user
    prune = getattr(args, "prune", False)
    logger.info("Syncing packages for PyPI user '%s'...", username)

    service = PackageStatsService(args.database)
    result = service.sync_packages_from_user(username, prune=prune)

    if result is None:
        logger.error(
            "Could not fetch packages for user '%s'. User may not exist.", username
        )
        return

    if result.added:
        logger.info("Added %d new packages: %s", len(result.added), ", ".join(result.added))
    else:
        logger.info("No new packages to add.")

    if result.pruned:
        logger.info("Pruned %d packages: %s", len(result.pruned), ", ".join(result.pruned))

    if result.already_tracked:
        logger.debug(
            "%d packages already tracked: %s",
            len(result.already_tracked),
            ", ".join(result.already_tracked),
        )

    if result.not_on_remote and not prune:
        logger.warning(
            "%d locally tracked packages not found in user's PyPI account: %s",
            len(result.not_on_remote),
            ", ".join(result.not_on_remote),
        )

    total = len(result.added) + len(result.already_tracked)
    logger.info("Total packages from '%s': %d", username, total)


def cmd_history(args: argparse.Namespace) -> None:
    """History command: show historical stats for a package."""
    service = PackageStatsService(args.database)
    history = service.get_history(args.package, limit=args.limit)

    if not history:
        logger.warning("No data found for package '%s'.", args.package)
        return

    # Filter by --since date if provided
    since_arg = getattr(args, "since", None)
    if since_arg:
        since, error = parse_date_arg(since_arg)
        if error:
            logger.error("Invalid --since value: %s", error)
            return
        history = [h for h in history if h["fetch_date"] >= since]
        if not history:
            logger.warning(
                "No data found for package '%s' since %s.", args.package, since
            )
            return

    print(f"Historical stats for {args.package}\n")

    rows = []
    for h in reversed(history):
        rows.append(
            [
                h["fetch_date"],
                f"{h['total'] or 0:,}",
                f"{h['last_month'] or 0:,}",
                f"{h['last_week'] or 0:,}",
                f"{h['last_day'] or 0:,}",
            ]
        )

    headers = ["Date", "Total", "Month", "Week", "Day"]
    print(tabulate(rows, headers=headers, tablefmt="simple"))


def cmd_export(args: argparse.Namespace) -> None:
    """Export command: export stats in various formats."""
    service = PackageStatsService(args.database)

    try:
        output = service.export(args.format)
    except ValueError as e:
        logger.error("%s", e)
        return

    if output is None:
        logger.warning("No data in database. Run 'fetch' first.")
        return

    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        logger.info("Exported to %s", args.output)
    else:
        print(output)


def cmd_stats(args: argparse.Namespace) -> None:
    """Stats command: show detailed statistics for a package."""
    service = PackageStatsService(args.database)
    logger.info("Fetching detailed stats for %s...", args.package)
    details = service.fetch_package_details(args.package)
    print()  # Blank line after log message

    if details.stats:
        print("=== Download Summary ===")
        print(f"  Total:      {details.stats['total']:>12,}")
        print(f"  Last month: {details.stats['last_month']:>12,}")
        print(f"  Last week:  {details.stats['last_week']:>12,}")
        print(f"  Last day:   {details.stats['last_day']:>12,}")
        print()

    if details.python_versions:
        print("=== Python Version Distribution ===")
        total_downloads = sum(v.get("downloads", 0) for v in details.python_versions)
        for v in details.python_versions[:10]:
            version = v.get("category", "unknown")
            downloads = v.get("downloads", 0)
            pct = (downloads / total_downloads * 100) if total_downloads > 0 else 0
            bar = "#" * int(pct / 2)
            print(f"  Python {version:<6} {downloads:>12,} ({pct:>5.1f}%) {bar}")
        print()

    if details.os_stats:
        print("=== Operating System Distribution ===")
        total_downloads = sum(s.get("downloads", 0) for s in details.os_stats)
        for s in details.os_stats:
            os_name = s.get("category", "unknown")
            if os_name == "null":
                os_name = "Unknown"
            downloads = s.get("downloads", 0)
            pct = (downloads / total_downloads * 100) if total_downloads > 0 else 0
            bar = "#" * int(pct / 2)
            print(f"  {os_name:<10} {downloads:>12,} ({pct:>5.1f}%) {bar}")
        print()


def cmd_cleanup(args: argparse.Namespace) -> None:
    """Cleanup command: remove orphaned stats and optionally prune old data."""
    service = PackageStatsService(args.database)

    # Remove orphaned stats (stats for packages no longer tracked)
    orphaned, remaining = service.cleanup()
    if orphaned > 0:
        logger.info("Removed %d orphaned stats records.", orphaned)
    else:
        logger.info("No orphaned stats to remove.")

    # Optionally prune old stats
    if hasattr(args, "days") and args.days:
        pruned = service.prune(args.days)
        if pruned > 0:
            logger.info("Pruned %d stats older than %d days.", pruned, args.days)
        else:
            logger.info("No stats older than %d days to prune.", args.days)

    logger.info("Database has %d tracked packages.", remaining)


def cmd_version(args: argparse.Namespace) -> None:
    """Version command: show pkgdb version."""
    print(f"pkgdb {__version__}")


def create_parser() -> argparse.ArgumentParser:
    """Create and configure the argument parser."""
    parser = argparse.ArgumentParser(
        description="Track PyPI package download statistics.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-d",
        "--database",
        default=DEFAULT_DB_FILE,
        help=f"SQLite database file (default: {DEFAULT_DB_FILE})",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose output (show debug messages)",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress informational output (only show warnings/errors)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # add command
    add_parser = subparsers.add_parser(
        "add",
        help="Add a package to tracking",
    )
    add_parser.add_argument(
        "name",
        help="Package name to add",
    )
    add_parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip verification that package exists on PyPI",
    )
    add_parser.set_defaults(func=cmd_add)

    # remove command
    remove_parser = subparsers.add_parser(
        "remove",
        help="Remove a package from tracking",
    )
    remove_parser.add_argument(
        "name",
        help="Package name to remove",
    )
    remove_parser.set_defaults(func=cmd_remove)

    # packages command
    packages_parser = subparsers.add_parser(
        "packages",
        aliases=["list"],
        help="Show tracked packages",
    )
    packages_parser.set_defaults(func=cmd_packages)

    # import command
    import_parser = subparsers.add_parser(
        "import",
        help="Import packages from file (JSON or text)",
    )
    import_parser.add_argument(
        "file",
        nargs="?",
        default=DEFAULT_PACKAGES_FILE,
        help=f"File to import from - supports .yml, .json, or plain text (default: {DEFAULT_PACKAGES_FILE})",
    )
    import_parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip verification that packages exist on PyPI",
    )
    import_parser.set_defaults(func=cmd_import)

    # sync command
    sync_parser = subparsers.add_parser(
        "sync",
        help="Sync package list from a PyPI user account",
    )
    sync_parser.add_argument(
        "--user",
        "-u",
        required=True,
        metavar="USERNAME",
        help="PyPI username to sync packages from",
    )
    sync_parser.add_argument(
        "--prune",
        action="store_true",
        help="Remove locally tracked packages not in user's PyPI account",
    )
    sync_parser.set_defaults(func=cmd_sync)

    # fetch command
    fetch_parser = subparsers.add_parser(
        "fetch",
        help="Fetch download statistics from PyPI for tracked packages",
    )
    fetch_parser.set_defaults(func=cmd_fetch)

    # show command (was 'list')
    show_parser = subparsers.add_parser(
        "show",
        help="Display download stats in terminal",
    )
    show_parser.add_argument(
        "-n",
        "--limit",
        type=int,
        metavar="N",
        help="Show only top N packages",
    )
    show_parser.add_argument(
        "-s",
        "--sort-by",
        choices=["total", "month", "week", "day", "growth", "name"],
        default="total",
        help="Sort by field (default: total)",
    )
    show_parser.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format",
    )
    show_parser.set_defaults(func=cmd_show)

    # report command
    report_parser = subparsers.add_parser(
        "report",
        help="Generate HTML report with charts",
    )
    report_parser.add_argument(
        "package",
        nargs="?",
        help="Package name for detailed single-package report (optional)",
    )
    report_parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_REPORT_FILE,
        help=f"Output HTML file (default: {DEFAULT_REPORT_FILE})",
    )
    report_parser.add_argument(
        "-e",
        "--env",
        action="store_true",
        help="Include environment summary (Python versions, OS) in report",
    )
    report_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't open report in browser (useful for automation)",
    )
    report_parser.set_defaults(func=cmd_report)

    # history command
    history_parser = subparsers.add_parser(
        "history",
        help="Show historical stats for a package",
    )
    history_parser.add_argument(
        "package",
        help="Package name to show history for",
    )
    history_parser.add_argument(
        "-n",
        "--limit",
        type=int,
        default=30,
        help="Number of days to show (default: 30)",
    )
    history_parser.add_argument(
        "--since",
        metavar="DATE",
        help="Show history since DATE (YYYY-MM-DD or relative: 7d, 2w, 1m)",
    )
    history_parser.set_defaults(func=cmd_history)

    # stats command
    stats_parser = subparsers.add_parser(
        "stats",
        help="Show detailed stats for a package (Python versions, OS breakdown)",
    )
    stats_parser.add_argument(
        "package",
        help="Package name to show detailed stats for",
    )
    stats_parser.set_defaults(func=cmd_stats)

    # export command
    export_parser = subparsers.add_parser(
        "export",
        help="Export stats in various formats (csv, json, markdown)",
    )
    export_parser.add_argument(
        "-f",
        "--format",
        choices=["csv", "json", "markdown", "md"],
        default="csv",
        help="Export format (default: csv)",
    )
    export_parser.add_argument(
        "-o",
        "--output",
        help="Output file (default: stdout)",
    )
    export_parser.set_defaults(func=cmd_export)

    # update command
    update_parser = subparsers.add_parser(
        "update",
        help="Fetch stats and generate report",
    )
    update_parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_REPORT_FILE,
        help=f"Output HTML file (default: {DEFAULT_REPORT_FILE})",
    )
    update_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't open report in browser (useful for automation)",
    )
    update_parser.set_defaults(func=cmd_update)

    # cleanup command
    cleanup_parser = subparsers.add_parser(
        "cleanup",
        help="Remove orphaned stats and optionally prune old data",
    )
    cleanup_parser.add_argument(
        "--days",
        type=int,
        metavar="N",
        help="Also prune stats older than N days",
    )
    cleanup_parser.set_defaults(func=cmd_cleanup)

    # version command
    version_parser = subparsers.add_parser(
        "version",
        help="Show pkgdb version",
    )
    version_parser.set_defaults(func=cmd_version)

    return parser


def main() -> None:
    """Main entry point for the CLI."""
    parser = create_parser()
    args = parser.parse_args()

    # Set up logging based on flags
    setup_logging(
        verbose=getattr(args, "verbose", False),
        quiet=getattr(args, "quiet", False),
    )

    if args.command is None:
        parser.print_help()
        return

    args.func(args)
