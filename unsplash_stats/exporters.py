from __future__ import annotations

import csv
import sqlite3
from pathlib import Path


USER_HISTORY_QUERY = """
SELECT
    r.id AS run_id,
    r.collected_at,
    u.username,
    u.total_photos,
    u.downloads_total,
    u.views_total,
    u.downloads_change_30d,
    u.views_change_30d
FROM user_stats_snapshots u
JOIN collection_runs r ON r.id = u.run_id
ORDER BY r.collected_at ASC, r.id ASC;
"""

PHOTO_HISTORY_QUERY = """
SELECT
    r.id AS run_id,
    r.collected_at,
    p.photo_id,
    p.photo_slug,
    p.photo_description,
    p.photo_created_at,
    p.downloads_total,
    p.views_total,
    p.downloads_change_30d,
    p.views_change_30d
FROM photo_stats_snapshots p
JOIN collection_runs r ON r.id = p.run_id
ORDER BY r.collected_at ASC, r.id ASC, p.photo_id ASC;
"""

PHOTO_LATEST_QUERY = """
WITH ranked AS (
    SELECT
        p.id,
        p.photo_id,
        p.photo_slug,
        p.photo_description,
        p.photo_created_at,
        p.downloads_total,
        p.views_total,
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
    latest.collected_at AS latest_collected_at,
    previous.collected_at AS previous_collected_at,
    latest.downloads_total - COALESCE(previous.downloads_total, latest.downloads_total)
        AS downloads_delta_since_previous,
    latest.views_total - COALESCE(previous.views_total, latest.views_total)
        AS views_delta_since_previous
FROM latest
LEFT JOIN previous ON previous.photo_id = latest.photo_id
ORDER BY downloads_delta_since_previous DESC,
         views_delta_since_previous DESC,
         latest.photo_id ASC;
"""


def export_csv_files(
    connection: sqlite3.Connection, output_dir: Path | str
) -> dict[str, int]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    files = {
        "user_stats_history.csv": USER_HISTORY_QUERY,
        "photo_stats_history.csv": PHOTO_HISTORY_QUERY,
        "photo_latest.csv": PHOTO_LATEST_QUERY,
    }

    counts: dict[str, int] = {}
    for filename, query in files.items():
        row_count = _write_query_to_csv(connection, query, out_dir / filename)
        counts[filename] = row_count

    return counts


def _write_query_to_csv(
    connection: sqlite3.Connection, query: str, output_path: Path
) -> int:
    cursor = connection.execute(query)
    rows = cursor.fetchall()
    headers = [column[0] for column in cursor.description]

    with output_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(headers)
        writer.writerows(rows)

    return len(rows)
