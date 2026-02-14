from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path

from .api import UnsplashAPIError
from .collector import collect_snapshot
from .db import connect_db, init_db
from .exporters import export_csv_files


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="unsplash-stats",
        description="Collect point-in-time Unsplash account stats into SQLite.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect_parser = subparsers.add_parser(
        "collect", help="Fetch latest Unsplash stats and save one snapshot run."
    )
    collect_parser.add_argument(
        "--username",
        default=os.getenv("UNSPLASH_USERNAME", "tfinklea"),
        help="Unsplash username without @ (default: tfinklea or UNSPLASH_USERNAME).",
    )
    collect_parser.add_argument(
        "--access-key",
        default=os.getenv("UNSPLASH_ACCESS_KEY"),
        help="Unsplash API access key (or set UNSPLASH_ACCESS_KEY).",
    )
    collect_parser.add_argument(
        "--database",
        default="data/unsplash_stats.sqlite",
        help="SQLite database path.",
    )
    collect_parser.add_argument(
        "--max-photos",
        type=int,
        default=None,
        help="Optional cap on number of photos to fetch.",
    )
    collect_parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Optional cap on paginated photo pages to fetch.",
    )
    collect_parser.add_argument(
        "--delay-seconds",
        type=float,
        default=0.25,
        help="Additional delay between per-photo stats calls.",
    )
    collect_parser.add_argument(
        "--rate-limit-fraction",
        type=float,
        default=_env_float("UNSPLASH_RATE_LIMIT_FRACTION", 0.8),
        help=(
            "Fraction of Unsplash hourly limit to use (default 0.8 for 80%% speed). "
            "Set 0 to disable auto throttle."
        ),
    )
    collect_parser.add_argument(
        "--min-request-interval-seconds",
        type=float,
        default=_env_float("UNSPLASH_MIN_REQUEST_INTERVAL_SECONDS", 0.0),
        help=(
            "Minimum seconds between all API requests. "
            "Auto throttle interval is applied on top of this floor."
        ),
    )
    collect_parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail run immediately if any photo stats call fails.",
    )
    collect_parser.add_argument(
        "--skip-export",
        action="store_true",
        help="Skip CSV export after collection.",
    )
    collect_parser.add_argument(
        "--export-dir",
        default="exports",
        help="Where to write CSV exports after each collection run.",
    )

    export_parser = subparsers.add_parser(
        "export-csv", help="Export SQLite data into CSV files."
    )
    export_parser.add_argument(
        "--database",
        default="data/unsplash_stats.sqlite",
        help="SQLite database path.",
    )
    export_parser.add_argument(
        "--export-dir",
        default="exports",
        help="Where to write CSV exports.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if args.command == "collect":
        return _run_collect(args)
    if args.command == "export-csv":
        return _run_export(args)

    parser.error(f"Unknown command: {args.command}")
    return 2


def _run_collect(args: argparse.Namespace) -> int:
    if not args.access_key:
        print(
            "UNSPLASH_ACCESS_KEY is required. "
            "Set env var or pass --access-key.",
            file=sys.stderr,
        )
        return 2

    if args.rate_limit_fraction < 0:
        print("--rate-limit-fraction must be >= 0.", file=sys.stderr)
        return 2
    if args.rate_limit_fraction > 1:
        print("--rate-limit-fraction must be <= 1.", file=sys.stderr)
        return 2
    if args.min_request_interval_seconds < 0:
        print("--min-request-interval-seconds must be >= 0.", file=sys.stderr)
        return 2
    if args.delay_seconds < 0:
        print("--delay-seconds must be >= 0.", file=sys.stderr)
        return 2

    try:
        result = collect_snapshot(
            access_key=args.access_key,
            username=args.username,
            db_path=Path(args.database),
            max_photos=args.max_photos,
            max_pages=args.max_pages,
            delay_seconds=args.delay_seconds,
            rate_limit_fraction=args.rate_limit_fraction,
            min_request_interval_seconds=args.min_request_interval_seconds,
            strict=args.strict,
        )
    except UnsplashAPIError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        "Collected run {run_id} at {timestamp}. photos_seen={seen}, "
        "photos_saved={saved}, photo_errors={errors}".format(
            run_id=result.run_id,
            timestamp=result.collected_at,
            seen=result.photos_seen,
            saved=result.photos_saved,
            errors=result.photo_errors,
        )
    )
    if result.api_rate_limit_per_hour is not None:
        print(f"Unsplash rate limit: {result.api_rate_limit_per_hour} requests/hour")
    if result.throttle_interval_seconds is not None:
        print(
            "Applied throttle: one request every {seconds:.2f}s "
            "(~{rph:.2f} requests/hour)".format(
                seconds=result.throttle_interval_seconds,
                rph=result.throttle_target_requests_per_hour or 0.0,
            )
        )

    if args.skip_export:
        return 0

    connection = None
    try:
        connection = connect_db(args.database)
        init_db(connection)
        counts = export_csv_files(connection, args.export_dir)
    except sqlite3.Error as exc:
        print(f"CSV export failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()

    for filename, row_count in counts.items():
        print(f"Exported {filename} ({row_count} rows)")

    return 0


def _run_export(args: argparse.Namespace) -> int:
    database = Path(args.database)
    if not database.exists():
        print(f"Database not found: {database}", file=sys.stderr)
        return 2

    connection = None
    try:
        connection = sqlite3.connect(database)
        counts = export_csv_files(connection, args.export_dir)
    except sqlite3.Error as exc:
        print(f"CSV export failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()

    for filename, row_count in counts.items():
        print(f"Exported {filename} ({row_count} rows)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
