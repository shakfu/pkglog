"""HTML page templates for the pkgdb interactive dashboard."""

from .reports import THEME_PRIMARY_COLOR

# Color palette for multi-series charts
CHART_COLORS = [
    "#4a90a4",
    "#e67e22",
    "#2ecc71",
    "#9b59b6",
    "#e74c3c",
    "#f1c40f",
    "#1abc9c",
    "#34495e",
]

# Maximum items to show in horizontal bar charts
BAR_CHART_MAX_ITEMS = 10


def _base_page(title: str, body: str, extra_head: str = "") -> str:
    """Wrap body content in a full HTML document with common styles and uPlot."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - pkgdb</title>
<link rel="stylesheet" href="/static/uplot.min.css">
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #f5f5f5;
    color: #333;
    padding: 20px;
}}
.container {{ max-width: 1200px; margin: 0 auto; }}
nav {{
    display: flex;
    align-items: center;
    gap: 24px;
    margin-bottom: 24px;
    padding: 12px 20px;
    background: white;
    border-radius: 8px;
    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
}}
nav .brand {{
    font-weight: 700;
    font-size: 1.2em;
    color: {THEME_PRIMARY_COLOR};
    text-decoration: none;
}}
nav a {{
    color: {THEME_PRIMARY_COLOR};
    text-decoration: none;
    font-weight: 500;
}}
nav a:hover {{ text-decoration: underline; }}
.card {{
    background: white;
    border-radius: 8px;
    padding: 20px;
    margin-bottom: 20px;
    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
}}
.card h2 {{
    margin-bottom: 16px;
    font-size: 1.1em;
    color: #555;
}}
.stats-row {{
    display: flex;
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 20px;
}}
.stat-card {{
    background: white;
    border-radius: 8px;
    padding: 16px 24px;
    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    text-align: center;
    min-width: 140px;
}}
.stat-card .value {{
    font-size: 1.6em;
    font-weight: 700;
    color: {THEME_PRIMARY_COLOR};
}}
.stat-card .label {{
    font-size: 0.85em;
    color: #888;
    margin-top: 4px;
}}
table {{
    width: 100%;
    border-collapse: collapse;
}}
th, td {{
    padding: 10px 12px;
    text-align: left;
    border-bottom: 1px solid #eee;
}}
th {{
    background: {THEME_PRIMARY_COLOR};
    color: white;
    cursor: pointer;
    user-select: none;
    position: sticky;
    top: 0;
    white-space: nowrap;
}}
th:hover {{ opacity: 0.9; }}
th .sort-arrow {{ margin-left: 4px; font-size: 0.8em; }}
tr:hover {{ background: #f9f9f9; }}
.number {{ text-align: right; font-family: monospace; }}
a.pkg-link {{
    color: {THEME_PRIMARY_COLOR};
    text-decoration: none;
    font-weight: 500;
}}
a.pkg-link:hover {{ text-decoration: underline; }}
.filter-input {{
    width: 100%;
    padding: 10px 14px;
    border: 1px solid #ddd;
    border-radius: 6px;
    font-size: 1em;
    margin-bottom: 16px;
    outline: none;
}}
.filter-input:focus {{ border-color: {THEME_PRIMARY_COLOR}; }}
.bar-chart {{
    margin: 8px 0;
}}
.bar-row {{
    display: flex;
    align-items: center;
    margin-bottom: 6px;
    font-size: 0.9em;
}}
.bar-label {{
    width: 120px;
    text-align: right;
    padding-right: 12px;
    color: #555;
    flex-shrink: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}}
.bar-track {{
    flex: 1;
    background: #eee;
    border-radius: 4px;
    height: 22px;
    position: relative;
    overflow: hidden;
}}
.bar-fill {{
    height: 100%;
    border-radius: 4px;
    background: {THEME_PRIMARY_COLOR};
    transition: width 0.3s;
    min-width: 2px;
}}
.bar-value {{
    width: 80px;
    text-align: right;
    padding-left: 10px;
    font-family: monospace;
    font-size: 0.85em;
    color: #555;
    flex-shrink: 0;
}}
.growth-pos {{ color: #2ecc71; }}
.growth-neg {{ color: #e74c3c; }}
.growth-neutral {{ color: #888; }}
.chart-wrapper {{
    width: 100%;
    overflow-x: auto;
}}
.u-legend .u-series:first-child {{ display: none; }}
.loading {{ color: #888; font-style: italic; padding: 20px; }}
.empty {{ color: #888; padding: 20px; text-align: center; }}
{extra_head}
</style>
</head>
<body>
<div class="container">
{body}
</div>
<script src="/static/uplot.min.js"></script>
</body>
</html>"""


