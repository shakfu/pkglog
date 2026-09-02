"""Configuration file support for pkgdb.

Loads settings from ~/.pkgdb/config.toml, providing persistent defaults
that CLI flags can override.
"""

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("pkgdb")

# Use tomllib (3.11+) or tomli (3.10 fallback)
if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]


def get_config_path() -> Path:
    """Get the path to the config file (~/.pkgdb/config.toml)."""
    return Path.home() / ".pkgdb" / "config.toml"


@dataclass
class PkgdbConfig:
    """Configuration loaded from config.toml.

    All fields are optional -- missing fields use the application defaults.
    CLI flags always override config values.
    """

    # [defaults]
    database: str | None = None
    github: bool = False
    environment: bool = False
    no_browser: bool = False
    sort_by: str = "total"

    # [report]
    report_output: str | None = None

    # [init]
    pypi_user: str | None = None

    # [check]
    check_milestones: list[int] = field(default_factory=list)
    check_baseline_weeks: int = 8
    check_z_threshold: float = 2.5
    check_min_weekly: float = 10.0

    # [github]
    github_user: str | None = None

    # [ci]
    # None scans each repository on its own default branch.
    ci_branch: str | None = None
    ci_ignore_workflows: list[str] = field(default_factory=list)

    # Raw parsed data for extensibility
    _raw: dict[str, Any] = field(default_factory=dict, repr=False)


def load_config(config_path: Path | None = None) -> PkgdbConfig:
    """Load configuration from TOML file.

    Args:
        config_path: Path to config file. If None, uses the default location.

    Returns:
        PkgdbConfig with values from the file, or defaults if file doesn't exist.
    """
    if config_path is None:
        config_path = get_config_path()

    if not config_path.exists():
        return PkgdbConfig()

    if tomllib is None:
        logger.debug(
            "config.toml found but tomli not installed (Python 3.10). "
            "Install 'tomli' or upgrade to Python 3.11+ for config file support."
        )
        return PkgdbConfig()

    try:
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
    except Exception as e:
        logger.warning("Could not parse %s: %s", config_path, e)
        return PkgdbConfig()

    defaults = raw.get("defaults", {})
    report = raw.get("report", {})
    init_section = raw.get("init", {})
    check = raw.get("check", {})
    github = raw.get("github", {})
    ci = raw.get("ci", {})

    # Milestones must be a list of ints; ignore malformed entries gracefully.
    raw_milestones = check.get("milestones", [])
    milestones: list[int] = []
    if isinstance(raw_milestones, list):
        for m in raw_milestones:
            try:
                milestones.append(int(m))
            except (TypeError, ValueError):
                logger.warning("Ignoring non-integer milestone in config: %r", m)

    raw_ignore = ci.get("ignore_workflows", [])
    ignore_workflows = (
        [str(w) for w in raw_ignore] if isinstance(raw_ignore, list) else []
    )

    return PkgdbConfig(
        database=defaults.get("database"),
        github=defaults.get("github", False),
        environment=defaults.get("environment", False),
        no_browser=defaults.get("no_browser", False),
        sort_by=defaults.get("sort_by", "total"),
        report_output=report.get("output"),
        pypi_user=init_section.get("pypi_user"),
        check_milestones=sorted(set(milestones)),
        check_baseline_weeks=int(check.get("baseline_weeks", 8)),
        check_z_threshold=float(check.get("z_threshold", 2.5)),
        check_min_weekly=float(check.get("min_weekly", 10.0)),
        github_user=github.get("user"),
        ci_branch=ci.get("branch"),
        ci_ignore_workflows=ignore_workflows,
        _raw=raw,
    )
