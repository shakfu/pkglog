"""HTTP server for the pkgdb interactive dashboard."""

import json
import logging
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from importlib import resources
from typing import Any
from urllib.parse import unquote, urlparse, parse_qs

from .service import PackageStatsService

logger = logging.getLogger("pkgdb")

# Content types for static assets
_CONTENT_TYPES = {
    ".js": "application/javascript",
    ".css": "text/css",
}


class DashboardHandler(BaseHTTPRequestHandler):
    """Request handler for the pkgdb dashboard.

    Routes:
        HTML pages:
            GET /                   -> Overview page
            GET /package/<name>     -> Package detail page
            GET /compare            -> Comparison page

        JSON API:
            GET /api/packages       -> All packages with latest stats
            GET /api/daily/<name>   -> True per-day download series
            GET /api/github-history/<name> -> GitHub stars/forks daily snapshots
            GET /api/history/<name> -> Download history for a package
            GET /api/history        -> All history data
            GET /api/env/<name>     -> Python version + OS breakdown
            GET /api/releases/<name> -> PyPI + GitHub releases

        Static assets:
            GET /static/<file>      -> Bundled JS/CSS files
    """

    def service(self) -> PackageStatsService:
        """Access the service instance attached to the server."""
        return self.server.service  # type: ignore[attr-defined, no-any-return]

    def log_message(self, format: str, *args: Any) -> None:
        """Route request logs through the pkgdb logger."""
        logger.debug(format, *args)

    def do_GET(self) -> None:
        """Dispatch GET requests to the appropriate handler."""
        parsed = urlparse(self.path)
        path = unquote(parsed.path).rstrip("/") or "/"
        query = parse_qs(parsed.query)

        # Static assets
        if path.startswith("/static/"):
            self._serve_static(path[8:])  # strip "/static/"
            return

        # JSON API
        if path.startswith("/api/"):
            self._dispatch_api(path, query)
            return

        # HTML pages
        from .dashboard import (
            generate_overview_page,
            generate_package_page,
            generate_comparison_page,
        )

        if path == "/":
            self._respond_html(generate_overview_page())
        elif path.startswith("/package/"):
            package_name = path[9:]  # strip "/package/"
            if package_name:
                self._respond_html(generate_package_page(package_name))
            else:
                self._respond_not_found()
        elif path == "/compare":
            self._respond_html(generate_comparison_page())
        else:
            self._respond_not_found()

    def _dispatch_api(self, path: str, query: dict[str, list[str]]) -> None:
        """Route API requests."""
        svc = self.service()

        if path == "/api/packages":
            stats = svc.get_stats(with_growth=True)
            self._respond_json(stats)

        elif path == "/api/history":
            limit = _parse_int(query, "limit", 90)
            all_history = svc.get_all_history(limit_per_package=limit)
            self._respond_json(all_history)

        elif path.startswith("/api/daily/"):
            package = path[11:]  # strip "/api/daily/"
            self._respond_json(svc.get_daily_totals(package))

        elif path.startswith("/api/github-history/"):
            package = path[20:]  # strip "/api/github-history/"
            self._respond_json(svc.get_github_history(package))

        elif path.startswith("/api/history/"):
            package = path[13:]  # strip "/api/history/"
            limit = _parse_int(query, "limit", 90)
            pkg_history = svc.get_history(package, limit=limit)
            self._respond_json(pkg_history)

        elif path.startswith("/api/env/"):
            package = path[9:]  # strip "/api/env/"
            py_versions, os_stats = svc.get_env_data(package)
            self._respond_json(
                {
                    "python_versions": py_versions or [],
                    "os_stats": os_stats or [],
                }
            )

        elif path == "/api/ci":
            # The last recorded scan, never a fresh one: a cold scan takes
            # seconds and the dashboard must not block on it.
            self._respond_json(svc.get_ci_rows())

        elif path.startswith("/api/releases/"):
            package = path[14:]  # strip "/api/releases/"
            pypi_releases, github_releases = svc.fetch_package_releases(package)
            self._respond_json(
                {
                    "pypi": pypi_releases or [],
                    "github": github_releases or [],
                }
            )

        else:
            self._respond_not_found()

    def _serve_static(self, filename: str) -> None:
        """Serve a bundled static file from the static/ package directory."""
        try:
            static_files = resources.files("pkgdb") / "static"
            file_ref = static_files / filename
            content = file_ref.read_bytes()
        except (FileNotFoundError, TypeError):
            self._respond_not_found()
            return

        # Determine content type
        ext = ""
        if "." in filename:
            ext = "." + filename.rsplit(".", 1)[-1]
        content_type = _CONTENT_TYPES.get(ext, "application/octet-stream")

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(content)

    def _respond_json(self, data: Any) -> None:
        """Send a JSON response."""
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_html(self, html: str) -> None:
        """Send an HTML response."""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_not_found(self) -> None:
        """Send a 404 response."""
        body = b"Not Found"
        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _parse_int(query: dict[str, list[str]], key: str, default: int) -> int:
    """Extract an integer query parameter with a default."""
    values = query.get(key, [])
    if values:
        try:
            return int(values[0])
        except (ValueError, IndexError):
            pass
    return default


def start_server(
    db_path: str,
    port: int = 8080,
    open_browser: bool = True,
) -> None:
    """Start the dashboard HTTP server.

    Args:
        db_path: Path to the SQLite database.
        port: Port to listen on.
        open_browser: Whether to open the dashboard in a browser.
    """
    service = PackageStatsService(db_path)

    try:
        server = HTTPServer(("", port), DashboardHandler)
    except OSError as e:
        if "Address already in use" in str(e) or getattr(e, "errno", 0) == 48:
            logger.error(
                "Port %d is already in use. Try a different port with --port.",
                port,
            )
            return
        raise

    server.service = service  # type: ignore[attr-defined]

    url = f"http://localhost:{port}"
    logger.info("Dashboard running at %s", url)
    logger.info("Press Ctrl+C to stop.")

    if open_browser:
        webbrowser.open_new_tab(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down dashboard server.")
    finally:
        server.server_close()
