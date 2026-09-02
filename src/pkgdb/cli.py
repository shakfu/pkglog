"""CLI argument parsing and command implementations."""

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any
import webbrowser

from tabulate import tabulate

from .config import PkgdbConfig, load_config
from .db import DEFAULT_DB_FILE, DEFAULT_REPORT_FILE, get_config_dir, get_db
from .db import get_next_update_seconds
from .github import CI_RUN_WINDOW
from .logging import setup_logging
from .service import PackageStatsService
from .types import PackageStats
from .utils import make_sparkline, parse_date_arg, utcnow
from . import __version__

logger = logging.getLogger("pkgdb")


DEFAULT_PACKAGES_FILE = str(get_config_dir() / "packages.json")
DEFAULT_CI_REPORT_FILE = str(get_config_dir() / "ci-report.html")


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

    Supports JSON and plain text formats.
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
            "Add packages with 'pkgdb add <name>' or import from a file with 'pkgdb import'."
        )
        return

    total = len(packages)
    logger.info("Checking %d tracked packages...", total)

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

    delay = getattr(args, "delay", 1.0)
    result = service.fetch_all_stats(progress_callback=on_progress, delay=delay)

    if result.skipped > 0:
        logger.info(
            "Skipped %d packages (already fetched in last 24 hours).", result.skipped
        )

    if result.success > 0 or result.failed > 0:
        logger.info("Done. (%d succeeded, %d failed)", result.success, result.failed)
    elif result.skipped == total:
        if result.next_update_seconds is not None and result.next_update_seconds > 0:
            hours = int(result.next_update_seconds // 3600)
            minutes = int((result.next_update_seconds % 3600) // 60)
            if hours > 0:
                logger.info(
                    "All packages up to date. Next update available in %dh %dm.",
                    hours,
                    minutes,
                )
            else:
                logger.info(
                    "All packages up to date. Next update available in %dm.",
                    max(1, minutes),
                )
        else:
            logger.info("All packages already up to date.")

    # Optionally fetch GitHub stats
    if getattr(args, "github", False):
        logger.info("Fetching GitHub stats...")
        gh_results = service.fetch_github_stats()
        gh_ok = sum(1 for r in gh_results if r.success)
        gh_fail = sum(1 for r in gh_results if not r.success)
        logger.info("GitHub stats: %d succeeded, %d failed.", gh_ok, gh_fail)


def cmd_report(args: argparse.Namespace) -> None:
    """Report command: generate HTML report from stored data."""
    service = PackageStatsService(args.database)
    package = getattr(args, "package", None)
    no_browser = getattr(args, "no_browser", False)
    project = getattr(args, "project", False)

    if package and project:
        logger.info("Generating project report for %s...", package)
        if not service.generate_project_report(package, args.output):
            logger.warning("Could not generate project report for %s.", package)
            return
    elif package:
        if not service.generate_package_report(package, args.output):
            logger.warning("Could not fetch stats for %s.", package)
            return
    else:
        include_env = getattr(args, "env", False)
        if include_env:
            logger.info("Fetching environment data (this may take a moment)...")

        include_github = getattr(args, "github", False)
        include_ci = getattr(args, "ci", False)
        if include_ci:
            logger.info("Scanning repositories for CI status...")

        if not service.generate_report(
            args.output,
            include_env=include_env,
            include_github=include_github,
            include_ci=include_ci,
        ):
            logger.warning("No data in database. Run 'fetch' first.")
            return

    if not no_browser:
        logger.info("Opening report in browser...")
        webbrowser.open_new_tab(Path(args.output).resolve().as_uri())


def cmd_update(args: argparse.Namespace) -> None:
    """Sync command: fetch stats then generate report."""
    cmd_fetch(args)
    cmd_report(args)


def cmd_serve(args: argparse.Namespace) -> None:
    """Serve command: launch interactive web dashboard."""
    from .server import start_server

    port = getattr(args, "port", 8080)
    no_browser = getattr(args, "no_browser", False)
    start_server(
        db_path=args.database,
        port=port,
        open_browser=not no_browser,
    )


def _format_size(size_bytes: int) -> str:
    """Format bytes as human-readable size."""
    size: float = size_bytes
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def cmd_show(args: argparse.Namespace) -> None:
    """Show command: display stored statistics in terminal."""
    service = PackageStatsService(args.database)

    # Handle --info flag
    if getattr(args, "info", False):
        db_info = service.get_database_info()
        print("Database Info")
        print(f"  Location:    {args.database}")
        print(f"  Size:        {_format_size(db_info['db_size_bytes'])}")
        print(f"  Packages:    {db_info['package_count']}")
        print(f"  Records:     {db_info['record_count']:,}")
        print(f"    Snapshots: {db_info['snapshot_records']:,}")
        print(f"    Daily:     {db_info['daily_records']:,}")
        print(f"    GitHub:    {db_info['github_history_records']:,}")
        if db_info["first_fetch"] and db_info["last_fetch"]:
            print(f"  Date range:  {db_info['first_fetch']} to {db_info['last_fetch']}")
        else:
            print("  Date range:  (no data)")
        return

    tag = getattr(args, "tag", None)
    stats = service.get_stats(with_growth=True, tag=tag)

    if not stats:
        if tag:
            logger.warning("No tracked packages with data are tagged '%s'.", tag)
        else:
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

    # Determine if we have enough history for trend/growth columns
    has_history = any(len(h) > 1 for h in history.values())

    rows = []
    for i, s in enumerate(stats, 1):
        pkg = s["package_name"]
        pkg_history = history.get(pkg, [])
        totals = [h["total"] or 0 for h in pkg_history]

        row = [
            i,
            pkg,
            f"{s['total'] or 0:,}",
            f"{s['last_month'] or 0:,}",
            f"{s['last_week'] or 0:,}",
            f"{s['last_day'] or 0:,}",
        ]

        if has_history:
            sparkline = make_sparkline(totals, width=7)
            growth_str = ""
            if s.get("month_growth") is not None:
                g = s["month_growth"]
                sign = "+" if g >= 0 else ""
                growth_str = f"{sign}{g:.1f}%"
            row.extend([sparkline, growth_str])

        rows.append(row)

    headers = ["#", "Package", "Total", "Month", "Week", "Day"]
    if has_history:
        headers.extend(["Trend", "Growth"])
    print(tabulate(rows, headers=headers, tablefmt="simple"))

    # When filtering to a group, print the aggregate rollup for the tag.
    if tag:
        g_total = sum(s.get("total") or 0 for s in stats)
        g_month = sum(s.get("last_month") or 0 for s in stats)
        g_week = sum(s.get("last_week") or 0 for s in stats)
        print(
            f"\nGroup '{tag}' ({len(stats)} packages): "
            f"{g_total:,} total, {g_month:,} month, {g_week:,} week"
        )

    # Show next update time
    with get_db(args.database) as conn:
        next_secs = get_next_update_seconds(conn)
    if next_secs is not None and next_secs > 0:
        hours = int(next_secs // 3600)
        minutes = int((next_secs % 3600) // 60)
        if hours > 0:
            print(f"\nNext update available in {hours}h {minutes}m.")
        else:
            print(f"\nNext update available in {max(1, minutes)}m.")


def _format_change(current: int, previous: int) -> str:
    """Format an absolute and percentage change between two values."""
    diff = current - previous
    sign = "+" if diff >= 0 else ""
    if previous == 0:
        pct = ""
    else:
        pct_val = (diff / previous) * 100
        pct_sign = "+" if pct_val >= 0 else ""
        pct = f" ({pct_sign}{pct_val:.1f}%)"
    return f"{sign}{diff:,}{pct}"


def _diff_from_daily(
    service: PackageStatsService, args: argparse.Namespace, period: str
) -> bool:
    """Render an exact period-over-period diff from the daily series.

    Returns True if daily data was available and output was produced; False to
    let the caller fall back to the snapshot-based comparison.
    """
    days = 7 if period == "week" else 30
    label_unit = "Week" if period == "week" else "Month"

    comparisons: list[dict[str, Any]] = []
    for info in service.list_packages():
        comp = service.get_period_comparison(info.name, days)
        if comp is None:
            continue
        current, previous = comp
        comparisons.append(
            {
                "package": info.name,
                "current": current,
                "previous": previous,
                "change": current - previous,
            }
        )

    if not comparisons:
        return False

    sort_by = getattr(args, "sort_by", "total")
    if sort_by == "name":
        comparisons.sort(key=lambda c: c["package"])
    elif sort_by == "change":
        comparisons.sort(key=lambda c: c["change"], reverse=True)
    else:
        comparisons.sort(key=lambda c: c["current"], reverse=True)

    if getattr(args, "json", False):
        output = [
            {
                "package": c["package"],
                "period": period,
                "current": c["current"],
                "previous": c["previous"],
                "change": c["change"],
            }
            for c in comparisons
        ]
        print(json.dumps(output, indent=2))
        return True

    print(f"{label_unit}-over-{label_unit} (daily downloads)\n")
    rows = [
        [
            c["package"],
            f"{c['current']:,}",
            f"{c['previous']:,}",
            _format_change(c["current"], c["previous"]),
        ]
        for c in comparisons
    ]
    headers = ["Package", f"This {label_unit}", f"Last {label_unit}", "Change"]
    print(tabulate(rows, headers=headers, tablefmt="simple"))
    return True


def cmd_diff(args: argparse.Namespace) -> None:
    """Diff command: compare stats between two time periods.

    For ``--period week`` and ``--period month`` the comparison uses the true
    daily download series when available (exact this-period vs last-period sums
    from a single fetch), falling back to the snapshot progression otherwise.
    ``--period latest`` always compares the two most recent fetches.
    """
    from datetime import datetime

    service = PackageStatsService(args.database)
    period = getattr(args, "period", "latest")

    # Exact period-over-period comparison from the daily series.
    if period in ("week", "month") and _diff_from_daily(service, args, period):
        return

    history = service.get_all_history(limit_per_package=60)

    if not history:
        logger.warning("No data in database. Run 'fetch' first.")
        return

    # For each package, find the current and comparison data points
    comparisons: list[dict[str, Any]] = []

    for pkg, records in history.items():
        if len(records) < 2:
            continue

        # Records are sorted ASC by date; latest is last
        current = records[-1]
        previous: dict[str, Any]

        if period == "latest":
            previous = records[-2]
        else:
            # Find a record approximately N days ago
            target_days = 7 if period == "week" else 30
            current_date = datetime.strptime(current["fetch_date"], "%Y-%m-%d")
            found = None
            for r in reversed(records[:-1]):
                r_date = datetime.strptime(r["fetch_date"], "%Y-%m-%d")
                if (current_date - r_date).days >= target_days:
                    found = r
                    break
            if found is None:
                continue
            previous = found

        comparisons.append(
            {
                "package": pkg,
                "current_date": current["fetch_date"],
                "previous_date": previous["fetch_date"],
                "total": current.get("total") or 0,
                "prev_total": previous.get("total") or 0,
                "month": current.get("last_month") or 0,
                "prev_month": previous.get("last_month") or 0,
                "week": current.get("last_week") or 0,
                "prev_week": previous.get("last_week") or 0,
                "day": current.get("last_day") or 0,
                "prev_day": previous.get("last_day") or 0,
            }
        )

    if not comparisons:
        if period == "latest":
            logger.warning(
                "Need at least 2 data points to compare. Run 'fetch' again later."
            )
        else:
            logger.warning("Not enough history for %s comparison.", period)
        return

    # Sort
    sort_by = getattr(args, "sort_by", "total")
    sort_keys: dict[str, Any] = {
        "total": lambda c: c["total"],
        "month": lambda c: c["month"],
        "week": lambda c: c["week"],
        "day": lambda c: c["day"],
        "change": lambda c: (c["total"] - c["prev_total"] if c["prev_total"] else 0),
        "name": lambda c: c["package"],
    }
    reverse = sort_by != "name"
    comparisons.sort(key=sort_keys.get(sort_by, sort_keys["total"]), reverse=reverse)

    # JSON output
    if getattr(args, "json", False):
        output = []
        for c in comparisons:
            entry: dict[str, Any] = {
                "package": c["package"],
                "current_date": c["current_date"],
                "previous_date": c["previous_date"],
            }
            for field in ("total", "month", "week", "day"):
                prev_key = f"prev_{field}"
                entry[field] = {
                    "current": c[field],
                    "previous": c[prev_key],
                    "change": c[field] - c[prev_key],
                }
            output.append(entry)
        print(json.dumps(output, indent=2))
        return

    # Determine which metric columns to show based on period
    if period == "week":
        label = "Week-over-Week"
    elif period == "month":
        label = "Month-over-Month"
    else:
        label = f"{comparisons[0]['previous_date']} vs {comparisons[0]['current_date']}"

    print(f"{label}\n")

    rows = []
    for c in comparisons:
        rows.append(
            [
                c["package"],
                f"{c['total']:,}",
                _format_change(c["total"], c["prev_total"]),
                f"{c['month']:,}",
                _format_change(c["month"], c["prev_month"]),
                f"{c['week']:,}",
                _format_change(c["week"], c["prev_week"]),
            ]
        )

    headers = ["Package", "Total", "Change", "Month", "Change", "Week", "Change"]
    print(tabulate(rows, headers=headers, tablefmt="simple"))


def cmd_check(args: argparse.Namespace) -> int:
    """Check command: detect download anomalies and milestone crossings.

    Analyzes the daily series for weekly spikes/drops and reports any configured
    download milestone crossed since the previous fetch. Exits non-zero when any
    event is found (so it composes with shell/CI notifiers); pass ``--exit-zero``
    to always exit 0.
    """
    service = PackageStatsService(args.database)
    config = load_config()

    milestones = list(config.check_milestones)
    if getattr(args, "milestone", None):
        milestones.extend(args.milestone)
    milestones = sorted(set(milestones))

    z_override = getattr(args, "z_threshold", None)
    z_threshold = z_override if z_override is not None else config.check_z_threshold

    events = service.run_checks(
        milestones=milestones,
        baseline_weeks=config.check_baseline_weeks,
        z_threshold=z_threshold,
        min_weekly=config.check_min_weekly,
    )

    if getattr(args, "json", False):
        print(json.dumps(events, indent=2))
    elif not events:
        logger.info("No anomalies or milestones detected.")
    else:
        print(f"Detected {len(events)} event(s):\n")
        rows = [[e["package"], e["kind"].upper(), e["message"]] for e in events]
        print(tabulate(rows, headers=["Package", "Event", "Detail"], tablefmt="simple"))

    if events and not getattr(args, "exit_zero", False):
        return 1
    return 0


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

    tags_by_pkg = {p.name: service.get_package_tags(p.name) for p in packages}

    if getattr(args, "json", False):
        output = [
            {
                "package": p.name,
                "added_date": p.added_date,
                "tags": tags_by_pkg.get(p.name, []),
            }
            for p in packages
        ]
        print(json.dumps(output, indent=2))
        return

    logger.info("Tracking %d packages:\n", len(packages))

    rows = [
        [pkg.name, pkg.added_date, ", ".join(tags_by_pkg.get(pkg.name, [])) or "-"]
        for pkg in packages
    ]
    headers = ["Package", "Added", "Tags"]
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


def cmd_tag(args: argparse.Namespace) -> None:
    """Tag command: add one or more tags to a tracked package."""
    service = PackageStatsService(args.database)
    tracked = {p.name for p in service.list_packages()}
    if args.package not in tracked:
        logger.warning(
            "Package '%s' is not tracked. Add it first with 'pkgdb add'.",
            args.package,
        )
        return

    added = [tag for tag in args.tags if service.add_tag(args.package, tag)]
    if added:
        logger.info("Tagged %s with: %s", args.package, ", ".join(sorted(added)))
    else:
        logger.info("No new tags added to %s.", args.package)


def cmd_untag(args: argparse.Namespace) -> None:
    """Untag command: remove tags from a package."""
    service = PackageStatsService(args.database)

    if getattr(args, "all", False):
        current = service.get_package_tags(args.package)
        for tag in current:
            service.remove_tag(args.package, tag)
        if current:
            logger.info("Removed all tags from %s.", args.package)
        else:
            logger.info("%s had no tags.", args.package)
        return

    if not args.tags:
        logger.error("Specify one or more tags to remove, or use --all.")
        return

    removed = [tag for tag in args.tags if service.remove_tag(args.package, tag)]
    if removed:
        logger.info("Removed from %s: %s", args.package, ", ".join(sorted(removed)))
    else:
        logger.info("No matching tags on %s.", args.package)


def cmd_tags(args: argparse.Namespace) -> None:
    """Tags command: list tags with aggregate download stats (portfolio rollup)."""
    service = PackageStatsService(args.database)
    summary = service.get_tag_summary()

    if getattr(args, "json", False):
        print(json.dumps(summary, indent=2))
        return

    if not summary:
        logger.info("No tags defined. Add one with 'pkgdb tag <package> <tag>'.")
        return

    rows = [
        [
            e["tag"],
            e["package_count"],
            f"{e['total']:,}",
            f"{e['last_month']:,}",
            f"{e['last_week']:,}",
        ]
        for e in summary
    ]
    headers = ["Tag", "Packages", "Total", "Month", "Week"]
    print(tabulate(rows, headers=headers, tablefmt="simple"))


def cmd_import(args: argparse.Namespace) -> None:
    """Import command: import packages from file (JSON or plain text)."""
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
        logger.info(
            "Added %d new packages: %s", len(result.added), ", ".join(result.added)
        )
    else:
        logger.info("No new packages to add.")

    if result.pruned:
        logger.info(
            "Pruned %d packages: %s", len(result.pruned), ", ".join(result.pruned)
        )

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
    """History command: show historical stats for a package.

    Default: generates an HTML report with download chart, release markers,
    and history table, then opens it in the browser.
    Use --text for terminal table output, --json for machine-readable output.

    Text and JSON output prefer the true per-day download series (populated
    from a single fetch), falling back to the snapshot progression (per-fetch
    rolling totals) when no daily data is available.
    """
    service = PackageStatsService(args.database)

    # Resolve --since (accepts relative forms like "7d" or an ISO date).
    since: str | None = None
    since_arg = getattr(args, "since", None)
    if since_arg:
        since, error = parse_date_arg(since_arg)
        if error:
            logger.error("Invalid --since value: %s", error)
            return

    as_json = getattr(args, "json", False)
    as_text = getattr(args, "text", False)

    if as_json or as_text:
        daily = service.get_daily_totals(args.package, since=since)
        if daily:
            _print_daily_history(args.package, daily, as_json=as_json)
            return
        # Fall back to the snapshot progression for pre-daily databases.
        _print_snapshot_history(
            service, args.package, args.limit, since, as_json=as_json
        )
        return

    # Default: HTML report (uses the daily series internally when present).
    # Guard against generating an empty report: require some data, honoring
    # --since when supplied.
    daily = service.get_daily_totals(args.package, since=since)
    snapshot = service.get_history(args.package, limit=args.limit)
    if since is not None:
        snapshot = [h for h in snapshot if h["fetch_date"] >= since]
    if not daily and not snapshot:
        if since is not None:
            logger.warning(
                "No data found for package '%s' since %s.", args.package, since
            )
        else:
            logger.warning("No data found for package '%s'.", args.package)
        return

    output_file = getattr(args, "output", DEFAULT_REPORT_FILE)
    no_browser = getattr(args, "no_browser", False)

    if not service.generate_project_report(
        args.package, output_file, since=since, limit=args.limit
    ):
        logger.warning("Could not generate history report for %s.", args.package)
        return

    if not no_browser:
        logger.info("Opening report in browser...")
        webbrowser.open_new_tab(Path(output_file).resolve().as_uri())


def _print_daily_history(
    package: str, daily: list[dict[str, Any]], as_json: bool
) -> None:
    """Render the per-day download series as JSON or a terminal table."""
    if as_json:
        output = [{"date": row["date"], "downloads": row["downloads"]} for row in daily]
        print(json.dumps(output, indent=2))
        return

    print(f"Daily downloads for {package}\n")
    rows = [[row["date"], f"{row['downloads']:,}"] for row in daily]
    print(tabulate(rows, headers=["Date", "Downloads"], tablefmt="simple"))


def _print_snapshot_history(
    service: PackageStatsService,
    package: str,
    limit: int,
    since: str | None,
    as_json: bool,
) -> None:
    """Render the snapshot (per-fetch rolling totals) progression."""
    history = service.get_history(package, limit=limit)
    if not history:
        logger.warning("No data found for package '%s'.", package)
        return

    if since:
        history = [h for h in history if h["fetch_date"] >= since]
        if not history:
            logger.warning("No data found for package '%s' since %s.", package, since)
            return

    if as_json:
        output = [
            {
                "date": h["fetch_date"],
                "total": h.get("total") or 0,
                "last_month": h.get("last_month") or 0,
                "last_week": h.get("last_week") or 0,
                "last_day": h.get("last_day") or 0,
            }
            for h in reversed(history)
        ]
        print(json.dumps(output, indent=2))
        return

    print(f"Historical stats for {package}\n")
    rows = [
        [
            h["fetch_date"],
            f"{h['total'] or 0:,}",
            f"{h['last_month'] or 0:,}",
            f"{h['last_week'] or 0:,}",
            f"{h['last_day'] or 0:,}",
        ]
        for h in reversed(history)
    ]
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

    if getattr(args, "json", False):
        output: dict[str, Any] = {"package": args.package}
        if details.stats:
            output["downloads"] = {
                "total": details.stats["total"],
                "last_month": details.stats["last_month"],
                "last_week": details.stats["last_week"],
                "last_day": details.stats["last_day"],
            }
        if details.python_versions:
            output["python_versions"] = [
                {
                    "version": v.get("category", "unknown"),
                    "downloads": v.get("downloads", 0),
                }
                for v in details.python_versions[:10]
            ]
        if details.os_stats:
            output["os"] = [
                {
                    "name": (
                        "Unknown"
                        if s.get("category") == "null"
                        else s.get("category", "unknown")
                    ),
                    "downloads": s.get("downloads", 0),
                }
                for s in details.os_stats
            ]
        print(json.dumps(output, indent=2))
        return

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


def cmd_releases(args: argparse.Namespace) -> None:
    """Releases command: show release history for a package."""
    service = PackageStatsService(args.database)
    logger.info("Fetching releases for %s...", args.package)
    pypi_releases, github_releases = service.fetch_package_releases(args.package)

    if getattr(args, "json", False):
        output: dict[str, Any] = {
            "package": args.package,
            "pypi_releases": [dict(r) for r in pypi_releases],
            "github_releases": [dict(r) for r in github_releases],
        }
        print(json.dumps(output, indent=2))
        return

    # Merge and sort by date descending
    rows: list[tuple[str, str, str]] = []
    for pr in pypi_releases:
        rows.append((pr["upload_date"], pr["version"], "PyPI"))
    for gr in github_releases:
        rows.append((gr["published_at"], gr["tag_name"], "GitHub"))
    rows.sort(key=lambda x: x[0], reverse=True)

    if not rows:
        logger.warning("No releases found for %s.", args.package)
        return

    limit = getattr(args, "limit", None)
    if limit:
        rows = rows[:limit]

    print(f"Releases for {args.package}")
    print(
        f"  PyPI: {len(pypi_releases)} releases | GitHub: {len(github_releases)} releases\n"
    )

    table_rows = [[date, version, source] for date, version, source in rows]
    headers = ["Date", "Version", "Source"]
    print(tabulate(table_rows, headers=headers, tablefmt="simple"))


def _deleted_by_table(counts: dict[str, int]) -> dict[str, int]:
    """Drop the summary entry so only real tables remain, and skip empty ones."""
    return {table: n for table, n in counts.items() if table != "total" and n > 0}


def _format_deleted(counts: dict[str, int]) -> str:
    """Render a per-table deletion breakdown for terminal output."""
    by_table = _deleted_by_table(counts)
    if not by_table:
        return "no rows"
    return ", ".join(f"{table}: {n:,}" for table, n in sorted(by_table.items()))


def cmd_cleanup(args: argparse.Namespace) -> None:
    """Cleanup command: remove orphaned stats and optionally prune old data."""
    service = PackageStatsService(args.database)

    # Remove orphaned stats (stats for packages no longer tracked)
    orphaned, remaining = service.cleanup()

    pruned: dict[str, int] = {"total": 0}
    if hasattr(args, "days") and args.days:
        pruned = service.prune(args.days)

    if getattr(args, "json", False):
        # Report the per-table breakdown alongside the total: these operations
        # span the whole schema, and a lone number reads as if it described it.
        output: dict[str, Any] = {
            "orphaned_removed": orphaned["total"],
            "orphaned_removed_by_table": _deleted_by_table(orphaned),
            "packages_remaining": remaining,
        }
        if hasattr(args, "days") and args.days:
            output["pruned"] = pruned["total"]
            output["pruned_by_table"] = _deleted_by_table(pruned)
            output["prune_days"] = args.days
        print(json.dumps(output, indent=2))
        return

    if orphaned["total"] > 0:
        logger.info(
            "Removed %d orphaned records (%s).",
            orphaned["total"],
            _format_deleted(orphaned),
        )
    else:
        logger.info("No orphaned stats to remove.")

    if hasattr(args, "days") and args.days:
        if pruned["total"] > 0:
            logger.info(
                "Pruned %d records older than %d days (%s).",
                pruned["total"],
                args.days,
                _format_deleted(pruned),
            )
        else:
            logger.info("No stats older than %d days to prune.", args.days)

    logger.info("Database has %d tracked packages.", remaining)


def cmd_badge(args: argparse.Namespace) -> None:
    """Badge command: generate SVG badge for a package."""
    service = PackageStatsService(args.database)

    svg = service.generate_badge(
        args.package,
        period=args.period,
        color=getattr(args, "color", None),
    )

    if svg is None:
        logger.error(
            "No stats found for package '%s'. Run 'fetch' first.", args.package
        )
        return

    output = getattr(args, "output", None)
    if output:
        with open(output, "w") as f:
            f.write(svg)
        logger.info("Badge saved to %s", output)
    else:
        print(svg)


def cmd_github(args: argparse.Namespace) -> None:
    """GitHub command: show GitHub repository stats for tracked packages."""
    service = PackageStatsService(args.database)

    packages = service.list_packages()
    if not packages:
        logger.warning("No packages are being tracked.")
        return

    subcommand = getattr(args, "github_command", "fetch")
    json_output = getattr(args, "json", False)

    if subcommand == "cache":
        cache_stats = service.get_github_cache_stats()
        if json_output:
            print(json.dumps(cache_stats, indent=2))
            return
        print("GitHub Cache Statistics:")
        print(f"  Total entries:   {cache_stats['total']}")
        print(f"  Valid entries:   {cache_stats['valid']}")
        print(f"  Expired entries: {cache_stats['expired']}")
        return

    if subcommand == "clear":
        all_entries = getattr(args, "all", False)
        cleared = service.clear_github_cache(expired_only=not all_entries)
        if json_output:
            label = "all" if all_entries else "expired"
            print(json.dumps({"cleared": cleared, "scope": label}, indent=2))
            return
        label = "all" if all_entries else "expired"
        logger.info("Cleared %d %s GitHub cache entries.", cleared, label)
        return

    # Default: fetch
    no_cache = getattr(args, "no_cache", False)
    sort_by = getattr(args, "sort", "stars")

    logger.info("Fetching GitHub stats for %d packages...", len(packages))
    results = service.fetch_github_stats(use_cache=not no_cache)

    successful = [r for r in results if r.success]
    failed = [r for r in results if not r.success]

    if sort_by == "stars":
        successful.sort(key=lambda r: r.stats.stars if r.stats else 0, reverse=True)
    elif sort_by == "name":
        successful.sort(key=lambda r: r.package_name)
    elif sort_by == "activity":
        successful.sort(
            key=lambda r: (
                r.stats.days_since_push
                if r.stats and r.stats.days_since_push is not None
                else 9999
            )
        )

    if json_output:
        output = []
        for r in successful:
            s = r.stats
            if s is None:
                continue
            output.append(
                {
                    "package": r.package_name,
                    "repo": s.full_name,
                    "stars": s.stars,
                    "star_growth": service.get_star_growth(r.package_name),
                    "forks": s.forks,
                    "open_issues": s.open_issues,
                    # Issues only, alongside the PR-inclusive figure above.
                    # None when the issues-only count was unavailable.
                    "open_issues_excl_prs": s.open_issues_excl_prs,
                    "language": s.language,
                    "activity": s.activity_status,
                    "archived": s.archived,
                }
            )
        for r in failed:
            output.append(
                {
                    "package": r.package_name,
                    "error": r.error,
                }
            )
        print(json.dumps(output, indent=2))
        return

    if successful:
        print()
        rows = []
        for r in successful:
            s = r.stats
            if s is None:
                continue
            growth = service.get_star_growth(r.package_name)
            if growth is None:
                growth_str = "-"
            else:
                growth_str = f"+{growth:,}" if growth >= 0 else f"{growth:,}"
            issues = (
                f"{s.open_issues_excl_prs:,}"
                if s.open_issues_excl_prs is not None
                else "-"
            )
            rows.append(
                [
                    r.package_name,
                    f"{s.stars:,}",
                    growth_str,
                    f"{s.forks:,}",
                    issues,
                    s.activity_status,
                    s.language or "-",
                ]
            )

        headers = [
            "Package",
            "Stars",
            "Stars Δ",
            "Forks",
            "Issues",
            "Activity",
            "Language",
        ]
        print(tabulate(rows, headers=headers, tablefmt="simple"))

        total_stars = sum(r.stats.stars for r in successful if r.stats)
        total_forks = sum(r.stats.forks for r in successful if r.stats)
        print(f"\nTotal: {total_stars:,} stars, {total_forks:,} forks")

    if failed:
        print()
        for r in failed:
            logger.warning("  %s: %s", r.package_name, r.error)

    logger.info("Done. (%d succeeded, %d failed)", len(successful), len(failed))


def _humanize_age(timestamp: str | None) -> str:
    """Render an ISO timestamp as a compact age (``3h``, ``6d``, ``-``)."""
    if not timestamp:
        return "-"
    try:
        moment = datetime.fromisoformat(timestamp.replace("Z", ""))
    except ValueError:
        return "-"
    if moment.tzinfo is not None:
        moment = moment.replace(tzinfo=None)
    seconds = (utcnow() - moment).total_seconds()
    if seconds < 0:
        return "0m"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def cmd_ci(args: argparse.Namespace) -> int:
    """CI command: report the latest workflow state of tracked repositories.

    Shows failures only unless ``--all`` is given, and exits non-zero when any
    workflow is failing so it composes with shell and CI notifiers. Repositories
    whose runs could not be fetched are reported as warnings but do not affect
    the exit code -- an unreachable repository is unknown, not broken.
    """
    service = PackageStatsService(args.database)
    config = load_config()

    repos = getattr(args, "repo", None) or None
    branch = getattr(args, "branch", None) or config.ci_branch
    show_all = getattr(args, "all", False)
    json_output = getattr(args, "json", False)

    results = service.fetch_ci_status(
        repos=repos,
        branch=branch,
        use_cache=not getattr(args, "no_cache", False),
        limit=getattr(args, "limit", CI_RUN_WINDOW),
        ignore_workflows=config.ci_ignore_workflows,
    )

    if not results:
        logger.warning(
            "No repositories to scan. Run 'pkgdb repo discover --user <name>' "
            "or 'pkgdb repo add <owner>/<name>'."
        )
        return 0

    failing = [r for r in results if r.failures]
    errored = [r for r in results if r.error]

    rows = []
    for result in results:
        for entry in result.entries:
            if not show_all and not entry.is_failure:
                continue
            failing_for = entry.failing_days
            rows.append(
                [
                    result.repo_key,
                    entry.workflow_name,
                    entry.state,
                    entry.branch or result.branch or "-",
                    _humanize_age(entry.run_started_at),
                    f"{failing_for}d" if failing_for is not None else "-",
                    entry.run_url or "-",
                ]
            )

    if json_output:
        print(
            json.dumps(
                [
                    {
                        "repo": result.repo_key,
                        "branch": result.branch,
                        "packages": result.packages,
                        "error": result.error,
                        "workflows": [
                            {
                                "workflow": entry.workflow_name,
                                "state": entry.state,
                                "branch": entry.branch,
                                "run_id": entry.run_id,
                                "url": entry.run_url,
                                "started_at": entry.run_started_at,
                                "first_failed_at": entry.first_failed_at,
                                "failing_days": entry.failing_days,
                            }
                            for entry in result.entries
                            if show_all or entry.is_failure
                        ],
                    }
                    for result in results
                    if show_all or result.failures or result.error
                ],
                indent=2,
            )
        )
    elif rows:
        print()
        print(
            tabulate(
                rows,
                headers=[
                    "Repo",
                    "Workflow",
                    "State",
                    "Branch",
                    "Age",
                    "Failing",
                    "Run",
                ],
                tablefmt="simple",
            )
        )
        print()

    if not json_output:
        if failing:
            logger.warning(
                "%d of %d repositories have failing workflows.",
                len(failing),
                len(results),
            )
        else:
            logger.info("All %d repositories are green.", len(results))
        for result in errored:
            logger.warning("  %s: %s", result.repo_key, result.error)

    output = getattr(args, "output", None)
    if output:
        service.generate_ci_report(output, scan=False, show_all=show_all)
        logger.info("CI report saved to %s", output)
        if not getattr(args, "no_browser", False):
            webbrowser.open_new_tab(Path(output).resolve().as_uri())

    if failing and not getattr(args, "exit_zero", False):
        return 1
    return 0


def cmd_repo(args: argparse.Namespace) -> None:
    """Repo command: manage the repository scan registry."""
    service = PackageStatsService(args.database)
    subcommand = getattr(args, "repo_command", "list")
    json_output = getattr(args, "json", False)

    if subcommand == "add":
        if service.add_repo(args.name):
            logger.info("Added %s to the scan registry.", args.name)
        else:
            logger.info("%s is already registered.", args.name)
        return

    if subcommand == "remove":
        if service.remove_repo(args.name):
            logger.info("Removed %s from the scan registry.", args.name)
        else:
            logger.warning("%s is not registered.", args.name)
        return

    if subcommand == "discover":
        config = load_config()
        user = getattr(args, "user", None) or config.github_user
        if not user:
            logger.error(
                "No GitHub user given. Pass --user, or set [github] user in "
                "~/.pkgdb/config.toml."
            )
            return
        logger.info("Discovering repositories for '%s'...", user)
        summary = service.discover_repos(
            user,
            include_forks=getattr(args, "include_forks", False),
            include_archived=getattr(args, "include_archived", False),
            probe_workflows=not getattr(args, "no_probe", False),
        )
        if json_output:
            print(json.dumps(summary, indent=2))
            return
        if "error" in summary:
            logger.error("%s", summary["error"])
            return
        logger.info(
            "Found %d repositories (%d with workflows), added %d, linked %d packages.",
            summary["found"],
            summary["with_workflows"],
            summary["added"],
            summary["linked"],
        )
        return

    # Default: list
    show_all = getattr(args, "all", False)
    repos = service.list_repos(enabled_only=not show_all)
    if json_output:
        print(json.dumps(repos, indent=2))
        return
    if not repos:
        logger.warning(
            "No repositories registered. Run 'pkgdb repo discover --user <name>'."
        )
        return

    rows = [
        [
            repo["repo_key"],
            repo["source"],
            "yes"
            if repo["has_workflows"]
            else ("no" if repo["has_workflows"] == 0 else "?"),
            "yes" if repo["enabled"] else "no",
            repo["default_branch"] or "-",
            ", ".join(repo["packages"]) or "-",
        ]
        for repo in repos
    ]
    print()
    print(
        tabulate(
            rows,
            headers=["Repo", "Source", "Workflows", "Enabled", "Branch", "Packages"],
            tablefmt="simple",
        )
    )
    print(f"\n{len(repos)} repositories registered.")


def cmd_version(args: argparse.Namespace) -> None:
    """Version command: show pkgdb version."""
    print(f"pkgdb {__version__}")


def cmd_init(args: argparse.Namespace) -> None:
    """Init command: guided first-run setup."""
    service = PackageStatsService(args.database)

    # Check if packages are already tracked
    existing = service.list_packages()
    if existing:
        logger.info("Already tracking %d packages.", len(existing))
        answer = input("Continue and fetch stats? [Y/n]: ").strip().lower()
        if answer in ("n", "no"):
            return
    else:
        # Use username from CLI flag, config, or prompt
        cli_user = getattr(args, "pypi_user", None) or ""
        if cli_user:
            username = cli_user
        else:
            username = input("PyPI username (leave blank to skip): ").strip()

        if username:
            logger.info("Syncing packages for PyPI user '%s'...", username)
            result = service.sync_packages_from_user(username)
            if result is None:
                logger.error(
                    "Could not fetch packages for user '%s'. User may not exist.",
                    username,
                )
                return
            if result.added:
                logger.info(
                    "Added %d packages: %s",
                    len(result.added),
                    ", ".join(result.added),
                )
            else:
                logger.info("No packages found for user '%s'.", username)
                return
        else:
            # Manual package entry
            print("Enter package names (one per line, blank line to finish):")
            added_count = 0
            while True:
                name = input("  > ").strip()
                if not name:
                    break
                try:
                    if service.add_package(name, verify=True):
                        logger.info("  Added '%s'.", name)
                        added_count += 1
                    else:
                        logger.info("  '%s' already tracked.", name)
                except ValueError as e:
                    logger.warning("  Skipped '%s': %s", name, e)
            if added_count == 0:
                logger.warning("No packages added. Nothing to do.")
                return

    # Fetch stats
    packages = service.list_packages()
    logger.info("Fetching stats for %d packages...", len(packages))

    def on_progress(
        current: int, total: int, package: str, stats: PackageStats | None
    ) -> None:
        logger.info("[%d/%d] %s", current, total, package)

    fetch_result = service.fetch_all_stats(progress_callback=on_progress)
    logger.info(
        "Fetch complete. (%d succeeded, %d failed)",
        fetch_result.success,
        fetch_result.failed,
    )

    # Generate report
    output = getattr(args, "output", DEFAULT_REPORT_FILE)
    no_browser = getattr(args, "no_browser", False)

    if service.generate_report(output):
        logger.info("Report saved to %s", output)
        if not no_browser:
            webbrowser.open_new_tab(Path(output).resolve().as_uri())
    else:
        logger.warning("No data available for report.")


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

    # tag command
    tag_parser = subparsers.add_parser(
        "tag",
        help="Add one or more tags (groups) to a package",
    )
    tag_parser.add_argument("package", help="Package to tag")
    tag_parser.add_argument("tags", nargs="+", metavar="TAG", help="Tag(s) to add")
    tag_parser.set_defaults(func=cmd_tag)

    # untag command
    untag_parser = subparsers.add_parser(
        "untag",
        help="Remove tags from a package",
    )
    untag_parser.add_argument("package", help="Package to untag")
    untag_parser.add_argument("tags", nargs="*", metavar="TAG", help="Tag(s) to remove")
    untag_parser.add_argument(
        "--all",
        action="store_true",
        help="Remove all tags from the package",
    )
    untag_parser.set_defaults(func=cmd_untag)

    # tags command
    tags_parser = subparsers.add_parser(
        "tags",
        help="List tags with aggregate download stats (portfolio rollup)",
    )
    tags_parser.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format",
    )
    tags_parser.set_defaults(func=cmd_tags)

    # packages command
    packages_parser = subparsers.add_parser(
        "packages",
        aliases=["list"],
        help="Show tracked packages",
    )
    packages_parser.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format",
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
        help=f"File to import from - supports .json or plain text (default: {DEFAULT_PACKAGES_FILE})",
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
    fetch_parser.add_argument(
        "-g",
        "--github",
        action="store_true",
        help="Also fetch GitHub repository stats (stars, forks, etc.)",
    )
    fetch_parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Delay in seconds between API requests per package (default: 1.0)",
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
    show_parser.add_argument(
        "--info",
        action="store_true",
        help="Show database info (size, record counts, date range)",
    )
    show_parser.add_argument(
        "--tag",
        metavar="TAG",
        help="Only show packages carrying this tag, with a group total",
    )
    show_parser.set_defaults(func=cmd_show)

    # diff command
    diff_parser = subparsers.add_parser(
        "diff",
        help="Compare stats between two time periods",
    )
    diff_parser.add_argument(
        "-p",
        "--period",
        choices=["latest", "week", "month"],
        default="latest",
        help="Comparison period: latest (previous fetch), week, or month (default: latest)",
    )
    diff_parser.add_argument(
        "-s",
        "--sort-by",
        choices=["total", "month", "week", "day", "change", "name"],
        default="total",
        help="Sort by field (default: total)",
    )
    diff_parser.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format",
    )
    diff_parser.set_defaults(func=cmd_diff)

    # check command
    check_parser = subparsers.add_parser(
        "check",
        help="Detect download anomalies (weekly spikes/drops) and milestone crossings",
    )
    check_parser.add_argument(
        "--milestone",
        type=int,
        action="append",
        metavar="N",
        help="Download milestone to watch (repeatable); merged with config",
    )
    check_parser.add_argument(
        "-z",
        "--z-threshold",
        type=float,
        dest="z_threshold",
        default=None,
        help="Std-devs from baseline to flag an anomaly (default: config or 2.5)",
    )
    check_parser.add_argument(
        "--exit-zero",
        action="store_true",
        help="Always exit 0, even when events are found",
    )
    check_parser.add_argument(
        "--json",
        action="store_true",
        help="Output events in JSON format",
    )
    check_parser.set_defaults(func=cmd_check)

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
    report_parser.add_argument(
        "-g",
        "--github",
        action="store_true",
        help="Include GitHub stats (stars, forks, etc.) in report table",
    )
    report_parser.add_argument(
        "-p",
        "--project",
        action="store_true",
        help="Generate project view with release timeline (requires package name)",
    )
    report_parser.add_argument(
        "-c",
        "--ci",
        action="store_true",
        help="Include a CI status section (scans registered repositories)",
    )
    report_parser.set_defaults(func=cmd_report)

    # releases command
    releases_parser = subparsers.add_parser(
        "releases",
        help="Show release history for a package (PyPI and GitHub)",
    )
    releases_parser.add_argument(
        "package",
        help="Package name to show releases for",
    )
    releases_parser.add_argument(
        "-n",
        "--limit",
        type=int,
        metavar="N",
        help="Show only the most recent N releases",
    )
    releases_parser.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format",
    )
    releases_parser.set_defaults(func=cmd_releases)

    # history command
    history_parser = subparsers.add_parser(
        "history",
        help="Show package history (HTML report by default, --text for terminal)",
    )
    history_parser.add_argument(
        "package",
        help="Package name to show history for",
    )
    history_parser.add_argument(
        "-n",
        "--limit",
        type=int,
        default=90,
        help="Number of days of history (default: 90)",
    )
    history_parser.add_argument(
        "--since",
        metavar="DATE",
        help="Show history since DATE (YYYY-MM-DD or relative: 7d, 2w, 1m)",
    )
    history_parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_REPORT_FILE,
        help=f"Output HTML file (default: {DEFAULT_REPORT_FILE})",
    )
    history_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't open report in browser",
    )
    history_parser.add_argument(
        "-t",
        "--text",
        action="store_true",
        help="Output as text table in terminal",
    )
    history_parser.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format",
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
    stats_parser.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format",
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
        "-e",
        "--env",
        action="store_true",
        help="Include environment summary (Python versions, OS) in report",
    )
    update_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't open report in browser (useful for automation)",
    )
    update_parser.add_argument(
        "-g",
        "--github",
        action="store_true",
        help="Also fetch GitHub repository stats (stars, forks, etc.)",
    )
    update_parser.add_argument(
        "-c",
        "--ci",
        action="store_true",
        help="Include a CI status section (scans registered repositories)",
    )
    update_parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Delay in seconds between API requests per package (default: 1.0)",
    )
    update_parser.set_defaults(func=cmd_update)

    # serve command
    serve_parser = subparsers.add_parser(
        "serve",
        help="Launch interactive web dashboard",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to serve on (default: 8080)",
    )
    serve_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't open browser automatically",
    )
    serve_parser.set_defaults(func=cmd_serve)

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
    cleanup_parser.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format",
    )
    cleanup_parser.set_defaults(func=cmd_cleanup)

    # badge command
    badge_parser = subparsers.add_parser(
        "badge",
        help="Generate SVG badge for a package",
    )
    badge_parser.add_argument(
        "package",
        help="Package name to generate badge for",
    )
    badge_parser.add_argument(
        "-p",
        "--period",
        choices=["total", "month", "week", "day"],
        default="total",
        help="Download period to show (default: total)",
    )
    badge_parser.add_argument(
        "-c",
        "--color",
        help="Badge color (e.g., 'green', 'blue', '#4c1')",
    )
    badge_parser.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        help="Output file (default: stdout)",
    )
    badge_parser.set_defaults(func=cmd_badge)

    # github command
    github_parser = subparsers.add_parser(
        "github",
        help="Show GitHub repository stats (stars, forks, activity)",
    )
    github_parser.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format",
    )
    github_sub = github_parser.add_subparsers(dest="github_command")

    gh_fetch = github_sub.add_parser("fetch", help="Fetch GitHub statistics")
    gh_fetch.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass cache and fetch fresh data from GitHub API",
    )
    gh_fetch.add_argument(
        "-s",
        "--sort",
        choices=["stars", "name", "activity"],
        default="stars",
        help="Sort results by field (default: stars)",
    )
    gh_fetch.set_defaults(func=cmd_github, github_command="fetch")

    gh_cache = github_sub.add_parser("cache", help="Show GitHub cache statistics")
    gh_cache.set_defaults(func=cmd_github, github_command="cache")

    gh_clear = github_sub.add_parser("clear", help="Clear GitHub cache")
    gh_clear.add_argument(
        "--all",
        action="store_true",
        help="Clear all cache entries (not just expired)",
    )
    gh_clear.set_defaults(func=cmd_github, github_command="clear")

    github_parser.set_defaults(func=cmd_github, github_command="fetch")

    # ci command
    ci_parser = subparsers.add_parser(
        "ci",
        help="Report failing GitHub Actions workflows",
        description=(
            "Scan registered repositories for the latest run of each workflow. "
            "Shows failures only unless --all is given, and exits 1 when any "
            "workflow is failing."
        ),
    )
    ci_parser.add_argument(
        "-r",
        "--repo",
        action="append",
        metavar="OWNER/NAME",
        help="Scan this repository instead of the registry (repeatable)",
    )
    ci_parser.add_argument(
        "-b",
        "--branch",
        help="Branch to scan (default: each repository's default branch)",
    )
    ci_parser.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="Show passing workflows too, not just failures",
    )
    ci_parser.add_argument(
        "--limit",
        type=int,
        default=CI_RUN_WINDOW,
        help=f"Runs to inspect per repository (default: {CI_RUN_WINDOW})",
    )
    ci_parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass the cache and fetch fresh run data",
    )
    ci_parser.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format",
    )
    ci_parser.add_argument(
        "--exit-zero",
        action="store_true",
        help="Always exit 0, even when workflows are failing",
    )
    ci_parser.add_argument(
        "-o",
        "--output",
        nargs="?",
        const=DEFAULT_CI_REPORT_FILE,
        help=(
            f"Also write an HTML report and open it (default: {DEFAULT_CI_REPORT_FILE})"
        ),
    )
    ci_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't open the HTML report in a browser",
    )
    ci_parser.set_defaults(func=cmd_ci)

    # repo command
    repo_parser = subparsers.add_parser(
        "repo",
        help="Manage the repository scan registry used by 'ci'",
    )
    repo_parser.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format",
    )
    repo_sub = repo_parser.add_subparsers(dest="repo_command")

    repo_list = repo_sub.add_parser("list", help="List registered repositories")
    repo_list.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="Include disabled repositories",
    )
    repo_list.set_defaults(func=cmd_repo, repo_command="list")

    repo_add = repo_sub.add_parser("add", help="Register a repository")
    repo_add.add_argument("name", help="Repository as owner/name or its GitHub URL")
    repo_add.set_defaults(func=cmd_repo, repo_command="add")

    repo_remove = repo_sub.add_parser("remove", help="Unregister a repository")
    repo_remove.add_argument("name", help="Repository as owner/name")
    repo_remove.set_defaults(func=cmd_repo, repo_command="remove")

    repo_discover = repo_sub.add_parser(
        "discover",
        help="Register a GitHub user's repositories",
    )
    repo_discover.add_argument(
        "-u",
        "--user",
        help="GitHub username (default: [github] user from config.toml)",
    )
    repo_discover.add_argument(
        "--include-forks",
        action="store_true",
        help="Register forks too",
    )
    repo_discover.add_argument(
        "--include-archived",
        action="store_true",
        help="Register archived repositories too",
    )
    repo_discover.add_argument(
        "--no-probe",
        action="store_true",
        help="Skip the per-repository workflow count (one request each)",
    )
    repo_discover.set_defaults(func=cmd_repo, repo_command="discover")

    repo_parser.set_defaults(func=cmd_repo, repo_command="list")

    # init command
    init_parser = subparsers.add_parser(
        "init",
        help="Guided first-run setup (sync packages, fetch stats, generate report)",
    )
    init_parser.add_argument(
        "--user",
        "-u",
        dest="pypi_user",
        metavar="USERNAME",
        help="PyPI username (skips interactive prompt)",
    )
    init_parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_REPORT_FILE,
        help=f"Output HTML file (default: {DEFAULT_REPORT_FILE})",
    )
    init_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't open report in browser",
    )
    init_parser.set_defaults(func=cmd_init)

    # version command
    version_parser = subparsers.add_parser(
        "version",
        help="Show pkgdb version",
    )
    version_parser.set_defaults(func=cmd_version)

    return parser


