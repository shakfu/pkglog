"""Utility functions for pkgdb."""

import os
import re
from pathlib import Path

# -----------------------------------------------------------------------------
# Package Validation Constants
# -----------------------------------------------------------------------------

# PyPI package name pattern (PEP 508 compatible)
# - Must start and end with alphanumeric
# - Can contain alphanumeric, hyphens, underscores, and periods
# - Max 100 characters
_PACKAGE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9._-]*[a-zA-Z0-9])?$")
_MAX_PACKAGE_NAME_LENGTH = 100

# -----------------------------------------------------------------------------
# Sparkline Constants
# -----------------------------------------------------------------------------

# Default width for sparkline charts (number of characters)
SPARKLINE_WIDTH = 7

# Characters used to represent values in sparklines (low to high)
SPARKLINE_CHARS = " _.,:-=+*#"


def validate_output_path(
    path: str,
    allowed_extensions: list[str] | None = None,
    must_be_writable: bool = True,
) -> tuple[bool, str]:
    """Validate that an output path is safe and writable.

    Security checks:
    - Resolves to absolute path to detect traversal attempts
    - Checks parent directory exists and is writable
    - Optionally validates file extension
    - Prevents writing to sensitive system directories

    Args:
        path: Output file path to validate.
        allowed_extensions: List of allowed extensions (e.g., ['.html', '.csv']).
                          If None, any extension is allowed.
        must_be_writable: If True, verify parent directory is writable.

    Returns:
        Tuple of (is_valid, error_message). If valid, error_message is empty.
    """
    if not path:
        return False, "Output path cannot be empty"

    try:
        # Resolve to absolute path (detects ../ traversal)
        resolved = Path(path).resolve()

        # Check for obviously sensitive paths
        resolved_str = str(resolved).lower()
        sensitive_prefixes = [
            "/etc/",
            "/usr/",
            "/bin/",
            "/sbin/",
            "/var/",
            "/sys/",
            "/proc/",
            "/boot/",
            "/root/",
            "c:\\windows\\",
            "c:\\program files\\",
            "c:\\programdata\\",
        ]
        for prefix in sensitive_prefixes:
            if resolved_str.startswith(prefix):
                return False, f"Cannot write to system directory: {resolved.parent}"

        # Check extension if specified
        if allowed_extensions:
            ext = resolved.suffix.lower()
            if ext not in [e.lower() for e in allowed_extensions]:
                return False, (
                    f"Invalid file extension '{ext}'. "
                    f"Allowed: {', '.join(allowed_extensions)}"
                )

        # Check parent directory exists
        parent = resolved.parent
        if not parent.exists():
            return False, f"Parent directory does not exist: {parent}"

        # Check parent is writable
        if must_be_writable and not os.access(parent, os.W_OK):
            return False, f"Parent directory is not writable: {parent}"

        return True, ""

    except (OSError, ValueError) as e:
        return False, f"Invalid path: {e}"


def validate_package_name(name: str) -> tuple[bool, str]:
    """Validate that a package name follows PyPI naming conventions.

    Args:
        name: Package name to validate.

    Returns:
        Tuple of (is_valid, error_message). If valid, error_message is empty.
    """
    if not name:
        return False, "Package name cannot be empty"

    if len(name) > _MAX_PACKAGE_NAME_LENGTH:
        return False, f"Package name exceeds {_MAX_PACKAGE_NAME_LENGTH} characters"

    if not _PACKAGE_NAME_PATTERN.match(name):
        return False, (
            "Package name must start and end with alphanumeric characters "
            "and contain only letters, numbers, hyphens, underscores, or periods"
        )

    return True, ""


def calculate_growth(current: int | None, previous: int | None) -> float | None:
    """Calculate percentage growth between two values."""
    if previous is None or previous == 0:
        return None
    if current is None:
        return None
    return ((current - previous) / previous) * 100


def make_sparkline(values: list[int], width: int = SPARKLINE_WIDTH) -> str:
    """Generate an ASCII sparkline from a list of values.

    Args:
        values: List of integer values to visualize.
        width: Number of characters in the sparkline (default: SPARKLINE_WIDTH).

    Returns:
        ASCII string representing the trend of values.
    """
    if not values:
        return " " * width

    # Use last 'width' values
    values = values[-width:]

    # Pad with zeros if not enough values
    if len(values) < width:
        values = [0] * (width - len(values)) + values

    min_val = min(values)
    max_val = max(values)

    if max_val == min_val:
        # All values equal - use middle character
        mid_idx = len(SPARKLINE_CHARS) // 2
        return SPARKLINE_CHARS[mid_idx] * width

    sparkline = ""
    for v in values:
        idx = int((v - min_val) / (max_val - min_val) * (len(SPARKLINE_CHARS) - 1))
        sparkline += SPARKLINE_CHARS[idx]

    return sparkline
