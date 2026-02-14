# Unsplash Stats Collector

Collects point-in-time stats for an Unsplash account and stores them in SQLite.
It also exports CSV files so you can open the data in a spreadsheet immediately.

## What gets collected

- Account-level totals per run:
  - total photos
  - total likes
  - total downloads
  - total views
- Per-photo totals per run:
  - downloads
  - views
  - likes
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

Use stricter throttling (50% of API limit):

```bash
uv run --env-file .env python -m unsplash_stats.cli collect --username tfinklea --rate-limit-fraction 0.5
```

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
- Per-photo stats are fetched one photo at a time and can take a long time at low request rates. Use `--max-photos`/`--max-pages` to shorten runs.
- SQLite + CSV output is ready for a Streamlit dashboard in the next step.
