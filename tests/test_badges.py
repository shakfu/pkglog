"""Tests for SVG badge generation."""

from pathlib import Path
from unittest.mock import patch

import pytest

from pkgdb import (
    get_db_connection,
    init_db,
    generate_badge_svg,
    generate_downloads_badge,
    BADGE_COLORS,
    PackageStatsService,
    main,
)


class TestBadgeGeneration:
    """Tests for SVG badge generation."""

    def test_generate_badge_svg_basic(self):
        """generate_badge_svg should return valid SVG."""
        svg = generate_badge_svg("downloads", "1.2M")

        assert svg.startswith("<svg")
        assert "</svg>" in svg
        assert "downloads" in svg
        assert "1.2M" in svg

    def test_generate_badge_svg_custom_colors(self):
        """generate_badge_svg should accept custom colors."""
        svg = generate_badge_svg(
            "test", "value", color="#ff0000", label_color="#00ff00"
        )

        assert "#ff0000" in svg
        assert "#00ff00" in svg

    def test_generate_downloads_badge_formats_count(self):
        """generate_downloads_badge should format large numbers."""
        # Test millions
        svg = generate_downloads_badge(1_500_000)
        assert "1.5M" in svg

        # Test thousands
        svg = generate_downloads_badge(45_000)
        assert "45.0K" in svg

        # Test small numbers
        svg = generate_downloads_badge(500)
        assert "500" in svg

    def test_generate_downloads_badge_periods(self):
        """generate_downloads_badge should use correct labels for periods."""
        svg_total = generate_downloads_badge(1000, period="total")
        assert "downloads" in svg_total

        svg_month = generate_downloads_badge(1000, period="month")
        assert "downloads/month" in svg_month

        svg_week = generate_downloads_badge(1000, period="week")
        assert "downloads/week" in svg_week

        svg_day = generate_downloads_badge(1000, period="day")
        assert "downloads/day" in svg_day

    def test_generate_downloads_badge_auto_color(self):
        """generate_downloads_badge should auto-select color based on count."""
        # High count should get bright green
        svg_high = generate_downloads_badge(2_000_000)
        assert BADGE_COLORS["brightgreen"] in svg_high

        # Low count should get gray
        svg_low = generate_downloads_badge(100)
        assert BADGE_COLORS["gray"] in svg_low

    def test_service_generate_badge(self, temp_db):
        """Service.generate_badge should return SVG for tracked package."""
        service = PackageStatsService(temp_db)

        # Add package and stats
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.execute(
            "INSERT INTO packages (package_name, added_date) VALUES ('test-pkg', '2024-01-01')"
        )
        conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES ('test-pkg', '2024-01-15', 100, 700, 3000, 50000)
        """)
        conn.commit()
        conn.close()

        svg = service.generate_badge("test-pkg")
        assert svg is not None
        assert "<svg" in svg
        assert "50.0K" in svg  # 50000 formatted

    def test_service_generate_badge_different_periods(self, temp_db):
        """Service.generate_badge should support different periods."""
        service = PackageStatsService(temp_db)

        # Add package and stats
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.execute(
            "INSERT INTO packages (package_name, added_date) VALUES ('test-pkg', '2024-01-01')"
        )
        conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES ('test-pkg', '2024-01-15', 100, 700, 3000, 50000)
        """)
        conn.commit()
        conn.close()

        svg_month = service.generate_badge("test-pkg", period="month")
        assert "3.0K" in svg_month  # 3000 formatted
        assert "downloads/month" in svg_month

    def test_service_generate_badge_nonexistent_package(self, temp_db):
        """Service.generate_badge should return None for unknown package."""
        service = PackageStatsService(temp_db)

        svg = service.generate_badge("nonexistent-pkg")
        assert svg is None

    def test_badge_cli_parser(self):
        """badge command should have correct arguments."""
        from pkgdb.cli import create_parser

        parser = create_parser()
        args = parser.parse_args(["badge", "test-pkg"])
        assert args.package == "test-pkg"
        assert args.period == "total"

        args = parser.parse_args(
            ["badge", "test-pkg", "-p", "month", "-o", "badge.svg"]
        )
        assert args.period == "month"
        assert args.output == "badge.svg"

    def test_cmd_badge_outputs_svg(self, temp_db, capsys):
        """cmd_badge should output SVG to stdout."""
        # Add package and stats
        conn = get_db_connection(temp_db)
        init_db(conn)
        conn.execute(
            "INSERT INTO packages (package_name, added_date) VALUES ('test-pkg', '2024-01-01')"
        )
        conn.execute("""
            INSERT INTO package_stats
            (package_name, fetch_date, last_day, last_week, last_month, total)
            VALUES ('test-pkg', '2024-01-15', 100, 700, 3000, 50000)
        """)
        conn.commit()
        conn.close()

        with patch("sys.argv", ["pkgdb", "-d", temp_db, "badge", "test-pkg"]):
            main()

        captured = capsys.readouterr()
        assert "<svg" in captured.out
        assert "</svg>" in captured.out
