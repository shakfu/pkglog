# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `pkgdb ci` - report the latest run of every GitHub Actions workflow across your repositories

  - Failing workflows only unless `--all`, and exits 1 when any is failing, so it composes with shell and CI notifiers the way `check` does. `--repo owner/name` scans one repository without registering it; `--branch`, `--json`, `--no-cache`, `--exit-zero` and `-o` (write an HTML report) round it out

  - Each repository is scanned on its own default branch, looked up once and stored. Reporting every branch makes a red run on someone's feature branch indistinguishable from broken CI

  - `github_ci_status` records the latest state per `(repo, workflow)` with a `first_failed_at` streak start - the one value a scan cannot recompute from the API, and what separates a break from twenty minutes ago from one that has stood for months. It survives cancelled and in-progress runs, neither of which says the failure is over, and clears only on a pass. Runs are grouped by `workflow_id` rather than name, because a workflow setting `run-name` reports a different name per run

- `pkgdb repo` and a repository registry (`github_repos`, `package_repos`), populated by `repo discover --user <name>`, `repo add`, and every `github fetch`

  - Repositories were previously reachable only by resolving a tracked package through its PyPI metadata, which covers just the projects published to PyPI with a repository URL recorded. On the author's account that reached 28 of the 64 repositories that run CI

  - `discover` records each repository's workflow count so later scans skip the ones with no CI, probing only repositories it has not seen before. Your own account is listed through `/user/repos`, because `/users/{user}/repos` returns public repositories only even with a token. It also links tracked packages to same-named repositories, recovering those whose PyPI metadata carries no repository URL: 41 of 46 packages linked, against 31 through the PyPI path alone

  - A first `pkgdb ci` with an empty registry seeds it from the repository keys previous GitHub fetches left in `github_cache` and `github_stats_history`, without a network call

- CI status in the HTML report and the dashboard

  - `pkgdb ci -o [PATH]` writes a standalone report (default `~/.pkgdb/ci-report.html`); `report --ci` and `update --ci` add the same section to the main report. Both render from the recorded scan, so a report can be regenerated without touching GitHub. Workflow names come from arbitrary repository YAML and are escaped

  - `serve` gains an `/api/ci` endpoint and an overview panel of failing workflows. It renders the last recorded scan and never starts one, because a cold scan takes seconds and the page must not block on it. The panel stays hidden until a scan exists rather than prompting for one

- `[github] user` and `[ci] branch` / `[ci] ignore_workflows` config keys

- Run listings are cached in `github_cache` under a per-branch key, expiring after one hour rather than the 24h used for repository metadata, because CI state changes on every push. On a 64-repository account a cold scan takes about 11 seconds and a repeat scan about 0.2

### Fixed

- An exhausted GitHub rate limit is no longer retried. A 403 carrying `x-ratelimit-remaining: 0` means the quota is gone until the reset, up to an hour away, so the backoff was sleeping through a wait it could not outlast - once per repository, over the whole registry. Permission errors and secondary limits, which lack that header, still retry as before

- Running without a token now says so, once per process: `pkgdb ci` unauthenticated runs out of quota partway through any real account and reports the repositories it could not reach as unknown, which looked like a GitHub fault rather than a missing credential. Exhaustion is reported the same way, with its reset time

- A workflow that is renamed or deleted no longer keeps reporting its last failure. Rows are keyed by workflow name, so a rename left the old row in place and the report showed a failure for a workflow that no longer exists. Each successful repository scan now drops the rows it did not see; a failed fetch prunes nothing, because unreachable is unknown rather than empty

- A repository with workflows but no runs on the scanned branch is recorded rather than only displayed. It produced no row, so the scan and the report disagreed on how many repositories were covered - 64 against 55 on the author's account. Such repositories now store a single `NO_RUNS` row under the workflow name `-`

### Changed

