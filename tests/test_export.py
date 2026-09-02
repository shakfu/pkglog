"""Tests for export format functions (CSV, JSON, Markdown)."""

import json

import pytest

from pkgdb import (
    export_csv,
    export_json,
    export_markdown,
)


class TestExportFormats:
    """Tests for export format functions."""

    @pytest.fixture
    def sample_stats(self):
        """Sample stats for export tests."""
        return [
            {
                "package_name": "pkg-a",
                "total": 10000,
                "last_month": 3000,
                "last_week": 700,
                "last_day": 100,
                "fetch_date": "2024-01-15",
            },
            {
                "package_name": "pkg-b",
                "total": 5000,
                "last_month": 1500,
                "last_week": 350,
                "last_day": 50,
                "fetch_date": "2024-01-15",
            },
        ]

    def test_export_csv_format(self, sample_stats):
        """export_csv should produce valid CSV output."""
        output = export_csv(sample_stats)

        # Check header
        assert (
            "rank,package_name,total,last_month,last_week,last_day,fetch_date" in output
        )

        # Check data rows
        assert "1,pkg-a,10000,3000,700,100,2024-01-15" in output
        assert "2,pkg-b,5000,1500,350,50,2024-01-15" in output

    def test_export_csv_empty_stats(self):
        """export_csv should handle empty stats."""
        output = export_csv([])
        # Should have header row
        assert "rank,package_name" in output
        # Should not have data rows beyond header
        assert output.count("pkg") == 0

    def test_export_json_format(self, sample_stats):
        """export_json should produce valid JSON output."""
        output = export_json(sample_stats)
        data = json.loads(output)

        assert "generated" in data
        assert "packages" in data
        assert len(data["packages"]) == 2

        pkg_a = data["packages"][0]
        assert pkg_a["rank"] == 1
        assert pkg_a["name"] == "pkg-a"
        assert pkg_a["total"] == 10000

    def test_export_json_empty_stats(self):
        """export_json should handle empty stats."""
        output = export_json([])
        data = json.loads(output)
        assert data["packages"] == []

    def test_export_markdown_format(self, sample_stats):
        """export_markdown should produce valid Markdown table."""
        output = export_markdown(sample_stats)
        lines = output.split("\n")

        # Check header
        assert "| Rank | Package | Total | Month | Week | Day |" in lines[0]
        assert "|------|---------|" in lines[1]

        # Check data rows contain package names
        assert "pkg-a" in output
        assert "pkg-b" in output

    def test_export_markdown_empty_stats(self):
        """export_markdown should handle empty stats."""
        output = export_markdown([])
        lines = output.split("\n")
        assert len(lines) == 2  # Just header and separator
