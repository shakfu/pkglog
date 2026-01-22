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

Edit `packages.yml` to list the packages you want to track:

```yaml
published:
  - my-package
  - another-package
```

### Commands

```bash
# Fetch latest stats from PyPI and store in database
pkgdb fetch

# Display stats in terminal (includes trend sparklines and growth %)
pkgdb list

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
```

### Options

```bash
# Use custom database file
pkgdb -d custom.db fetch

# Use custom packages file
pkgdb -p custom.yml fetch

# Specify output file for report
pkgdb report -o custom-report.html

# Limit history output to N days
pkgdb history my-package -n 14

# Export to file instead of stdout
pkgdb export -f json -o stats.json
```

## Architecture

Single-file CLI application with seven commands:

- **fetch**: Calls `pypistats.recent()` and `pypistats.overall()` for each package, stores results in SQLite
- **list**: Queries latest stats per package with trend sparklines and growth metrics
- **history**: Shows historical data for a specific package over time
- **report**: Generates self-contained HTML report with SVG charts (bar charts + time-series). With `-e` flag, includes Python version and OS distribution summary. With package argument, generates detailed single-package report
- **export**: Exports stats in CSV, JSON, or Markdown format
- **stats**: Shows detailed breakdown (Python versions, OS) for a single package
- **update**: Runs fetch then report

### Data flow

```
packages.yml -> pypistats API -> SQLite (pkg.db) -> HTML/terminal output
```

### Database schema

The `package_stats` table stores:
- `package_name`: Package identifier
- `fetch_date`: Date stats were fetched (YYYY-MM-DD)
- `last_day`, `last_week`, `last_month`: Recent download counts
- `total`: Total downloads (excluding mirrors)

Stats are upserted per package per day, so running fetch multiple times on the same day updates rather than duplicates.

## Files

- `pkgdb.py`: Main CLI application
- `packages.yml`: Package list configuration
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
- `pyyaml`: YAML configuration parsing
- `tabulate`: Terminal table formatting

Development:
- `pytest`: Testing framework