- `get_github_token()` falls back to the token an authenticated `gh` CLI already holds, after `GITHUB_TOKEN` and `GH_TOKEN`

  - Unauthenticated GitHub allows 60 requests an hour, which one `pkgdb ci` pass over a 64-repository account exhausts, so authentication is effectively mandatory for the new commands. Requiring a hand-made token to reach what `gh` is already logged in for is friction with no security benefit

  - Resolved once per process with a 10-second timeout, querying `--hostname github.com` explicitly so a GitHub Enterprise default host does not answer. Neither the token nor gh's stderr is logged, and `gh` stays optional: absent or logged out, the lookup returns nothing

### Removed

- `scripts/wflw-scan.sh`, replaced by `pkgdb ci`

  - The script derived its repository set from `github_cache`, which is expiring cache state rather than a record of what you own: `pkgdb github clear --all` left it scanning nothing, and the suffixed keys written for the issues-only count would have been passed to `gh run list` as repository names. It also required `gh` and `jq`, reported passing workflows on every branch, always exited 0, and kept no state, so a workflow broken for a month looked like one broken this morning

## [0.2.3]

### Added

- Open issue counts, excluding pull requests, in the HTML report, the `github` command, and the recorded history

  - GitHub's repository endpoint reports `open_issues_count`, which counts open pull requests as issues; a busy PR queue can make that figure several times the real issue backlog. New `fetch_open_issue_count()` asks the search API for `is:issue is:open` instead, and `RepoStats` gains `open_issues_excl_prs` to carry it. The raw `open_issues` field keeps its GitHub meaning

  - The `update` and `report` HTML table gains an "Open Issues" column, the `github` table gains an "Issues" column, and `github_stats_history` gains an `open_issues_excl_prs` column recorded on every GitHub fetch. `github --json` gains an `open_issues_excl_prs` key beside the existing `open_issues`, which keeps its GitHub meaning

  - `github_stats_history.open_issues_excl_prs` is added to existing databases by `init_db()`, through a new additive-migration helper. It is nullable: rows recorded before this release read as unknown rather than zero, and so do rows whose count could not be fetched. `store_github_stats_snapshot()` gains a matching optional argument, and `get_github_stats_history()` returns the column

  - The count is cached in `github_cache` under its own key with the same 24h TTL, so a warm cache costs no requests. A repository whose data is already cached still gets its issue count looked up

  - The search API has a tighter rate limit than the rest of the GitHub API (30 requests/minute authenticated, 10 unauthenticated), so this lookup is the likeliest part of a fetch to come back empty. When it does, the column shows `-` rather than falling back to a number that includes pull requests. Setting `GITHUB_TOKEN`/`GH_TOKEN` raises the limit

  - Counts link to the repository's issue list

### Changed

- Project names in the `update` (and `report`) HTML table now link to the git repository

  - The name cell previously always pointed at the PyPI project page, which duplicated the "View on PyPI" route already reachable from the report while leaving the repository one column away

  - The repo link only exists when the report is generated with `-g/--github` (or `[defaults] github = true`); packages with no repository data keep their PyPI link, as do all rows when GitHub stats are not requested

  - `RepoStats` gains a `repo_url` property, replacing the URL string the report was assembling from `full_name` inline

## [0.2.2]

### Fixed

- Time windows measured against the database clock are now the length they claim

  - Timestamps were written with `datetime.now()` (local) and then compared against SQLite's `datetime('now')` (always UTC), so every such window ran for 24 hours plus the machine's UTC offset. East of UTC packages stayed throttled well past a day -- `update` reported "Skipped N packages (already fetched in last 24 hours)" more than 24 hours after the last fetch -- while west of UTC the window expired early, refetching into the PyPI rate limit the cooldown exists to avoid

  - Affected the per-package fetch cooldown (`record_fetch_attempt`), the GitHub API response cache TTL (`store_cached_repo_data`), and `RepoStats.days_since_push`, which measured a UTC `pushed_at` against a local clock and so could report an age off by a day, changing `activity_status`. The `release_cache` TTL was already UTC on both sides

  - `get_next_update_seconds()` computed its countdown entirely in local time. That was internally consistent but disagreed with the query gating the fetch, so `show` could report an update as due while `update` skipped it

  - New `utcnow()` in `pkgdb.utils` is what these paths now write

  - Attempt rows written by an earlier version keep their local timestamps and are read as UTC, so a machine east of UTC may see one final cooldown up to its offset too long; the next successful fetch rewrites them

