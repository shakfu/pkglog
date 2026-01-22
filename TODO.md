# TODO

Feature ideas for pkgdb, ordered by priority.

## High Priority

### Historical Trends
- [x] Time-series chart showing downloads over time per package
- [x] Growth metrics (week-over-week, month-over-month % change)
- [x] `pkgdb history <package>` command to show historical stats for one package
- [x] Sparklines in the terminal table view

### Export Formats
- [x] `pkgdb export --format csv` for spreadsheet analysis
- [x] `pkgdb export --format json` for programmatic use
- [x] Markdown table output for embedding in READMEs

### CLI Improvements
- [ ] Package validation - verify package exists on PyPI before adding
- [ ] Sorting/filtering - `pkgdb show --sort-by month --top 10`
- [ ] Relative date queries - `pkgdb history requests --since 7d`

### Badges & CI
- [ ] Badge generation - SVG badges for READMEs showing download counts
- [ ] GitHub Actions template - automated daily/weekly fetch + report

## Medium Priority

### Richer pypistats Data
- [ ] Per-version download breakdown (not available via pypistats API)
- [x] Python version distribution
- [x] OS/platform breakdown

### Database Maintenance
- [ ] `pkgdb prune --older-than 90d` to clean old data
- [ ] Database size/stats info in `pkgdb show`
- [ ] Backup/restore - `pkgdb backup` / `pkgdb restore`
- [ ] Merge databases - combine stats from multiple machines

### Package Discovery
- [ ] `pkgdb init --user <pypi-username>` to auto-populate from PyPI account
- [ ] Import packages from pyproject.toml `[project]` section

### GitHub Integration (via `gh` CLI)
- [ ] Auto-discover packages from your repos (scan for pyproject.toml)
- [ ] Link packages to repos - `pkgdb link <package> <owner/repo>`
- [ ] Show GitHub stats alongside downloads (stars, forks, open issues)
- [ ] Publish HTML report to GitHub Pages - `pkgdb publish`
- [ ] Compare downloads before/after GitHub releases
- [ ] Include download stats in release notes

### Organization
- [ ] Package groups/tags - group related packages, aggregate stats per group
- [ ] Notes - attach notes to packages (e.g., "deprecated", "v2 coming")

## Low Priority

### Comparison Mode
- [ ] Track packages you don't own (competitors, dependencies)
- [ ] Separate `watched` key in packages.yml
- [ ] Side-by-side comparison charts
- [ ] Diff command - `pkgdb diff --from 2024-01-01 --to 2024-02-01`

### Alerts
- [ ] Detect significant spikes or drops in downloads
- [ ] Milestones - set download targets, notify when reached
- [ ] Weekly digest summary
- [ ] Email notification support
- [ ] Webhook support (Slack, Discord, generic)

### Advanced
- [ ] Multiple profiles - separate tracking for different projects
- [ ] Cron-friendly mode - quiet output, meaningful exit codes
- [ ] Server/API mode - REST endpoint for dashboard integration