def _nav(active: str = "") -> str:
    """Generate the navigation bar."""
    links = [
        ("/", "Overview"),
        ("/compare", "Compare"),
    ]
    items = []
    for href, label in links:
        if label.lower() == active.lower():
            items.append(f'<span style="font-weight:700">{label}</span>')
        else:
            items.append(f'<a href="{href}">{label}</a>')
    return f"""<nav>
<a class="brand" href="/">pkgdb</a>
{"".join(items)}
</nav>"""


def generate_overview_page() -> str:
    """Generate the overview/index page HTML.

    Displays a sortable, filterable table of all tracked packages with
    sparkline charts. Each package name links to its detail page.
    Data is fetched client-side from /api/packages.
    """
    body = f"""{_nav("Overview")}

<div class="card" id="ci-card" style="display:none">
<h2>CI Status</h2>
<div id="ci-wrap"></div>
</div>

<div class="card">
<h2>Tracked Packages</h2>
<input type="text" class="filter-input" id="pkg-filter"
       placeholder="Filter packages..." autocomplete="off">
<div id="stats-summary" class="stats-row"></div>
<div id="pkg-table-wrap">
<p class="loading" id="loading-msg">Loading package data...</p>
</div>
</div>

<script>
document.addEventListener("DOMContentLoaded", function() {{
    fetch("/api/packages")
        .then(r => r.json())
        .then(data => renderOverview(data))
        .catch(e => {{
            document.getElementById("loading-msg").textContent =
                "Error loading data: " + e.message;
        }});
    fetch("/api/ci")
        .then(r => r.json())
        .then(rows => renderCI(rows))
        .catch(function() {{ /* CI is optional; stay quiet when absent */ }});
}});

function esc(s) {{
    var d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
}}

function renderCI(rows) {{
    // Hidden entirely until a scan has run, so the panel never nags.
    if (!rows || !rows.length) return;
    var card = document.getElementById("ci-card");
    card.style.display = "";

    var repos = {{}}, failingRepos = {{}};
    var failures = [];
    for (var i = 0; i < rows.length; i++) {{
        repos[rows[i].repo_key] = 1;
        if (rows[i].state === "FAIL") {{
            failingRepos[rows[i].repo_key] = 1;
            failures.push(rows[i]);
        }}
    }}
    var nRepos = Object.keys(repos).length;
    var nFailing = Object.keys(failingRepos).length;

    if (!failures.length) {{
        document.getElementById("ci-wrap").innerHTML =
            '<p class="loading">All ' + nRepos + ' repositories are green.</p>';
        return;
    }}

    var html = '<p class="loading">' + nFailing + ' of ' + nRepos +
        ' repositories have failing workflows.</p>' +
        '<table><thead><tr><th>Repository</th><th>Workflow</th>' +
        '<th>Branch</th><th class="number">Failing</th><th>Run</th>' +
        '</tr></thead><tbody>';
    for (var j = 0; j < failures.length; j++) {{
        var f = failures[j];
        html += '<tr>' +
            '<td><a href="https://github.com/' + encodeURI(f.repo_key) + '">' +
            esc(f.repo_key) + '</a></td>' +
            '<td>' + esc(f.workflow_name) + '</td>' +
            '<td>' + esc(f.branch || "-") + '</td>' +
            '<td class="number">' +
            (f.failing_days == null ? "-" : f.failing_days + "d") + '</td>' +
            '<td>' + (f.run_url
                ? '<a href="' + encodeURI(f.run_url) + '">run</a>' : "-") +
            '</td></tr>';
    }}
    html += '</tbody></table>';
    document.getElementById("ci-wrap").innerHTML = html;
}}

function fmt(n) {{
    if (n == null) return "-";
    return n.toLocaleString();
}}

function growthBadge(val) {{
    if (val == null) return '<span class="growth-neutral">-</span>';
    var sign = val >= 0 ? "+" : "";
    var cls = val > 0 ? "growth-pos" : val < 0 ? "growth-neg" : "growth-neutral";
    return '<span class="' + cls + '">' + sign + val.toFixed(1) + '%</span>';
}}

function renderOverview(packages) {{
    // Summary cards
    var totalPkgs = packages.length;
    var totalDl = packages.reduce(function(s, p) {{ return s + (p.total || 0); }}, 0);
    var totalMonth = packages.reduce(function(s, p) {{ return s + (p.last_month || 0); }}, 0);
    var totalWeek = packages.reduce(function(s, p) {{ return s + (p.last_week || 0); }}, 0);

    document.getElementById("stats-summary").innerHTML =
        '<div class="stat-card"><div class="value">' + totalPkgs + '</div><div class="label">Packages</div></div>' +
        '<div class="stat-card"><div class="value">' + fmt(totalDl) + '</div><div class="label">Total Downloads</div></div>' +
        '<div class="stat-card"><div class="value">' + fmt(totalMonth) + '</div><div class="label">Last Month</div></div>' +
        '<div class="stat-card"><div class="value">' + fmt(totalWeek) + '</div><div class="label">Last Week</div></div>';

    // Build table
    var html = '<table id="pkg-table"><thead><tr>' +
        '<th data-col="package_name" data-type="string">Package <span class="sort-arrow"></span></th>' +
        '<th data-col="total" data-type="number" class="number">Total <span class="sort-arrow"></span></th>' +
        '<th data-col="last_month" data-type="number" class="number">Month <span class="sort-arrow"></span></th>' +
        '<th data-col="last_week" data-type="number" class="number">Week <span class="sort-arrow"></span></th>' +
        '<th data-col="last_day" data-type="number" class="number">Day <span class="sort-arrow"></span></th>' +
        '<th data-col="week_growth" data-type="number" class="number">Week +/- <span class="sort-arrow"></span></th>' +
        '<th data-col="month_growth" data-type="number" class="number">Month +/- <span class="sort-arrow"></span></th>' +
        '</tr></thead><tbody>';

    for (var i = 0; i < packages.length; i++) {{
        var p = packages[i];
        html += '<tr data-name="' + (p.package_name || "").toLowerCase() + '">' +
            '<td><a class="pkg-link" href="/package/' + encodeURIComponent(p.package_name) + '">' + p.package_name + '</a></td>' +
            '<td class="number">' + fmt(p.total) + '</td>' +
            '<td class="number">' + fmt(p.last_month) + '</td>' +
            '<td class="number">' + fmt(p.last_week) + '</td>' +
            '<td class="number">' + fmt(p.last_day) + '</td>' +
            '<td class="number">' + growthBadge(p.week_growth) + '</td>' +
            '<td class="number">' + growthBadge(p.month_growth) + '</td>' +
            '</tr>';
    }}
    html += '</tbody></table>';

    document.getElementById("pkg-table-wrap").innerHTML = html;

    // Sortable columns
    var sortCol = "total";
    var sortAsc = false;
    var ths = document.querySelectorAll("#pkg-table th[data-col]");
    ths.forEach(function(th) {{
        th.addEventListener("click", function() {{
            var col = th.getAttribute("data-col");
            var typ = th.getAttribute("data-type");
            if (sortCol === col) {{ sortAsc = !sortAsc; }} else {{ sortCol = col; sortAsc = (typ === "string"); }}
            packages.sort(function(a, b) {{
                var va = a[col], vb = b[col];
                if (va == null) va = typ === "string" ? "" : -Infinity;
                if (vb == null) vb = typ === "string" ? "" : -Infinity;
                if (typ === "string") {{
                    var cmp = String(va).localeCompare(String(vb));
                    return sortAsc ? cmp : -cmp;
                }}
                return sortAsc ? va - vb : vb - va;
            }});
            renderOverview(packages);
            // Update sort arrows
            document.querySelectorAll("#pkg-table th .sort-arrow").forEach(function(s) {{ s.textContent = ""; }});
            var activeArrow = document.querySelector('#pkg-table th[data-col="' + col + '"] .sort-arrow');
            if (activeArrow) activeArrow.textContent = sortAsc ? " \\u25B2" : " \\u25BC";
        }});
    }});

    // Filter
    document.getElementById("pkg-filter").addEventListener("input", function(e) {{
        var q = e.target.value.toLowerCase();
        var rows = document.querySelectorAll("#pkg-table tbody tr");
        rows.forEach(function(row) {{
            row.style.display = row.getAttribute("data-name").indexOf(q) >= 0 ? "" : "none";
        }});
    }});
}}
</script>"""

    return _base_page("Overview", body)