- `prune_old_stats()` no longer prunes a day early or a day late

  - The dated tables are stamped on the local calendar, but the cutoff was `date('now', '-N days')`, which is the UTC calendar. For the part of each day when the two disagree, `cleanup --prune` deleted a day more or a day less of history than asked. The cutoff is now computed on the same calendar the rows are dated on

## [0.2.1]

### Fixed

- Removed and pruned packages no longer appear in stats, reports, exports, or history

  - `remove` and `sync --prune` deliberately retain a package's collected data so that `cleanup` stays the single physical purge, but the read paths did not filter on the `packages` table -- a removed package kept showing up in `show`, `report`, `export`, `history`, `diff --period latest`, badges, and the `serve` dashboard until `cleanup` was run

  - `get_latest_stats()`, `get_package_history()`, `get_all_history()`, `get_stats_with_growth()`, and `get_daily_downloads()` gain a `tracked_only` option; `PackageStatsService` enables it on every read, so the database functions keep their raw behavior by default

- `history` now applies `--since` and `--limit` to the default HTML report

  - Both flags were parsed and then used only as an empty-data guard before being dropped, so `pkgdb history <package> --since 7d` charted every stored day rather than the last 7; the `--text` and `--json` paths were unaffected

  - `generate_project_report()` gains `since` and `limit` parameters, and `get_package_history()` gains a `since` filter

- Milestone crossings can no longer fire twice for the same threshold

  - `detect_milestones()` only ever compared two adjacent observations, so a total that dipped below a threshold and later rose past it announced the same milestone again

  - New `milestone_state` table records a per-package high-water mark, which is what crossings are now measured against; `cleanup` clears it, so a re-added package starts its milestone history over

  - New database functions `get_milestone_high_water()` and `set_milestone_high_water()`

- `show --info` and `cleanup` now account for the whole schema rather than `package_stats` alone

  - `get_database_stats()` counted only snapshot rows and dated the database only by them, understating both its size and its historical range once the daily series dominates. It now reports `snapshot_records`, `daily_records`, and `github_history_records`, sums them into `record_count`, and spans the date range across all three

  - `cleanup_orphaned_stats()` and `prune_old_stats()` reported only the `package_stats` count while deleting from as many as eight tables; both now return per-table counts alongside a `total`

  - `DatabaseInfo` gains `snapshot_records`, `daily_records`, and `github_history_records`

- Tags can no longer be attached to untracked packages through the service or database API

  - `package_tags` has no foreign key, so `add_package_tag()` accepted any name; the tag then appeared in `tags` with a member count but no downloads behind it. The CLI checked membership, but nothing below it did

  - `add_package_tag()` now raises `ValueError` for an untracked package; new database function `is_tracked()`

### Changed

- **BREAKING**: `PackageStatsService.cleanup()` and `.prune()` return per-table deletion counts (a `dict[str, int]` carrying a `total` entry) instead of a single `int`; `cleanup --json` gains `orphaned_removed_by_table` and `pruned_by_table`, and the text output names the tables it touched

- Milestones are measured against downloads observed since tracking began -- the accumulated local daily series -- rather than the `package_stats.total` rolling ~180-day window, which could fall back below a threshold as old days aged out

  - Event wording is now "crossed N observed downloads (now M)", so it no longer reads as a lifetime figure; databases predating the daily series still fall back to snapshot totals

  - Whatever a package had already accumulated at its first `check` becomes its starting point, so a ~180-day backfill that arrives already past a threshold is not reported as a crossing

- `show --info` prints a per-table record breakdown beneath the total

## [0.2.0]

### Added

- Package tags / groups for portfolio organization and rollups

  - `tag <package> <tag>...` and `untag <package> <tag>... | --all` to manage
    tags (case-insensitive); `tags` lists every tag with its member count and
    aggregate `total`/`month`/`week` downloads (a portfolio rollup)

  - `show --tag <tag>` filters the table to a group and prints a group total

  - `packages` (and `packages --json`) now show each package's tags

  - New `package_tags` table; tags are removed with their package and cleaned up by `cleanup`

  - New database functions `add_package_tag()`, `remove_package_tag()`, `get_package_tags()`, `get_packages_for_tag()`, `get_tags_map()`; service methods `add_tag()`, `remove_tag()`, `get_package_tags()`, `get_tag_summary()`, and a `tag=` filter on `get_stats()`

