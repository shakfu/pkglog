"""Tests for HTML report generation, pie charts, package reports, and chart edge cases."""

import json
import os
import tempfile
from datetime import datetime, timedelta

from pathlib import Path
from unittest.mock import patch

import pytest

from pkgdb import (
    make_svg_pie_chart,
    generate_html_report,
    generate_package_html_report,
    generate_project_html_report,
    RepoStats,
    PyPIRelease,
    GitHubRelease,
)
from pkgdb.utils import utcnow


class TestHTMLReportGeneration:
    """Tests for HTML report generation."""

    def test_generate_html_report_creates_file(self):
        """generate_html_report should create a self-contained HTML file with SVG."""
        stats = [
            {
                "package_name": "test-pkg",
                "total": 1000,
                "last_month": 300,
                "last_week": 70,
                "last_day": 10,
            }
        ]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
            output_path = f.name

        try:
            generate_html_report(stats, output_path)
            assert Path(output_path).exists()

            content = Path(output_path).read_text()
            assert "<!DOCTYPE html>" in content
            assert "test-pkg" in content
            assert "<svg" in content
            assert "cdn" not in content.lower()
        finally:
            Path(output_path).unlink(missing_ok=True)

    def test_generate_html_report_includes_all_packages(self):
        """generate_html_report should include all packages in the report."""
        stats = [
            {
                "package_name": "pkg-a",
                "total": 1000,
                "last_month": 300,
                "last_week": 70,
                "last_day": 10,
            },
            {
                "package_name": "pkg-b",
                "total": 500,
                "last_month": 150,
                "last_week": 35,
                "last_day": 5,
            },
        ]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
            output_path = f.name

        try:
            generate_html_report(stats, output_path)
            content = Path(output_path).read_text()
            assert "pkg-a" in content
            assert "pkg-b" in content
        finally:
            Path(output_path).unlink(missing_ok=True)

    def test_generate_html_report_empty_stats(self, caplog):
        """generate_html_report should handle empty stats gracefully."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
            output_path = f.name

        try:
            generate_html_report([], output_path)
            assert "No statistics available" in caplog.text
        finally:
            Path(output_path).unlink(missing_ok=True)

    def test_generate_html_report_with_history(self):
        """generate_html_report should include time-series chart when history provided."""
        stats = [
            {
                "package_name": "pkg-a",
                "total": 3000,
                "last_month": 900,
                "last_week": 210,
                "last_day": 30,
            },
        ]
        history = {
            "pkg-a": [
                {
                    "package_name": "pkg-a",
                    "fetch_date": "2024-01-01",
                    "total": 1000,
                    "last_month": 300,
                    "last_week": 70,
                    "last_day": 10,
                },
                {
                    "package_name": "pkg-a",
                    "fetch_date": "2024-01-02",
                    "total": 2000,
                    "last_month": 600,
                    "last_week": 140,
                    "last_day": 20,
                },
                {
                    "package_name": "pkg-a",
                    "fetch_date": "2024-01-03",
                    "total": 3000,
                    "last_month": 900,
                    "last_week": 210,
                    "last_day": 30,
                },
            ]
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
            output_path = f.name

        try:
            generate_html_report(stats, output_path, history)
            content = Path(output_path).read_text()
            assert "Downloads Over Time" in content
            assert "time-series-chart" in content
            assert "polyline" in content  # SVG line element
        finally:
            Path(output_path).unlink(missing_ok=True)

    def test_generate_html_report_includes_growth_columns(self):
        """generate_html_report should include growth columns when stats have growth data."""
        stats = [
            {
                "package_name": "test-pkg",
                "total": 1000,
                "last_month": 300,
                "last_week": 70,
                "last_day": 10,
                "week_growth": 25.0,
                "month_growth": -10.5,
            }
        ]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
            output_path = f.name

        try:
            generate_html_report(stats, output_path)
            content = Path(output_path).read_text()
            assert "Week Growth" in content
            assert "Month Growth" in content
            assert "+25.0%" in content
            assert "-10.5%" in content
        finally:
            Path(output_path).unlink(missing_ok=True)

    def test_generate_html_report_omits_growth_when_absent(self):
        """generate_html_report should not show growth columns when stats lack growth data."""
        stats = [
            {
                "package_name": "test-pkg",
                "total": 1000,
                "last_month": 300,
                "last_week": 70,
                "last_day": 10,
            }
        ]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
            output_path = f.name

        try:
            generate_html_report(stats, output_path)
            content = Path(output_path).read_text()
            assert "Week Growth" not in content
            assert "Month Growth" not in content
        finally:
            Path(output_path).unlink(missing_ok=True)


class TestPieChart:
    """Tests for SVG pie chart generation."""

    def test_make_svg_pie_chart_creates_svg(self):
        """make_svg_pie_chart should create valid SVG."""
        data = [("Linux", 5000), ("Windows", 2000), ("Darwin", 1000)]
        svg = make_svg_pie_chart(data, "test-chart")

        assert "<svg" in svg
        assert "test-chart" in svg
        assert "</svg>" in svg
        assert "path" in svg  # Pie slices are paths

    def test_make_svg_pie_chart_includes_legend(self):
        """make_svg_pie_chart should include legend with percentages."""
        data = [("Linux", 5000), ("Windows", 2500), ("Darwin", 2500)]
        svg = make_svg_pie_chart(data, "test-chart")

        assert "Linux" in svg
        assert "Windows" in svg
        assert "Darwin" in svg
        assert "%" in svg  # Should show percentages

    def test_make_svg_pie_chart_empty_data(self):
        """make_svg_pie_chart should handle empty data."""
        svg = make_svg_pie_chart([], "test-chart")
        assert svg == ""

    def test_make_svg_pie_chart_zero_total(self):
        """make_svg_pie_chart should handle zero total."""
        data = [("Linux", 0), ("Windows", 0)]
        svg = make_svg_pie_chart(data, "test-chart")
        assert "No data" in svg

    def test_make_svg_pie_chart_groups_others(self):
        """make_svg_pie_chart should group items beyond top 5 as 'Other'."""
        data = [
            ("A", 100),
            ("B", 90),
            ("C", 80),
            ("D", 70),
            ("E", 60),
            ("F", 50),
            ("G", 40),
            ("H", 30),
        ]
        svg = make_svg_pie_chart(data, "test-chart")

        # Should have "Other" in legend
        assert "Other" in svg
        # Should not have all individual items beyond top 5
        assert "H" not in svg


class TestPackageHTMLReport:
    """Tests for single-package HTML report generation."""

    def test_generate_package_html_report_creates_file(self):
        """generate_package_html_report should create HTML file."""
        stats = {"total": 1000, "last_month": 300, "last_week": 70, "last_day": 10}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
            output_path = f.name

        python_response = json.dumps(
            {"data": [{"category": "3.11", "downloads": 2000}]}
        )
        system_response = json.dumps(
            {"data": [{"category": "Linux", "downloads": 4000}]}
        )

        try:
            with patch(
                "pkgdb.api.pypistats.python_minor", return_value=python_response
            ):
                with patch("pkgdb.api.pypistats.system", return_value=system_response):
                    generate_package_html_report("test-pkg", output_path, stats=stats)

            assert Path(output_path).exists()
            content = Path(output_path).read_text()
            assert "<!DOCTYPE html>" in content
            assert "test-pkg" in content
        finally:
            Path(output_path).unlink(missing_ok=True)

    def test_generate_package_html_report_includes_stats_cards(self):
        """generate_package_html_report should include download stat cards."""
        stats = {"total": 1000, "last_month": 300, "last_week": 70, "last_day": 10}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
            output_path = f.name

        python_response = json.dumps({"data": []})
        system_response = json.dumps({"data": []})

        try:
            with patch(
                "pkgdb.api.pypistats.python_minor", return_value=python_response
            ):
                with patch("pkgdb.api.pypistats.system", return_value=system_response):
                    generate_package_html_report("test-pkg", output_path, stats=stats)

            content = Path(output_path).read_text()
            assert "Total Downloads" in content
            assert "Last Month" in content
            assert "Last Week" in content
            assert "Last Day" in content
            assert "1,000" in content  # Formatted total
        finally:
            Path(output_path).unlink(missing_ok=True)

    def test_generate_package_html_report_includes_env_charts(self):
        """generate_package_html_report should include environment pie charts."""
        stats = {"total": 1000, "last_month": 300, "last_week": 70, "last_day": 10}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
            output_path = f.name

        python_response = json.dumps(
            {
                "data": [
                    {"category": "3.11", "downloads": 2000},
                    {"category": "3.10", "downloads": 1000},
                ]
            }
        )
        system_response = json.dumps(
            {
                "data": [
                    {"category": "Linux", "downloads": 4000},
                    {"category": "Windows", "downloads": 1000},
                ]
            }
        )

        try:
            with patch(
                "pkgdb.api.pypistats.python_minor", return_value=python_response
            ):
                with patch("pkgdb.api.pypistats.system", return_value=system_response):
                    generate_package_html_report("test-pkg", output_path, stats=stats)

            content = Path(output_path).read_text()
            assert "Environment Distribution" in content
            assert "py-version-chart" in content
            assert "os-chart" in content
        finally:
            Path(output_path).unlink(missing_ok=True)

    def test_generate_package_html_report_with_history(self):
        """generate_package_html_report should include history chart when available."""
        stats = {"total": 3000, "last_month": 900, "last_week": 210, "last_day": 30}
        history = [
            {
                "package_name": "test-pkg",
                "fetch_date": "2024-01-01",
                "total": 1000,
                "last_month": 300,
                "last_week": 70,
                "last_day": 10,
            },
            {
                "package_name": "test-pkg",
                "fetch_date": "2024-01-02",
                "total": 2000,
                "last_month": 600,
                "last_week": 140,
                "last_day": 20,
            },
            {
                "package_name": "test-pkg",
                "fetch_date": "2024-01-03",
                "total": 3000,
                "last_month": 900,
                "last_week": 210,
                "last_day": 30,
            },
        ]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
            output_path = f.name

        python_response = json.dumps({"data": []})
        system_response = json.dumps({"data": []})

        try:
            with patch(
                "pkgdb.api.pypistats.python_minor", return_value=python_response
            ):
                with patch("pkgdb.api.pypistats.system", return_value=system_response):
                    generate_package_html_report(
                        "test-pkg", output_path, stats=stats, history=history
                    )

            content = Path(output_path).read_text()
            assert "Downloads Over Time" in content
            assert "polyline" in content  # SVG line element
        finally:
            Path(output_path).unlink(missing_ok=True)


class TestChartEdgeCases:
    """Tests for edge cases in chart generation."""

    def test_pie_chart_exactly_six_items(self):
        """Pie chart with exactly PIE_CHART_MAX_ITEMS should not group into Other."""
        data = [
            ("A", 100),
            ("B", 90),
            ("C", 80),
            ("D", 70),
            ("E", 60),
            ("F", 50),
        ]
        svg = make_svg_pie_chart(data, "test-chart")

        # All items should be present (no "Other")
        assert "A" in svg
        assert "B" in svg
        assert "C" in svg
        assert "D" in svg
        assert "E" in svg
        assert "F" in svg
        assert "Other" not in svg

    def test_pie_chart_seven_items_groups_other(self):
        """Pie chart with 7 items should group last items into Other."""
        data = [
            ("A", 100),
            ("B", 90),
            ("C", 80),
            ("D", 70),
            ("E", 60),
            ("F", 50),
            ("G", 40),
        ]
        svg = make_svg_pie_chart(data, "test-chart")

        # Should have "Other" for items beyond limit
        assert "Other" in svg
        # Last item should not appear individually
        assert "G" not in svg

    def test_pie_chart_single_item(self):
        """Pie chart with single item should render correctly."""
        data = [("Only", 100)]
        svg = make_svg_pie_chart(data, "test-chart")

        assert "<svg" in svg
        assert "Only" in svg
        assert "100.0%" in svg

    def test_pie_chart_very_small_slice(self):
        """Pie chart should handle very small percentage slices."""
        data = [("Big", 99999), ("Tiny", 1)]
        svg = make_svg_pie_chart(data, "test-chart")

        assert "Big" in svg
        assert "Tiny" in svg
        # Small slice should still render (even if thin)
        assert svg.count("<path") >= 2

    def test_line_chart_single_data_point(self):
        """Line chart with single data point should return empty."""
        from pkgdb.reports import _make_single_line_chart

        dates = ["2024-01-01"]
        values = [1000]
        svg = _make_single_line_chart(dates, values)

        # Single point cannot form a line
        assert svg == ""

    def test_line_chart_two_data_points(self):
        """Line chart with two data points should render."""
        from pkgdb.reports import _make_single_line_chart

        dates = ["2024-01-01", "2024-01-02"]
        values = [1000, 2000]
        svg = _make_single_line_chart(dates, values)

        assert "<svg" in svg
        assert "polyline" in svg

    def test_line_chart_constant_values(self):
        """Line chart with all same values should render flat line."""
        from pkgdb.reports import _make_single_line_chart

        dates = ["2024-01-01", "2024-01-02", "2024-01-03"]
        values = [1000, 1000, 1000]
        svg = _make_single_line_chart(dates, values)

        assert "<svg" in svg
        assert "polyline" in svg

    def test_line_chart_zero_values(self):
        """Line chart with zero values should render."""
        from pkgdb.reports import _make_single_line_chart

        dates = ["2024-01-01", "2024-01-02", "2024-01-03"]
        values = [0, 0, 0]
        svg = _make_single_line_chart(dates, values)

        assert "<svg" in svg

    def test_bar_chart_very_large_numbers(self):
        """Bar chart should handle very large download numbers."""
        from pkgdb.reports import _make_svg_bar_chart

        data = [
            ("huge-pkg", 1_000_000_000),  # 1 billion
            ("big-pkg", 100_000_000),  # 100 million
        ]
        svg = _make_svg_bar_chart(data, "Downloads", "test-chart")

        assert "<svg" in svg
        assert "huge-pkg" in svg
        assert "1,000,000,000" in svg

    def test_bar_chart_single_item(self):
        """Bar chart with single item should render."""
        from pkgdb.reports import _make_svg_bar_chart

        data = [("only-pkg", 1000)]
        svg = _make_svg_bar_chart(data, "Downloads", "test-chart")

        assert "<svg" in svg
        assert "only-pkg" in svg

    def test_bar_chart_empty_data(self):
        """Bar chart with empty data should return empty string."""
        from pkgdb.reports import _make_svg_bar_chart

        svg = _make_svg_bar_chart([], "Downloads", "test-chart")
        assert svg == ""

    def test_multi_line_chart_single_package(self):
        """Multi-line chart with single package should render."""
        from pkgdb.reports import _make_multi_line_chart

        history = {
            "pkg-a": [
                {"fetch_date": "2024-01-01", "total": 1000},
                {"fetch_date": "2024-01-02", "total": 2000},
            ]
        }
        svg = _make_multi_line_chart(history, "test-chart")

        assert "<svg" in svg
        assert "pkg-a" in svg

    def test_multi_line_chart_empty_history(self):
        """Multi-line chart with empty history should return empty."""
        from pkgdb.reports import _make_multi_line_chart

        svg = _make_multi_line_chart({}, "test-chart")
        assert svg == ""

    def test_multi_line_chart_single_date(self):
        """Multi-line chart with single date should show message."""
        from pkgdb.reports import _make_multi_line_chart

        history = {"pkg-a": [{"fetch_date": "2024-01-01", "total": 1000}]}
        svg = _make_multi_line_chart(history, "test-chart")

        assert "Not enough" in svg


class TestGithubInReport:
    """Tests for GitHub stats in HTML reports."""

    def test_report_includes_github_columns(self, temp_db):
        from pkgdb.reports import generate_html_report

        stats = [
            {
                "package_name": "test-pkg",
                "total": 50000,
                "last_month": 3000,
                "last_week": 700,
                "last_day": 100,
            }
        ]
        gh_stats = {
            "test-pkg": _make_repo_stats(
                stars=42,
                forks=5,
                language="Python",
                full_name="owner/test-pkg",
            ),
        }
        output = os.path.join(os.path.dirname(temp_db), "test_report.html")
        generate_html_report(stats, output, github_stats=gh_stats)

        with open(output) as f:
            html = f.read()

        assert "Stars" in html
        assert "Forks" in html
        assert "Language" in html
        assert "Activity" in html
        assert "Repository" in html
        assert "owner/test-pkg" in html
        assert "42" in html
        Path(output).unlink(missing_ok=True)

    def test_report_without_github_has_no_github_columns(self, temp_db):
        from pkgdb.reports import generate_html_report

        stats = [
            {
                "package_name": "test-pkg",
                "total": 50000,
                "last_month": 3000,
                "last_week": 700,
                "last_day": 100,
            }
        ]
        output = os.path.join(os.path.dirname(temp_db), "test_report.html")
        generate_html_report(stats, output)

        with open(output) as f:
            html = f.read()

        assert "Stars" not in html
        assert "Forks" not in html
        Path(output).unlink(missing_ok=True)

    def test_report_github_missing_package_shows_dash(self, temp_db):
        from pkgdb.reports import generate_html_report

        stats = [
            {
                "package_name": "has-gh",
                "total": 50000,
                "last_month": 3000,
                "last_week": 700,
                "last_day": 100,
            },
            {
                "package_name": "no-gh",
                "total": 1000,
                "last_month": 100,
                "last_week": 20,
                "last_day": 5,
            },
        ]
        gh_stats = {
            "has-gh": _make_repo_stats(stars=99, forks=7, full_name="owner/has-gh"),
        }
        output = os.path.join(os.path.dirname(temp_db), "test_report.html")
        generate_html_report(stats, output, github_stats=gh_stats)

        with open(output) as f:
            html = f.read()

        assert "owner/has-gh" in html
        # The no-gh row should have dash placeholders
        assert html.count("Stars") == 1  # only in header
        Path(output).unlink(missing_ok=True)

    def test_report_includes_open_issues(self, temp_db):
        """The GitHub columns should carry each repo's open issue count."""
        from pkgdb.reports import generate_html_report

        stats = [
            {
                "package_name": "test-pkg",
                "total": 50000,
                "last_month": 3000,
                "last_week": 700,
                "last_day": 100,
            }
        ]
        gh_stats = {
            "test-pkg": _make_repo_stats(
                full_name="owner/test-pkg",
                open_issues=25,
                open_issues_excl_prs=17,
            ),
        }
        output = os.path.join(os.path.dirname(temp_db), "test_report.html")
        generate_html_report(stats, output, github_stats=gh_stats)

        with open(output) as f:
            html = f.read()

        assert "Open Issues" in html
        # The issues-only count, not GitHub's PR-inclusive open_issues.
        assert '<a href="https://github.com/owner/test-pkg/issues">17</a>' in html
        assert ">25<" not in html
        Path(output).unlink(missing_ok=True)

    def test_report_open_issues_unknown_shows_dash(self, temp_db):
        """Without an issues-only count the cell is unknown, not zero."""
        from pkgdb.reports import generate_html_report

        stats = [
            {
                "package_name": "test-pkg",
                "total": 50000,
                "last_month": 3000,
                "last_week": 700,
                "last_day": 100,
            }
        ]
        gh_stats = {
            "test-pkg": _make_repo_stats(
                full_name="owner/test-pkg",
                open_issues=25,
                open_issues_excl_prs=None,
            ),
        }
        output = os.path.join(os.path.dirname(temp_db), "test_report.html")
        generate_html_report(stats, output, github_stats=gh_stats)

        with open(output) as f:
            html = f.read()

        assert "/issues" not in html
        assert ">25<" not in html
        assert html.count('<td class="number">-</td>') == 1
        Path(output).unlink(missing_ok=True)

    def test_report_open_issues_missing_package_shows_dash(self, temp_db):
        """A package without repo data gets a dash in the issues column."""
        from pkgdb.reports import generate_html_report

        stats = [
            {
                "package_name": "has-gh",
                "total": 50000,
                "last_month": 3000,
                "last_week": 700,
                "last_day": 100,
            },
            {
                "package_name": "no-gh",
                "total": 1000,
                "last_month": 100,
                "last_week": 20,
                "last_day": 5,
            },
        ]
        gh_stats = {
            "has-gh": _make_repo_stats(
                full_name="owner/has-gh", open_issues_excl_prs=3
            ),
        }
        output = os.path.join(os.path.dirname(temp_db), "test_report.html")
        generate_html_report(stats, output, github_stats=gh_stats)

        with open(output) as f:
            html = f.read()

        # The row without repo data emits six dash cells; stars, forks and
        # issues are the numeric ones.
        assert html.count("Open Issues") == 1
        assert html.count('<td class="number">-</td>') == 3
        Path(output).unlink(missing_ok=True)

    def test_package_name_links_to_git_repo(self, temp_db):
        """The project name should hyperlink to its git repo when known."""
        from pkgdb.reports import generate_html_report

        stats = [
            {
                "package_name": "test-pkg",
                "total": 50000,
                "last_month": 3000,
                "last_week": 700,
                "last_day": 100,
            }
        ]
        gh_stats = {
            "test-pkg": _make_repo_stats(full_name="owner/test-pkg"),
        }
        output = os.path.join(os.path.dirname(temp_db), "test_report.html")
        generate_html_report(stats, output, github_stats=gh_stats)

        with open(output) as f:
            html = f.read()

        assert '<a href="https://github.com/owner/test-pkg">test-pkg</a>' in html
        assert 'href="https://pypi.org/project/test-pkg/"' not in html
        Path(output).unlink(missing_ok=True)

    def test_package_name_falls_back_to_pypi_link(self, temp_db):
        """Packages without repo data keep their PyPI link on the name."""
        from pkgdb.reports import generate_html_report

        stats = [
            {
                "package_name": "has-gh",
                "total": 50000,
                "last_month": 3000,
                "last_week": 700,
                "last_day": 100,
            },
            {
                "package_name": "no-gh",
                "total": 1000,
                "last_month": 100,
                "last_week": 20,
                "last_day": 5,
            },
        ]
        gh_stats = {
            "has-gh": _make_repo_stats(full_name="owner/has-gh"),
        }
        output = os.path.join(os.path.dirname(temp_db), "test_report.html")
        generate_html_report(stats, output, github_stats=gh_stats)

        with open(output) as f:
            html = f.read()

        assert '<a href="https://github.com/owner/has-gh">has-gh</a>' in html
        assert '<a href="https://pypi.org/project/no-gh/">no-gh</a>' in html
        Path(output).unlink(missing_ok=True)


