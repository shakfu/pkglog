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
- [x] Sorting/filtering - `pkgdb show --sort-by month --limit 10`
- [x] Date filtering - `pkgdb history requests --since 2024-01-01`
- [x] Package validation - verify package exists on PyPI before adding
- [x] Relative date queries - `pkgdb history requests --since 7d`

### Badges & CI
- [ ] Badge generation - SVG badges for READMEs showing download counts
- [ ] GitHub Actions template - automated daily/weekly fetch + report

## Medium Priority

### Richer pypistats Data
- [x] Python version distribution
- [x] OS/platform breakdown

### Database Maintenance
- [x] `pkgdb cleanup --days N` to prune old data
- [ ] Database size/stats info in `pkgdb show`
- [ ] Backup/restore - `pkgdb backup` / `pkgdb restore`

### Package Discovery
- [x] `pkgdb init --user <pypi-username>` to auto-populate from PyPI account
- [ ] Import packages from pyproject.toml `[project]` section

### GitHub Integration (via `gh` CLI)
- [ ] Auto-discover packages from your repos (scan for pyproject.toml)
- [ ] Publish HTML report to GitHub Pages - `pkgdb publish`

### Organization
- [ ] Package groups/tags - group related packages, aggregate stats per group

## Low Priority

### Comparison Mode
- [ ] Track packages you don't own (competitors, dependencies)
- [ ] Side-by-side comparison charts

### Alerts
- [ ] Detect significant spikes or drops in downloads
- [ ] Milestones - set download targets, notify when reached

### Advanced
- [ ] Server/API mode - REST endpoint for dashboard integration