- GitHub metrics history: `github` fetches now record a daily snapshot of each repo's stars, forks, open issues, and watchers

  - New `github_stats_history` table (keyed by `package_name` + `date`, idempotent per day); GitHub exposes no history, so the series accumulates going forward

  - `github` output gains a "Stars Δ" column (and `star_growth` in `--json`) showing the change in stars over roughly the last 30 days, once at least two snapshots exist

  - New database functions `store_github_stats_snapshot()` and `get_github_stats_history()`; service methods `PackageStatsService.get_github_history()` and `get_star_growth()`

  - `cleanup` and `cleanup --days` also cover the new table

  - The `serve` dashboard package detail page now shows a "GitHub Stars" stars-over-time chart (uPlot) once two or more snapshots exist, fed by a new `GET /api/github-history/<package>` endpoint

- `check` command: detect download anomalies and milestone crossings (built on the daily series) -- turns passive tracking into actionable alerts

  - Weekly spike/drop detection: aggregates the daily series into whole weeks (removing day-of-week seasonality) and flags the most recent week when it deviates from its trailing baseline by more than a z-score threshold. Works from a single fetch thanks to the ~180-day backfill

  - Milestone crossings: reports when a package's tracked download total crosses a configured target (upward only) since the previous fetch

  - Exits non-zero when any event is found (composes with shell/CI notifiers); `--exit-zero` to always exit 0, `--json` for machine-readable output, `--milestone N` (repeatable) and `-z/--z-threshold` for ad-hoc overrides

  - New `[check]` config section: `milestones`, `baseline_weeks`, `z_threshold`, `min_weekly`

  - New module `checks.py` (`weekly_totals`, `detect_anomaly`, `detect_milestones`), service method `PackageStatsService.run_checks()`, and `CheckEvent` type

  - The example GitHub Actions workflow now runs `pkgdb check` daily, so spikes, drops, and milestones are reported automatically

- Daily download time-series capture (foundation for true trend analysis)

  - New `daily_downloads` table stores per-date download counts across three dimensions: `overall` (mirror split), `python` (version), and `os`

  - `fetch`/`update` now also pull the daily series via pypistats `total="daily"`, so a newly tracked package shows real history from its very first fetch (~180 days backfilled) instead of only from the day tracking began

  - Idempotent upsert on `(package_name, date, dimension, category)`: re-fetching an already-captured day refreshes its count without duplicating; the local store extends history indefinitely past the ~180-day pypistats window

  - New API function `fetch_daily_downloads()`; partial-failure tolerant (one failing dimension does not discard the others)

  - New database functions `store_daily_downloads()` and `get_daily_downloads()` (filterable by dimension, category, and `since` date)

  - New service method `PackageStatsService.get_daily_downloads()`

  - New type `DailyDownload`

  - `cleanup` and `cleanup --days` now also cover the `daily_downloads` table

### Changed

- `history` now renders the true per-day download series when available

  - `history <package> --text` shows a `Date | Downloads` table of actual daily
    downloads (`without_mirrors`) instead of the per-fetch rolling snapshot

  - `history <package> --json` emits `[{"date", "downloads"}]` for the daily series; `--since` filters it by date

  - The default HTML report chart plots the dense daily curve (titled "Daily Downloads & Releases") with release markers, so a single fetch yields a full ~180-day trend rather than requiring many fetches to accumulate one

  - Falls back to the previous snapshot progression (per-fetch totals) when no daily data exists, e.g. databases populated before daily capture -- existing output and tests are preserved

  - New service method `PackageStatsService.get_daily_totals()`

  - `generate_project_html_report()` gains a `daily_series` parameter