class TestProjectReport:
    """Tests for project report generation."""

    def test_generate_project_report_html(self, temp_db):
        """generate_project_html_report should produce valid HTML."""
        stats = {"total": 50000, "last_month": 3000, "last_week": 700, "last_day": 100}
        history = [
            {
                "package_name": "my-pkg",
                "fetch_date": "2026-03-01",
                "total": 40000,
                "last_month": 2500,
                "last_week": 600,
                "last_day": 80,
            },
            {
                "package_name": "my-pkg",
                "fetch_date": "2026-04-01",
                "total": 50000,
                "last_month": 3000,
                "last_week": 700,
                "last_day": 100,
            },
        ]
        pypi_releases = [
            PyPIRelease(version="0.1.0", upload_date="2026-03-15"),
        ]
        github_releases = [
            GitHubRelease(tag_name="v0.1.0", published_at="2026-03-16", name=None),
        ]

        python_versions = [{"category": "3.12", "downloads": 30000}]
        os_stats = [{"category": "Linux", "downloads": 20000}]

        output = os.path.join(os.path.dirname(temp_db), "project_report.html")
        result = generate_project_html_report(
            "my-pkg",
            output,
            stats=stats,
            history=history,
            pypi_releases=pypi_releases,
            github_releases=github_releases,
            python_versions=python_versions,
            os_stats=os_stats,
        )
        assert result is True

        with open(output) as f:
            html = f.read()

        assert "Project View" in html
        assert "my-pkg" in html
        assert "50,000" in html
        assert "0.1.0" in html
        assert "v0.1.0" in html
        assert "PyPI" in html
        assert "GitHub" in html
        # Chart markers should use the marker colors
        assert "#4a90a4" in html  # PyPI marker color
        assert "#e67e22" in html  # GitHub marker color
        # Env breakdown charts
        assert "3.12" in html
        assert "Linux" in html
        Path(output).unlink(missing_ok=True)

    def test_generate_project_report_no_github(self, temp_db):
        """Project report should work with PyPI releases only."""
        stats = {"total": 5000, "last_month": 300, "last_week": 70, "last_day": 10}
        history = [
            {
                "package_name": "my-pkg",
                "fetch_date": "2026-03-01",
                "total": 4000,
                "last_month": 250,
                "last_week": 60,
                "last_day": 8,
            },
            {
                "package_name": "my-pkg",
                "fetch_date": "2026-04-01",
                "total": 5000,
                "last_month": 300,
                "last_week": 70,
                "last_day": 10,
            },
        ]
        pypi_releases = [
            PyPIRelease(version="1.0", upload_date="2026-03-10"),
        ]

        output = os.path.join(os.path.dirname(temp_db), "project_report.html")
        result = generate_project_html_report(
            "my-pkg",
            output,
            stats=stats,
            history=history,
            pypi_releases=pypi_releases,
            github_releases=[],
            python_versions=[],
            os_stats=[],
        )
        assert result is True

        with open(output) as f:
            html = f.read()

        assert "PyPI" in html
        assert "GitHub releases" not in html  # No GitHub releases text
        Path(output).unlink(missing_ok=True)

    def test_generate_project_report_no_releases(self, temp_db):
        """Project report should handle no releases gracefully."""
        stats = {"total": 5000, "last_month": 300, "last_week": 70, "last_day": 10}

        output = os.path.join(os.path.dirname(temp_db), "project_report.html")
        result = generate_project_html_report(
            "my-pkg",
            output,
            stats=stats,
            history=[],
            pypi_releases=[],
            github_releases=[],
            python_versions=[],
            os_stats=[],
        )
        assert result is True

        with open(output) as f:
            html = f.read()

        assert "No release data available" in html
        Path(output).unlink(missing_ok=True)

    def test_project_report_prefers_daily_series(self, temp_db):
        """When daily_series is supplied, the chart uses it and is titled Daily."""
        stats = {"total": 5000, "last_month": 300, "last_week": 70, "last_day": 10}
        daily = [
            {
                "date": "2026-06-01",
                "dimension": "overall",
                "category": "without_mirrors",
                "downloads": 100,
            },
            {
                "date": "2026-06-02",
                "dimension": "overall",
                "category": "without_mirrors",
                "downloads": 137,
            },
            {
                "date": "2026-06-03",
                "dimension": "overall",
                "category": "without_mirrors",
                "downloads": 121,
            },
        ]
        output = os.path.join(os.path.dirname(temp_db), "daily_project.html")
        result = generate_project_html_report(
            "my-pkg",
            output,
            stats=stats,
            history=[],
            daily_series=daily,
            pypi_releases=[],
            github_releases=[],
            python_versions=[],
            os_stats=[],
        )
        assert result is True
        html = Path(output).read_text()
        assert "Daily Downloads" in html
        Path(output).unlink(missing_ok=True)

    def test_project_report_falls_back_to_snapshot(self, temp_db):
        """Without daily_series, the chart falls back to snapshot totals."""
        stats = {"total": 5000, "last_month": 300, "last_week": 70, "last_day": 10}
        history = [
            {
                "package_name": "my-pkg",
                "fetch_date": "2026-03-01",
                "total": 4000,
                "last_month": 250,
                "last_week": 60,
                "last_day": 8,
            },
            {
                "package_name": "my-pkg",
                "fetch_date": "2026-04-01",
                "total": 5000,
                "last_month": 300,
                "last_week": 70,
                "last_day": 10,
            },
        ]
        output = os.path.join(os.path.dirname(temp_db), "snap_project.html")
        result = generate_project_html_report(
            "my-pkg",
            output,
            stats=stats,
            history=history,
            daily_series=None,
            pypi_releases=[],
            github_releases=[],
            python_versions=[],
            os_stats=[],
        )
        assert result is True
        html = Path(output).read_text()
        assert "Downloads &amp; Releases" in html or "Downloads & Releases" in html
        assert "Daily Downloads" not in html
        Path(output).unlink(missing_ok=True)


def _make_repo_stats(**overrides):
    """Helper to create a RepoStats with sensible defaults."""
    defaults = dict(
        owner="test",
        name="repo",
        full_name="test/repo",
        description="Test repo",
        stars=100,
        forks=10,
        open_issues=5,
        watchers=50,
        language="Python",
        license="MIT",
        created_at=utcnow() - timedelta(days=365),
        updated_at=utcnow(),
        pushed_at=utcnow() - timedelta(days=1),
        archived=False,
        fork=False,
        default_branch="main",
        topics=["test"],
    )
    defaults.update(overrides)
    return RepoStats(**defaults)
