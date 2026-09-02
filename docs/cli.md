# CLI Reference

All commands support `-d <path>` for a custom database, `-v` for verbose output, and `-q` for quiet mode.

## Setup

### `pkgdb init`

Guided first-run setup: sync packages, fetch stats, generate report.

```bash
pkgdb init                          # interactive
pkgdb init --user <username>        # non-interactive
pkgdb init --no-browser             # don't open report
```

## Package Management

### `pkgdb add <name>`

Add a package to tracking. Verifies it exists on PyPI by default.

```bash
pkgdb add requests
pkgdb add my-pkg --no-verify        # skip PyPI check
```

### `pkgdb remove <name>`

Remove a package from tracking. It disappears from `show`, `report`, `export`,
`history`, badges, and the dashboard straight away.

Its collected statistics stay on disk rather than being deleted with it, so a
package removed by mistake can be re-added without losing its history. Run
`pkgdb cleanup` to purge that retained data for good. The same applies to
packages dropped by `pkgdb sync --prune`.

### `pkgdb packages`

List tracked packages with their added dates and tags. Alias: `pkgdb list`.

```bash
pkgdb packages --json               # JSON output (includes tags)
```

### `pkgdb tag` / `pkgdb untag` / `pkgdb tags`

Group related packages with tags and view per-group (portfolio) rollups. Tags
are case-insensitive.

```bash
pkgdb tag requests web api          # add one or more tags to a package
pkgdb untag requests api            # remove a tag
pkgdb untag requests --all          # remove all tags
pkgdb tags                          # list tags with aggregate downloads
pkgdb tags --json
pkgdb show --tag web                # filter show to a group, with a group total
```

### `pkgdb import <file>`

Import packages from a JSON or plain text file.

```bash
pkgdb import packages.json
pkgdb import packages.txt --no-verify
```

### `pkgdb sync --user <username>`

Sync package list from a PyPI user account.

```bash
pkgdb sync --user shakfu
pkgdb sync --user shakfu --prune    # remove packages no longer on PyPI
```

## Data Operations

### `pkgdb fetch`

Fetch download stats from PyPI. Skips packages fetched in the last 24 hours.

```bash
pkgdb fetch
pkgdb fetch --github                # also fetch GitHub stats
```

### `pkgdb show`

Display stats in terminal with trend sparklines and growth percentages.

```bash
pkgdb show
pkgdb show --sort-by month
pkgdb show --limit 10
pkgdb show --json
pkgdb show --info                   # database info
```

On first run (single data point), Trend and Growth columns are hidden automatically.

### `pkgdb diff`

Compare download stats between time periods.

```bash
pkgdb diff                          # vs previous fetch
pkgdb diff --period week            # this week vs last week (exact, from daily data)
pkgdb diff --period month           # this month vs last month
pkgdb diff --sort-by change
pkgdb diff --json
```

### `pkgdb check`

Detect download anomalies (weekly spikes/drops vs a trailing baseline) and
milestone crossings. Exits non-zero when any event is found, so it composes with
shell and CI notifiers. Configure milestones and thresholds under `[check]` in
`config.toml`.

Milestones are measured against **observed downloads**: the sum of the daily
series pkgdb has stored locally. That figure keeps accumulating across fetches,
unlike the rolling ~180-day totals PyPI reports, but it is not a lifetime count
either - it starts at whatever the first fetch backfilled and grows from there.
Downloads from before you began tracking are not included.

Each milestone is announced once. Whatever a package had already accumulated at
its first check becomes the starting point, so a backfill that arrives already
past a threshold is not reported as a crossing, and a total that dips (through
pruning, or through the rolling-window fallback used by databases predating the
daily series) cannot announce the same milestone a second time.

```bash
pkgdb check                         # report events; exit 1 if any found
pkgdb check --json                  # machine-readable events
pkgdb check --milestone 100000      # watch a download target (repeatable)
pkgdb check -z 3.0                  # require a larger deviation to flag
pkgdb check --exit-zero             # report but always exit 0
```

### `pkgdb history <package>`

Show historical stats for a specific package. Generates an HTML report by
default; `--text` and `--json` render to the terminal instead.

`--since` and `--limit` apply to all three output modes, including the charted
daily series in the HTML report.

```bash
pkgdb history requests
pkgdb history requests --since 7d
pkgdb history requests --since 2026-01-01
pkgdb history requests --json
```

### `pkgdb stats <package>`

Show detailed stats breakdown (Python versions, OS distribution).

```bash
pkgdb stats requests
pkgdb stats requests --json
```

### `pkgdb releases <package>`

Show release history from PyPI and GitHub.

```bash
pkgdb releases requests
pkgdb releases requests --limit 10
pkgdb releases requests --json
```

### `pkgdb github`

Fetch and display GitHub repository stats. Each fetch records a daily snapshot of
stars/forks/issues/watchers, so the output includes a "Stars Δ" column showing
star growth over roughly the last 30 days once enough history has accumulated.

