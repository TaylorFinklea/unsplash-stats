from __future__ import annotations

import os
from pathlib import Path

from .dashboard import create_app


def _resolve_database_path() -> Path:
    db_path = Path(os.getenv("UNSPLASH_DATABASE", "data/unsplash_stats.sqlite"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


dash_app = create_app(_resolve_database_path())
server = dash_app.server

