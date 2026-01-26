# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.4]

### Added

- `list` alias for `packages` subcommand: `pkgdb list` now works as an alias for `pkgdb packages`

### Fixed

- Graceful handling of HTTP 404 errors during fetch: packages not found on PyPI stats no longer crash the entire fetch operation; they are logged as warnings and counted as failed

## [0.1.3]

### Added

- `version` subcommand: `pkgdb version` displays the package version
- `init` command: `pkgdb init --user <username>` auto-populates packages from a PyPI user account
- `show` command enhancements:
  - `--limit N` to show only top N packages
  - `--sort-by` option to sort by total, month, week, day, growth, or name
  - `--json` flag for machine-readable JSON output
- `history` command: `--since DATE` flag to filter history by date (YYYY-MM-DD)
- `--no-browser` flag for `report` and `update` commands (useful for automation/cron)
- Progress indicator during fetch: `[1/27] Fetching stats for package...`
- Database context manager `get_db()` for safer resource handling
- Service layer `PackageStatsService` for decoupled, testable operations
- Dataclasses: `PackageInfo`, `FetchResult`, `PackageDetails`
- Package name validation: `validate_package_name()` enforces PyPI naming conventions
- Logging module with `-v/--verbose` and `-q/--quiet` flags
- TypedDict types for type safety: `PackageStats`, `CategoryDownloads`, `EnvSummary`, `HistoryRecord`, `StatsWithGrowth`
- Parallel API fetching with `fetch_all_package_stats()` and improved `aggregate_env_stats()`
- `cleanup` command with `--orphans` and `--prune` flags for database maintenance
- Database functions: `cleanup_orphaned_stats()`, `prune_old_stats()`
- Service methods: `cleanup()`, `prune()`
- Named constants for theme colors (`THEME_PRIMARY_COLOR`), chart dimensions, limits (`PIE_CHART_MAX_ITEMS`, `LINE_CHART_MAX_SERIES`), and sparkline parameters (`SPARKLINE_WIDTH`, `SPARKLINE_CHARS`)
- Integration tests (require network, run with `RUN_INTEGRATION=1 pytest -m integration`)
- Performance tests (run with `RUN_SLOW_TESTS=1 pytest -m slow`)
- Edge case tests for chart generation (boundary conditions, single data points, large numbers)
- Error path tests (invalid files, partial API failures, database edge cases)
- Output path validation: `validate_output_path()` checks for path traversal, sensitive directories, file extensions, and write permissions
- Batch stats storage: `store_stats_batch()` for efficient multi-package inserts with single commit
- 98 new tests (167 total, 8 skipped by default)

### Changed

- **BREAKING**: Default config file changed from `packages.yml` to `~/.pkgdb/packages.json`
- **BREAKING**: Renamed `list` command to `packages` for clarity (`pkgdb packages`)
- Removed `pyyaml` dependency - now uses stdlib `json` only
- All data files now consistently use `~/.pkgdb/` directory (packages.json, pkg.db, report.html)
- Service `fetch_all_stats()` now uses batch commits for better performance
- Service report/export methods validate output paths before writing
- Narrowed exception handling in API functions to specific exceptions (`JSONDecodeError`, `URLError`, `ValueError`, `KeyError`, `TypeError`, `OSError`) instead of bare `except` - improves debugging
- Replaced print statements with Python logging throughout CLI/API/reports
- Modular architecture: split monolithic `__init__.py` into focused modules:
  - `utils.py` - Helper functions (sparkline, growth calculation)
  - `export.py` - CSV/JSON/Markdown export
  - `api.py` - pypistats API wrapper functions (now with parallel fetching)
  - `db.py` - Database operations and context manager
  - `service.py` - High-level service layer abstraction
  - `cli.py` - CLI argument parsing and commands
  - `reports.py` - HTML/SVG report generation
  - `logging.py` - Logging configuration with verbose/quiet modes
  - `types.py` - TypedDict definitions for type safety
  - `__init__.py` - Public API re-exports
- All CLI commands now use context manager for database connections
- Refactored `reports.py` to extract shared components:
  - `_render_html_document()` for HTML boilerplate
  - `_make_single_line_chart()` for single-series line charts
  - `_make_multi_line_chart()` for multi-package time-series charts
  - `_build_env_charts()` for Python version and OS pie charts
  - Eliminated ~110 lines of duplicated CSS and SVG chart code

### Fixed

- N+1 query performance issue in `get_stats_with_growth()`: now uses single query via `get_all_history()` instead of one query per package

## [0.1.2]

### Added

- HTML report enhancements:
  - `pkgdb report <package>` generates detailed single-package report with download stats, history chart, Python version and OS distribution pie charts
  - `pkgdb report -e` includes aggregated Python version and OS distribution summary in the main report
- New functions: `make_svg_pie_chart`, `aggregate_env_stats`, `generate_package_html_report`
- 14 new tests for pie charts, environment aggregation, and package reports (69 total)

- `stats` command for detailed package statistics:
  - Python version distribution with visual bars
  - Operating system breakdown (Linux, Windows, Darwin)
  - Download summary (total, month, week, day)
- New functions: `fetch_python_versions`, `fetch_os_stats`

### Note

- Per-version (package version) downloads not available through pypistats API

## [0.1.1]

### Added

- `export` command with support for multiple formats:
  - CSV (`pkgdb export -f csv`)
  - JSON (`pkgdb export -f json`)
  - Markdown (`pkgdb export -f markdown`)
- Export to file with `-o` option or stdout by default
- New functions: `export_csv`, `export_json`, `export_markdown`

- `history` command to view historical stats for a specific package
- Growth metrics (month-over-month percentage change) in `list` output
- Sparkline trend indicators in `list` output
- Time-series chart in HTML report showing downloads over time (top 5 packages)
- New functions: `get_package_history`, `get_all_history`, `calculate_growth`, `make_sparkline`

### Changed

- `list` command now shows trend sparklines and growth percentages
- HTML report now includes "Downloads Over Time" chart when historical data available

## [0.1.0]

### Added

- Initial release
- CLI commands: `fetch`, `list`, `report`, `update`
- SQLite database storage for historical stats
- HTML report generation with SVG visualizations
- YAML-based package configuration (`packages.yml`)
- Support for custom database and packages file paths
- Pytest test suite with 24 tests covering:
  - Database operations
  - Package loading from YAML
  - Statistics storage and retrieval
  - HTML report generation
  - CLI argument parsing