- `serve` dashboard, `diff`, and growth % now use the daily series

  - Package detail chart plots the true per-day download series ("Downloads/day") from a single fetch, with release markers; new `GET /api/daily/<package>` endpoint. Falls back to the snapshot daily/weekly/monthly chart when no daily data exists

  - `diff --period week` / `--period month` compute exact this-period vs last-period download totals from the daily series (works from one fetch), rendered as a `This/Last <period>` table or JSON; `--period latest` still compares the two most recent fetches

  - Week/month growth % (shown in `show` and dashboard stat cards) computed as exact adjacent-window sums (last 7/30 days vs the prior 7/30) from the daily series, falling back to snapshot deltas for pre-daily databases

  - New helper `daily_window_sums()` and service method `PackageStatsService.get_period_comparison()`

## [0.1.12]

### Added

- `serve` command: launch a local interactive web dashboard for browsing package stats

  - `pkgdb serve` starts a local HTTP server (stdlib `http.server`, no Flask/FastAPI dependency)

  - Overview page: sortable/filterable table of all tracked packages with growth metrics, click any package to drill down

  - Package detail page: zoomable download history chart (uPlot), release date markers with toggles for PyPI/GitHub sources, ranked horizontal bar charts for Python version and OS breakdown

  - Comparison page: select multiple packages and overlay their download trends on a single chart

  - Live data from SQLite database on each request

  - `--port` flag for custom port (default: 8080)

  - `--no-browser` flag to suppress auto-open

  - uPlot charting library (~40KB) bundled as a package static asset (no CDN dependency)

- `--delay` option for `fetch` and `update` commands to throttle API requests and avoid HTTP 429 rate-limit errors from pypistats (default: 1.0 second between packages, use `--delay 0` to disable)

## [0.1.11]

### Added

- `init` command: guided first-run setup that combines package discovery, stats fetching, and report generation in a single interactive workflow

  - `pkgdb init` prompts for PyPI username or manual package entry, fetches stats, and generates an HTML report

  - `pkgdb init --user <username>` runs non-interactively (useful for scripts and CI)

  - Supports `--no-browser` and `-o` output flags

  - If packages are already tracked, asks whether to continue with a fetch

  - Note: this is a new command distinct from the `init` removed in v0.1.5; that command only synced packages, while this one provides the full setup-to-report workflow

- Configuration file support: `~/.pkgdb/config.toml` for persistent defaults

  - `[defaults]` section: `database`, `github`, `environment`, `no_browser`, `sort_by`

  - `[report]` section: `output` path

  - `[init]` section: `pypi_user` for default PyPI username

  - CLI flags always override config values

  - Uses `tomllib` (stdlib in Python 3.11+); degrades gracefully on Python 3.10 without `tomli`

  - Invalid TOML files log a warning and fall back to defaults

- New module: `config.py` with `PkgdbConfig`, `load_config()`, `get_config_path()`

- New function: `apply_config()` for merging config defaults into CLI args

- `--json` flag added to `packages`, `history`, `stats`, `cleanup`, and `github` commands (including `github cache` and `github clear` subcommands) for machine-readable output

- `show` command now displays a "Next update available in Xh Ym" footer when packages are within the 24-hour fetch cooldown

- `releases` command: show release history for a package from PyPI and GitHub

  - `pkgdb releases <package>` displays a merged, date-sorted table of all releases

  - `--limit N` to show only the most recent N releases

  - `--json` flag for machine-readable output

  - PyPI releases fetched from `https://pypi.org/pypi/{package}/json`

  - GitHub releases fetched from the GitHub Releases API (auto-discovered from PyPI metadata)

  - 24-hour caching for both sources via `release_cache` table

- Project view report: `pkgdb report <package> --project`

  - Generates HTML report with download history line chart overlaid with release markers

  - PyPI releases shown as blue dashed vertical lines; GitHub releases as orange dashed lines

  - Includes merged release history table with date, version, and source

  - Includes environment distribution (Python versions, OS breakdown)

  - Uses 90-day history window (vs 30-day for standard package report)

- New database tables: `pypi_releases`, `github_releases`, `release_cache`

- New types: `PyPIRelease`, `GitHubRelease`

- New API functions: `fetch_pypi_releases()`, `fetch_github_releases()`

