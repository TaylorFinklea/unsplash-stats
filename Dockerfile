FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UNSPLASH_DATABASE=/app/data/unsplash_stats.sqlite \
    UNSPLASH_EXPORT_DIR=/app/exports \
    UNSPLASH_PHOTO_CACHE_DIR=/app/data/photo_cache

COPY pyproject.toml uv.lock README.md /app/
RUN uv sync --frozen --no-dev --no-install-project

COPY unsplash_stats /app/unsplash_stats

RUN mkdir -p /app/data /app/exports

EXPOSE 8050

CMD ["uv", "run", "--no-sync", "gunicorn", "--bind", "0.0.0.0:8050", "--workers", "2", "--threads", "4", "--timeout", "120", "unsplash_stats.wsgi:server"]

