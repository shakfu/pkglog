# pkgdb

Track, store, and analyze PyPI package download statistics.

Fetches download stats via the pypistats API, stores historical data in SQLite, and generates HTML reports with charts.

## Installation

```sh
pip install pkgdb
```

To build:

Requires Python 3.10+. Uses [uv](https://github.com/astral-sh/uv) for dependency management.

```bash
uv sync
```

## Usage

### Configure packages

Create `~/.pkgdb/packages.json` to list packages to track:

```json
["my-package", "another-package"]
```

Or use an object with a `published` key:

```json
{"published": ["my-package", "another-package"]}
```

Alternatively, use `pkgdb add <package>` to add packages individually.

### Commands

```bash
# Add a package to tracking
pkgdb add <package-name>

# Remove a package from tracking
pkgdb remove <package-name>

# Show tracked packages
pkgdb packages

# Import packages from a file (JSON or plain text)
pkgdb import packages.json

# Fetch latest stats from PyPI and store in database
pkgdb fetch

# Display stats in terminal (includes trend sparklines and growth %)
pkgdb show

# Show historical stats for a specific package
pkgdb history <package-name>

# Generate HTML report with charts (opens in browser)
pkgdb report

# Generate detailed HTML report for a single package
pkgdb report <package-name>

# Include environment summary (Python versions, OS) in report
pkgdb report -e

# Export stats in various formats
pkgdb export -f csv      # CSV format (default)
pkgdb export -f json     # JSON format
pkgdb export -f markdown # Markdown table

# Show detailed stats for a package (Python versions, OS breakdown)
pkgdb stats <package-name>

# Fetch stats and generate report in one step
pkgdb update

# Clean up orphaned stats (for packages no longer tracked)
pkgdb cleanup

# Prune stats older than N days
pkgdb cleanup --days 365

# Auto-populate packages from PyPI user account
pkgdb init --user <pypi-username>

# Refresh package list from PyPI user (add new packages since last sync)
pkgdb sync --user <pypi-username>

# Sync and remove packages no longer in user's PyPI account
pkgdb sync --user <pypi-username> --prune

# Show version
pkgdb version
```

### Options

```bash
# Use custom database file
pkgdb -d custom.db fetch

# Verbose output (show debug messages)
pkgdb -v fetch

# Quiet mode (only show warnings/errors)
pkgdb -q fetch

# Specify output file for report
pkgdb report -o custom-report.html

# Generate report without opening browser (useful for automation)
pkgdb report --no-browser

# Limit history output to N days
pkgdb history my-package -n 14

# Show history since a specific date
pkgdb history my-package --since 2024-01-01

# Limit show output to top N packages
pkgdb show --limit 10

# Sort show output by field (total, month, week, day, growth, name)
pkgdb show --sort-by month

# Output show in JSON format
pkgdb show --json

# Export to file instead of stdout
pkgdb export -f json -o stats.json
```

## Architecture

Modular CLI application with the following commands:

**Package management:**
- **add**: Add a package to tracking
- **remove**: Remove a package from tracking
- **packages**: Show tracked packages with their added dates
- **import**: Import packages from file (JSON or text)
- **init**: Auto-populate packages from a PyPI user account
- **sync**: Refresh package list from a PyPI user account (adds new packages only)

**Data operations:**
- **fetch**: Fetch download stats from PyPI and store in SQLite
- **show**: Display stats in terminal with trend sparklines and growth %
- **history**: Show historical data for a specific package
- **stats**: Show detailed breakdown (Python versions, OS) for a package
- **export**: Export stats in CSV, JSON, or Markdown format

**Reporting:**
- **report**: Generate HTML report with SVG charts. With `-e` flag, includes Python/OS summary. With package argument, generates detailed single-package report
- **update**: Run fetch then report in one step

**Maintenance:**
- **cleanup**: Remove orphaned stats and optionally prune old data
- **version**: Show pkgdb version

### Data flow

```
packages.json -> pypistats API -> SQLite (pkg.db) -> HTML/terminal output
```

### Database schema

The `package_stats` table stores:
- `package_name`: Package identifier
- `fetch_date`: Date stats were fetched (YYYY-MM-DD)
- `last_day`, `last_week`, `last_month`: Recent download counts
- `total`: Total downloads (excluding mirrors)

Stats are upserted per package per day, so running fetch multiple times on the same day updates rather than duplicates.

## Files

Source modules in `src/pkgdb/`:
- `__init__.py`: Public API and version
- `cli.py`: CLI argument parsing and commands
- `service.py`: High-level service layer
- `db.py`: Database operations and context manager
- `api.py`: pypistats API wrapper with parallel fetching
- `reports.py`: HTML/SVG report generation
- `export.py`: CSV/JSON/Markdown export
- `utils.py`: Helper functions and validation
- `types.py`: TypedDict definitions for type safety
- `logging.py`: Logging configuration

Data files (all in `~/.pkgdb/`):
- `packages.json`: Package list configuration (optional, can use `add` command instead)
- `pkg.db`: SQLite database (auto-created)
- `report.html`: Generated HTML report (default output)

## Development

```bash
# Install dev dependencies
uv sync

# Run tests
pytest

# Run tests with verbose output
pytest -v
```

## Dependencies

Runtime:
- `pypistats`: PyPI download statistics API client
- `tabulate`: Terminal table formatting

Development:
- `pytest`: Testing framework
