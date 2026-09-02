# TODO

Feature ideas for pkgdb, ordered by priority.

## High Priority

## Medium Priority

### Database Maintenance
- [ ] Backup/restore - `pkgdb backup` / `pkgdb restore`

### Package Discovery
- [ ] Import packages from pyproject.toml `[project]` section

### GitHub Integration
- [ ] Auto-discover packages from your repos (scan for pyproject.toml) - the `github_repos` registry added for `pkgdb ci` is the prerequisite
- [ ] Publish HTML report to GitHub Pages - `pkgdb publish`
- [ ] Report CI failures as `pkgdb check` events, so one command covers downloads and CI
- [ ] CI status column in the HTML report and dashboard

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
