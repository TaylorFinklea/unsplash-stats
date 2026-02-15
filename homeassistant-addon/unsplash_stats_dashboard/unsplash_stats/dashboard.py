from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
from dash import ALL, Dash, Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate
from flask import abort, send_from_directory

from .collector import collect_snapshot
from .db import connect_db, init_db
from .exporters import export_csv_files


USER_HISTORY_SQL = """
SELECT
    r.id AS run_id,
    r.collected_at,
    u.username,
    u.total_photos,
    u.downloads_total,
    u.views_total
FROM user_stats_snapshots u
JOIN collection_runs r ON r.id = u.run_id
ORDER BY r.collected_at ASC, r.id ASC;
"""

PHOTO_HISTORY_SQL = """
SELECT
    r.id AS run_id,
    r.collected_at,
    p.photo_id,
    p.photo_slug,
    p.photo_description,
    p.photo_created_at,
    p.downloads_total,
    p.views_total,
    p.raw_json
FROM photo_stats_snapshots p
JOIN collection_runs r ON r.id = p.run_id
ORDER BY r.collected_at ASC, r.id ASC, p.photo_id ASC;
"""

PHOTO_LATEST_SQL = """
WITH ranked AS (
    SELECT
        p.id,
        p.photo_id,
        p.photo_slug,
        p.photo_description,
        p.photo_created_at,
        p.downloads_total,
        p.views_total,
        p.raw_json,
        r.collected_at,
        ROW_NUMBER() OVER (
            PARTITION BY p.photo_id
            ORDER BY r.collected_at DESC, p.id DESC
        ) AS row_num
    FROM photo_stats_snapshots p
    JOIN collection_runs r ON r.id = p.run_id
),
latest AS (
    SELECT * FROM ranked WHERE row_num = 1
),
previous AS (
    SELECT * FROM ranked WHERE row_num = 2
)
SELECT
    latest.photo_id,
    latest.photo_slug,
    latest.photo_description,
    latest.photo_created_at,
    latest.downloads_total,
    latest.views_total,
    latest.raw_json,
    latest.collected_at AS latest_collected_at,
    previous.collected_at AS previous_collected_at,
    latest.downloads_total - COALESCE(previous.downloads_total, latest.downloads_total)
        AS downloads_delta_since_previous,
    latest.views_total - COALESCE(previous.views_total, latest.views_total)
        AS views_delta_since_previous
FROM latest
LEFT JOIN previous ON previous.photo_id = latest.photo_id
ORDER BY latest.views_total DESC, latest.photo_id ASC;
"""

METRIC_LABELS = {
    "downloads_total": "Downloads",
    "views_total": "Views",
}
METRIC_COLUMNS = tuple(METRIC_LABELS.keys())

DELTA_COLUMNS = {
    "downloads_total": "downloads_delta_since_previous",
    "views_total": "views_delta_since_previous",
}

COLORS = {
    "downloads_total": "#ff8c42",
    "views_total": "#0ea5a6",
}


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _empty_figure(title: str, message: str):
    fig = px.scatter(title=title)
    fig.update_layout(
        template="plotly_white",
        xaxis={"visible": False},
        yaxis={"visible": False},
        annotations=[
            {
                "text": message,
                "xref": "paper",
                "yref": "paper",
                "x": 0.5,
                "y": 0.5,
                "showarrow": False,
                "font": {"size": 16, "color": "#334155"},
            }
        ],
        margin={"l": 24, "r": 24, "t": 56, "b": 24},
    )
    return fig