def apply_config(args: argparse.Namespace, config: PkgdbConfig) -> None:
    """Apply config file defaults to parsed args where CLI didn't set them.

    CLI flags always take precedence over config values. Config values only
    fill in defaults that weren't explicitly provided on the command line.
    """
    # Database path: config overrides the hardcoded default, CLI overrides config
    if config.database and args.database == DEFAULT_DB_FILE:
        args.database = config.database

    # Report output: config overrides the hardcoded default
    if config.report_output and getattr(args, "output", None) == DEFAULT_REPORT_FILE:
        args.output = config.report_output

    # Boolean flags: config sets them if CLI didn't
    if config.github and not getattr(args, "github", False):
        args.github = True
    if config.environment and not getattr(args, "env", False):
        if hasattr(args, "env"):
            args.env = True
    if config.no_browser and not getattr(args, "no_browser", False):
        args.no_browser = True

    # Sort order: config overrides the hardcoded default
    if config.sort_by != "total" and getattr(args, "sort_by", "total") == "total":
        args.sort_by = config.sort_by

    # Init: PyPI user default from config
    if config.pypi_user and not getattr(args, "pypi_user", None):
        args.pypi_user = config.pypi_user


def main() -> None:
    """Main entry point for the CLI."""
    parser = create_parser()
    args = parser.parse_args()

    # Set up logging based on flags
    setup_logging(
        verbose=getattr(args, "verbose", False),
        quiet=getattr(args, "quiet", False),
    )

    # Load config file and apply defaults
    config = load_config()
    apply_config(args, config)

    if args.command is None:
        parser.print_help()
        return

    # Commands may return an int exit code (e.g. `check` signals events); other
    # commands return None. Propagate a non-zero code to the process.
    result = args.func(args)
    if isinstance(result, int) and result != 0:
        raise SystemExit(result)
