# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.4]

### Added

- HTML report enhancements:
  - `pkgdb report <package>` generates detailed single-package report with download stats, history chart, Python version and OS distribution pie charts
  - `pkgdb report -e` includes aggregated Python version and OS distribution summary in the main report
- New functions: `make_svg_pie_chart`, `aggregate_env_stats`, `generate_package_html_report`
- 14 new tests for pie charts, environment aggregation, and package reports (69 total)

## [0.1.3]

### Added

- `stats` command for detailed package statistics:
  - Python version distribution with visual bars
  - Operating system breakdown (Linux, Windows, Darwin)
  - Download summary (total, month, week, day)
- New functions: `fetch_python_versions`, `fetch_os_stats`

### Note

- Per-version (package version) downloads not available through pypistats API

## [0.1.2]

### Added

- `export` command with support for multiple formats:
  - CSV (`pkgdb export -f csv`)
  - JSON (`pkgdb export -f json`)
  - Markdown (`pkgdb export -f markdown`)
- Export to file with `-o` option or stdout by default
- New functions: `export_csv`, `export_json`, `export_markdown`

## [0.1.1]

### Added

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
