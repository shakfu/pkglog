"""Tests for config.toml loading, parsing, and applying to CLI args."""

import argparse
from pathlib import Path

import pytest

from pkgdb import (
    PkgdbConfig,
    load_config,
    get_config_path,
    apply_config,
    DEFAULT_DB_FILE,
    DEFAULT_REPORT_FILE,
)


class TestConfig:
    """Tests for config.toml loading and parsing."""

    def test_load_config_no_file(self, tmp_path):
        """load_config returns defaults when file doesn't exist."""
        config = load_config(tmp_path / "nonexistent.toml")
        assert config.database is None
        assert config.github is False
        assert config.environment is False
        assert config.no_browser is False
        assert config.sort_by == "total"
        assert config.report_output is None
        assert config.pypi_user is None

    def test_load_config_empty_file(self, tmp_path):
        """load_config handles empty TOML file."""
        config_path = tmp_path / "config.toml"
        config_path.write_text("")
        config = load_config(config_path)
        assert config.database is None
        assert config.github is False

    def test_load_config_defaults_section(self, tmp_path):
        """load_config reads [defaults] section."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            "[defaults]\n"
            'database = "/custom/path.db"\n'
            "github = true\n"
            "environment = true\n"
            "no_browser = true\n"
            'sort_by = "month"\n'
        )
        config = load_config(config_path)
        assert config.database == "/custom/path.db"
        assert config.github is True
        assert config.environment is True
        assert config.no_browser is True
        assert config.sort_by == "month"

    def test_load_config_report_section(self, tmp_path):
        """load_config reads [report] section."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[report]\noutput = "/custom/report.html"\n')
        config = load_config(config_path)
        assert config.report_output == "/custom/report.html"

    def test_load_config_init_section(self, tmp_path):
        """load_config reads [init] section."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[init]\npypi_user = "testuser"\n')
        config = load_config(config_path)
        assert config.pypi_user == "testuser"

    def test_load_config_partial_sections(self, tmp_path):
        """load_config handles partially filled config."""
        config_path = tmp_path / "config.toml"
        config_path.write_text("[defaults]\ngithub = true\n")
        config = load_config(config_path)
        assert config.github is True
        assert config.database is None

    def test_load_config_check_section(self, tmp_path):
        """load_config reads the [check] section."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            "[check]\n"
            "milestones = [10000, 1000, 1000, 100000]\n"
            "baseline_weeks = 6\n"
            "z_threshold = 3.0\n"
            "min_weekly = 25\n"
        )
        config = load_config(config_path)
        # De-duplicated and sorted
        assert config.check_milestones == [1000, 10000, 100000]
        assert config.check_baseline_weeks == 6
        assert config.check_z_threshold == 3.0
        assert config.check_min_weekly == 25.0

    def test_load_config_check_ignores_bad_milestones(self, tmp_path):
        """Non-integer milestone entries are skipped, not fatal."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[check]\nmilestones = [1000, "oops", 5000]\n')
        config = load_config(config_path)
        assert config.check_milestones == [1000, 5000]

    def test_load_config_check_defaults(self, tmp_path):
        """[check] fields fall back to defaults when absent."""
        config = load_config(tmp_path / "nonexistent.toml")
        assert config.check_milestones == []
        assert config.check_baseline_weeks == 8
        assert config.check_z_threshold == 2.5
        assert config.check_min_weekly == 10.0
        assert config.environment is False
        assert config.sort_by == "total"

    def test_load_config_invalid_toml(self, tmp_path):
        """load_config warns and returns defaults on invalid TOML."""
        config_path = tmp_path / "config.toml"
        config_path.write_text("this is not valid [toml")
        config = load_config(config_path)
        # Should return defaults without crashing
        assert config.database is None
        assert config.github is False

    def test_load_config_raw_data_preserved(self, tmp_path):
        """load_config preserves raw parsed data."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[defaults]\ngithub = true\n[custom]\nkey = "value"\n')
        config = load_config(config_path)
        assert config._raw.get("custom", {}).get("key") == "value"

    def test_get_config_path(self):
        """get_config_path returns expected path."""
        path = get_config_path()
        assert path.name == "config.toml"
        assert path.parent.name == ".pkgdb"

    def test_pkgdb_config_defaults(self):
        """PkgdbConfig has correct default values."""
        config = PkgdbConfig()
        assert config.database is None
        assert config.github is False
        assert config.environment is False
        assert config.no_browser is False
        assert config.sort_by == "total"
        assert config.report_output is None
        assert config.pypi_user is None


class TestApplyConfig:
    """Tests for applying config to parsed CLI args."""

    def _make_args(self, **kwargs):
        """Create a Namespace with sensible defaults."""
        defaults = {
            "database": DEFAULT_DB_FILE,
            "output": DEFAULT_REPORT_FILE,
            "github": False,
            "env": False,
            "no_browser": False,
            "sort_by": "total",
            "pypi_user": None,
        }
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_config_overrides_defaults(self):
        """Config values should override hardcoded defaults."""
        args = self._make_args()
        config = PkgdbConfig(
            database="/custom/path.db",
            github=True,
            environment=True,
            no_browser=True,
            sort_by="month",
            report_output="/custom/report.html",
            pypi_user="testuser",
        )
        apply_config(args, config)
        assert args.database == "/custom/path.db"
        assert args.github is True
        assert args.env is True
        assert args.no_browser is True
        assert args.sort_by == "month"
        assert args.output == "/custom/report.html"
        assert args.pypi_user == "testuser"

    def test_cli_flags_override_config(self):
        """CLI flags should take precedence over config values."""
        args = self._make_args(
            database="/cli/path.db",
            github=True,
            sort_by="week",
        )
        config = PkgdbConfig(
            database="/config/path.db",
            github=False,
            sort_by="month",
        )
        apply_config(args, config)
        # CLI values should be preserved, not overwritten by config
        assert args.database == "/cli/path.db"
        assert args.github is True
        assert args.sort_by == "week"

    def test_empty_config_changes_nothing(self):
        """Default config should not change any args."""
        args = self._make_args()
        original_db = args.database
        original_output = args.output
        config = PkgdbConfig()
        apply_config(args, config)
        assert args.database == original_db
        assert args.output == original_output
        assert args.github is False

    def test_config_does_not_add_missing_attrs(self):
        """Config should not add attributes that don't exist on args."""
        # Simulates a command like 'version' that has no 'env' attribute
        args = argparse.Namespace(database=DEFAULT_DB_FILE)
        config = PkgdbConfig(environment=True)
        apply_config(args, config)
        # env should not have been added since it didn't exist
        assert not hasattr(args, "env")