- New service methods: `fetch_package_releases()`, `generate_project_report()`

- New report function: `generate_project_html_report()` with `_make_line_chart_with_markers()`

- MkDocs-based API documentation with mkdocs-material theme and mkdocstrings autodoc

  - `make docs` to build, `make docs-serve` to preview locally, `make docs-deploy` to publish to GitHub Pages

  - Covers: getting started, CLI reference, Python API (service, types, database, PyPI, GitHub, reports, config)

  - API reference auto-generated from source docstrings

- `diff` command: compare download stats between two time periods

  - `pkgdb diff` compares current stats to the previous fetch (default)

  - `--period week` compares this week to last week

  - `--period month` compares this month to last month

  - `--sort-by` option to sort by total, month, week, day, change, or name

  - `--json` flag for machine-readable output

  - Shows absolute change and percentage change for each metric

### Changed

- `history` command now generates an HTML report by default with download chart, release markers, and history table (opens in browser)

  - `--text` / `-t` flag for the previous terminal table output

  - `--json` flag unchanged

  - `-o` / `--output` for custom output path, `--no-browser` to suppress browser

  - Default history window increased from 30 to 90 days

- `show` command now hides Trend and Growth columns when there is only one data point per package, producing a cleaner output on first run instead of showing empty sparklines and blank growth percentages

- Split monolithic `tests/test_pkgdb.py` (6100+ lines) into 13 focused test modules: `test_db`, `test_api`, `test_utils`, `test_export`, `test_reports`, `test_badges`, `test_github`, `test_service`, `test_cli`, `test_config`, `test_releases`, `test_integration`, and shared fixtures in `conftest.py`

## [0.1.10]

### Fixed

- `check_package_exists` now normalizes package names per PEP 503 before querying PyPI Simple API, so names with mixed case or underscores (e.g. `Requests`, `my_pkg`) resolve correctly

- `remove_package` now deletes the corresponding `fetch_attempts` row, preventing re-added packages from being incorrectly skipped by `fetch` due to stale attempt records

- `cleanup_orphaned_stats` now also cleans orphaned `fetch_attempts` entries

- GitHub `--sort activity` no longer ranks repos pushed today as stale: fixed operator precedence bug and falsy-zero handling in the sort key

- `pkgdb report <package>` now correctly reports failure when stats cannot be fetched, instead of announcing success and opening a non-existent file

## [0.1.9]

### Added

- GitHub repository statistics: fetch stars, forks, open issues, language, activity status, and more for tracked packages

  - New `github` command with subcommands:

    - `pkgdb github [fetch]` displays GitHub stats table (stars, forks, activity, language) for all tracked packages

    - `pkgdb github cache` shows cache statistics

    - `pkgdb github clear [--all]` clears cached GitHub API responses

  - `--sort` option for GitHub fetch: sort by `stars` (default), `name`, or `activity`

  - `--no-cache` flag to bypass the 24-hour cache and fetch fresh data

  - `-g/--github` flag on `fetch`, `update`, and `report` commands to include GitHub stats alongside PyPI download stats

  - HTML report includes GitHub columns (Stars, Forks, Language, Activity, Repository) when `--github` is passed; pulls from cache if available, skips gracefully if not

  - GitHub repo URL auto-discovery from PyPI package metadata (`project_urls`, `home_page`)

  - Supports `GITHUB_TOKEN` / `GH_TOKEN` environment variables for higher API rate limits

  - 24-hour response caching in SQLite (`github_cache` table) to minimize API calls

  - Exponential backoff with jitter on rate limiting (HTTP 403)

- New module: `github.py` with `RepoStats`, `RepoResult`, `parse_github_url()`, `extract_github_url()`, `fetch_repo_stats()`, `fetch_package_github_stats()`

- New service methods: `fetch_github_stats()`, `clear_github_cache()`, `get_github_cache_stats()`

## [0.1.8]

### Fixed

- `get_stats_with_growth` now computes `week_growth` from `last_week` column instead of `last_month`

- `get_packages_needing_update` now only filters on successful attempts, so transient API failures no longer block retries for 24 hours

