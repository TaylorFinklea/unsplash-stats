# Unsplash Stats Dashboard

## What this add-on does

- Collects snapshot stats from Unsplash for a configured account.
- Stores history in SQLite.
- Shows trends and per-photo analytics in the dashboard UI.
- Lets you trigger collection directly from the dashboard (`Collect Now`).

## Configuration

```yaml
unsplash_access_key: "your_unsplash_access_key"
unsplash_username: "tfinklea"
rate_limit_fraction: 0.8
min_request_interval_seconds: 0
delay_seconds: 0.25
dashboard_image_cache_warm_limit: 6
database_path: /data/unsplash_stats.sqlite
export_dir: /data/exports
photo_cache_dir: /data/photo_cache
```

### Option details

- `unsplash_access_key`: Unsplash API access key (required).
- `unsplash_username`: Unsplash username (without `@`).
- `rate_limit_fraction`: Fraction of Unsplash hourly limit to use (`0.0` to `1.0`).
- `min_request_interval_seconds`: Minimum delay between all API calls.
- `delay_seconds`: Additional delay between paginated photo requests.
- `dashboard_image_cache_warm_limit`: Number of photo previews to warm in cache at load.
- `database_path`: SQLite file path inside the add-on container.
- `export_dir`: CSV export directory.
- `photo_cache_dir`: Local image cache directory for previews.

## Storage

All persistent files are stored in the add-on `/data` volume:

- SQLite DB: `/data/unsplash_stats.sqlite`
- CSV exports: `/data/exports`
- Cached images: `/data/photo_cache`

## Open the dashboard

- From Home Assistant: open the add-on, then click **Open Web UI**.
- Or (if you map port `8099`) browse directly to Home Assistant host port `8099`.

## Notes

- This add-on currently supports `amd64` and `aarch64` architectures.
- If `unsplash_access_key` is missing, the add-on exits with a clear startup error.

