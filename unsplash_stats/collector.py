from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

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
    api_calls_made: int
    estimated_total_api_calls: int | None
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


def _estimate_photo_pages(
    total_photos: int | None, *, max_photos: int | None, max_pages: int | None, per_page: int
) -> int | None:
    if total_photos is None:
        return None
    if total_photos <= 0:
        return 0

    constrained_total = total_photos
    if max_photos is not None:
        constrained_total = min(constrained_total, max(0, max_photos))

    page_count = (constrained_total + per_page - 1) // per_page
    if max_pages is not None:
        page_count = min(page_count, max(0, max_pages))
    return page_count


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
    progress_hook: Callable[[dict[str, Any]], None] | None = None,
) -> CollectionResult:
    expected_total_api_calls: int | None = None
    api_calls_made = 0

    def _handle_request_event(event: dict[str, Any]) -> None:
        nonlocal api_calls_made, expected_total_api_calls
        api_calls_made = int(event.get("request_count", api_calls_made))

        if (
            expected_total_api_calls is None
            and event.get("path") == f"/users/{username}"
            and int(event.get("status_code", 0)) < 400
        ):
            response_data = event.get("response_data")
            if isinstance(response_data, dict):
                photo_pages = _estimate_photo_pages(
                    _as_int(response_data.get("total_photos")),
                    max_photos=max_photos,
                    max_pages=max_pages,
                    per_page=30,
                )
                if photo_pages is not None:
                    expected_total_api_calls = 2 + photo_pages

        percent_complete: float | None = None
        if expected_total_api_calls and expected_total_api_calls > 0:
            percent_complete = min(
                100.0, (api_calls_made / expected_total_api_calls) * 100.0
            )

        if progress_hook is not None:
            progress_hook(
                {
                    **event,
                    "completed_calls": api_calls_made,
                    "expected_total_calls": expected_total_api_calls,
                    "percent_complete": percent_complete,
                }
            )

    client = UnsplashClient(
        access_key,
        min_request_interval_seconds=min_request_interval_seconds,
        request_observer=_handle_request_event,
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
        username,
        per_page=30,
        max_pages=max_pages,
        max_items=max_photos,
        include_stats=True,
        stats_resolution="days",
        stats_quantity=30,
        page_delay_seconds=delay_seconds,
    ):
        photos_seen += 1
        photo_id = str(photo.get("id"))

        stats = _as_dict(photo.get("statistics"))
        if not stats:
            photo_errors += 1
            message = (
                f"Missing statistics for photo {photo_id} in /users/{username}/photos "
                "(expected when requesting stats=true)."
            )
            if strict:
                raise UnsplashAPIError(0, message)
            logger.warning(message)

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
        api_calls_made=api_calls_made,
        estimated_total_api_calls=expected_total_api_calls,
        api_rate_limit_per_hour=client.rate_limit,
        throttle_interval_seconds=throttle_interval_seconds,
        throttle_target_requests_per_hour=(
            None
            if not throttle_interval_seconds
            else 3600.0 / throttle_interval_seconds
        ),
    )
