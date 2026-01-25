"""Utility functions for pkgdb."""

import re

# PyPI package name pattern (PEP 508 compatible)
# - Must start and end with alphanumeric
# - Can contain alphanumeric, hyphens, underscores, and periods
# - Max 100 characters
_PACKAGE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9._-]*[a-zA-Z0-9])?$")
_MAX_PACKAGE_NAME_LENGTH = 100


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


def make_sparkline(values: list[int], width: int = 7) -> str:
    """Generate an ASCII sparkline from a list of values."""
    if not values:
        return " " * width

    # Use last 'width' values
    values = values[-width:]

    # Pad with zeros if not enough values
    if len(values) < width:
        values = [0] * (width - len(values)) + values

    blocks = " _.,:-=+*#"
    min_val = min(values)
    max_val = max(values)

    if max_val == min_val:
        return blocks[4] * width

    sparkline = ""
    for v in values:
        idx = int((v - min_val) / (max_val - min_val) * (len(blocks) - 1))
        sparkline += blocks[idx]

    return sparkline
