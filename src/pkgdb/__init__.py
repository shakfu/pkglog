#!/usr/bin/env python3
"""
pkgdb - Track PyPI package download statistics.

Reads published packages from packages.yml, fetches download statistics
via pypistats, stores data in SQLite, and generates HTML reports.
"""

import argparse
import csv
import io
import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path
import webbrowser

import pypistats  # type: ignore[import-untyped]
import yaml
from tabulate import tabulate
from typing import Any


__version__ = "0.1.4"


DEFAULT_PACKAGES_FILE = "packages.yml"


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


def load_packages(packages_file: str) -> list[str]:
    """Load published packages from YAML file."""
    with open(packages_file) as f:
        data = yaml.safe_load(f)
    result: list[str] = data.get("published", [])
    return result


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
    cursor = conn.execute(
        "SELECT package_name FROM packages ORDER BY package_name"
    )
    return [row["package_name"] for row in cursor.fetchall()]


def load_packages_from_file(file_path: str) -> list[str]:
    """Load package names from a file (YAML, JSON, or plain text).

    Supports:
    - YAML (.yml, .yaml): expects 'published' key with list of packages
    - JSON (.json): expects list of strings or object with 'packages'/'published' key
    - Plain text: one package name per line (comments with # supported)
    """
    path = Path(file_path)
    suffix = path.suffix.lower()

    with open(file_path) as f:
        content = f.read()

    if suffix in (".yml", ".yaml"):
        data = yaml.safe_load(content)
        if isinstance(data, dict):
            return data.get("published", []) or data.get("packages", []) or []
        return []

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


def import_packages_from_file(conn: sqlite3.Connection, file_path: str) -> tuple[int, int]:
    """Import packages from a file into the database.

    Supports YAML, JSON, and plain text formats.
    Returns tuple of (added_count, skipped_count).
    """
    packages = load_packages_from_file(file_path)
    added = 0
    skipped = 0
    for pkg in packages:
        if add_package(conn, pkg):
            added += 1
        else:
            skipped += 1
    return added, skipped


def fetch_package_stats(package_name: str) -> dict[str, Any] | None:
    """Fetch download statistics for a package from PyPI."""
    try:
        recent_json = pypistats.recent(package_name, format="json")
        recent_data = json.loads(recent_json)

        data = recent_data.get("data", {})
        stats: dict[str, Any] = {
            "last_day": data.get("last_day", 0),
            "last_week": data.get("last_week", 0),
            "last_month": data.get("last_month", 0),
        }

        overall_json = pypistats.overall(package_name, format="json")
        overall_data = json.loads(overall_json)

        total = 0
        for item in overall_data.get("data", []):
            if item.get("category") == "without_mirrors":
                total = item.get("downloads", 0)
                break
        stats["total"] = total

        return stats
    except Exception as e:
        print(f"  Error fetching stats for {package_name}: {e}")
        return None


def fetch_python_versions(package_name: str) -> list[dict[str, Any]] | None:
    """Fetch download breakdown by Python version for a package."""
    try:
        result = pypistats.python_minor(package_name, format="json")
        data = json.loads(result)
        versions: list[dict[str, Any]] = data.get("data", [])
        # Sort by downloads descending
        return sorted(versions, key=lambda x: x.get("downloads", 0), reverse=True)
    except Exception as e:
        print(f"  Error fetching Python versions for {package_name}: {e}")
        return None


def fetch_os_stats(package_name: str) -> list[dict[str, Any]] | None:
    """Fetch download breakdown by operating system for a package."""
    try:
        result = pypistats.system(package_name, format="json")
        data = json.loads(result)
        systems: list[dict[str, Any]] = data.get("data", [])
        # Sort by downloads descending
        return sorted(systems, key=lambda x: x.get("downloads", 0), reverse=True)
    except Exception as e:
        print(f"  Error fetching OS stats for {package_name}: {e}")
        return None


def aggregate_env_stats(packages: list[str]) -> dict[str, list[tuple[str, int]]]:
    """Aggregate Python version and OS distribution across all packages.

    Returns dict with 'python_versions' and 'os_distribution' lists of (name, count) tuples.
    """
    py_totals: dict[str, int] = {}
    os_totals: dict[str, int] = {}

    for pkg in packages:
        py_data = fetch_python_versions(pkg)
        if py_data:
            for item in py_data:
                version = item.get("category", "unknown")
                if version and version != "null":
                    py_totals[version] = py_totals.get(version, 0) + item.get(
                        "downloads", 0
                    )

        os_data = fetch_os_stats(pkg)
        if os_data:
            for item in os_data:
                os_name = item.get("category", "unknown")
                if os_name == "null":
                    os_name = "Unknown"
                os_totals[os_name] = os_totals.get(os_name, 0) + item.get(
                    "downloads", 0
                )

    # Convert to sorted lists
    py_versions = sorted(py_totals.items(), key=lambda x: x[1], reverse=True)
    os_distribution = sorted(os_totals.items(), key=lambda x: x[1], reverse=True)

    return {
        "python_versions": py_versions,
        "os_distribution": os_distribution,
    }