def generate_package_page(package_name: str) -> str:
    """Generate the package detail page HTML.

    Displays stats cards, a zoomable download history chart (uPlot),
    and horizontal bar charts for Python version and OS breakdown.
    Data is fetched client-side from /api/history, /api/env, /api/releases.
    """
    escaped = (
        package_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )

    body = f"""{_nav()}

<div style="margin-bottom: 16px">
<a href="/" style="color:{THEME_PRIMARY_COLOR};text-decoration:none">&larr; Back to overview</a>
</div>

<h1 style="margin-bottom:20px">{escaped}</h1>

<div class="stats-row" id="detail-stats">
<p class="loading">Loading...</p>
</div>

<div class="card">
<h2>Download History</h2>
<p style="font-size:0.85em;color:#888;margin-bottom:8px">Click and drag to zoom, double-click to reset</p>
<div id="release-toggles" style="margin-bottom:8px;display:none">
<label style="margin-right:16px;font-size:0.9em"><input type="checkbox" id="toggle-pypi" checked> <span style="color:{CHART_COLORS[0]}">PyPI releases</span></label>
<label style="font-size:0.9em"><input type="checkbox" id="toggle-github" checked> <span style="color:{CHART_COLORS[1]}">GitHub releases</span></label>
</div>
<div class="chart-wrapper" id="history-chart"></div>
</div>

<div class="card" id="github-card" style="display:none">
<h2>GitHub Stars</h2>
<p style="font-size:0.85em;color:#888;margin-bottom:8px">Recorded once per day on <code>github</code> fetch; accumulates over time</p>
<div class="chart-wrapper" id="github-chart"></div>
</div>

<div style="display:flex;gap:20px;flex-wrap:wrap">
<div class="card" style="flex:1;min-width:300px">
<h2>Python Versions</h2>
<div id="py-bars"><p class="loading">Loading...</p></div>
</div>
<div class="card" style="flex:1;min-width:300px">
<h2>Operating Systems</h2>
<div id="os-bars"><p class="loading">Loading...</p></div>
</div>
</div>

<div class="card" id="releases-section" style="display:none">
<h2>Releases</h2>
<div id="releases-list"></div>
</div>

<script>
var pkgName = {_js_string(package_name)};

document.addEventListener("DOMContentLoaded", function() {{
    Promise.all([
        fetch("/api/packages").then(r => r.json()),
        fetch("/api/daily/" + encodeURIComponent(pkgName)).then(r => r.json()),
        fetch("/api/history/" + encodeURIComponent(pkgName) + "?limit=90").then(r => r.json()),
        fetch("/api/env/" + encodeURIComponent(pkgName)).then(r => r.json()),
        fetch("/api/releases/" + encodeURIComponent(pkgName)).then(r => r.json()),
        fetch("/api/github-history/" + encodeURIComponent(pkgName)).then(r => r.json()),
    ]).then(function(results) {{
        var allStats = results[0];
        var daily = results[1];
        var history = results[2];
        var env = results[3];
        var releases = results[4];
        var githubHistory = results[5];

        var pkg = allStats.find(function(p) {{ return p.package_name === pkgName; }});
        renderDetailStats(pkg);
        // Store for toggle rebuilds; prefer the true daily series when present.
        window._dailyData = daily;
        window._historyData = history;
        window._releasesData = releases;
        window._useDaily = !!(daily && daily.length);
        renderTrendChart(releases);
        // Show toggles if any releases exist
        if ((releases.pypi && releases.pypi.length) || (releases.github && releases.github.length)) {{
            document.getElementById("release-toggles").style.display = "";
        }}
        document.getElementById("toggle-pypi").addEventListener("change", rebuildChart);
        document.getElementById("toggle-github").addEventListener("change", rebuildChart);
        renderBarChart("py-bars", env.python_versions, "category", "downloads");
        renderBarChart("os-bars", env.os_stats, "category", "downloads");
        renderGithubChart(githubHistory);
        renderReleases(releases);
    }}).catch(function(e) {{
        document.getElementById("detail-stats").innerHTML =
            '<p style="color:#e74c3c">Error loading data: ' + e.message + '</p>';
    }});
}});

function fmt(n) {{
    if (n == null) return "-";
    return n.toLocaleString();
}}

function growthBadge(val) {{
    if (val == null) return "";
    var sign = val >= 0 ? "+" : "";
    var cls = val > 0 ? "growth-pos" : val < 0 ? "growth-neg" : "growth-neutral";
    return ' <span class="' + cls + '">(' + sign + val.toFixed(1) + '%)</span>';
}}

function renderDetailStats(pkg) {{
    if (!pkg) {{
        document.getElementById("detail-stats").innerHTML = '<p class="empty">No stats available.</p>';
        return;
    }}
    document.getElementById("detail-stats").innerHTML =
        '<div class="stat-card"><div class="value">' + fmt(pkg.total) + '</div><div class="label">Total Downloads</div></div>' +
        '<div class="stat-card"><div class="value">' + fmt(pkg.last_month) + growthBadge(pkg.month_growth) + '</div><div class="label">Last Month</div></div>' +
        '<div class="stat-card"><div class="value">' + fmt(pkg.last_week) + growthBadge(pkg.week_growth) + '</div><div class="label">Last Week</div></div>' +
        '<div class="stat-card"><div class="value">' + fmt(pkg.last_day) + '</div><div class="label">Last Day</div></div>';
}}

function rebuildChart() {{
    var rel = window._releasesData || {{ pypi: [], github: [] }};
    var filtered = {{
        pypi: document.getElementById("toggle-pypi").checked ? rel.pypi : [],
        github: document.getElementById("toggle-github").checked ? rel.github : [],
    }};
    renderTrendChart(filtered);
}}

var _historyChart = null;
var _githubChart = null;

// Route to the true daily series when available, else the snapshot progression.
function renderTrendChart(releases) {{
    if (window._useDaily) {{
        renderDailyChart(window._dailyData, releases);
    }} else {{
        renderHistoryChart(window._historyData, releases);
    }}
}}

function buildReleaseMarkers(releases) {{
    var releaseMarkers = [];
    var pypi = (releases && releases.pypi) || [];
    var github = (releases && releases.github) || [];
    pypi.forEach(function(r) {{
        if (r.upload_date) releaseMarkers.push({{
            ts: new Date(r.upload_date + "T00:00:00").getTime() / 1000,
            label: r.version,
            color: "{CHART_COLORS[0]}",
            source: "pypi",
        }});
    }});
    github.forEach(function(r) {{
        if (r.published_at) releaseMarkers.push({{
            ts: new Date(r.published_at + "T00:00:00").getTime() / 1000,
            label: r.tag_name,
            color: "{CHART_COLORS[1]}",
            source: "github",
        }});
    }});
    // Deduplicate within same source at same date
    var seen = {{}};
    return releaseMarkers.filter(function(m) {{
        var key = m.source + ":" + m.ts;
        if (seen[key]) return false;
        seen[key] = true;
        return true;
    }});
}}

function mountTrendChart(el, data, series, releaseMarkers, timestamps) {{
    // Reserve top padding for release labels (longest label ~6 chars at 10px rotated)
    var topPad = releaseMarkers.length > 0 ? 50 : 10;

    var opts = {{
        width: Math.min(el.clientWidth, 1100),
        height: 320 + topPad,
        padding: [topPad, 0, 0, 0],
        cursor: {{ drag: {{ x: true, y: false, setScale: true }} }},
        select: {{ show: true }},
        scales: {{
            x: {{ time: true }},
            y: {{ auto: true }},
        }},
        axes: [
            {{ stroke: "#888", grid: {{ stroke: "#eee" }} }},
            {{
                stroke: "#888",
                grid: {{ stroke: "#eee" }},
                values: function(u, vals) {{
                    return vals.map(function(v) {{
                        if (v >= 1e6) return (v/1e6).toFixed(1) + "M";
                        if (v >= 1e3) return (v/1e3).toFixed(1) + "K";
                        return v;
                    }});
                }},
            }},
        ],
        series: series,
        plugins: [releaseMarkersPlugin(releaseMarkers)],
    }};

    _historyChart = new uPlot(opts, data, el);
    var chart = _historyChart;

    // Double-click to reset zoom
    el.addEventListener("dblclick", function() {{
        chart.setScale("x", {{
            min: timestamps[0],
            max: timestamps[timestamps.length - 1],
        }});
    }});

    // Responsive resize
    window.addEventListener("resize", function() {{
        chart.setSize({{ width: Math.min(el.clientWidth, 1100), height: 320 }});
    }});
}}

function renderDailyChart(daily, releases) {{
    var el = document.getElementById("history-chart");
    if (_historyChart) {{
        _historyChart.destroy();
        _historyChart = null;
    }}
    if (!daily || daily.length === 0) {{
        el.innerHTML = '<p class="empty">No history data available.</p>';
        return;
    }}

    daily = daily.slice().sort(function(a, b) {{ return a.date < b.date ? -1 : 1; }});
    var timestamps = daily.map(function(d) {{
        return new Date(d.date + "T00:00:00").getTime() / 1000;
    }});
    var downloads = daily.map(function(d) {{ return d.downloads; }});
    var data = [timestamps, downloads];
    var series = [
        {{ label: "" }},
        {{ label: "Downloads/day", stroke: "{CHART_COLORS[0]}", width: 2, fill: "{CHART_COLORS[0]}22" }},
    ];
    mountTrendChart(el, data, series, buildReleaseMarkers(releases), timestamps);
}}

function renderHistoryChart(history, releases) {{
    var el = document.getElementById("history-chart");
    if (_historyChart) {{
        _historyChart.destroy();
        _historyChart = null;
    }}
    if (!history || history.length === 0) {{
        el.innerHTML = '<p class="empty">No history data available.</p>';
        return;
    }}

    // Sort by date ascending
    history.sort(function(a, b) {{ return a.fetch_date < b.fetch_date ? -1 : 1; }});

    var timestamps = history.map(function(h) {{
        return new Date(h.fetch_date + "T00:00:00").getTime() / 1000;
    }});
    var daily = history.map(function(h) {{ return h.last_day; }});
    var weekly = history.map(function(h) {{ return h.last_week; }});
    var monthly = history.map(function(h) {{ return h.last_month; }});
    var data = [timestamps, daily, weekly, monthly];
    var series = [
        {{ label: "" }},
        {{ label: "Daily", stroke: "{CHART_COLORS[0]}", width: 2, fill: "{CHART_COLORS[0]}22" }},
        {{ label: "Weekly", stroke: "{CHART_COLORS[1]}", width: 2 }},
        {{ label: "Monthly", stroke: "{CHART_COLORS[2]}", width: 2 }},
    ];
    mountTrendChart(el, data, series, buildReleaseMarkers(releases), timestamps);
}}

function releaseMarkersPlugin(markers) {{
    // Sort by timestamp so we can detect overlap
    markers.sort(function(a, b) {{ return a.ts - b.ts; }});

    return {{
        hooks: {{
            draw: [function(u) {{
                var ctx = u.ctx;
                var xMin = u.scales.x.min;
                var xMax = u.scales.x.max;
                var pxRatio = devicePixelRatio || 1;
                var plotTop = u.bbox.top / pxRatio;
                var plotHeight = u.bbox.height / pxRatio;

                ctx.save();
                ctx.setTransform(pxRatio, 0, 0, pxRatio, 0, 0);

                // Group markers by pixel position to detect overlaps
                var byPos = {{}};
                markers.forEach(function(m) {{
                    if (m.ts < xMin || m.ts > xMax) return;
                    var x = Math.round(u.valToPos(m.ts, "x"));
                    if (!byPos[x]) byPos[x] = [];
                    byPos[x].push(m);
                }});

                Object.keys(byPos).forEach(function(xStr) {{
                    var group = byPos[xStr];
                    var x = parseInt(xStr);
                    for (var gi = 0; gi < group.length; gi++) {{
                        var m = group[gi];
                        // Offset overlapping markers by 4px each
                        var xOff = x + (group.length > 1 ? (gi - (group.length - 1) / 2) * 4 : 0);

                        // Draw vertical dashed line
                        ctx.beginPath();
                        ctx.setLineDash(m.source === "github" ? [2, 3] : [5, 3]);
                        ctx.strokeStyle = m.color;
                        ctx.lineWidth = 1.5;
                        ctx.moveTo(xOff, plotTop);
                        ctx.lineTo(xOff, plotTop + plotHeight);
                        ctx.stroke();
                        ctx.setLineDash([]);

                        // Draw rotated label in the padding area above the plot
                        ctx.fillStyle = m.color;
                        ctx.font = "10px sans-serif";
                        ctx.save();
                        ctx.translate(xOff + 4, plotTop - 4);
                        ctx.rotate(-Math.PI / 2);
                        ctx.textAlign = "right";
                        ctx.fillText(m.label, 0, 0);
                        ctx.restore();
                    }}
                }});
                ctx.restore();
            }}],
        }},
    }};
}}

function renderGithubChart(history) {{
    // Stars-over-time. Needs at least two daily snapshots to draw a trend;
    // GitHub exposes no history, so this fills in only as `github` is re-run.
    if (!history || history.length < 2) {{
        return;
    }}
    document.getElementById("github-card").style.display = "";
    var el = document.getElementById("github-chart");
    if (_githubChart) {{
        _githubChart.destroy();
        _githubChart = null;
    }}

    history = history.slice().sort(function(a, b) {{ return a.date < b.date ? -1 : 1; }});
    var timestamps = history.map(function(h) {{
        return new Date(h.date + "T00:00:00").getTime() / 1000;
    }});
    var stars = history.map(function(h) {{ return h.stars; }});
    var data = [timestamps, stars];

    var opts = {{
        width: Math.min(el.clientWidth, 1100),
        height: 260,
        cursor: {{ drag: {{ x: true, y: false, setScale: true }} }},
        scales: {{ x: {{ time: true }}, y: {{ auto: true }} }},
        axes: [
            {{ stroke: "#888", grid: {{ stroke: "#eee" }} }},
            {{
                stroke: "#888",
                grid: {{ stroke: "#eee" }},
                values: function(u, vals) {{
                    return vals.map(function(v) {{
                        if (v >= 1e6) return (v/1e6).toFixed(1) + "M";
                        if (v >= 1e3) return (v/1e3).toFixed(1) + "K";
                        return v;
                    }});
                }},
            }},
        ],
        series: [
            {{ label: "" }},
            {{ label: "Stars", stroke: "{CHART_COLORS[1]}", width: 2, fill: "{CHART_COLORS[1]}22" }},
        ],
    }};

    _githubChart = new uPlot(opts, data, el);
    var chart = _githubChart;
    window.addEventListener("resize", function() {{
        chart.setSize({{ width: Math.min(el.clientWidth, 1100), height: 260 }});
    }});
}}

function renderBarChart(containerId, items, labelKey, valueKey) {{
    var el = document.getElementById(containerId);
    if (!items || items.length === 0) {{
        el.innerHTML = '<p class="empty">No data available.</p>';
        return;
    }}

    // Sort descending, limit to top items
    items.sort(function(a, b) {{ return (b[valueKey] || 0) - (a[valueKey] || 0); }});
    var top = items.slice(0, {BAR_CHART_MAX_ITEMS});
    var maxVal = top.length > 0 ? (top[0][valueKey] || 1) : 1;

    var html = '<div class="bar-chart">';
    for (var i = 0; i < top.length; i++) {{
        var label = top[i][labelKey];
        if (!label || label === "null") label = "Unknown";
        var val = top[i][valueKey] || 0;
        var pct = (val / maxVal * 100).toFixed(1);
        // Alternate colors
        var color = "{CHART_COLORS[0]}";
        html += '<div class="bar-row">' +
            '<div class="bar-label" title="' + label + '">' + label + '</div>' +
            '<div class="bar-track"><div class="bar-fill" style="width:' + pct + '%;background:' + color + '"></div></div>' +
            '<div class="bar-value">' + val.toLocaleString() + '</div>' +
            '</div>';
    }}
    html += '</div>';
    el.innerHTML = html;
}}

function renderReleases(releases) {{
    var pypi = releases.pypi || [];
    var github = releases.github || [];
    if (pypi.length === 0 && github.length === 0) return;

    document.getElementById("releases-section").style.display = "";

    // Merge and sort by date descending
    var all = [];
    pypi.forEach(function(r) {{ all.push({{ version: r.version, date: r.upload_date, source: "PyPI" }}); }});
    github.forEach(function(r) {{ all.push({{ version: r.tag_name, date: r.published_at, source: "GitHub" }}); }});
    all.sort(function(a, b) {{ return b.date < a.date ? -1 : 1; }});

    var html = '<table><thead><tr><th>Version</th><th>Date</th><th>Source</th></tr></thead><tbody>';
    var limit = Math.min(all.length, 20);
    for (var i = 0; i < limit; i++) {{
        html += '<tr><td>' + all[i].version + '</td><td>' + all[i].date + '</td><td>' + all[i].source + '</td></tr>';
    }}
    html += '</tbody></table>';
    document.getElementById("releases-list").innerHTML = html;
}}
</script>"""

    return _base_page(escaped, body)


