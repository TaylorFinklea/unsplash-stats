from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class UnsplashAPIError(RuntimeError):
    status_code: int
    message: str
    payload: dict[str, Any] | None = None

    def __str__(self) -> str:
        return f"Unsplash API error {self.status_code}: {self.message}"


class UnsplashClient:
    def __init__(
        self,
        access_key: str,
        *,
        timeout_seconds: int = 30,
        user_agent: str = "unsplash-stats-tracker/0.1",
        min_request_interval_seconds: float = 0.0,
        rate_limit_retry_max_sleep_seconds: float = 1800.0,
    ) -> None:
        if not access_key:
            raise ValueError("access_key is required")

        self.access_key = access_key
        self.timeout_seconds = timeout_seconds
        self.base_url = "https://api.unsplash.com"
        self.headers = {
            "Accept-Version": "v1",
            "Authorization": f"Client-ID {access_key}",
            "User-Agent": user_agent,
        }
        self.rate_limit: int | None = None
        self.rate_limit_remaining: int | None = None
        self.min_request_interval_seconds = max(0.0, float(min_request_interval_seconds))
        self.rate_limit_retry_max_sleep_seconds = max(
            5.0, float(rate_limit_retry_max_sleep_seconds)
        )
        self._last_request_at_monotonic: float | None = None

    def set_min_request_interval(self, interval_seconds: float) -> None:
        self.min_request_interval_seconds = max(0.0, float(interval_seconds))

    def get_user(self, username: str) -> dict[str, Any]:
        return self._request(f"/users/{username}")

    def get_user_statistics(
        self,
        username: str,
        *,
        resolution: str = "days",
        quantity: int = 30,
    ) -> dict[str, Any]:
        return self._request(
            f"/users/{username}/statistics",
            params={"resolution": resolution, "quantity": quantity},
        )

    def get_photo_statistics(
        self,
        photo_id: str,
        *,
        resolution: str = "days",
        quantity: int = 30,
    ) -> dict[str, Any]:
        return self._request(
            f"/photos/{photo_id}/statistics",
            params={"resolution": resolution, "quantity": quantity},
        )

    def iter_user_photos(
        self,
        username: str,
        *,
        per_page: int = 30,
        max_pages: int | None = None,
        max_items: int | None = None,
    ):
        per_page = max(1, min(per_page, 30))
        emitted = 0
        page = 1

        while True:
            if max_pages is not None and page > max_pages:
                break

            photos = self._request(
                f"/users/{username}/photos",
                params={"page": page, "per_page": per_page, "order_by": "latest"},
            )
            if not isinstance(photos, list) or not photos:
                break

            for photo in photos:
                yield photo
                emitted += 1
                if max_items is not None and emitted >= max_items:
                    return

            if len(photos) < per_page:
                break

            page += 1

    def _request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = ""
        if params:
            query = "?" + urllib.parse.urlencode(params, doseq=True)

        url = f"{self.base_url}{path}{query}"
        request = urllib.request.Request(url=url, headers=self.headers, method="GET")
        rate_limit_hits = 0

        while True:
            self._enforce_min_request_interval()

            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    self._last_request_at_monotonic = time.monotonic()
                    self._update_rate_limit(response.headers)
                    raw_body = response.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                self._last_request_at_monotonic = time.monotonic()
                self._update_rate_limit(exc.headers)
                body = exc.read().decode("utf-8", errors="replace")
                payload: dict[str, Any] | None
                payload = None
                message = body.strip() or exc.reason
                try:
                    parsed = json.loads(body) if body else {}
                    if isinstance(parsed, dict):
                        payload = parsed
                        if "errors" in parsed and isinstance(parsed["errors"], list):
                            message = ", ".join(str(item) for item in parsed["errors"])
                        elif "error" in parsed:
                            message = str(parsed["error"])
                except json.JSONDecodeError:
                    pass

                if self._is_rate_limited(exc.code, message, payload):
                    wait_seconds = self._compute_rate_limit_wait_seconds(
                        exc.headers, rate_limit_hits
                    )
                    rate_limit_hits += 1
                    logger.warning(
                        "Rate limit response received (status=%s, remaining=%s). "
                        "Sleeping %.2fs before retrying.",
                        exc.code,
                        self.rate_limit_remaining,
                        wait_seconds,
                    )
                    time.sleep(wait_seconds)
                    continue

                raise UnsplashAPIError(exc.code, message, payload) from exc
            except urllib.error.URLError as exc:
                raise UnsplashAPIError(0, f"Connection error: {exc.reason}") from exc

            if not raw_body:
                return {}
            return json.loads(raw_body)

    def _enforce_min_request_interval(self) -> None:
        if self.min_request_interval_seconds <= 0:
            return
        if self._last_request_at_monotonic is None:
            return

        elapsed = time.monotonic() - self._last_request_at_monotonic
        sleep_seconds = self.min_request_interval_seconds - elapsed
        if sleep_seconds > 0:
            logger.info(
                "Sleeping %.2fs to respect request throttle.", sleep_seconds
            )
            time.sleep(sleep_seconds)

    def _is_rate_limited(
        self, status_code: int, message: str, payload: dict[str, Any] | None
    ) -> bool:
        if status_code not in (403, 429):
            return False

        if self.rate_limit_remaining == 0:
            return True

        message_text = message.lower()
        if "rate limit" in message_text or "too many requests" in message_text:
            return True

        if payload and isinstance(payload.get("errors"), list):
            for value in payload["errors"]:
                text = str(value).lower()
                if "rate limit" in text or "too many requests" in text:
                    return True

        return False

    def _compute_rate_limit_wait_seconds(
        self, headers: Any, rate_limit_hits: int
    ) -> float:
        retry_after = headers.get("Retry-After") if headers else None
        retry_after_seconds = self._parse_retry_after_seconds(retry_after)
        if retry_after_seconds is not None:
            return min(retry_after_seconds, self.rate_limit_retry_max_sleep_seconds)

        reset_header = headers.get("X-Ratelimit-Reset") if headers else None
        if reset_header is not None:
            try:
                reset_epoch = float(reset_header)
                now_epoch = time.time()
                if reset_epoch > now_epoch:
                    return min(
                        reset_epoch - now_epoch + 1.0,
                        self.rate_limit_retry_max_sleep_seconds,
                    )
            except ValueError:
                pass

        base = max(self.min_request_interval_seconds, 5.0)
        backoff_multiplier = 2 ** min(rate_limit_hits, 8)
        return min(base * backoff_multiplier, self.rate_limit_retry_max_sleep_seconds)

    def _parse_retry_after_seconds(self, value: str | None) -> float | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None

        try:
            seconds = float(value)
            if seconds > 0:
                return seconds
        except ValueError:
            pass

        try:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            seconds_until = (dt - datetime.now(timezone.utc)).total_seconds()
            if seconds_until > 0:
                return seconds_until
        except (TypeError, ValueError):
            return None

        return None

    def _update_rate_limit(self, headers: Any) -> None:
        if not headers:
            return

        limit = headers.get("X-Ratelimit-Limit")
        remaining = headers.get("X-Ratelimit-Remaining")
        if limit is not None:
            try:
                self.rate_limit = int(limit)
            except ValueError:
                self.rate_limit = None
        if remaining is not None:
            try:
                self.rate_limit_remaining = int(remaining)
            except ValueError:
                self.rate_limit_remaining = None