def store_stats(
    conn: sqlite3.Connection, package_name: str, stats: dict[str, Any]
) -> None:
    """Store package statistics in the database."""
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
    conn.commit()


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


def calculate_growth(current: int | None, previous: int | None) -> float | None:
    """Calculate percentage growth between two values."""
    if previous is None or previous == 0:
        return None
    if current is None:
        return None
    return ((current - previous) / previous) * 100


def get_stats_with_growth(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Get latest stats with week-over-week and month-over-month growth metrics."""
    stats = get_latest_stats(conn)

    for s in stats:
        pkg = s["package_name"]
        history = get_package_history(conn, pkg, limit=31)

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


def make_sparkline(values: list[int], width: int = 7) -> str:
    """Generate an ASCII sparkline from a list of values."""
    if not values:
        return " " * width

    # Use last 'width' values
    values = values[-width:]

    # Pad with zeros if not enough values
    if len(values) < width:
        values = [0] * (width - len(values)) + values

    blocks = " _.,:-=+*#"
    min_val = min(values)
    max_val = max(values)

    if max_val == min_val:
        return blocks[4] * width

    sparkline = ""
    for v in values:
        idx = int((v - min_val) / (max_val - min_val) * (len(blocks) - 1))
        sparkline += blocks[idx]

    return sparkline


def export_csv(stats: list[dict[str, Any]], output: io.StringIO | None = None) -> str:
    """Export stats to CSV format."""
    if output is None:
        output = io.StringIO()

    writer = csv.writer(output)
    writer.writerow(
        [
            "rank",
            "package_name",
            "total",
            "last_month",
            "last_week",
            "last_day",
            "fetch_date",
        ]
    )

    for i, s in enumerate(stats, 1):
        writer.writerow(
            [
                i,
                s["package_name"],
                s.get("total") or 0,
                s.get("last_month") or 0,
                s.get("last_week") or 0,
                s.get("last_day") or 0,
                s.get("fetch_date", ""),
            ]
        )

    return output.getvalue()


def export_json(stats: list[dict[str, Any]]) -> str:
    """Export stats to JSON format."""
    export_data = {
        "generated": datetime.now().isoformat(),
        "packages": [
            {
                "rank": i,
                "name": s["package_name"],
                "total": s.get("total") or 0,
                "last_month": s.get("last_month") or 0,
                "last_week": s.get("last_week") or 0,
                "last_day": s.get("last_day") or 0,
                "fetch_date": s.get("fetch_date", ""),
            }
            for i, s in enumerate(stats, 1)
        ],
    }
    return json.dumps(export_data, indent=2)


def export_markdown(stats: list[dict[str, Any]]) -> str:
    """Export stats to Markdown table format."""
    lines = [
        "| Rank | Package | Total | Month | Week | Day |",
        "|------|---------|------:|------:|-----:|----:|",
    ]

    for i, s in enumerate(stats, 1):
        lines.append(
            f"| {i} | {s['package_name']} | {s.get('total') or 0:,} | "
            f"{s.get('last_month') or 0:,} | {s.get('last_week') or 0:,} | "
            f"{s.get('last_day') or 0:,} |"
        )

    return "\n".join(lines)


def make_svg_pie_chart(
    data: list[tuple[str, int]], chart_id: str, size: int = 200
) -> str:
    """Generate an SVG pie chart."""
    if not data:
        return ""

    total = sum(v for _, v in data)
    if total == 0:
        return "<p>No data available.</p>"

    # Limit to top 6 items, group rest as "Other"
    if len(data) > 6:
        top_data = data[:5]
        other_total = sum(v for _, v in data[5:])
        if other_total > 0:
            top_data.append(("Other", other_total))
        data = top_data

    cx, cy = size // 2, size // 2
    radius = size // 2 - 10
    legend_width = 150
    total_width = size + legend_width

    svg_parts = [
        f'<svg id="{chart_id}" viewBox="0 0 {total_width} {size}" '
        f'style="width:100%;max-width:{total_width}px;height:auto;font-family:system-ui,sans-serif;font-size:11px;">'
    ]

    start_angle: float = 0
    for i, (name, value) in enumerate(data):
        if value == 0:
            continue
        pct = value / total
        angle = pct * 360
        end_angle = start_angle + angle

        # Calculate arc path
        start_rad = math.radians(start_angle - 90)
        end_rad = math.radians(end_angle - 90)

        x1 = cx + radius * math.cos(start_rad)
        y1 = cy + radius * math.sin(start_rad)
        x2 = cx + radius * math.cos(end_rad)
        y2 = cy + radius * math.sin(end_rad)

        large_arc = 1 if angle > 180 else 0
        hue = (i * 360 // len(data)) % 360

        path = f"M {cx} {cy} L {x1:.1f} {y1:.1f} A {radius} {radius} 0 {large_arc} 1 {x2:.1f} {y2:.1f} Z"
        svg_parts.append(f'<path d="{path}" fill="hsl({hue}, 70%, 50%)"/>')

        # Legend item
        ly = 20 + i * 25
        svg_parts.append(
            f'<rect x="{size + 10}" y="{ly - 8}" width="12" height="12" fill="hsl({hue}, 70%, 50%)"/>'
        )
        svg_parts.append(
            f'<text x="{size + 28}" y="{ly}" fill="#333">{name} ({pct * 100:.1f}%)</text>'
        )

        start_angle = end_angle

    svg_parts.append("</svg>")
    return "\n".join(svg_parts)


def generate_html_report(
    stats: list[dict[str, Any]],
    output_file: str,
    history: dict[str, list[dict[str, Any]]] | None = None,
    packages: list[str] | None = None,
    env_summary: dict[str, list[tuple[str, int]]] | None = None,
) -> None:
    """Generate a self-contained HTML report with inline SVG charts.

    Args:
        stats: List of package statistics
        output_file: Path to write HTML file
        history: Historical data for time-series chart
        packages: List of package names (for fetching env data if env_summary not provided)
        env_summary: Pre-fetched Python version and OS summary data
    """
    if not stats:
        print("No statistics available to generate report.")
        return

    def make_svg_bar_chart(
        data: list[tuple[str, int]], title: str, chart_id: str
    ) -> str:
        """Generate an SVG bar chart."""
        if not data:
            return ""

        max_val = max(v for _, v in data) or 1
        bar_height = 28
        bar_gap = 6
        label_width = 160
        value_width = 80
        chart_width = 700
        bar_area_width = chart_width - label_width - value_width
        chart_height = len(data) * (bar_height + bar_gap) + 20

        svg_parts = [
            f'<svg id="{chart_id}" viewBox="0 0 {chart_width} {chart_height}" '
            f'style="width:100%;max-width:{chart_width}px;height:auto;font-family:system-ui,sans-serif;font-size:12px;">'
        ]

        for i, (name, value) in enumerate(data):
            y = i * (bar_height + bar_gap) + 10
            bar_width = (value / max_val) * bar_area_width if max_val > 0 else 0
            hue = (i * 360 // len(data)) % 360

            # Label
            svg_parts.append(
                f'<text x="{label_width - 8}" y="{y + bar_height // 2 + 4}" '
                f'text-anchor="end" fill="#333">{name}</text>'
            )
            # Bar
            svg_parts.append(
                f'<rect x="{label_width}" y="{y}" width="{bar_width:.1f}" '
                f'height="{bar_height}" fill="hsl({hue}, 70%, 50%)" rx="3"/>'
            )
            # Value
            svg_parts.append(
                f'<text x="{label_width + bar_area_width + 8}" y="{y + bar_height // 2 + 4}" '
                f'fill="#666">{value:,}</text>'
            )

        svg_parts.append("</svg>")
        return "\n".join(svg_parts)

    def make_svg_line_chart(
        history_data: dict[str, list[dict[str, Any]]] | None, chart_id: str
    ) -> str:
        """Generate an SVG line chart showing downloads over time."""
        if not history_data:
            return ""

        # Collect all dates and find date range
        all_dates = set()
        for pkg_history in history_data.values():
            for h in pkg_history:
                all_dates.add(h["fetch_date"])

        if not all_dates:
            return ""

        sorted_dates = sorted(all_dates)
        if len(sorted_dates) < 2:
            return "<p>Not enough historical data for time-series chart.</p>"

        chart_width = 700
        chart_height = 300
        margin = {"top": 20, "right": 120, "bottom": 40, "left": 80}
        plot_width = chart_width - margin["left"] - margin["right"]
        plot_height = chart_height - margin["top"] - margin["bottom"]

        # Find max value across all packages
        max_val = 0
        for pkg_history in history_data.values():
            for h in pkg_history:
                max_val = max(max_val, h["total"] or 0)
        max_val = max_val or 1

        svg_parts = [
            f'<svg id="{chart_id}" viewBox="0 0 {chart_width} {chart_height}" '
            f'style="width:100%;max-width:{chart_width}px;height:auto;font-family:system-ui,sans-serif;font-size:11px;">'
        ]

        # Draw axes
        svg_parts.append(
            f'<line x1="{margin["left"]}" y1="{margin["top"]}" '
            f'x2="{margin["left"]}" y2="{chart_height - margin["bottom"]}" '
            f'stroke="#ccc" stroke-width="1"/>'
        )
        svg_parts.append(
            f'<line x1="{margin["left"]}" y1="{chart_height - margin["bottom"]}" '
            f'x2="{chart_width - margin["right"]}" y2="{chart_height - margin["bottom"]}" '
            f'stroke="#ccc" stroke-width="1"/>'
        )

        # Draw Y-axis labels
        for i in range(5):
            y_val = max_val * (4 - i) / 4
            y_pos = margin["top"] + (i * plot_height / 4)
            svg_parts.append(
                f'<text x="{margin["left"] - 8}" y="{y_pos + 4}" '
                f'text-anchor="end" fill="#666">{int(y_val):,}</text>'
            )
            svg_parts.append(
                f'<line x1="{margin["left"]}" y1="{y_pos}" '
                f'x2="{chart_width - margin["right"]}" y2="{y_pos}" '
                f'stroke="#eee" stroke-width="1"/>'
            )

        # Draw X-axis labels (show first, middle, last dates)
        date_positions = [0, len(sorted_dates) // 2, len(sorted_dates) - 1]
        for idx in date_positions:
            if idx < len(sorted_dates):
                x_pos = (
                    margin["left"] + (idx / max(1, len(sorted_dates) - 1)) * plot_width
                )
                svg_parts.append(
                    f'<text x="{x_pos}" y="{chart_height - margin["bottom"] + 16}" '
                    f'text-anchor="middle" fill="#666">{sorted_dates[idx]}</text>'
                )

        # Draw lines for each package (top 5 by total)
        top_packages = sorted(
            history_data.keys(),
            key=lambda p: max((h["total"] or 0) for h in history_data[p]),
            reverse=True,
        )[:5]

        for pkg_idx, pkg in enumerate(top_packages):
            pkg_history = sorted(history_data[pkg], key=lambda h: h["fetch_date"])
            hue = (pkg_idx * 360 // len(top_packages)) % 360
            color = f"hsl({hue}, 70%, 50%)"

            # Build path
            points = []
            for h in pkg_history:
                date_idx = sorted_dates.index(h["fetch_date"])
                x = (
                    margin["left"]
                    + (date_idx / max(1, len(sorted_dates) - 1)) * plot_width
                )
                y = (
                    margin["top"]
                    + plot_height
                    - ((h["total"] or 0) / max_val) * plot_height
                )
                points.append(f"{x:.1f},{y:.1f}")

            if points:
                svg_parts.append(
                    f'<polyline points="{" ".join(points)}" '
                    f'fill="none" stroke="{color}" stroke-width="2"/>'
                )

                # Add label at end
                last_x = (
                    margin["left"]
                    + ((len(sorted_dates) - 1) / max(1, len(sorted_dates) - 1))
                    * plot_width
                )
                last_h = pkg_history[-1]
                last_y = (
                    margin["top"]
                    + plot_height
                    - ((last_h["total"] or 0) / max_val) * plot_height
                )
                svg_parts.append(
                    f'<text x="{last_x + 8}" y="{last_y + 4}" fill="{color}">{pkg}</text>'
                )

        svg_parts.append("</svg>")
        return "\n".join(svg_parts)

    totals_data = [(s["package_name"], s["total"] or 0) for s in stats]
    month_data = [(s["package_name"], s["last_month"] or 0) for s in stats]

    totals_chart = make_svg_bar_chart(totals_data, "Total Downloads", "totals-chart")
    month_chart = make_svg_bar_chart(month_data, "Last Month", "month-chart")
    time_series_chart = (
        make_svg_line_chart(history, "time-series-chart") if history else ""
    )

    # Generate environment summary charts if data available
    py_version_chart = ""
    os_chart = ""
    if env_summary:
        py_data = env_summary.get("python_versions", [])
        if py_data:
            py_version_chart = make_svg_pie_chart(py_data, "py-version-chart", size=200)
        os_data = env_summary.get("os_distribution", [])
        if os_data:
            os_chart = make_svg_pie_chart(os_data, "os-chart", size=200)

    env_summary_html = ""
    if py_version_chart or os_chart:
        env_summary_html = f"""
    <div class="chart-container">
        <h2>Environment Summary (Aggregated)</h2>
        <div class="pie-charts-row">
            {f'<div class="pie-chart-wrapper"><h3>Python Versions</h3>{py_version_chart}</div>' if py_version_chart else ""}
            {f'<div class="pie-chart-wrapper"><h3>Operating Systems</h3>{os_chart}</div>' if os_chart else ""}
        </div>
    </div>
"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PyPI Package Download Statistics</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            background: #f5f5f5;
        }}
        h1, h2 {{
            color: #333;
        }}
        .chart-container {{
            background: white;
            border-radius: 8px;
            padding: 20px;
            margin: 20px 0;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            overflow-x: auto;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            background: white;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        th, td {{
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #eee;
        }}
        th {{
            background: #4a90a4;
            color: white;
        }}
        tr:hover {{
            background: #f9f9f9;
        }}
        .number {{
            text-align: right;
            font-family: monospace;
        }}
        .generated {{
            color: #666;
            font-size: 0.9em;
            margin-top: 20px;
        }}
        .pie-charts-row {{
            display: flex;
            flex-wrap: wrap;
            gap: 40px;
            justify-content: flex-start;
        }}
        .pie-chart-wrapper {{
            flex: 0 0 auto;
        }}
        .pie-chart-wrapper h3 {{
            margin: 0 0 10px 0;
            font-size: 14px;
            color: #555;
        }}
    </style>
</head>
<body>
    <h1>PyPI Package Download Statistics</h1>

    <div class="chart-container">
        <h2>Total Downloads by Package</h2>
        {totals_chart}
    </div>

    <div class="chart-container">
        <h2>Recent Downloads (Last Month)</h2>
        {month_chart}
    </div>

    {f'<div class="chart-container"><h2>Downloads Over Time (Top 5)</h2>{time_series_chart}</div>' if time_series_chart else ""}

    {env_summary_html}

    <h2>Detailed Statistics</h2>
    <table>
        <thead>
            <tr>
                <th>Rank</th>
                <th>Package</th>
                <th class="number">Total</th>
                <th class="number">Last Month</th>
                <th class="number">Last Week</th>
                <th class="number">Last Day</th>
            </tr>
        </thead>
        <tbody>
"""

    for i, s in enumerate(stats, 1):
        html += f"""            <tr>
                <td>{i}</td>
                <td><a href="https://pypi.org/project/{s["package_name"]}/">{s["package_name"]}</a></td>
                <td class="number">{s["total"] or 0:,}</td>
                <td class="number">{s["last_month"] or 0:,}</td>
                <td class="number">{s["last_week"] or 0:,}</td>
                <td class="number">{s["last_day"] or 0:,}</td>
            </tr>
"""

    html += f"""        </tbody>
    </table>

    <p class="generated">Generated on {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>
</body>
</html>
"""

    with open(output_file, "w") as f:
        f.write(html)
    print(f"Report generated: {output_file}")


def generate_package_html_report(
    package: str,
    output_file: str,
    stats: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
) -> None:
    """Generate a detailed HTML report for a single package.

    Includes download stats, Python version distribution, and OS breakdown.
    """
    print(f"Fetching detailed stats for {package}...")

    # Fetch fresh stats from API if not provided
    if stats is None:
        stats = fetch_package_stats(package)

    if not stats:
        print(f"Could not fetch stats for {package}")
        return

    # Fetch environment data
    py_versions = fetch_python_versions(package)
    os_stats = fetch_os_stats(package)

    # Build pie charts
    py_version_chart = ""
    if py_versions:
        py_data = [
            (v.get("category", "unknown"), v.get("downloads", 0))
            for v in py_versions
            if v.get("category") and v.get("category") != "null"
        ]
        py_version_chart = make_svg_pie_chart(py_data, "py-version-chart", size=220)

    os_chart = ""
    if os_stats:
        os_data = []
        for s in os_stats:
            name = s.get("category", "unknown")
            if name == "null":
                name = "Unknown"
            os_data.append((name, s.get("downloads", 0)))
        os_chart = make_svg_pie_chart(os_data, "os-chart", size=220)

    # Build history chart if available
    history_chart = ""
    if history and len(history) >= 2:
        # Create line chart from history
        chart_width = 600
        chart_height = 200
        margin = {"top": 20, "right": 20, "bottom": 40, "left": 80}
        plot_width = chart_width - margin["left"] - margin["right"]
        plot_height = chart_height - margin["top"] - margin["bottom"]

        sorted_history = sorted(history, key=lambda h: h["fetch_date"])
        dates = [h["fetch_date"] for h in sorted_history]
        values = [h["total"] or 0 for h in sorted_history]
        max_val = max(values) or 1

        svg_parts = [
            f'<svg viewBox="0 0 {chart_width} {chart_height}" '
            f'style="width:100%;max-width:{chart_width}px;height:auto;font-family:system-ui,sans-serif;font-size:11px;">'
        ]

        # Y-axis
        for i in range(5):
            y_val = max_val * (4 - i) / 4
            y_pos = margin["top"] + (i * plot_height / 4)
            svg_parts.append(
                f'<text x="{margin["left"] - 8}" y="{y_pos + 4}" '
                f'text-anchor="end" fill="#666">{int(y_val):,}</text>'
            )
            svg_parts.append(
                f'<line x1="{margin["left"]}" y1="{y_pos}" '
                f'x2="{chart_width - margin["right"]}" y2="{y_pos}" '
                f'stroke="#eee" stroke-width="1"/>'
            )

        # X-axis labels
        if len(dates) > 1:
            for idx in [0, len(dates) // 2, len(dates) - 1]:
                x_pos = margin["left"] + (idx / (len(dates) - 1)) * plot_width
                svg_parts.append(
                    f'<text x="{x_pos}" y="{chart_height - 10}" '
                    f'text-anchor="middle" fill="#666">{dates[idx]}</text>'
                )

        # Line
        points = []
        for i, val in enumerate(values):
            x = margin["left"] + (i / max(1, len(values) - 1)) * plot_width
            y = margin["top"] + plot_height - (val / max_val) * plot_height
            points.append(f"{x:.1f},{y:.1f}")

        svg_parts.append(
            f'<polyline points="{" ".join(points)}" '
            f'fill="none" stroke="hsl(200, 70%, 50%)" stroke-width="2"/>'
        )
        svg_parts.append("</svg>")
        history_chart = "\n".join(svg_parts)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{package} - Download Statistics</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 1000px;
            margin: 0 auto;
            padding: 20px;
            background: #f5f5f5;
        }}
        h1, h2, h3 {{
            color: #333;
        }}
        .chart-container {{
            background: white;
            border-radius: 8px;
            padding: 20px;
            margin: 20px 0;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 20px;
            margin: 20px 0;
        }}
        .stat-card {{
            background: white;
            border-radius: 8px;
            padding: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            text-align: center;
        }}
        .stat-value {{
            font-size: 24px;
            font-weight: bold;
            color: #4a90a4;
        }}
        .stat-label {{
            font-size: 12px;
            color: #666;
            margin-top: 5px;
        }}
        .pie-charts-row {{
            display: flex;
            flex-wrap: wrap;
            gap: 40px;
            justify-content: flex-start;
        }}
        .pie-chart-wrapper {{
            flex: 0 0 auto;
        }}
        .pie-chart-wrapper h3 {{
            margin: 0 0 10px 0;
            font-size: 14px;
            color: #555;
        }}
        .generated {{
            color: #666;
            font-size: 0.9em;
            margin-top: 20px;
        }}
        a {{
            color: #4a90a4;
        }}
    </style>
</head>
<body>
    <h1>{package}</h1>
    <p><a href="https://pypi.org/project/{package}/">View on PyPI</a></p>

    <div class="stats-grid">
        <div class="stat-card">
            <div class="stat-value">{stats["total"]:,}</div>
            <div class="stat-label">Total Downloads</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">{stats["last_month"]:,}</div>
            <div class="stat-label">Last Month</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">{stats["last_week"]:,}</div>
            <div class="stat-label">Last Week</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">{stats["last_day"]:,}</div>
            <div class="stat-label">Last Day</div>
        </div>
    </div>

    {f'<div class="chart-container"><h2>Downloads Over Time</h2>{history_chart}</div>' if history_chart else ""}

    <div class="chart-container">
        <h2>Environment Distribution</h2>
        <div class="pie-charts-row">
            {f'<div class="pie-chart-wrapper"><h3>Python Versions</h3>{py_version_chart}</div>' if py_version_chart else "<p>Python version data not available</p>"}
            {f'<div class="pie-chart-wrapper"><h3>Operating Systems</h3>{os_chart}</div>' if os_chart else "<p>OS data not available</p>"}
        </div>
    </div>

    <p class="generated">Generated on {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>
</body>
</html>
"""

    with open(output_file, "w") as f:
        f.write(html)
    print(f"Report generated: {output_file}")


def cmd_fetch(args: argparse.Namespace) -> None:
    """Fetch command: download stats and store in database."""
    conn = get_db_connection(args.database)
    init_db(conn)

    packages = get_packages(conn)
    if not packages:
        print("No packages are being tracked.")
        print("Add packages with 'pkgdb add <name>' or import from YAML with 'pkgdb import'.")
        conn.close()
        return

    print(f"Fetching stats for {len(packages)} tracked packages...")

    for package in packages:
        print(f"Fetching stats for {package}...")
        stats = fetch_package_stats(package)
        if stats:
            store_stats(conn, package, stats)
            print(
                f"  Total: {stats['total']:,} | Month: {stats['last_month']:,} | "
                f"Week: {stats['last_week']:,} | Day: {stats['last_day']:,}"
            )

    conn.close()
    print("Done.")


def cmd_report(args: argparse.Namespace) -> None:
    """Report command: generate HTML report from stored data.

    If a package name is provided, generates a detailed report for that package.
    Otherwise, generates a summary report for all packages.
    """
    conn = get_db_connection(args.database)
    init_db(conn)

    # Check if single-package report requested
    package = getattr(args, "package", None)

    if package:
        # Single package detailed report
        pkg_history = get_package_history(conn, package, limit=30)
        conn.close()

        # Find stats in database or fetch fresh
        pkg_stats: dict[str, Any] | None = None
        for h in pkg_history:
            if h["package_name"] == package:
                pkg_stats = {
                    "total": h["total"],
                    "last_month": h["last_month"],
                    "last_week": h["last_week"],
                    "last_day": h["last_day"],
                }
                break

        generate_package_html_report(
            package, args.output, stats=pkg_stats, history=pkg_history
        )
    else:
        # Summary report for all packages
        stats = get_latest_stats(conn)
        all_history = get_all_history(conn, limit_per_package=30)
        packages = [s["package_name"] for s in stats]
        conn.close()

        if not stats:
            print("No data in database. Run 'fetch' first.")
            return

        # Fetch environment summary (aggregated across all packages)
        env_summary: dict[str, list[tuple[str, int]]] | None = None
        if args.env:
            print("Fetching environment data (this may take a moment)...")
            env_summary = aggregate_env_stats(packages)

        generate_html_report(stats, args.output, all_history, packages, env_summary)

    print("opening...")
    webbrowser.open_new_tab(Path(args.output).resolve().as_uri())


def cmd_update(args: argparse.Namespace) -> None:
    """Sync command: fetch stats then generate report."""
    cmd_fetch(args)
    cmd_report(args)


def cmd_show(args: argparse.Namespace) -> None:
    """Show command: display stored statistics in terminal."""
    conn = get_db_connection(args.database)
    init_db(conn)

    stats = get_stats_with_growth(conn)
    history = get_all_history(conn, limit_per_package=14)
    conn.close()

    if not stats:
        print("No data in database. Run 'fetch' first.")
        return

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

        rows.append([
            i,
            pkg,
            f"{s['total'] or 0:,}",
            f"{s['last_month'] or 0:,}",
            f"{s['last_week'] or 0:,}",
            f"{s['last_day'] or 0:,}",
            sparkline,
            growth_str,
        ])

    headers = ["#", "Package", "Total", "Month", "Week", "Day", "Trend", "Growth"]
    print(tabulate(rows, headers=headers, tablefmt="simple"))


def cmd_list(args: argparse.Namespace) -> None:
    """List command: show tracked packages."""
    conn = get_db_connection(args.database)
    init_db(conn)

    packages = get_packages(conn)

    if not packages:
        conn.close()
        print("No packages are being tracked.")
        print("Add packages with 'pkgdb add <name>' or import from YAML with 'pkgdb import'.")
        return

    # Get added dates for each package
    cursor = conn.execute(
        "SELECT package_name, added_date FROM packages ORDER BY package_name"
    )
    pkg_data = {row["package_name"]: row["added_date"] for row in cursor.fetchall()}
    conn.close()

    print(f"Tracking {len(packages)} packages:\n")

    rows = [[pkg, pkg_data.get(pkg, "")] for pkg in packages]
    headers = ["Package", "Added"]
    print(tabulate(rows, headers=headers, tablefmt="simple"))


def cmd_add(args: argparse.Namespace) -> None:
    """Add command: add a package to tracking."""
    conn = get_db_connection(args.database)
    init_db(conn)

    if add_package(conn, args.name):
        print(f"Added '{args.name}' to tracking.")
    else:
        print(f"Package '{args.name}' is already being tracked.")

    conn.close()


def cmd_remove(args: argparse.Namespace) -> None:
    """Remove command: remove a package from tracking."""
    conn = get_db_connection(args.database)
    init_db(conn)

    if remove_package(conn, args.name):
        print(f"Removed '{args.name}' from tracking.")
    else:
        print(f"Package '{args.name}' was not being tracked.")

    conn.close()


def cmd_import(args: argparse.Namespace) -> None:
    """Import command: import packages from file (YAML, JSON, or text)."""
    conn = get_db_connection(args.database)
    init_db(conn)

    try:
        added, skipped = import_packages_from_file(conn, args.file)
        print(f"Imported {added} packages ({skipped} already tracked).")
    except FileNotFoundError:
        print(f"File not found: {args.file}")

    conn.close()


def cmd_history(args: argparse.Namespace) -> None:
    """History command: show historical stats for a package."""
    conn = get_db_connection(args.database)
    init_db(conn)

    history = get_package_history(conn, args.package, limit=args.limit)
    conn.close()

    if not history:
        print(f"No data found for package '{args.package}'.")
        return

    print(f"Historical stats for {args.package}\n")

    rows = []
    for h in reversed(history):
        rows.append([
            h["fetch_date"],
            f"{h['total'] or 0:,}",
            f"{h['last_month'] or 0:,}",
            f"{h['last_week'] or 0:,}",
            f"{h['last_day'] or 0:,}",
        ])

    headers = ["Date", "Total", "Month", "Week", "Day"]
    print(tabulate(rows, headers=headers, tablefmt="simple"))


def cmd_export(args: argparse.Namespace) -> None:
    """Export command: export stats in various formats."""
    conn = get_db_connection(args.database)
    init_db(conn)

    stats = get_latest_stats(conn)
    conn.close()

    if not stats:
        print("No data in database. Run 'fetch' first.")
        return

    # Generate export based on format
    if args.format == "csv":
        output = export_csv(stats)
    elif args.format == "json":
        output = export_json(stats)
    elif args.format == "markdown" or args.format == "md":
        output = export_markdown(stats)
    else:
        print(f"Unknown format: {args.format}")
        return

    # Write to file or stdout
    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"Exported to {args.output}")
    else:
        print(output)


def cmd_stats(args: argparse.Namespace) -> None:
    """Stats command: show detailed statistics for a package."""
    package = args.package
    print(f"Fetching detailed stats for {package}...\n")

    # Fetch basic stats
    basic = fetch_package_stats(package)
    if basic:
        print("=== Download Summary ===")
        print(f"  Total:      {basic['total']:>12,}")
        print(f"  Last month: {basic['last_month']:>12,}")
        print(f"  Last week:  {basic['last_week']:>12,}")
        print(f"  Last day:   {basic['last_day']:>12,}")
        print()

    # Fetch Python version breakdown
    py_versions = fetch_python_versions(package)
    if py_versions:
        print("=== Python Version Distribution ===")
        total_downloads = sum(v.get("downloads", 0) for v in py_versions)
        for v in py_versions[:10]:  # Top 10
            version = v.get("category", "unknown")
            downloads = v.get("downloads", 0)
            pct = (downloads / total_downloads * 100) if total_downloads > 0 else 0
            bar = "#" * int(pct / 2)
            print(f"  Python {version:<6} {downloads:>12,} ({pct:>5.1f}%) {bar}")
        print()

    # Fetch OS breakdown
    os_stats = fetch_os_stats(package)
    if os_stats:
        print("=== Operating System Distribution ===")
        total_downloads = sum(s.get("downloads", 0) for s in os_stats)
        for s in os_stats:
            os_name = s.get("category", "unknown")
            if os_name == "null":
                os_name = "Unknown"
            downloads = s.get("downloads", 0)
            pct = (downloads / total_downloads * 100) if total_downloads > 0 else 0
            bar = "#" * int(pct / 2)
            print(f"  {os_name:<10} {downloads:>12,} ({pct:>5.1f}%) {bar}")
        print()


def main() -> None:
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

    # list command
    list_parser = subparsers.add_parser(
        "list",
        help="List tracked packages",
    )
    list_parser.set_defaults(func=cmd_list)

    # import command
    import_parser = subparsers.add_parser(
        "import",
        help="Import packages from file (YAML, JSON, or text)",
    )
    import_parser.add_argument(
        "file",
        nargs="?",
        default=DEFAULT_PACKAGES_FILE,
        help=f"File to import from - supports .yml, .json, or plain text (default: {DEFAULT_PACKAGES_FILE})",
    )
    import_parser.set_defaults(func=cmd_import)

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
    update_parser.set_defaults(func=cmd_update)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()
