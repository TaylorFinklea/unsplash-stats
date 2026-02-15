from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


def connect_db(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON;")
    return connection


def init_db(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS collection_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            collected_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS user_stats_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES collection_runs(id) ON DELETE CASCADE,
            username TEXT NOT NULL,
            total_photos INTEGER,
            total_likes INTEGER,
            downloads_total INTEGER,
            views_total INTEGER,
            likes_total INTEGER,
            downloads_change_30d INTEGER,
            views_change_30d INTEGER,
            likes_change_30d INTEGER,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(run_id, username)
        );

        CREATE TABLE IF NOT EXISTS photo_stats_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES collection_runs(id) ON DELETE CASCADE,
            photo_id TEXT NOT NULL,
            photo_slug TEXT,
            photo_description TEXT,
            photo_created_at TEXT,
            photo_likes INTEGER,
            downloads_total INTEGER,
            views_total INTEGER,
            likes_total INTEGER,
            downloads_change_30d INTEGER,
            views_change_30d INTEGER,
            likes_change_30d INTEGER,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(run_id, photo_id)
        );

        CREATE INDEX IF NOT EXISTS idx_runs_collected_at
            ON collection_runs(collected_at);

        CREATE INDEX IF NOT EXISTS idx_photo_snapshots_photo_id
            ON photo_stats_snapshots(photo_id);

        CREATE INDEX IF NOT EXISTS idx_photo_snapshots_run_id
            ON photo_stats_snapshots(run_id);
        """
    )


def insert_collection_run(
    connection: sqlite3.Connection, *, username: str, collected_at: str
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO collection_runs (username, collected_at)
        VALUES (?, ?)
        """,
        (username, collected_at),
    )
    return int(cursor.lastrowid)


def insert_user_snapshot(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    username: str,
    total_photos: int | None,
    total_likes: int | None,
    downloads_total: int | None,
    views_total: int | None,
    likes_total: int | None,
    downloads_change_30d: int | None,
    views_change_30d: int | None,
    likes_change_30d: int | None,
    raw_json: dict[str, Any],
) -> None:
    connection.execute(
        """
        INSERT INTO user_stats_snapshots (
            run_id,
            username,
            total_photos,
            total_likes,
            downloads_total,
            views_total,
            likes_total,
            downloads_change_30d,
            views_change_30d,
            likes_change_30d,
            raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            username,
            total_photos,
            total_likes,
            downloads_total,
            views_total,
            likes_total,
            downloads_change_30d,
            views_change_30d,
            likes_change_30d,
            json.dumps(raw_json, separators=(",", ":"), ensure_ascii=False),
        ),
    )


def insert_photo_snapshot_rows(
    connection: sqlite3.Connection, rows: list[dict[str, Any]]
) -> None:
    connection.executemany(
        """
        INSERT INTO photo_stats_snapshots (
            run_id,
            photo_id,
            photo_slug,
            photo_description,
            photo_created_at,
            photo_likes,
            downloads_total,
            views_total,
            likes_total,
            downloads_change_30d,
            views_change_30d,
            likes_change_30d,
            raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row["run_id"],
                row["photo_id"],
                row.get("photo_slug"),
                row.get("photo_description"),
                row.get("photo_created_at"),
                row.get("photo_likes"),
                row.get("downloads_total"),
                row.get("views_total"),
                row.get("likes_total"),
                row.get("downloads_change_30d"),
                row.get("views_change_30d"),
                row.get("likes_change_30d"),
                json.dumps(row["raw_json"], separators=(",", ":"), ensure_ascii=False),
            )
            for row in rows
        ],
    )