```bash
pkgdb github                        # fetch stats (records a daily snapshot)
pkgdb github fetch --sort stars
pkgdb github fetch --no-cache
pkgdb github cache                  # cache info
pkgdb github clear                  # clear expired cache
pkgdb github --json                 # includes a star_growth field
```

### `pkgdb ci`

Report the latest run of every GitHub Actions workflow in your repositories.
Failures only unless `--all` is given, and exits non-zero when any workflow is
failing, so it composes with shell and CI notifiers.

Repositories come from the scan registry (`pkgdb repo`). Each is scanned on its
own default branch unless `--branch` says otherwise, so a red run on a feature
branch is not reported as broken CI.

The "Failing" column is the age of the current failure streak, which separates a
break from twenty minutes ago from one that has stood for months. It survives
cancelled and in-progress runs and resets only when the workflow passes.

Run listings are cached for an hour, so a repeat scan costs no requests.
Repositories whose runs could not be fetched are reported as warnings and do not
affect the exit code: unreachable is unknown, not broken.

```bash
pkgdb ci                            # failing workflows; exit 1 if any
pkgdb ci --all                      # include passing workflows
pkgdb ci --repo owner/name          # one repository, registry not required
pkgdb ci --branch develop           # scan a specific branch everywhere
pkgdb ci --json                     # machine-readable
pkgdb ci --no-cache                 # bypass the one-hour cache
pkgdb ci --exit-zero                # report but always exit 0
pkgdb ci -o                         # also write ~/.pkgdb/ci-report.html and open it
pkgdb ci -o out.html --no-browser   # write somewhere else, do not open
```

A workflow this scan did not see is forgotten, so a renamed or deleted workflow
stops reporting its last failure. A repository whose runs could not be fetched
keeps whatever was recorded before, because unreachable is unknown, not empty.

CI status also appears in `pkgdb report --ci` and, once a scan has run, as a
panel on the `pkgdb serve` dashboard. The dashboard renders the last recorded
scan and never starts one of its own.

Authentication is required in practice: unauthenticated GitHub allows 60
requests an hour, which a 64-repository scan exhausts. If you are logged in with
the `gh` CLI, pkgdb uses that token and there is nothing to set up. Otherwise set
`GITHUB_TOKEN` or `GH_TOKEN`, which take precedence over `gh`.

Without a token the scan still runs, but sees public repositories only and gives
up partway through a large account. It warns once at the start that it is
unauthenticated, and again if the quota runs out, naming the reset time.
Repositories it could not reach are reported as warnings and counted as unknown,
never as passing, and they do not affect the exit code.

### `pkgdb repo`

Manage the repository registry that `ci` scans.

The registry exists because a package-derived repository list only reaches
projects published to PyPI with a repository URL in their metadata. On a typical
account that is a minority of the repositories that actually run CI.

`discover` lists an account's repositories and records how many workflows each
defines, so later scans skip the ones with no CI. The probe costs one request per
repository, but only the first time each is seen. It also links tracked packages
to same-named repositories, recovering the ones whose PyPI metadata carries no
repository URL. Your own account is listed through the authenticated endpoint,
so private repositories are included.

```bash
pkgdb repo discover --user <github-user>   # register an account's repos
pkgdb repo discover --no-probe             # register without counting workflows
pkgdb repo list                            # show the registry
pkgdb repo add owner/name                  # register one repository
pkgdb repo remove owner/name               # unregister it
pkgdb repo --json list
```

Set `[github] user` in `config.toml` to omit `--user`.

### `pkgdb export`

Export stats in various formats.

```bash
pkgdb export -f csv
pkgdb export -f json -o stats.json
pkgdb export -f markdown
```

## Reporting

### `pkgdb report`

Generate HTML report with charts.

```bash
pkgdb report                        # all packages
pkgdb report <package>              # single package
pkgdb report <package> --project    # project view with releases
pkgdb report -e                     # include environment data
pkgdb report -g                     # include GitHub stats
pkgdb report -c                     # include a CI status section
pkgdb report -o custom.html
pkgdb report --no-browser
```

`-c/--ci` scans registered repositories and adds a CI section to the report.
Run listings are cached for an hour, so a report regenerated soon after a
`pkgdb ci` costs no requests. `pkgdb update --ci` does the same after fetching.

### `pkgdb badge <package>`

Generate shields.io-style SVG badge.

```bash
pkgdb badge requests
pkgdb badge requests --period month
pkgdb badge requests -o badge.svg
```

### `pkgdb update`

Shortcut: fetch stats then generate report.

```bash
pkgdb update
pkgdb update -e -g                  # with env and GitHub
```

## Maintenance

### `pkgdb cleanup`

Purge the retained data of packages that are no longer tracked, and optionally
prune old rows from every package's history.

```bash
pkgdb cleanup
pkgdb cleanup --days 365            # prune stats older than 1 year
pkgdb cleanup --json
```

Both operations span the whole schema, so the counts are reported per table -
snapshots, the daily series, environment stats, and GitHub history each get
their own figure alongside the total. `--json` exposes them as
`orphaned_removed_by_table` and `pruned_by_table`.

### `pkgdb version`

Show pkgdb version.
