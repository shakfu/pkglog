"""Shared fixtures for pkgdb tests."""

import ipaddress
import json
import os
import socket
import tempfile
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

import pypistats._cache
import pytest

from pkgdb import get_db_connection, init_db

# Hostnames that resolve to this machine and so never leave it.
_LOCAL_HOSTS = frozenset({"localhost", "localhost.localdomain", ""})


def _is_local(address) -> bool:
    """Return True if `address` is loopback, unspecified, or a non-IP family.

    Non-tuple addresses belong to families like AF_UNIX that cannot reach the
    network, so they are always allowed.
    """
    if not isinstance(address, (tuple, list)) or not address:
        return True
    host = address[0]
    if not isinstance(host, str):
        return True
    if host.lower() in _LOCAL_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified


@pytest.fixture(autouse=True)
def isolate_pypistats_cache(tmp_path_factory, monkeypatch):
    """Point pypistats at a throwaway cache directory.

    pypistats serves responses from `~/.cache/pypistats` (or the platform
    equivalent) before hitting the network, so a developer machine that has
    ever run a real fetch will silently satisfy an unmocked call. CI starts
    with an empty cache and hits the network instead, which is how a missing
    mock ends up passing locally and failing there. An empty per-run cache
    makes both environments behave the same.
    """
    monkeypatch.setattr(
        pypistats._cache, "CACHE_DIR", tmp_path_factory.mktemp("pypistats-cache")
    )


@pytest.fixture(autouse=True)
def block_network(request, monkeypatch):
    """Fail any test that opens a connection beyond this machine.

    Live API calls make tests non-deterministic: they pass locally and then
    fail in CI when the upstream service rate-limits the runner. Blocking them
    turns a missing mock into an immediate, obvious error instead.

    Tests marked `integration` are exempt, since making real API calls is
    their whole purpose. Loopback is always allowed so the HTTP server tests
    keep working.
    """
    if request.node.get_closest_marker("integration"):
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create_connection = socket.create_connection

    def guard(address):
        if not _is_local(address):
            raise RuntimeError(
                f"Blocked network connection to {address!r} in "
                f"{request.node.nodeid}. Mock the API call, or mark the test "
                f"'integration' if it is meant to hit the real service."
            )

    def connect(self, address):
        guard(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        guard(address)
        return real_connect_ex(self, address)

    def create_connection(address, *args, **kwargs):
        guard(address)
        return real_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)


# Responses standing in for a package with no recorded downloads. They are
# valid payloads of the right shape, so code under test takes its normal path
# rather than an error path.
NO_RECENT = json.dumps({"data": {"last_day": 0, "last_week": 0, "last_month": 0}})
NO_CATEGORIES = json.dumps({"data": []})


@contextmanager
def mock_pypistats(recent=None, overall=None, python_minor=None, system=None):
    """Patch every pypistats endpoint that a fetch touches.

    A single package fetch calls `recent`, `overall` (once for the aggregate
    total and again for the daily series), `python_minor` and `system`.
    Patching only the two that a test asserts on leaves the others pointed at
    the live API, so this patches all four and lets a test supply just the
    responses it cares about; the rest report no downloads.

    Each argument is the JSON string that endpoint should return.
    """
    responses = {
        "recent": NO_RECENT if recent is None else recent,
        "overall": NO_CATEGORIES if overall is None else overall,
        "python_minor": NO_CATEGORIES if python_minor is None else python_minor,
        "system": NO_CATEGORIES if system is None else system,
    }
    with ExitStack() as stack:
        for name, response in responses.items():
            stack.enter_context(
                patch(f"pkgdb.api.pypistats.{name}", return_value=response)
            )
        yield


def track(conn, *package_names, added_date="2024-01-01"):
    """Register packages in the `packages` table.

    Tests that seed `package_stats` directly still need their packages listed
    as tracked, because tracked-package views (show, report, export, history)
    filter on `packages` so that removed packages stop appearing before
    `cleanup` physically purges their retained rows.
    """
    for name in package_names:
        conn.execute(
            "INSERT OR IGNORE INTO packages (package_name, added_date) VALUES (?, ?)",
            (name, added_date),
        )
    conn.commit()


@pytest.fixture
def in_timezone(request):
    """Run the test as if the machine were in the parametrized `tz`.

    Restores the original zone itself rather than through monkeypatch, since
    the process-wide zone only takes effect once `time.tzset()` reloads it,
    and that has to happen after the environment variable is put back.
    """
    original = os.environ.get("TZ")
    os.environ["TZ"] = request.getfixturevalue("tz")
    time.tzset()
    yield
    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    time.tzset()


@pytest.fixture(autouse=True)
def no_gh_cli_token(monkeypatch):
    """Keep the developer's `gh` login out of the tests.

    get_github_token() falls back to `gh auth token`, so on an authenticated
    machine every request would silently carry a real credential and the
    no-token tests would fail. Making the subprocess unavailable leaves the
    real code path in place for the tests that exercise it, which re-patch
    `subprocess.run` themselves. The once-per-process resolution cache is
    cleared either side so a lookup cannot leak between tests.
    """
    import pkgdb.github as gh

    monkeypatch.setattr(gh.subprocess, "run", _no_subprocess)
    gh._gh_cli_token_resolved = False
    gh._gh_cli_token_cached = None
    # The no-token and rate-limit warnings fire once per process, so a test
    # asserting on either needs them cleared rather than already spent.
    gh._warned_unauthenticated = False
    gh._warned_rate_limited = False
    yield
    gh._gh_cli_token_resolved = False
    gh._gh_cli_token_cached = None
    gh._warned_unauthenticated = False
    gh._warned_rate_limited = False


def _no_subprocess(*args, **kwargs):
    raise FileNotFoundError("subprocess is unavailable in tests")


@pytest.fixture
def temp_db():
    """Create a temporary database file."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture
def temp_packages_file():
    """Create a temporary packages.json file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"published": ["package-a", "package-b"]}, f)
        packages_path = f.name
    yield packages_path
    Path(packages_path).unlink(missing_ok=True)


@pytest.fixture
def db_conn(temp_db):
    """Create an initialized database connection."""
    conn = get_db_connection(temp_db)
    init_db(conn)
    yield conn
    conn.close()