- License classifier in `pyproject.toml` corrected from "BSD License" to "MIT License" to match actual license

- CLI "All packages already up to date" message now shows when the next update will be available (e.g. "Next update available in 23h 45m")

### Added

- Environment stats caching: Python version and OS distribution data now stored in SQLite during fetch

  - New tables: `python_version_stats` and `os_stats`

  - `pkgdb report <package>` uses cached env data instead of live API calls (offline-capable)

  - `pkgdb report --env` reads cached aggregated env data (falls back to live API if no cache)

  - Env stats are fetched alongside download stats in the same 24h fetch cycle

- `-e/--env` flag on `pkgdb update` for parity with `pkgdb report` (fetch + env-enabled report in one step)

- Growth indicators (week-over-week, month-over-month) in the HTML report table, with colored arrows for positive/negative trends

- New functions: `store_env_stats()`, `get_cached_python_versions()`, `get_cached_os_stats()`, `get_cached_env_summary()`

- `cleanup_orphaned_stats()` and `prune_old_stats()` now also clean env stats tables

- `get_next_update_seconds()` function to compute seconds until the next package becomes eligible for update

- `FetchResult.next_update_seconds` field for programmatic access to next update timing

### Removed

- Removed references to YAML support in docstrings and help text (YAML parsing was removed in v0.1.3; only JSON and plain text are supported)

- Deleted legacy `packages.yml` (unused since v0.1.3)

## [0.1.7]

### Added

- Fetch attempt tracking: packages are only fetched once per 24-hour period

  - New `fetch_attempts` table tracks when each package was last fetched

  - Both successful and failed fetch attempts are recorded

  - Subsequent `pkgdb update` or `pkgdb fetch` runs skip recently-attempted packages

  - CLI reports skipped count: "Skipped N packages (already fetched in last 24 hours)"

  - Shows "All packages already up to date" when nothing needs fetching

- "Recent Downloads (Last Day)" chart in HTML reports, displayed after the "Last Month" chart

- New functions: `record_fetch_attempt()`, `get_packages_needing_update()`

- `FetchResult` dataclass now includes `skipped` field

## [0.1.6]

### Added

- Package validation: `add` and `import` commands now verify packages exist on PyPI before adding

  - Uses HEAD request to PyPI Simple API for minimal overhead

  - `--no-verify` flag to skip verification for offline/bulk operations

  - Network errors warn but allow operation (fail open)

- Relative date queries: `--since` flag now accepts relative formats

  - `7d` for 7 days ago

  - `2w` for 2 weeks ago

  - `1m` for 1 month ago (treated as 30 days)

  - Still supports `YYYY-MM-DD` format

- New functions: `check_package_exists()`, `parse_date_arg()`, `get_database_stats()`

- Service methods `add_package()` and `import_packages()` now accept `verify` parameter

- Database info: `pkgdb show --info` displays database statistics (file size, package count, record count, date range)

- New type: `DatabaseInfo` TypedDict for database statistics

- Badge generation: `pkgdb badge <package>` generates shields.io-style SVG badges

  - Supports `--period` flag for total/month/week/day

  - Auto-selects color based on download count

  - Output to file with `-o` or stdout

- GitHub Actions workflow template: `.github/workflows/fetch-stats.yml.example`

- New module: `badges.py` with `generate_badge_svg()` and `generate_downloads_badge()`

### Changed

- `import_packages()` now returns 4-tuple: `(added, skipped, invalid, not_found)`

### Fixed

- "Recent Downloads (Last Month)" chart now sorted by decreasing downloads (consistent with "Total Downloads by Package" chart)

## [0.1.5]

### Added

- `sync` command: `pkgdb sync --user <username>` populates or refreshes the package list from a PyPI user account, adding any new packages without duplicating existing ones

- `sync --prune` option: removes locally tracked packages no longer in the user's PyPI account

- `SyncResult` dataclass for programmatic access to sync results (added, already_tracked, not_on_remote, pruned)

- Service method `sync_packages_from_user(username, prune=False)` for the service layer API

### Removed

- `init` command: use `sync --user <username>` instead (same functionality, plus refresh and prune capabilities)

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
