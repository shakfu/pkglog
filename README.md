# pkgdb

A tool designed to monitor, collect, and analyze download statistics for PyPI packages, along with GitHub metrics for Python projects.

It retrieves data through the pypistats and GitHub APIs, storing it as historical records in a SQLite database. On the first fetch it backfills roughly 180 days of true daily download data per package, so trends are useful immediately rather than only after weeks of collection. The data can then be analyzed and presented as static HTML reports with visual charts or through an interactive local web interface, compared across time periods, grouped with tags for portfolio rollups, and monitored for download anomalies and milestones.

[Documentation](https://shakfu.github.io/pkgdb/)

## Features

- **True daily download time series** - the first fetch backfills roughly 180 days of real per-day downloads (overall, by Python version, and by OS), so trends are useful immediately. The local store keeps history past the ~180-day pypistats window as it accumulates.

- **Self-owned historical database** - all data lives in a single SQLite file you control: offline, scriptable, no rate-limited web UI.

- **Terminal views** - `show` (trend sparklines and growth %), `diff` (previous fetch / exact week-over-week / month-over-month), `stats` (Python version and OS breakdown).

- **HTML reports** - download charts with a release-timeline overlay (PyPI and GitHub), environment distribution, and per-package project views.

- **Interactive web dashboard** (`serve`) - live from SQLite, with zoomable download and GitHub-stars charts, release markers, and multi-package comparison. No Flask/FastAPI dependency.

- **Anomaly and milestone alerts** (`check`) - weekly spike/drop detection and download-milestone crossings, exiting non-zero for shell/CI notifiers. Milestones measure downloads observed since tracking began and fire once each.

- **Package tags / portfolio rollups** - group related packages and view aggregate download stats per group.

- **GitHub integration** - repository stats (stars, forks, activity, language), a daily metrics snapshot with star-growth tracking, and release history.

- **CI failure scanning** (`ci`) - the latest run of every GitHub Actions workflow across your repositories, failures only, with how long each has been broken. Exits non-zero for shell/CI notifiers. Repositories come from a registry (`repo discover`) rather than from the package list, so projects with no PyPI release are covered too. Authenticates through the `gh` CLI if you are logged in, so there is usually no token to configure. Renders as a terminal table, JSON, a standalone HTML report (`ci -o`), a section of the main report (`report --ci`), or a dashboard panel.

- **Export and badges** - CSV / JSON / Markdown export, shields.io-style SVG badges, and machine-readable `--json` on most commands.

- **Configuration file** - `~/.pkgdb/config.toml` for persistent defaults, including anomaly thresholds, milestones, and CI scan settings.

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

### Quick start

The fastest way to get started is `pkgdb init`, which walks you through setup:

```bash
pkgdb init
```

This prompts for your PyPI username (optional), syncs your packages, fetches current stats, and generates an HTML report -- all in one step. For non-interactive use:

```bash
pkgdb init --user <pypi-username> --no-browser
```

### Configure packages

Create `~/.pkgdb/packages.json` to list packages to track:

```json
["my-package", "another-package"]
```

Or use an object with a `published` key:

```json
{"published": ["my-package", "another-package"]}
```

Alternatively, use `pkgdb add <package>` to add packages individually. By default, packages are verified to exist on PyPI before adding. Use `--no-verify` to skip this check.

### Configuration file

Create `~/.pkgdb/config.toml` to set persistent defaults:

```toml
[defaults]
github = true           # always include GitHub stats
environment = true      # always include environment summary
no_browser = false      # don't auto-open reports
sort_by = "total"       # default sort order (total, month, week, day, growth, name)
# database = "~/.pkgdb/pkg.db"  # custom database path

[report]
# output = "~/.pkgdb/report.html"  # custom report path

[init]
# pypi_user = "myusername"  # default PyPI username for init command

[check]
# milestones = [1000, 10000, 100000, 1000000]  # download targets to watch
# baseline_weeks = 8    # weeks of history used as the anomaly baseline
# z_threshold = 2.5     # std-devs from baseline to flag a spike/drop
# min_weekly = 10       # ignore packages averaging fewer weekly downloads

[github]
# user = "myusername"   # default GitHub user for 'repo discover'

[ci]
# branch = "main"       # scan this branch everywhere (default: each repo's own)
# ignore_workflows = ["nightly*"]  # skip workflows matching these patterns
```

CLI flags always override config values. The config file is optional -- all settings have sensible defaults.

### Commands

```bash
# Guided first-run setup (sync packages, fetch stats, generate report)
pkgdb init

# Non-interactive init with PyPI username
pkgdb init --user <pypi-username>

# Add a package to tracking (verifies it exists on PyPI)
pkgdb add <package-name>

# Add without verification (offline/bulk use)
pkgdb add <package-name> --no-verify

# Remove a package from tracking
pkgdb remove <package-name>

# Show tracked packages (with their tags)
pkgdb packages

# Group packages with tags, then view portfolio rollups
pkgdb tag <package-name> web api    # add one or more tags
pkgdb untag <package-name> api      # remove a tag (or --all)
pkgdb tags                          # per-tag aggregate downloads
pkgdb show --tag web                # filter show to a group, with a group total

# Import packages from a file (JSON or plain text)
pkgdb import packages.json

# Fetch latest stats from PyPI and store in database
# (skips packages already fetched in the last 24 hours)
pkgdb fetch

# Display stats in terminal (includes trend sparklines and growth %)
pkgdb show

# Show package history (HTML report with the daily download chart + releases, opens in browser)
pkgdb history <package-name>

# Show the daily download series as a text table in terminal
pkgdb history <package-name> --text

# Filter history by date (works with both HTML and text output)
pkgdb history <package-name> --since 7d   # last 7 days
pkgdb history <package-name> --since 2w   # last 2 weeks
pkgdb history <package-name> --since 1m   # last month (30 days)
pkgdb history <package-name> --since 2024-01-01

# Compare stats between time periods
pkgdb diff                   # compare to previous fetch
pkgdb diff --period week     # this week vs last week (exact, from daily data)
pkgdb diff --period month    # this month vs last month

# Detect download anomalies (weekly spikes/drops) and milestone crossings
pkgdb check                       # exits non-zero if any event is found
pkgdb check --json                # machine-readable events
pkgdb check --milestone 100000    # watch a download target (repeatable)
pkgdb check --exit-zero           # report but always exit 0

# Show release history for a package (PyPI and GitHub)
pkgdb releases <package-name>

# Show only the most recent 10 releases
pkgdb releases <package-name> --limit 10

# Generate HTML report with charts (opens in browser)
pkgdb report

# Generate detailed HTML report for a single package
pkgdb report <package-name>

# Generate project view with release timeline overlay
pkgdb report <package-name> --project

# Include environment summary (Python versions, OS) in report
pkgdb report -e

# Export stats in various formats
pkgdb export -f csv      # CSV format (default)
pkgdb export -f json     # JSON format
pkgdb export -f markdown # Markdown table

# Show detailed stats for a package (Python versions, OS breakdown)
pkgdb stats <package-name>

# Show database info (size, record counts, date range)
pkgdb show --info

# Generate SVG badge for a package
pkgdb badge <package-name>

# Badge for monthly downloads
pkgdb badge <package-name> --period month

# Save badge to file
pkgdb badge <package-name> -o badge.svg

# Fetch GitHub repository stats (stars, forks, activity, language)
# Records a daily snapshot; shows star growth (Stars Δ) once history accumulates
pkgdb github

# Sort GitHub stats by name or activity instead of stars
pkgdb github fetch --sort name

# Bypass GitHub cache and fetch fresh data
pkgdb github fetch --no-cache

# Show GitHub cache statistics
pkgdb github cache

# Clear expired GitHub cache entries (or --all for everything)
pkgdb github clear

# Register your GitHub repositories for CI scanning (once)
pkgdb repo discover --user <github-user>

# Report failing workflows; exits non-zero if any are failing, so it gates a
# shell or cron step. Run listings are cached for an hour
pkgdb ci

# Include passing workflows, or scan one repository directly
pkgdb ci --all
pkgdb ci --repo owner/name

# Machine-readable, or report without affecting the exit code
pkgdb ci --json
pkgdb ci --exit-zero

# Write an HTML CI report to ~/.pkgdb/ci-report.html and open it
pkgdb ci -o

# Add a CI section to the main report. Always exits 0, and needs stored download
# stats -- with none it writes nothing at all
pkgdb report --ci

# Inspect the scan registry
pkgdb repo list

# Launch interactive web dashboard (opens browser)
pkgdb serve

# Serve on a custom port without opening browser
pkgdb serve --port 3000 --no-browser

# Fetch stats and generate report in one step
# (skips packages already fetched in the last 24 hours)
pkgdb update

# Fetch stats and generate report with environment summary
pkgdb update -e

# Include GitHub stats in fetch, report, or update
pkgdb fetch --github
pkgdb report --github
pkgdb update --github

# Clean up orphaned stats (for packages no longer tracked)
pkgdb cleanup

# Prune stats older than N days
pkgdb cleanup --days 365

# Sync packages from PyPI user account (initial or refresh)
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

# Limit history to N days
pkgdb history my-package -n 14

# History report to file without opening browser
pkgdb history my-package -o history.html --no-browser

# Show history since a specific date (or relative: 7d, 2w, 1m)
pkgdb history my-package --since 2024-01-01
pkgdb history my-package --since 7d

# Skip package verification on import
pkgdb import packages.json --no-verify

# Limit show output to top N packages
pkgdb show --limit 10

# Sort show output by field (total, month, week, day, growth, name)
pkgdb show --sort-by month

# JSON output (available on show, packages, history, stats, cleanup, github)
pkgdb show --json
pkgdb packages --json
pkgdb history my-package --json
pkgdb stats my-package --json
pkgdb cleanup --json
pkgdb github --json

# Export to file instead of stdout
pkgdb export -f json -o stats.json
```

## Architecture

Modular CLI application with the following commands:

**Setup:**
- **init**: Guided first-run setup (sync packages, fetch stats, generate report)

**Package management:**
- **add**: Add a package to tracking

- **remove**: Remove a package from tracking

- **packages**: Show tracked packages with their added dates and tags

- **import**: Import packages from file (JSON or text)

- **sync**: Sync packages from a PyPI user account (with optional `--prune`)

- **tag** / **untag** / **tags**: Group packages with tags and view per-group (portfolio) rollups

**Data operations:**
- **fetch**: Fetch download stats from PyPI and store in SQLite (with `-g` for GitHub stats). Also backfills roughly 180 days of true daily downloads per package on the first fetch

- **show**: Display stats in terminal with trend sparklines and growth %; `--tag` filters to a group with an aggregate total

- **diff**: Compare download stats between time periods (previous fetch, week-over-week, month-over-month). Week/month comparisons use exact daily sums

- **check**: Detect download anomalies (weekly spikes/drops) and milestone crossings; exits non-zero on any event for CI/notifier use. Milestones track downloads observed since tracking began (the accumulated daily series, not a lifetime total) and each one is announced once

- **history**: Show package history as HTML report (default) or text table (`--text`), using the true daily download series

- **stats**: Show detailed breakdown (Python versions, OS) for a package

- **releases**: Show release history for a package (PyPI and GitHub)

- **github**: Fetch and display GitHub repository stats (stars, forks, activity, language). Records a daily snapshot and shows star growth

- **export**: Export stats in CSV, JSON, or Markdown format

**CI monitoring:**
- **repo**: Manage the repository registry that `ci` scans. `discover --user <name>` registers an account's repositories, records how many workflows each defines, and links tracked packages to same-named repositories; `add` / `remove` / `list` manage it by hand

- **ci**: Report the latest run of every GitHub Actions workflow in the registered repositories, failures only unless `--all`. Exits non-zero when any workflow is failing, for CI/notifier use. Each repository is scanned on its own default branch, and each failure carries the age of its streak. `-o` also writes an HTML report

**Reporting:**
- **report**: Generate HTML report with SVG charts. With `-e` flag, includes Python/OS summary. With `-g` flag, includes GitHub stats (stars, forks, language, activity) in the table. With `-c` flag, includes a CI status section. With package argument, generates detailed single-package report. With `-p/--project` flag, generates project view with release timeline overlay

- **badge**: Generate shields.io-style SVG badge for a package

- **update**: Run fetch then report in one step (supports `-e` for environment summary, `-g` for GitHub stats, `-c` for CI status)

- **serve**: Launch interactive web dashboard with live data from SQLite. Overview with sortable/filterable stats table and a CI failure panel, package detail with zoomable download and GitHub-stars charts plus release markers, comparison with multi-package overlay

**Maintenance:**
- **cleanup**: Remove orphaned stats and optionally prune old data

- **version**: Show pkgdb version

### Data flow

```
packages.json -> pypistats + GitHub APIs -> SQLite (pkg.db) -> terminal / HTML reports / web dashboard / anomaly checks
repo registry -> GitHub Actions API ----^                                              / CI reports
```

### Database schema

The `package_stats` table stores:
- `package_name`: Package identifier

- `fetch_date`: Date stats were fetched (YYYY-MM-DD)

- `last_day`, `last_week`, `last_month`: Recent download counts

- `total`: Total downloads (excluding mirrors)

The `daily_downloads` table stores the true per-day download time series:
- `package_name`: Package identifier

- `date`: Download date (YYYY-MM-DD)

- `dimension`: `overall` (mirror split), `python` (version), or `os`

- `category`: e.g. "without_mirrors", "3.12", or "Linux"

- `downloads`: Downloads on that date for that category

This series is backfilled (~180 days) from pypistats on the first fetch and drives the history/report charts, the exact `diff` period math, and growth %. Unlike the snapshot table it survives past the ~180-day pypistats window as it accumulates locally.

The `python_version_stats` and `os_stats` tables cache the latest environment breakdown:
- `package_name`: Package identifier

- `fetch_date`: Date stats were fetched (YYYY-MM-DD)

- `category`: Python version (e.g. "3.12") or OS name (e.g. "Linux")

- `downloads`: Download count for that category

The `fetch_attempts` table tracks API requests:
- `package_name`: Package identifier (primary key)

- `attempt_time`: ISO timestamp of last fetch attempt

- `success`: Whether the fetch succeeded (1) or failed (0)

The `github_cache` table caches GitHub API responses:
- `repo_key`: Lowercased `owner/repo` identifier (primary key)

- `data`: Full JSON response from the GitHub API

- `fetched_at`: When the response was cached

- `expires_at`: Cache expiry time (default: 24 hours)

The `pypi_releases` table caches PyPI release history:
- `package_name`: Package identifier

- `version`: Release version string

- `upload_date`: Date the version was uploaded (YYYY-MM-DD)

The `github_releases` table caches GitHub release history:
- `repo_key`: Lowercased `owner/repo` identifier

- `tag_name`: Release tag (e.g. "v0.1.0")

- `published_at`: Date the release was published (YYYY-MM-DD)

The `release_cache` table tracks freshness of release data:
- `cache_key`: Cache identifier (e.g. "pypi:my-package" or "github:owner/repo")

- `fetched_at`: When the data was last fetched

- `expires_at`: Cache expiry time (default: 24 hours)

The `github_stats_history` table stores a daily snapshot of GitHub metrics:
- `package_name`: Package identifier

- `repo_key`: Lowercased `owner/repo` identifier

- `date`: Snapshot date (YYYY-MM-DD)

- `stars`, `forks`, `open_issues`, `watchers`: Repository counts on that date, with `open_issues` as GitHub reports it (open pull requests included)

- `open_issues_excl_prs`: Open issues with pull requests excluded, from the search API. Null when the count was unavailable, and for rows recorded before this column existed

The `package_tags` table groups packages:
- `package_name`: Package identifier

- `tag`: Normalized (lowercased) tag name

The `github_repos` table is the registry of repositories that `ci` scans:
- `repo_key`: Lowercased `owner/repo` identifier (primary key)

- `source`: How it was registered - `package`, `discover`, or `manual`

- `has_workflows`: Workflow count from discovery. Null until probed, and repositories are scanned while unknown

- `enabled`: Whether scans include it

- `default_branch`: Branch the scan uses when none is given

The `package_repos` table links packages to repositories:
- `package_name`, `repo_key`: Many-to-one - several published packages can be built from one repository

The `github_ci_status` table stores the latest state of each workflow:
- `repo_key`, `workflow_name`: Primary key. A repository with no runs on the scanned branch stores one row under the name `-`

- `state`: `PASS`, `FAIL`, `RUNNING`, `NO_RUNS`, or the GitHub conclusion upcased (`CANCELLED`, `SKIPPED`)

- `branch`, `run_id`, `run_url`, `run_started_at`: The run the state came from

- `first_failed_at`: Start of the current failing streak, and the one value a scan cannot recompute from the API. Carried across cancelled and in-progress runs, cleared only by a pass

- `checked_at`: When the scan last saw this workflow

Stats are upserted per package per day. Fetch attempts are tracked to avoid hitting PyPI rate limits - packages are only fetched once per 24-hour period. The daily download series is backfilled (~180 days) on first fetch and refreshed idempotently on each run. Environment stats are cached alongside download stats, so reports can be generated offline. GitHub API responses are cached for 24 hours to minimize API calls, while a daily GitHub metrics snapshot accumulates for star/fork history. Release data (PyPI and GitHub) is cached for 24 hours. Workflow run listings share `github_cache` under a per-branch key but expire after one hour, because CI state changes on every push. A scan forgets workflows it did not see, so a renamed or deleted workflow stops reporting its last failure; a repository it could not reach keeps whatever was recorded, because unreachable is unknown rather than empty.

## Files

Source modules in `src/pkgdb/`:
- `__init__.py`: Public API and version

- `cli.py`: CLI argument parsing and commands

- `config.py`: Configuration file loading (`~/.pkgdb/config.toml`)

- `service.py`: High-level service layer

- `db.py`: Database operations and context manager

- `api.py`: pypistats API wrapper with parallel fetching (incl. daily series)

- `checks.py`: anomaly and milestone detection for `pkgdb check`

- `reports.py`: HTML/SVG report generation

- `server.py`: HTTP server for the interactive web dashboard

- `dashboard.py`: HTML page templates for the dashboard (overview, detail, comparison)

- `github.py`: GitHub API client with caching and rate limit handling, including Actions workflow runs for `pkgdb ci`

- `badges.py`: SVG badge generation

- `export.py`: CSV/JSON/Markdown export

- `utils.py`: Helper functions and validation

- `types.py`: TypedDict definitions for type safety

- `logging.py`: Logging configuration

Data files (all in `~/.pkgdb/`):
- `config.toml`: Configuration file for persistent defaults (optional)

- `packages.json`: Package list configuration (optional, can use `add` command instead)

- `pkg.db`: SQLite database (auto-created)

- `report.html`: Generated HTML report (default output)

- `ci-report.html`: Generated CI status report (`pkgdb ci -o`)

## GitHub Actions

An example workflow is provided at `.github/workflows/fetch-stats.yml.example` for automated daily stats fetching. To use it:

1. Copy to `.github/workflows/fetch-stats.yml` (remove `.example`)

2. Configure your package list or PyPI username

3. The workflow will fetch stats daily and commit updates to your repo

The example also runs `pkgdb check` each day so download spikes, drops, and milestone crossings show up automatically. Because `pkgdb fetch` backfills roughly 180 days of daily history on the first run, anomaly detection is useful immediately rather than only after weeks of collection. Drop the step's `|| true` to turn any detected event into a failed run (a red X and GitHub's usual failure notification), or pipe the JSON output to Slack, email, or an issue.

## Documentation

API documentation is built with MkDocs:

```bash
# Build docs
make docs

# Serve locally with live reload
make docs-serve

# Deploy to GitHub Pages
make docs-deploy
```

Then open `http://127.0.0.1:8000` to browse the docs locally.

## Development

```bash
# Install dev dependencies
uv sync

# Run tests
pytest

# Run tests with verbose output
pytest -v

# Full QA (test + lint + typecheck + format)
make qa
```

## Dependencies

Runtime:
- `pypistats`: PyPI download statistics API client

- `tabulate`: Terminal table formatting

Development:
- `pytest`: Testing framework

Documentation:
- `mkdocs`: Static site generator

- `mkdocs-material`: Material theme

- `mkdocstrings[python]`: Auto-generated API docs from docstrings
