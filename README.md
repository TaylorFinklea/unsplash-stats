# Unsplash Stats Collector

Collects point-in-time stats for an Unsplash account and stores them in SQLite.
It also exports CSV files so you can open the data in a spreadsheet immediately.

## What gets collected

- Account-level totals per run:
  - total photos
  - total downloads
  - total views
- Per-photo totals per run:
  - downloads
  - views
  - photo metadata (id, slug, description, created_at)

Each run is timestamped (UTC), so you can compute trends and deltas over time.

## Quick start

1. Create an Unsplash API app and copy your `Access Key`:
   - https://unsplash.com/oauth/applications
2. Create or update `.env` in the project root:

```bash
UNSPLASH_ACCESS_KEY=your_access_key_here
UNSPLASH_USERNAME=tfinklea
UNSPLASH_RATE_LIMIT_FRACTION=0.8
UNSPLASH_MIN_REQUEST_INTERVAL_SECONDS=0
```

3. Sync the project environment with `uv`:

```bash
uv sync
```

4. Run one collection for your account (`@tfinklea`):

```bash
uv run --env-file .env python -m unsplash_stats.cli collect --username tfinklea
```

The CLI prints progress after every API call, including estimated done percent when available.

This writes:

- SQLite DB: `data/unsplash_stats.sqlite`
- CSV exports: `exports/user_stats_history.csv`, `exports/photo_stats_history.csv`, `exports/photo_latest.csv`

## Commands

Collect snapshot:

```bash
uv run --env-file .env python -m unsplash_stats.cli collect --username tfinklea
```

Collect with limits (useful for testing):

```bash
uv run --env-file .env python -m unsplash_stats.cli collect --username tfinklea --max-photos 20 --max-pages 1
```

Collect without CSV export:

```bash
uv run --env-file .env python -m unsplash_stats.cli collect --username tfinklea --skip-export
```

Export CSVs from an existing database:

```bash
uv run --env-file .env python -m unsplash_stats.cli export-csv --database data/unsplash_stats.sqlite --export-dir exports
```

Run the dashboard:

```bash
uv run --env-file .env python -m unsplash_stats.dashboard
```

In the UI, use `Collect Now` to run a fresh snapshot collection directly from the dashboard
using your current `.env` credentials and rate-limit settings.
Use the `Collection Progress` tab to monitor live API call counts and done percentage while the run is active.
Photo previews are cached locally (default: `data/photo_cache`; override with `UNSPLASH_PHOTO_CACHE_DIR`).
Startup pre-caches a small set of images (`UNSPLASH_DASHBOARD_IMAGE_CACHE_WARM_LIMIT`, default `6`).

Run regression tests:

```bash
uv run --env-file .env python -m unittest discover -s tests -v
```

Use stricter throttling (50% of API limit):

```bash
uv run --env-file .env python -m unsplash_stats.cli collect --username tfinklea --rate-limit-fraction 0.5
```

Add extra delay between paginated photo requests:

```bash
uv run --env-file .env python -m unsplash_stats.cli collect --username tfinklea --delay-seconds 2
```

## API strategy

- Account totals: `GET /users/:username/statistics`
- Per-photo stats: `GET /users/:username/photos?stats=true&resolution=days&quantity=30`
- Request volume is approximately:
  - `2 + number_of_photo_pages`
  - instead of `2 + number_of_photo_pages + number_of_photos`

## Rate limiting

- Collector auto-throttles using `X-Ratelimit-Limit` from Unsplash API responses.
- Default `UNSPLASH_RATE_LIMIT_FRACTION=0.8` means 80% speed:
  - Demo limit `50/hour` -> target `40/hour` -> one request every `90` seconds.
  - Production limit `5000/hour` -> target `4000/hour` -> one request every `0.9` seconds.
- On 403/429 rate-limit responses, the collector automatically waits and retries until it can continue.
- Add a hard floor with `UNSPLASH_MIN_REQUEST_INTERVAL_SECONDS` if you want to force even slower requests.

## Suggested schedule

Run every 6 hours via cron (safer when throttling heavily):

```cron
0 */6 * * * cd /Users/tfinklea/git/unsplash-stats && /usr/bin/env uv run --env-file .env python -m unsplash_stats.cli collect --username tfinklea >> cron.log 2>&1
```

## Notes

- The collector uses official Unsplash API endpoints (more stable than scraping HTML).
- Per-photo stats are taken from paginated user-photo responses (`stats=true`) to keep request count low.
- Use `--max-photos`/`--max-pages` to shorten runs for smoke tests.
- Dash UI includes account totals, per-run growth, tracked/new photo trends, movers, momentum/efficiency scatter plots, and per-photo drilldowns.