def generate_comparison_page() -> str:
    """Generate the comparison page HTML.

    Allows selecting multiple packages and overlaying their download
    histories on a single chart. Data fetched client-side.
    """
    body = f"""{_nav("Compare")}

<div class="card">
<h2>Compare Packages</h2>
<p style="margin-bottom:12px;color:#555">Select packages to compare their download trends side by side.</p>
<div id="pkg-selector"><p class="loading">Loading packages...</p></div>
</div>

<div class="card" id="compare-chart-card" style="display:none">
<h2>Download Trends</h2>
<p style="font-size:0.85em;color:#888;margin-bottom:8px">Click and drag to zoom, double-click to reset</p>
<div class="chart-wrapper" id="compare-chart"></div>
</div>

<div class="card" id="compare-table-card" style="display:none">
<h2>Stats Comparison</h2>
<div id="compare-table"></div>
</div>

<script>
var COLORS = {_js_array(CHART_COLORS)};
var allPackages = [];
var selectedPkgs = new Set();
var compareChart = null;

document.addEventListener("DOMContentLoaded", function() {{
    fetch("/api/packages")
        .then(r => r.json())
        .then(function(data) {{
            allPackages = data;
            renderSelector(data);
        }});
}});

function fmt(n) {{
    if (n == null) return "-";
    return n.toLocaleString();
}}

function renderSelector(packages) {{
    var html = '<div style="display:flex;flex-wrap:wrap;gap:8px">';
    for (var i = 0; i < packages.length; i++) {{
        var p = packages[i];
        html += '<label style="display:inline-flex;align-items:center;gap:4px;padding:6px 12px;' +
            'background:#f0f0f0;border-radius:4px;cursor:pointer;font-size:0.9em">' +
            '<input type="checkbox" class="pkg-cb" value="' + p.package_name + '"> ' +
            p.package_name + '</label>';
    }}
    html += '</div>';
    document.getElementById("pkg-selector").innerHTML = html;

    document.querySelectorAll(".pkg-cb").forEach(function(cb) {{
        cb.addEventListener("change", function() {{
            if (cb.checked) selectedPkgs.add(cb.value);
            else selectedPkgs.delete(cb.value);
            updateComparison();
        }});
    }});
}}

function updateComparison() {{
    if (selectedPkgs.size === 0) {{
        document.getElementById("compare-chart-card").style.display = "none";
        document.getElementById("compare-table-card").style.display = "none";
        return;
    }}

    var names = Array.from(selectedPkgs);
    var fetches = names.map(function(n) {{
        return fetch("/api/history/" + encodeURIComponent(n) + "?limit=90").then(r => r.json());
    }});

    Promise.all(fetches).then(function(results) {{
        renderCompareChart(names, results);
        renderCompareTable(names);
    }});
}}

function renderCompareChart(names, histories) {{
    document.getElementById("compare-chart-card").style.display = "";
    var el = document.getElementById("compare-chart");
    el.innerHTML = "";

    // Collect all unique dates, build aligned data
    var dateSet = new Set();
    var histByName = {{}};
    for (var i = 0; i < names.length; i++) {{
        var h = histories[i] || [];
        h.sort(function(a, b) {{ return a.fetch_date < b.fetch_date ? -1 : 1; }});
        histByName[names[i]] = {{}};
        h.forEach(function(rec) {{
            dateSet.add(rec.fetch_date);
            histByName[names[i]][rec.fetch_date] = rec.last_day;
        }});
    }}

    var dates = Array.from(dateSet).sort();
    if (dates.length === 0) {{
        el.innerHTML = '<p class="empty">No history data for selected packages.</p>';
        return;
    }}

    var timestamps = dates.map(function(d) {{
        return new Date(d + "T00:00:00").getTime() / 1000;
    }});

    var data = [timestamps];
    var series = [{{ label: "" }}];
    for (var i = 0; i < names.length; i++) {{
        var name = names[i];
        var vals = dates.map(function(d) {{ return histByName[name][d] != null ? histByName[name][d] : null; }});
        data.push(vals);
        series.push({{
            label: name,
            stroke: COLORS[i % COLORS.length],
            width: 2,
        }});
    }}

    var opts = {{
        width: Math.min(el.clientWidth, 1100),
        height: 350,
        cursor: {{ drag: {{ x: true, y: false, setScale: true }} }},
        select: {{ show: true }},
        scales: {{
            x: {{ time: true }},
            y: {{ auto: true }},
        }},
        axes: [
            {{ stroke: "#888", grid: {{ stroke: "#eee" }} }},
            {{
                stroke: "#888",
                grid: {{ stroke: "#eee" }},
                values: function(u, vals) {{
                    return vals.map(function(v) {{
                        if (v >= 1e6) return (v/1e6).toFixed(1) + "M";
                        if (v >= 1e3) return (v/1e3).toFixed(1) + "K";
                        return v;
                    }});
                }},
            }},
        ],
        series: series,
    }};

    if (compareChart) compareChart.destroy();
    compareChart = new uPlot(opts, data, el);

    el.addEventListener("dblclick", function() {{
        compareChart.setScale("x", {{
            min: timestamps[0],
            max: timestamps[timestamps.length - 1],
        }});
    }});
}}

function renderCompareTable(names) {{
    document.getElementById("compare-table-card").style.display = "";
    var html = '<table><thead><tr><th>Package</th><th class="number">Total</th>' +
        '<th class="number">Month</th><th class="number">Week</th><th class="number">Day</th></tr></thead><tbody>';

    names.forEach(function(name) {{
        var p = allPackages.find(function(x) {{ return x.package_name === name; }});
        if (p) {{
            html += '<tr><td><a class="pkg-link" href="/package/' + encodeURIComponent(name) + '">' + name + '</a></td>' +
                '<td class="number">' + fmt(p.total) + '</td>' +
                '<td class="number">' + fmt(p.last_month) + '</td>' +
                '<td class="number">' + fmt(p.last_week) + '</td>' +
                '<td class="number">' + fmt(p.last_day) + '</td></tr>';
        }}
    }});

    html += '</tbody></table>';
    document.getElementById("compare-table").innerHTML = html;
}}
</script>"""

    return _base_page("Compare", body)


def _js_string(s: str) -> str:
    """Escape a Python string for safe embedding in JavaScript."""
    return (
        '"'
        + s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("<", "\\x3c")
        + '"'
    )


def _js_array(items: list[str]) -> str:
    """Convert a list of strings to a JS array literal."""
    return "[" + ",".join(_js_string(s) for s in items) + "]"
