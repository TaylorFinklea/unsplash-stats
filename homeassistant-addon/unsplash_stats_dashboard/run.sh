#!/usr/bin/env bash
set -euo pipefail

python3 - <<'PY'
from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


OPTIONS_PATH = pathlib.Path("/data/options.json")


def load_options() -> dict[str, object]:
    if not OPTIONS_PATH.exists():
        return {}
    try:
        return json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"WARN: Failed to parse {OPTIONS_PATH}: {exc}", flush=True)
        return {}


def get_str(options: dict[str, object], key: str, default: str) -> str:
    value = options.get(key, default)
    if value is None:
        return default
    return str(value).strip()


def get_float(
    options: dict[str, object],
    key: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    raw = options.get(key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        print(f"WARN: Invalid `{key}` value `{raw}`, using {default}", flush=True)
        value = float(default)

    if minimum is not None and value < minimum:
        print(f"WARN: `{key}` below {minimum}; clamping", flush=True)
        value = minimum
    if maximum is not None and value > maximum:
        print(f"WARN: `{key}` above {maximum}; clamping", flush=True)
        value = maximum
    return value


def get_int(
    options: dict[str, object],
    key: str,
    default: int,
    *,
    minimum: int | None = None,
) -> int:
    raw = options.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        print(f"WARN: Invalid `{key}` value `{raw}`, using {default}", flush=True)
        value = int(default)

    if minimum is not None and value < minimum:
        print(f"WARN: `{key}` below {minimum}; clamping", flush=True)
        value = minimum
    return value


def normalize_prefix(value: str | None) -> str | None:
    if value is None:
        return None
    prefix = str(value).strip()
    if not prefix:
        return None
    if "://" in prefix:
        try:
            from urllib.parse import urlparse

            prefix = urlparse(prefix).path or "/"
        except Exception:
            return None
    if not prefix.startswith("/"):
        prefix = f"/{prefix}"
    if not prefix.endswith("/"):
        prefix = f"{prefix}/"
    return prefix


def detect_ingress_prefix() -> str | None:
    direct = normalize_prefix(os.getenv("UNSPLASH_DASH_REQUESTS_PATHNAME_PREFIX"))
    if direct:
        return direct

    ingress_entry_env = normalize_prefix(os.getenv("INGRESS_ENTRY"))
    if ingress_entry_env:
        return ingress_entry_env

    supervisor_token = os.getenv("SUPERVISOR_TOKEN", "").strip()
    if not supervisor_token:
        return None

    request = urllib.request.Request(
        "http://supervisor/addons/self/info",
        headers={
            "Authorization": f"Bearer {supervisor_token}",
            "Content-Type": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"WARN: Failed to fetch ingress info from supervisor: {exc}", flush=True)
        return None

    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    return normalize_prefix(data.get("ingress_entry"))


options = load_options()

access_key = get_str(options, "unsplash_access_key", "")
if not access_key:
    print(
        "ERROR: `unsplash_access_key` is required. Set it in add-on Configuration.",
        file=sys.stderr,
        flush=True,
    )
    sys.exit(1)

username = get_str(options, "unsplash_username", "tfinklea") or "tfinklea"
rate_limit_fraction = get_float(
    options,
    "rate_limit_fraction",
    0.8,
    minimum=0.0,
    maximum=1.0,
)
min_request_interval_seconds = get_float(
    options,
    "min_request_interval_seconds",
    0.0,
    minimum=0.0,
)
delay_seconds = get_float(
    options,
    "delay_seconds",
    0.25,
    minimum=0.0,
)
cache_warm_limit = get_int(
    options,
    "dashboard_image_cache_warm_limit",
    6,
    minimum=0,
)
database_path = get_str(options, "database_path", "/data/unsplash_stats.sqlite")
export_dir = get_str(options, "export_dir", "/data/exports")
photo_cache_dir = get_str(options, "photo_cache_dir", "/data/photo_cache")

pathlib.Path(database_path).parent.mkdir(parents=True, exist_ok=True)
pathlib.Path(export_dir).mkdir(parents=True, exist_ok=True)
pathlib.Path(photo_cache_dir).mkdir(parents=True, exist_ok=True)

os.environ["UNSPLASH_ACCESS_KEY"] = access_key
os.environ["UNSPLASH_USERNAME"] = username
os.environ["UNSPLASH_RATE_LIMIT_FRACTION"] = str(rate_limit_fraction)
os.environ["UNSPLASH_MIN_REQUEST_INTERVAL_SECONDS"] = str(min_request_interval_seconds)
os.environ["UNSPLASH_DELAY_SECONDS"] = str(delay_seconds)
os.environ["UNSPLASH_DASHBOARD_IMAGE_CACHE_WARM_LIMIT"] = str(cache_warm_limit)
os.environ["UNSPLASH_DATABASE"] = database_path
os.environ["UNSPLASH_EXPORT_DIR"] = export_dir
os.environ["UNSPLASH_PHOTO_CACHE_DIR"] = photo_cache_dir

ingress_prefix = detect_ingress_prefix()
if ingress_prefix:
    os.environ["UNSPLASH_DASH_REQUESTS_PATHNAME_PREFIX"] = ingress_prefix
    # Home Assistant ingress strips this prefix before proxying to the add-on.
    os.environ["UNSPLASH_DASH_ROUTES_PATHNAME_PREFIX"] = "/"

command = [
    "python",
    "-m",
    "gunicorn",
    "--bind",
    "0.0.0.0:8099",
    "--workers",
    "2",
    "--threads",
    "4",
    "--timeout",
    "120",
    "unsplash_stats.wsgi:server",
]

print("Starting Unsplash Stats Dashboard add-on...", flush=True)
print(f"Configured Unsplash username: @{username}", flush=True)
print(f"Database path: {database_path}", flush=True)
if ingress_prefix:
    print(f"Dash requests path prefix: {ingress_prefix}", flush=True)
    print("Dash routes path prefix: /", flush=True)
os.execvp(command[0], command)
PY
