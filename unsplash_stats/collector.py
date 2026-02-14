from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .api import UnsplashAPIError, UnsplashClient
from .db import (
    connect_db,
    init_db,
    insert_collection_run,
    insert_photo_snapshot_rows,
    insert_user_snapshot,
)

logger = logging.getLogger(__name__)


@dataclass
class CollectionResult:
    run_id: int
    collected_at: str
    photos_seen: int
    photos_saved: int
    photo_errors: int
    api_rate_limit_per_hour: int | None
    throttle_interval_seconds: float | None
    throttle_target_requests_per_hour: float | None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _request_interval_for_hourly_budget(
    requests_per_hour: int | None, fraction: float
) -> float | None:
    if requests_per_hour is None or requests_per_hour <= 0:
        return None
    if fraction <= 0:
        return None
    target_requests_per_hour = requests_per_hour * fraction
    if target_requests_per_hour <= 0:
        return None
    return 3600.0 / target_requests_per_hour


def collect_snapshot(
    *,
    access_key: str,
    username: str,
    db_path: Path | str,
    max_photos: int | None = None,
    max_pages: int | None = None,
    delay_seconds: float = 0.25,
    rate_limit_fraction: float = 0.8,
    min_request_interval_seconds: float = 0.0,
    strict: bool = False,
) -> CollectionResult:
    client = UnsplashClient(
        access_key, min_request_interval_seconds=min_request_interval_seconds
    )
    collected_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    user = client.get_user(username)
    throttle_interval_seconds = client.min_request_interval_seconds or None
    auto_interval = _request_interval_for_hourly_budget(
        client.rate_limit, rate_limit_fraction
    )
    if auto_interval is not None:
        effective_interval = max(client.min_request_interval_seconds, auto_interval)
        client.set_min_request_interval(effective_interval)
        throttle_interval_seconds = effective_interval
        logger.info(
            "Unsplash hourly limit=%s, fraction=%.3f, request interval=%.2fs.",
            client.rate_limit,
            rate_limit_fraction,
            effective_interval,
        )

    user_stats = client.get_user_statistics(username, resolution="days", quantity=30)

    photos_seen = 0
    photo_errors = 0
    photo_rows: list[dict[str, Any]] = []

    for photo in client.iter_user_photos(
        username, per_page=30, max_pages=max_pages, max_items=max_photos
    ):
        photos_seen += 1
        photo_id = str(photo.get("id"))
        try:
            stats = client.get_photo_statistics(photo_id, resolution="days", quantity=30)
        except UnsplashAPIError as exc:
            photo_errors += 1
            if strict:
                raise
            logger.warning("Skipping photo %s due to API error: %s", photo_id, exc)
            if delay_seconds > 0:
                time.sleep(delay_seconds)
            continue

        stats = _as_dict(stats)
        downloads = _as_dict(stats.get("downloads"))
        views = _as_dict(stats.get("views"))
        likes = _as_dict(stats.get("likes"))

        photo_rows.append(
            {
                "photo_id": photo_id,
                "photo_slug": photo.get("slug"),
                "photo_description": photo.get("description")
                or photo.get("alt_description"),
                "photo_created_at": photo.get("created_at"),
                "photo_likes": _as_int(photo.get("likes")),
                "downloads_total": _as_int(downloads.get("total")),
                "views_total": _as_int(views.get("total")),
                "likes_total": _as_int(likes.get("total")),
                "downloads_change_30d": _as_int(
                    _as_dict(downloads.get("historical")).get("change")
                ),
                "views_change_30d": _as_int(_as_dict(views.get("historical")).get("change")),
                "likes_change_30d": _as_int(_as_dict(likes.get("historical")).get("change")),
                "raw_json": {
                    "photo": photo,
                    "statistics": stats,
                    "rate_limit_remaining": client.rate_limit_remaining,
                },
            }
        )

        if delay_seconds > 0:
            time.sleep(delay_seconds)

    user = _as_dict(user)
    user_stats = _as_dict(user_stats)
    downloads = _as_dict(user_stats.get("downloads"))
    views = _as_dict(user_stats.get("views"))
    likes = _as_dict(user_stats.get("likes"))

    connection = connect_db(db_path)
    init_db(connection)
    try:
        with connection:
            run_id = insert_collection_run(
                connection, username=username, collected_at=collected_at
            )
            insert_user_snapshot(
                connection,
                run_id=run_id,
                username=username,
                total_photos=_as_int(user.get("total_photos")),
                total_likes=_as_int(user.get("total_likes")),
                downloads_total=_as_int(downloads.get("total")),
                views_total=_as_int(views.get("total")),
                likes_total=_as_int(likes.get("total")),
                downloads_change_30d=_as_int(
                    _as_dict(downloads.get("historical")).get("change")
                ),
                views_change_30d=_as_int(_as_dict(views.get("historical")).get("change")),
                likes_change_30d=_as_int(_as_dict(likes.get("historical")).get("change")),
                raw_json={"user": user, "statistics": user_stats},
            )

            for row in photo_rows:
                row["run_id"] = run_id
            insert_photo_snapshot_rows(connection, photo_rows)
    finally:
        connection.close()

    return CollectionResult(
        run_id=run_id,
        collected_at=collected_at,
        photos_seen=photos_seen,
        photos_saved=len(photo_rows),
        photo_errors=photo_errors,
        api_rate_limit_per_hour=client.rate_limit,
        throttle_interval_seconds=throttle_interval_seconds,
        throttle_target_requests_per_hour=(
            None
            if not throttle_interval_seconds
            else 3600.0 / throttle_interval_seconds
        ),
    )
