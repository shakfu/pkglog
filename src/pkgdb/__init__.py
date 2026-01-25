"""
pkgdb - Track PyPI package download statistics.

Reads published packages from packages.yml, fetches download statistics
via pypistats, stores data in SQLite, and generates HTML reports.
"""

__version__ = "0.1.2"

# Re-export public API from submodules
from .api import (
    aggregate_env_stats,
    fetch_os_stats,
    fetch_package_stats,
    fetch_python_versions,
)
from .cli import (
    DEFAULT_PACKAGES_FILE,
    import_packages_from_file,
    load_packages,
    load_packages_from_file,
    main,
)
from .db import (
    DEFAULT_DB_FILE,
    DEFAULT_REPORT_FILE,
    add_package,
    get_all_history,
    get_config_dir,
    get_db,
    get_db_connection,
    get_latest_stats,
    get_package_history,
    get_packages,
    get_stats_with_growth,
    init_db,
    remove_package,
    store_stats,
)
from .export import (
    export_csv,
    export_json,
    export_markdown,
)
from .reports import (
    generate_html_report,
    generate_package_html_report,
    make_svg_pie_chart,
)
from .service import (
    FetchResult,
    PackageDetails,
    PackageInfo,
    PackageStatsService,
)
from .utils import (
    calculate_growth,
    make_sparkline,
    validate_package_name,
)

__all__ = [
    # Version
    "__version__",
    # API
    "aggregate_env_stats",
    "fetch_os_stats",
    "fetch_package_stats",
    "fetch_python_versions",
    # CLI
    "DEFAULT_PACKAGES_FILE",
    "import_packages_from_file",
    "load_packages",
    "load_packages_from_file",
    "main",
    # Database
    "DEFAULT_DB_FILE",
    "DEFAULT_REPORT_FILE",
    "add_package",
    "get_all_history",
    "get_config_dir",
    "get_db",
    "get_db_connection",
    "get_latest_stats",
    "get_package_history",
    "get_packages",
    "get_stats_with_growth",
    "init_db",
    "remove_package",
    "store_stats",
    # Export
    "export_csv",
    "export_json",
    "export_markdown",
    # Reports
    "generate_html_report",
    "generate_package_html_report",
    "make_svg_pie_chart",
    # Service
    "FetchResult",
    "PackageDetails",
    "PackageInfo",
    "PackageStatsService",
    # Utils
    "calculate_growth",
    "make_sparkline",
    "validate_package_name",
]