def _fmt_int(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "-"


def _photo_option_label(row: pd.Series) -> str:
    slug = row.get("photo_slug")
    desc = row.get("photo_description")
    if isinstance(slug, str) and slug.strip():
        base = slug.strip()
    else:
        base = str(row["photo_id"])
    if isinstance(desc, str) and desc.strip():
        trimmed = desc.strip()
        if len(trimmed) > 42:
            trimmed = trimmed[:39] + "..."
        return f"{base} - {trimmed}"
    return base


def _fmt_delta(value: Any) -> str:
    if value is None:
        return "-"
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        return "-"
    if ivalue > 0:
        return f"+{ivalue:,}"
    return f"{ivalue:,}"


def _extract_photo_url(raw_payload: Any) -> str | None:
    payload = raw_payload
    if isinstance(raw_payload, str):
        raw_payload = raw_payload.strip()
        if not raw_payload:
            return None
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError:
            return None

    if not isinstance(payload, dict):
        return None
    photo = payload.get("photo")
    if not isinstance(photo, dict):
        return None
    urls = photo.get("urls")
    if not isinstance(urls, dict):
        return None

    for key in ("small", "regular", "thumb", "full", "raw"):
        value = urls.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _safe_file_token(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    if cleaned:
        return cleaned
    return "photo"


def _photo_cache_filename(photo_id: str, image_url: str) -> str:
    parsed = urllib.parse.urlparse(image_url)
    suffix = Path(parsed.path).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        suffix = ".jpg"
    return f"{_safe_file_token(photo_id)}{suffix}"


def _cache_photo_if_needed(cache_dir: Path, photo_id: str, image_url: str) -> str | None:
    if not photo_id.strip() or not image_url.strip():
        return None

    filename = _photo_cache_filename(photo_id, image_url)
    cache_path = cache_dir / filename
    if cache_path.exists():
        return filename

    cache_dir.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        image_url,
        headers={"User-Agent": "unsplash-stats-dashboard/0.1"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read()
    except Exception:
        return None

    tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
    try:
        tmp_path.write_bytes(body)
        tmp_path.replace(cache_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    return filename


def _resolve_photo_src(
    *,
    cache_dir: Path,
    photo_id: str,
    raw_json_payload: Any,
    route_prefix: str,
) -> str | None:
    remote_url = _extract_photo_url(raw_json_payload)
    if not remote_url:
        return None

    cached_filename = _cache_photo_if_needed(cache_dir, photo_id, remote_url)
    if cached_filename is not None:
        return f"{route_prefix}/{urllib.parse.quote(cached_filename)}"
    return remote_url


def _photo_image_or_placeholder(src: str | None, label: str, *, height_px: int) -> html.Div:
    if src:
        return html.Div(
            [
                html.Img(
                    src=src,
                    alt=label,
                    style={
                        "width": "100%",
                        "height": f"{height_px}px",
                        "objectFit": "cover",
                        "display": "block",
                    },
                )
            ],
            style={
                "backgroundColor": "#e2e8f0",
                "borderBottom": "1px solid #e2e8f0",
            },
        )

    return html.Div(
        f"No image available for {label}",
        style={
            "height": f"{height_px}px",
            "display": "flex",
            "alignItems": "center",
            "justifyContent": "center",
            "padding": "12px",
            "color": "#64748b",
            "backgroundColor": "#f1f5f9",
            "borderBottom": "1px solid #e2e8f0",
            "fontWeight": 600,
            "textAlign": "center",
        },
    )


def _build_selected_photo_preview(
    row: pd.Series | None, image_src: str | None
) -> html.Div:
    if row is None:
        return html.Div(
            "Select a photo to preview it.",
            style={"color": "#475569", "padding": "8px 2px"},
        )

    label = _photo_option_label(row)
    return html.Div(
        style={
            "backgroundColor": "white",
            "borderRadius": "14px",
            "overflow": "hidden",
            "boxShadow": "0 8px 20px rgba(15, 23, 42, 0.08)",
            "marginBottom": "12px",
        },
        children=[
            _photo_image_or_placeholder(image_src, label, height_px=320),
            html.Div(
                style={"padding": "12px 14px"},
                children=[
                    html.Div(label, style={"fontWeight": 700, "marginBottom": "8px"}),
                    html.Div(
                        f"Views: {_fmt_int(row.get('views_total'))} "
                        f"({_fmt_delta(row.get('views_delta_since_previous'))})",
                        style={"color": "#0f172a", "marginBottom": "4px"},
                    ),
                    html.Div(
                        f"Downloads: {_fmt_int(row.get('downloads_total'))} "
                        f"({_fmt_delta(row.get('downloads_delta_since_previous'))})",
                        style={"color": "#0f172a", "marginBottom": "4px"},
                    ),
                    html.Div(
                        f"Photo ID: {row.get('photo_id')}",
                        style={"color": "#475569", "fontSize": "0.92rem"},
                    ),
                ],
            ),
        ],
    )


def _build_latest_photo_card(row: pd.Series, image_src: str | None) -> html.Div:
    label = _photo_option_label(row)
    photo_id = str(row.get("photo_id", "")).strip()
    return html.Div(
        id={"type": "photo-card", "photo_id": photo_id},
        n_clicks=0,
        style={
            "backgroundColor": "white",
            "borderRadius": "14px",
            "overflow": "hidden",
            "boxShadow": "0 8px 20px rgba(15, 23, 42, 0.08)",
            "display": "flex",
            "flexDirection": "column",
            "cursor": "pointer",
        },
        children=[
            _photo_image_or_placeholder(image_src, label, height_px=210),
            html.Div(
                style={"padding": "10px 12px"},
                children=[
                    html.Div(
                        label,
                        style={
                            "fontWeight": 700,
                            "fontSize": "0.98rem",
                            "marginBottom": "6px",
                            "lineHeight": "1.25",
                        },
                    ),
                    html.Div(
                        f"Views: {_fmt_int(row.get('views_total'))}",
                        style={"color": "#0f172a", "marginBottom": "2px"},
                    ),
                    html.Div(
                        f"Downloads: {_fmt_int(row.get('downloads_total'))}",
                        style={"color": "#0f172a", "marginBottom": "2px"},
                    ),
                    html.Div(
                        f"Delta Views: {_fmt_delta(row.get('views_delta_since_previous'))}",
                        style={"color": "#334155", "fontSize": "0.92rem", "marginBottom": "2px"},
                    ),
                    html.Div(
                        f"Delta Downloads: {_fmt_delta(row.get('downloads_delta_since_previous'))}",
                        style={"color": "#334155", "fontSize": "0.92rem"},
                    ),
                ],
            ),
        ],
    )


def _load_data(db_path: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    connection = sqlite3.connect(db_path)
    try:
        init_db(connection)
        user_df = pd.read_sql_query(USER_HISTORY_SQL, connection)
        photo_history_df = pd.read_sql_query(PHOTO_HISTORY_SQL, connection)
        photo_latest_df = pd.read_sql_query(PHOTO_LATEST_SQL, connection)
    finally:
        connection.close()

    for frame in (user_df, photo_history_df, photo_latest_df):
        if "collected_at" in frame.columns:
            frame["collected_at"] = pd.to_datetime(frame["collected_at"], utc=True)
        if "latest_collected_at" in frame.columns:
            frame["latest_collected_at"] = pd.to_datetime(
                frame["latest_collected_at"], utc=True
            )
        if "previous_collected_at" in frame.columns:
            frame["previous_collected_at"] = pd.to_datetime(
                frame["previous_collected_at"], utc=True
            )

    for col in METRIC_COLUMNS:
        if col in user_df.columns:
            user_df[col] = pd.to_numeric(user_df[col], errors="coerce")
        if col in photo_history_df.columns:
            photo_history_df[col] = pd.to_numeric(photo_history_df[col], errors="coerce")
        if col in photo_latest_df.columns:
            photo_latest_df[col] = pd.to_numeric(photo_latest_df[col], errors="coerce")

    for col in (
        "downloads_delta_since_previous",
        "views_delta_since_previous",
    ):
        if col in photo_latest_df.columns:
            photo_latest_df[col] = pd.to_numeric(photo_latest_df[col], errors="coerce")

    for frame in (photo_history_df, photo_latest_df):
        if "raw_json" in frame.columns:
            frame["photo_image_url"] = frame["raw_json"].map(_extract_photo_url)

    return user_df, photo_history_df, photo_latest_df


def _extract_photo_id_from_click(click_data: Any) -> str | None:
    if not isinstance(click_data, dict):
        return None
    points = click_data.get("points")
    if not isinstance(points, list) or not points:
        return None
    first_point = points[0]
    if not isinstance(first_point, dict):
        return None
    custom_data = first_point.get("customdata")
    if isinstance(custom_data, list) and custom_data:
        value = custom_data[0]
    else:
        value = custom_data
    if value is None:
        return None
    photo_id = str(value).strip()
    return photo_id or None


def _build_layout(db_path: Path) -> html.Div:
    return html.Div(
        style={
            "minHeight": "100vh",
            "padding": "24px",
            "background": "linear-gradient(140deg, #fff7ed 0%, #f0fdfa 45%, #f8fafc 100%)",
            "fontFamily": "'Space Grotesk', 'IBM Plex Sans', sans-serif",
            "color": "#0f172a",
        },
        children=[
            html.Div(
                style={
                    "display": "flex",
                    "justifyContent": "space-between",
                    "alignItems": "center",
                    "gap": "12px",
                    "flexWrap": "wrap",
                    "marginBottom": "18px",
                },
                children=[
                    html.Div(
                        [
                            html.H1(
                                "Unsplash Stats Dashboard",
                                style={"margin": "0", "fontSize": "2rem"},
                            ),
                            html.Div(
                                f"Database: {db_path}",
                                style={"color": "#475569", "fontSize": "0.95rem"},
                            ),
                        ]
                    ),
                    html.Div(
                        style={
                            "display": "flex",
                            "gap": "8px",
                            "alignItems": "center",
                            "flexWrap": "wrap",
                        },
                        children=[
                            html.Button(
                                "Collect Now",
                                id="collect-button",
                                n_clicks=0,
                                style={
                                    "border": "none",
                                    "borderRadius": "10px",
                                    "background": "#ea580c",
                                    "color": "white",
                                    "padding": "10px 16px",
                                    "fontWeight": 700,
                                    "cursor": "pointer",
                                },
                            ),
                            html.Button(
                                "Refresh from DB",
                                id="refresh-button",
                                n_clicks=0,
                                style={
                                    "border": "none",
                                    "borderRadius": "10px",
                                    "background": "#0ea5a6",
                                    "color": "white",
                                    "padding": "10px 16px",
                                    "fontWeight": 700,
                                    "cursor": "pointer",
                                },
                            ),
                        ],
                    ),
                ],
            ),
            html.Div(
                id="run-meta",
                style={"color": "#334155", "marginBottom": "14px", "fontSize": "0.95rem"},
            ),
            html.Div(
                id="action-status",
                children="Ready.",
                style={"color": "#334155", "marginBottom": "16px", "fontSize": "0.95rem"},
            ),
            dcc.Store(id="collection-refresh-token", data=0),
            dcc.Interval(
                id="progress-interval",
                interval=1000,
                n_intervals=0,
                disabled=True,
            ),
            dcc.Tabs(
                id="main-tab",
                value="dashboard",
                children=[
                    dcc.Tab(label="Dashboard", value="dashboard"),
                    dcc.Tab(label="Collection Progress", value="progress"),
                ],
            ),
            html.Div(
                id="dashboard-page",
                style={"display": "block"},
                children=[
                    html.Div(
                        style={
                            "display": "grid",
                            "gridTemplateColumns": "repeat(auto-fit, minmax(180px, 1fr))",
                            "gap": "12px",
                            "marginBottom": "18px",
                        },
                        children=[
                            _kpi_card("Total Views", "kpi-views"),
                            _kpi_card("Total Downloads", "kpi-downloads"),
                            _kpi_card("Tracked Photos", "kpi-photos"),
                        ],
                    ),
                    html.Div(
                        style={
                            "display": "grid",
                            "gridTemplateColumns": "repeat(auto-fit, minmax(min(100%, 520px), 1fr))",
                            "gap": "20px",
                        },
                        children=[
                            dcc.Graph(id="account-totals-graph", style={"height": "430px"}),
                            dcc.Graph(id="account-growth-graph", style={"height": "430px"}),
                            dcc.Graph(id="tracked-photos-graph", style={"height": "430px"}),
                            dcc.Graph(id="new-photos-per-run-graph", style={"height": "430px"}),
                        ],
                    ),
                    html.H2(
                        "Photo Trends",
                        style={"marginTop": "20px", "marginBottom": "10px", "fontSize": "1.5rem"},
                    ),
                    html.Div(
                        style={
                            "display": "grid",
                            "gridTemplateColumns": "repeat(auto-fit, minmax(260px, 1fr))",
                            "gap": "12px",
                            "marginBottom": "12px",
                        },
                        children=[
                            html.Div(
                                [
                                    html.Div(
                                        "Metric",
                                        style={"fontWeight": 600, "marginBottom": "4px"},
                                    ),
                                    dcc.Dropdown(
                                        id="metric-dropdown",
                                        value="views_total",
                                        options=[
                                            {"label": "Views", "value": "views_total"},
                                            {"label": "Downloads", "value": "downloads_total"},
                                        ],
                                        clearable=False,
                                    ),
                                ]
                            ),
                            html.Div(
                                [
                                    html.Div(
                                        "Photo",
                                        style={"fontWeight": 600, "marginBottom": "4px"},
                                    ),
                                    dcc.Dropdown(
                                        id="photo-dropdown",
                                        value=None,
                                        options=[],
                                        placeholder="Select a photo...",
                                        clearable=False,
                                    ),
                                ]
                            ),
                        ],
                    ),
                    html.Div(
                        id="selected-photo-preview",
                        style={"marginBottom": "12px"},
                    ),
                    html.Div(
                        style={
                            "display": "grid",
                            "gridTemplateColumns": "repeat(auto-fit, minmax(min(100%, 520px), 1fr))",
                            "gap": "20px",
                        },
                        children=[
                            dcc.Graph(id="photo-trend-graph", style={"height": "460px"}),
                            dcc.Graph(id="top-movers-graph", style={"height": "460px"}),
                            dcc.Graph(id="momentum-scatter-graph", style={"height": "460px"}),
                            dcc.Graph(id="efficiency-scatter-graph", style={"height": "460px"}),
                        ],
                    ),
                    html.H3(
                        "Latest Snapshot by Photo",
                        style={"marginTop": "20px", "marginBottom": "10px", "fontSize": "1.2rem"},
                    ),
                    html.Div(
                        id="latest-photo-cards",
                        style={
                            "display": "grid",
                            "gridTemplateColumns": "repeat(auto-fit, minmax(250px, 1fr))",
                            "gap": "12px",
                        },
                    ),
                ],
            ),
            html.Div(
                id="progress-page",
                style={"display": "none"},
                children=[
                    html.H2(
                        "Collection Progress",
                        style={"marginTop": "16px", "marginBottom": "8px", "fontSize": "1.5rem"},
                    ),
                    html.Div(
                        id="progress-summary",
                        style={"color": "#334155", "marginBottom": "10px", "fontSize": "1rem"},
                    ),
                    html.Div(
                        style={
                            "height": "18px",
                            "backgroundColor": "#cbd5e1",
                            "borderRadius": "999px",
                            "overflow": "hidden",
                            "marginBottom": "8px",
                        },
                        children=[
                            html.Div(
                                id="progress-bar-fill",
                                style={
                                    "height": "100%",
                                    "width": "0%",
                                    "backgroundColor": "#0ea5a6",
                                    "transition": "width 0.35s ease",
                                },
                            )
                        ],
                    ),
                    html.Div(
                        id="progress-percent-text",
                        style={"fontWeight": 700, "fontSize": "1.1rem", "marginBottom": "8px"},
                    ),
                    html.Div(
                        id="progress-calls-text",
                        style={"color": "#334155", "marginBottom": "4px"},
                    ),
                    html.Div(
                        id="progress-endpoint-text",
                        style={"color": "#334155", "marginBottom": "4px"},
                    ),
                    html.Div(
                        id="progress-updated-text",
                        style={"color": "#475569", "fontSize": "0.95rem"},
                    ),
                ],
            ),
        ],
    )


def _kpi_card(title: str, value_id: str) -> html.Div:
    return html.Div(
        style={
            "backgroundColor": "white",
            "borderRadius": "14px",
            "padding": "14px 16px",
            "boxShadow": "0 8px 20px rgba(15, 23, 42, 0.08)",
        },
        children=[
            html.Div(title, style={"color": "#475569", "fontSize": "0.9rem"}),
            html.Div(id=value_id, style={"fontSize": "1.45rem", "fontWeight": 700}),
        ],
    )


def create_app(db_path: Path) -> Dash:
    app = Dash(
        __name__,
        external_stylesheets=[
            "https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=IBM+Plex+Sans:wght@400;500;700&display=swap"
        ],
    )
    app.title = "Unsplash Stats"
    app.layout = _build_layout(db_path)
    photo_cache_dir = Path(
        os.getenv("UNSPLASH_PHOTO_CACHE_DIR", str(db_path.parent / "photo_cache"))
    )
    photo_route_prefix = "/photo-cache"

    @app.server.route(f"{photo_route_prefix}/<path:filename>")
    def serve_photo_cache(filename: str):
        safe_root = photo_cache_dir.resolve()
        requested = (safe_root / filename).resolve()
        if safe_root not in requested.parents:
            abort(404)
        if not requested.is_file():
            abort(404)
        return send_from_directory(str(safe_root), requested.name)

    state_lock = threading.Lock()
    worker_ref: dict[str, threading.Thread | None] = {"thread": None}
    collection_state: dict[str, Any] = {
        "phase": "idle",
        "message": "Ready.",
        "username": os.getenv("UNSPLASH_USERNAME", "tfinklea") or "tfinklea",
        "completed_calls": 0,
        "expected_total_calls": None,
        "percent_complete": 0.0,
        "last_endpoint": "-",
        "last_status_code": None,
        "rate_limited": False,
        "rate_limit_wait_seconds": None,
        "updated_at": _utc_now_str(),
        "refresh_token": 0,
    }

    def _snapshot_collection_state() -> dict[str, Any]:
        with state_lock:
            return dict(collection_state)

    def _set_collection_state(**updates: Any) -> None:
        with state_lock:
            collection_state.update(updates)

    def _run_collection_worker(username: str) -> None:
        access_key = os.getenv("UNSPLASH_ACCESS_KEY")
        if not access_key:
            _set_collection_state(
                phase="error",
                message="Collection failed: UNSPLASH_ACCESS_KEY is not set.",
                updated_at=_utc_now_str(),
                rate_limited=False,
                rate_limit_wait_seconds=None,
            )
            with state_lock:
                worker_ref["thread"] = None
            return

        rate_limit_fraction = max(
            0.0, min(1.0, _env_float("UNSPLASH_RATE_LIMIT_FRACTION", 0.8))
        )
        min_request_interval_seconds = max(
            0.0, _env_float("UNSPLASH_MIN_REQUEST_INTERVAL_SECONDS", 0.0)
        )
        delay_seconds = max(0.0, _env_float("UNSPLASH_DELAY_SECONDS", 0.25))
        export_dir = os.getenv("UNSPLASH_EXPORT_DIR", "exports")

        def _progress_hook(event: dict[str, Any]) -> None:
            completed_calls = int(event.get("completed_calls", 0))
            expected_raw = event.get("expected_total_calls")
            expected_total_calls: int | None
            if isinstance(expected_raw, (int, float)):
                expected_total_calls = int(expected_raw)
            else:
                expected_total_calls = None

            percent_raw = event.get("percent_complete")
            if isinstance(percent_raw, (int, float)):
                percent_complete = max(0.0, min(100.0, float(percent_raw)))
            elif expected_total_calls and expected_total_calls > 0:
                percent_complete = min(
                    100.0, (completed_calls / expected_total_calls) * 100.0
                )
            else:
                percent_complete = 0.0

            endpoint = str(event.get("path") or "-")
            status_code = event.get("status_code")
            rate_limited = bool(event.get("rate_limited", False))

            wait_raw = event.get("rate_limit_wait_seconds")
            if isinstance(wait_raw, (int, float)):
                wait_seconds = float(wait_raw)
            else:
                wait_seconds = None

            if expected_total_calls and expected_total_calls > 0:
                progress_text = (
                    f"{completed_calls}/{expected_total_calls} "
                    f"({percent_complete:.1f}%)"
                )
            else:
                progress_text = f"{completed_calls} calls"

            message = f"Collecting data: {progress_text}."
            if rate_limited:
                if wait_seconds is not None:
                    message = (
                        f"Rate limited at {endpoint}; waiting "
                        f"{wait_seconds:.2f}s before retry."
                    )
                else:
                    message = f"Rate limited at {endpoint}; retrying."

            _set_collection_state(
                phase="running",
                message=message,
                completed_calls=completed_calls,
                expected_total_calls=expected_total_calls,
                percent_complete=percent_complete,
                last_endpoint=endpoint,
                last_status_code=status_code,
                rate_limited=rate_limited,
                rate_limit_wait_seconds=wait_seconds,
                updated_at=_utc_now_str(),
            )

        try:
            result = collect_snapshot(
                access_key=access_key,
                username=username,
                db_path=db_path,
                delay_seconds=delay_seconds,
                rate_limit_fraction=rate_limit_fraction,
                min_request_interval_seconds=min_request_interval_seconds,
                strict=False,
                progress_hook=_progress_hook,
            )

            connection = connect_db(db_path)
            try:
                init_db(connection)
                export_counts = export_csv_files(connection, export_dir)
            finally:
                connection.close()

            if (
                result.estimated_total_api_calls is not None
                and result.estimated_total_api_calls > 0
            ):
                percent_complete = min(
                    100.0,
                    (result.api_calls_made / result.estimated_total_api_calls) * 100.0,
                )
            else:
                percent_complete = 100.0

            exported_files = ", ".join(
                f"{name} ({rows} rows)" for name, rows in export_counts.items()
            )
            success_message = (
                f"Collection complete for @{username}: run {result.run_id}, "
                f"{result.photos_saved} photos, {result.api_calls_made} API calls."
            )
            if exported_files:
                success_message += f" Exported {exported_files}."

            with state_lock:
                collection_state.update(
                    {
                        "phase": "done",
                        "message": success_message,
                        "completed_calls": result.api_calls_made,
                        "expected_total_calls": result.estimated_total_api_calls,
                        "percent_complete": percent_complete,
                        "last_endpoint": "complete",
                        "last_status_code": 200,
                        "rate_limited": False,
                        "rate_limit_wait_seconds": None,
                        "updated_at": _utc_now_str(),
                        "refresh_token": int(collection_state.get("refresh_token", 0)) + 1,
                    }
                )
        except Exception as exc:
            _set_collection_state(
                phase="error",
                message=f"Collection failed: {exc}",
                rate_limited=False,
                rate_limit_wait_seconds=None,
                updated_at=_utc_now_str(),
            )
        finally:
            with state_lock:
                worker_ref["thread"] = None

    def _start_collection() -> str:
        username = (os.getenv("UNSPLASH_USERNAME", "tfinklea") or "tfinklea").strip()
        if not username:
            username = "tfinklea"

        with state_lock:
            existing_worker = worker_ref["thread"]
            if existing_worker is not None and existing_worker.is_alive():
                return "Collection is already running."

            collection_state.update(
                {
                    "phase": "running",
                    "message": f"Collection started for @{username}.",
                    "username": username,
                    "completed_calls": 0,
                    "expected_total_calls": None,
                    "percent_complete": 0.0,
                    "last_endpoint": "-",
                    "last_status_code": None,
                    "rate_limited": False,
                    "rate_limit_wait_seconds": None,
                    "updated_at": _utc_now_str(),
                }
            )

            worker = threading.Thread(
                target=_run_collection_worker,
                args=(username,),
                name="unsplash-collector-worker",
                daemon=True,
            )
            worker_ref["thread"] = worker

        worker.start()
        return (
            f"Collection started for @{username}. "
            "Progress updates are available in the Collection Progress tab."
        )

    @app.callback(
        Output("dashboard-page", "style"),
        Output("progress-page", "style"),
        Input("main-tab", "value"),
    )
    def switch_tab(active_tab: str | None):
        if active_tab == "progress":
            return {"display": "none"}, {"display": "block"}
        return {"display": "block"}, {"display": "none"}

    @app.callback(
        Output("action-status", "children"),
        Output("collect-button", "disabled"),
        Output("collect-button", "children"),
        Output("progress-summary", "children"),
        Output("progress-percent-text", "children"),
        Output("progress-bar-fill", "style"),
        Output("progress-calls-text", "children"),
        Output("progress-endpoint-text", "children"),
        Output("progress-updated-text", "children"),
        Output("collection-refresh-token", "data"),
        Input("collect-button", "n_clicks"),
        Input("progress-interval", "n_intervals"),
        State("collection-refresh-token", "data"),
    )
    def update_collection_progress(
        _collect_clicks: int,
        _interval_ticks: int,
        current_refresh_token: int | None,
    ):
        try:
            triggered_id = ctx.triggered_id
        except Exception:
            triggered_id = None

        if triggered_id == "collect-button":
            action_status = _start_collection()
        else:
            action_status = None

        state = _snapshot_collection_state()
        phase = str(state.get("phase", "idle"))

        if not action_status:
            action_status = str(state.get("message", "Ready."))

        running = phase == "running"
        button_disabled = running
        button_text = "Collecting..." if running else "Collect Now"

        percent_raw = state.get("percent_complete")
        if isinstance(percent_raw, (int, float)):
            percent_complete = max(0.0, min(100.0, float(percent_raw)))
        else:
            percent_complete = 0.0

        if phase == "idle":
            progress_summary = "No active collection. Click Collect Now to start a run."
        elif phase == "running":
            progress_summary = (
                f"Collecting stats for @{state.get('username', 'tfinklea')} "
                "using current environment settings."
            )
        elif phase == "done":
            progress_summary = "Latest collection finished. Dashboard data auto-refreshes."
        else:
            progress_summary = "Collection failed. Check the status message and retry."

        completed_calls = int(state.get("completed_calls", 0))
        expected_total_calls = state.get("expected_total_calls")
        if isinstance(expected_total_calls, int) and expected_total_calls > 0:
            progress_calls = f"API calls: {completed_calls}/{expected_total_calls}"
        else:
            progress_calls = f"API calls: {completed_calls}"

        endpoint = str(state.get("last_endpoint") or "-")
        status_code = state.get("last_status_code")
        progress_endpoint = f"Last endpoint: {endpoint}"
        if isinstance(status_code, int):
            progress_endpoint += f" (status {status_code})"

        if bool(state.get("rate_limited", False)):
            wait_seconds = state.get("rate_limit_wait_seconds")
            if isinstance(wait_seconds, (int, float)):
                progress_endpoint += f" | rate-limited, waiting {float(wait_seconds):.2f}s"
            else:
                progress_endpoint += " | rate-limited, retrying"

        last_updated = str(state.get("updated_at", _utc_now_str()))
        progress_updated = f"Last update: {last_updated}"

        if phase == "error":
            bar_color = "#dc2626"
        elif phase == "idle":
            bar_color = "#94a3b8"
        else:
            bar_color = "#0ea5a6"

        progress_bar_style = {
            "height": "100%",
            "width": f"{percent_complete:.1f}%",
            "backgroundColor": bar_color,
            "transition": "width 0.35s ease",
        }

        state_refresh_token = state.get("refresh_token")
        current_refresh_token_int = int(current_refresh_token or 0)
        if isinstance(state_refresh_token, int):
            next_refresh_token = max(current_refresh_token_int, state_refresh_token)
        else:
            next_refresh_token = current_refresh_token_int
        if next_refresh_token == current_refresh_token_int:
            refresh_token_output: int | Any = no_update
        else:
            refresh_token_output = next_refresh_token

        return (
            action_status,
            button_disabled,
            button_text,
            progress_summary,
            f"{percent_complete:.1f}% complete",
            progress_bar_style,
            progress_calls,
            progress_endpoint,
            progress_updated,
            refresh_token_output,
        )

    @app.callback(
        Output("progress-interval", "disabled"),
        Input("collect-button", "disabled"),
    )
    def set_progress_interval_disabled(collect_button_disabled: bool | None) -> bool:
        running = bool(collect_button_disabled)
        return not running

    @app.callback(
        Output("photo-dropdown", "value", allow_duplicate=True),
        Input("top-movers-graph", "clickData"),
        Input("momentum-scatter-graph", "clickData"),
        Input("efficiency-scatter-graph", "clickData"),
        State("photo-dropdown", "value"),
        prevent_initial_call=True,
    )
    def select_photo_from_graph_click(
        movers_click: dict[str, Any] | None,
        momentum_click: dict[str, Any] | None,
        efficiency_click: dict[str, Any] | None,
        current_photo_id: str | None,
    ) -> str:
        click_by_graph = {
            "top-movers-graph": movers_click,
            "momentum-scatter-graph": momentum_click,
            "efficiency-scatter-graph": efficiency_click,
        }
        click_data = click_by_graph.get(str(ctx.triggered_id))
        photo_id = _extract_photo_id_from_click(click_data)
        if not photo_id or photo_id == current_photo_id:
            raise PreventUpdate
        return photo_id

    @app.callback(
        Output("photo-dropdown", "value", allow_duplicate=True),
        Input({"type": "photo-card", "photo_id": ALL}, "n_clicks"),
        State("photo-dropdown", "value"),
        prevent_initial_call=True,
    )
    def select_photo_from_card_click(
        _card_clicks: list[int],
        current_photo_id: str | None,
    ) -> str:
        triggered = ctx.triggered_id
        if not isinstance(triggered, dict):
            raise PreventUpdate
        if triggered.get("type") != "photo-card":
            raise PreventUpdate
        try:
            triggered_value = ctx.triggered[0].get("value")
            click_count = int(triggered_value or 0)
        except Exception:
            click_count = 0
        if click_count <= 0:
            raise PreventUpdate
        photo_id = str(triggered.get("photo_id", "")).strip()
        if not photo_id or photo_id == current_photo_id:
            raise PreventUpdate
        return photo_id

    @app.callback(
        Output("run-meta", "children"),
        Output("kpi-views", "children"),
        Output("kpi-downloads", "children"),
        Output("kpi-photos", "children"),
        Output("account-totals-graph", "figure"),
        Output("account-growth-graph", "figure"),
        Output("tracked-photos-graph", "figure"),
        Output("new-photos-per-run-graph", "figure"),
        Output("photo-trend-graph", "figure"),
        Output("top-movers-graph", "figure"),
        Output("momentum-scatter-graph", "figure"),
        Output("efficiency-scatter-graph", "figure"),
        Output("photo-dropdown", "options"),
        Output("photo-dropdown", "value"),
        Output("selected-photo-preview", "children"),
        Output("latest-photo-cards", "children"),
        Input("refresh-button", "n_clicks"),
        Input("collection-refresh-token", "data"),
        Input("metric-dropdown", "value"),
        Input("photo-dropdown", "value"),
    )
    def refresh_dashboard(
        _refresh_clicks: int,
        _collection_refresh_token: int,
        metric: str | None,
        selected_photo_id: str | None,
    ):
        metric = metric or "views_total"
        user_df, photo_history_df, photo_latest_df = _load_data(db_path)

        if user_df.empty:
            empty = _empty_figure("No Data Yet", "Run the collector to populate snapshots.")
            return (
                f"No runs found in {db_path}",
                "-",
                "-",
                "-",
                empty,
                empty,
                empty,
                empty,
                empty,
                empty,
                empty,
                empty,
                [],
                None,
                _build_selected_photo_preview(None, None),
                [
                    html.Div(
                        "No photos available yet.",
                        style={"color": "#475569", "padding": "6px"},
                    )
                ],
            )

        latest_user = user_df.iloc[-1]
        runs_count = int(user_df["run_id"].nunique())
        latest_ts = latest_user["collected_at"]
        latest_timestamp = pd.to_datetime(latest_ts).strftime("%Y-%m-%d %H:%M UTC")

        totals_long = user_df.melt(
            id_vars=["collected_at"],
            value_vars=list(METRIC_COLUMNS),
            var_name="metric",
            value_name="value",
        )
        totals_long["value"] = pd.to_numeric(totals_long["value"], errors="coerce")
        totals_long["metric_label"] = totals_long["metric"].map(METRIC_LABELS)
        account_totals_fig = px.line(
            totals_long,
            x="collected_at",
            y="value",
            color="metric_label",
            markers=True,
            title="Account Totals Over Time",
            color_discrete_map={
                METRIC_LABELS[k]: v for k, v in COLORS.items() if k in METRIC_LABELS
            },
        )
        account_totals_fig.update_layout(
            template="plotly_white",
            legend_title_text="",
            margin={"l": 24, "r": 16, "t": 56, "b": 24},
            xaxis_title="Collected At",
            yaxis_title="Total",
        )

        growth_source = user_df.copy()
        for base_col in METRIC_COLUMNS:
            numeric_series = pd.to_numeric(growth_source[base_col], errors="coerce")
            growth_source[f"{base_col}_delta"] = numeric_series.diff()
        growth_df = growth_source.melt(
            id_vars=["collected_at"],
            value_vars=[
                "downloads_total_delta",
                "views_total_delta",
            ],
            var_name="metric_delta",
            value_name="delta",
        )
        growth_df["metric"] = growth_df["metric_delta"].str.replace("_delta", "", regex=False)
        growth_df["metric_label"] = growth_df["metric"].map(METRIC_LABELS)
        growth_df = growth_df.fillna(0)
        account_growth_fig = px.bar(
            growth_df,
            x="collected_at",
            y="delta",
            color="metric_label",
            barmode="group",
            title="Growth Between Runs",
            color_discrete_map={
                METRIC_LABELS[k]: v for k, v in COLORS.items() if k in METRIC_LABELS
            },
        )
        account_growth_fig.update_layout(
            template="plotly_white",
            legend_title_text="",
            margin={"l": 24, "r": 16, "t": 56, "b": 24},
            xaxis_title="Collected At",
            yaxis_title="Delta vs Previous Run",
        )

        tracked_photos_source = user_df[["collected_at", "total_photos"]].copy()
        tracked_photos_source["total_photos"] = pd.to_numeric(
            tracked_photos_source["total_photos"], errors="coerce"
        )
        if tracked_photos_source["total_photos"].dropna().empty:
            tracked_photos_fig = _empty_figure(
                "Tracked Photos Over Time", "No tracked photo totals available yet."
            )
        else:
            tracked_photos_fig = px.line(
                tracked_photos_source,
                x="collected_at",
                y="total_photos",
                markers=True,
                title="Tracked Photos Over Time",
                color_discrete_sequence=["#0284c7"],
            )
            tracked_photos_fig.update_layout(
                template="plotly_white",
                showlegend=False,
                margin={"l": 24, "r": 16, "t": 56, "b": 24},
                xaxis_title="Collected At",
                yaxis_title="Tracked Photos",
            )
            tracked_photos_fig.update_traces(connectgaps=False)

        new_photos_source = tracked_photos_source.copy()
        new_photos_source["new_photos"] = (
            new_photos_source["total_photos"].diff().fillna(0).clip(lower=0)
        )
        new_photos_fig = px.bar(
            new_photos_source,
            x="collected_at",
            y="new_photos",
            title="New Photos Added Per Run",
            color_discrete_sequence=["#f97316"],
        )
        new_photos_fig.update_layout(
            template="plotly_white",
            showlegend=False,
            margin={"l": 24, "r": 16, "t": 56, "b": 24},
            xaxis_title="Collected At",
            yaxis_title="New Photos",
        )

        photo_options: list[dict[str, str]] = []
        for _, row in photo_latest_df.iterrows():
            photo_options.append(
                {"label": _photo_option_label(row), "value": str(row["photo_id"])}
            )

        option_values = {opt["value"] for opt in photo_options}
        if selected_photo_id not in option_values:
            selected_photo_id = photo_options[0]["value"] if photo_options else None

        if selected_photo_id and not photo_history_df.empty:
            selected_photo_df = photo_history_df[
                photo_history_df["photo_id"] == selected_photo_id
            ].copy()
            selected_photo_df = selected_photo_df.sort_values("collected_at")
            selected_photo_df[metric] = pd.to_numeric(
                selected_photo_df[metric], errors="coerce"
            )
            metric_label = METRIC_LABELS.get(metric, metric)
            photo_trend_fig = px.line(
                selected_photo_df,
                x="collected_at",
                y=metric,
                markers=True,
                title=f"{metric_label} Trend: {selected_photo_id}",
                color_discrete_sequence=[COLORS.get(metric, "#0ea5a6")],
            )
            photo_trend_fig.update_layout(
                template="plotly_white",
                showlegend=False,
                margin={"l": 24, "r": 16, "t": 56, "b": 24},
                xaxis_title="Collected At",
                yaxis_title=metric_label,
            )
        else:
            photo_trend_fig = _empty_figure(
                "Photo Trend", "No photo history found for the selected photo."
            )

        delta_col = DELTA_COLUMNS.get(metric, "views_delta_since_previous")
        metric_label = METRIC_LABELS.get(metric, metric)
        movers_df = photo_latest_df.copy()
        movers_df[delta_col] = pd.to_numeric(movers_df[delta_col], errors="coerce").fillna(0)
        movers_df["photo_label"] = movers_df.apply(_photo_option_label, axis=1)
        movers_df["direction"] = movers_df[delta_col].apply(
            lambda val: "Gainer" if val >= 0 else "Decliner"
        )
        top_gainers = movers_df.sort_values(delta_col, ascending=False).head(8)
        top_decliners = movers_df.sort_values(delta_col, ascending=True).head(8)
        movers_display = pd.concat([top_decliners, top_gainers], ignore_index=True)
        movers_display = movers_display.drop_duplicates(subset=["photo_id"], keep="last")
        movers_display = movers_display.sort_values(delta_col, ascending=True)
        top_movers_fig = px.bar(
            movers_display,
            x=delta_col,
            y="photo_label",
            title=f"Biggest Movers by {metric_label} (Latest vs Previous Run)",
            color="direction",
            orientation="h",
            custom_data=["photo_id"],
            color_discrete_map={"Gainer": "#16a34a", "Decliner": "#dc2626"},
        )
        top_movers_fig.update_layout(
            template="plotly_white",
            margin={"l": 24, "r": 16, "t": 56, "b": 24},
            legend_title_text="",
            xaxis_title=f"{metric_label} Delta",
            yaxis_title="Photo",
        )
        top_movers_fig.update_traces(marker_line_width=0, hovertemplate=None)

        momentum_df = photo_latest_df.copy()
        momentum_df["photo_label"] = momentum_df.apply(_photo_option_label, axis=1)
        momentum_df[metric] = pd.to_numeric(momentum_df[metric], errors="coerce").fillna(0)
        momentum_df[delta_col] = pd.to_numeric(momentum_df[delta_col], errors="coerce").fillna(0)
        other_metric = "downloads_total" if metric == "views_total" else "views_total"
        momentum_df[other_metric] = pd.to_numeric(
            momentum_df[other_metric], errors="coerce"
        ).fillna(0)
        momentum_df["bubble_size"] = (momentum_df[other_metric] + 1).pow(0.5)
        momentum_df = momentum_df.sort_values(delta_col, ascending=False).head(120)
        momentum_fig = px.scatter(
            momentum_df,
            x=metric,
            y=delta_col,
            size="bubble_size",
            color=delta_col,
            color_continuous_scale="RdYlGn",
            custom_data=["photo_id"],
            hover_name="photo_label",
            hover_data={
                metric: ":,.0f",
                delta_col: ":,.0f",
                other_metric: ":,.0f",
                "bubble_size": False,
            },
            title=f"Momentum vs Reach ({metric_label})",
        )
        momentum_fig.update_layout(
            template="plotly_white",
            margin={"l": 24, "r": 16, "t": 56, "b": 24},
            coloraxis_showscale=False,
            xaxis_title=f"Current {metric_label}",
            yaxis_title=f"{metric_label} Delta (Latest vs Previous)",
        )
        momentum_fig.update_traces(
            marker={"sizemin": 5, "line": {"width": 0}},
            hovertemplate=None,
        )

        efficiency_df = photo_latest_df.copy()
        efficiency_df["photo_label"] = efficiency_df.apply(_photo_option_label, axis=1)
        efficiency_df["views_total"] = pd.to_numeric(
            efficiency_df["views_total"], errors="coerce"
        ).fillna(0)
        efficiency_df["downloads_total"] = pd.to_numeric(
            efficiency_df["downloads_total"], errors="coerce"
        ).fillna(0)
        efficiency_df["downloads_delta_since_previous"] = pd.to_numeric(
            efficiency_df["downloads_delta_since_previous"], errors="coerce"
        ).fillna(0)
        efficiency_df["views_delta_since_previous"] = pd.to_numeric(
            efficiency_df["views_delta_since_previous"], errors="coerce"
        ).fillna(0)
        safe_views = efficiency_df["views_total"].replace(0, pd.NA)
        efficiency_df["download_rate_pct"] = (
            (efficiency_df["downloads_total"] / safe_views) * 100.0
        ).fillna(0.0)
        efficiency_df = efficiency_df.sort_values("views_total", ascending=False).head(120)
        efficiency_fig = px.scatter(
            efficiency_df,
            x="views_total",
            y="downloads_total",
            color="download_rate_pct",
            size="views_total",
            custom_data=["photo_id"],
            hover_name="photo_label",
            hover_data={
                "views_total": ":,.0f",
                "downloads_total": ":,.0f",
                "download_rate_pct": ":.2f",
                "views_delta_since_previous": ":,.0f",
                "downloads_delta_since_previous": ":,.0f",
            },
            color_continuous_scale="Turbo",
            title="Download Efficiency by Photo",
        )
        efficiency_fig.update_layout(
            template="plotly_white",
            margin={"l": 24, "r": 16, "t": 56, "b": 24},
            coloraxis_colorbar={"title": "Download Rate %"},
            xaxis_title="Views",
            yaxis_title="Downloads",
        )
        efficiency_fig.update_traces(
            marker={"sizemin": 5, "line": {"width": 0}},
            hovertemplate=None,
        )

        latest_photo_with_images = photo_latest_df.copy()
        latest_photo_with_images["photo_id"] = latest_photo_with_images["photo_id"].astype(str)
        latest_photo_with_images = latest_photo_with_images.sort_values(
            "views_total", ascending=False
        )

        cache_warm_limit = max(
            0, _env_int("UNSPLASH_DASHBOARD_IMAGE_CACHE_WARM_LIMIT", 6)
        )
        cache_candidate_photo_ids = {
            str(pid)
            for pid in latest_photo_with_images.head(cache_warm_limit)["photo_id"].tolist()
        }
        if selected_photo_id:
            cache_candidate_photo_ids.add(selected_photo_id)

        image_src_by_photo_id: dict[str, str | None] = {}
        for _, row in latest_photo_with_images.iterrows():
            photo_id = str(row.get("photo_id", "")).strip()
            if not photo_id:
                continue
            raw_payload = row.get("raw_json")
            if photo_id in cache_candidate_photo_ids:
                image_src_by_photo_id[photo_id] = _resolve_photo_src(
                    cache_dir=photo_cache_dir,
                    photo_id=photo_id,
                    raw_json_payload=raw_payload,
                    route_prefix=photo_route_prefix,
                )
            else:
                image_src_by_photo_id[photo_id] = _extract_photo_url(raw_payload)

        selected_row: pd.Series | None = None
        selected_image_src: str | None = None
        if selected_photo_id:
            selected_match = latest_photo_with_images[
                latest_photo_with_images["photo_id"] == selected_photo_id
            ]
            if not selected_match.empty:
                selected_row = selected_match.iloc[0]
                selected_image_src = image_src_by_photo_id.get(selected_photo_id)

        if selected_row is None and not latest_photo_with_images.empty:
            selected_row = latest_photo_with_images.iloc[0]
            selected_image_src = image_src_by_photo_id.get(str(selected_row["photo_id"]))

        selected_photo_preview = _build_selected_photo_preview(selected_row, selected_image_src)

        latest_photo_cards = [
            _build_latest_photo_card(
                row,
                image_src_by_photo_id.get(str(row.get("photo_id", ""))),
            )
            for _, row in latest_photo_with_images.iterrows()
        ]
        if not latest_photo_cards:
            latest_photo_cards = [
                html.Div(
                    "No photos available yet.",
                    style={"color": "#475569", "padding": "6px"},
                )
            ]

        return (
            f"Runs: {runs_count} | Last collected: {latest_timestamp}",
            _fmt_int(latest_user.get("views_total")),
            _fmt_int(latest_user.get("downloads_total")),
            _fmt_int(latest_user.get("total_photos")),
            account_totals_fig,
            account_growth_fig,
            tracked_photos_fig,
            new_photos_fig,
            photo_trend_fig,
            top_movers_fig,
            momentum_fig,
            efficiency_fig,
            photo_options,
            selected_photo_id,
            selected_photo_preview,
            latest_photo_cards,
        )

    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a Dash dashboard for Unsplash stats snapshots."
    )
    parser.add_argument(
        "--database",
        default="data/unsplash_stats.sqlite",
        help="Path to SQLite database generated by collector.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind the server.")
    parser.add_argument("--port", type=int, default=8050, help="Port to bind the server.")
    parser.add_argument(
        "--debug", action="store_true", help="Run Dash in debug mode."
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = Path(args.database)
    app = create_app(db_path)
    app.run(host=args.host, port=args.port, debug=args.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
