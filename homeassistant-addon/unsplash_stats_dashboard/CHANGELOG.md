# Changelog

## 0.1.4

- Add a dedicated "Biggest Movers by Downloads" chart to the dashboard.
- Keep the existing movers chart focused on views for side-by-side comparison.
- Add regression coverage for the new downloads-movers callback.

## 0.1.3

- Fix ingress redirect loop by separating Dash external request prefix from internal route prefix.
- Configure add-on runtime with `UNSPLASH_DASH_ROUTES_PATHNAME_PREFIX=/` when ingress is active.
- Keep internal Flask routes at root while generating ingress-prefixed frontend URLs.

## 0.1.2

- Fix Home Assistant "Open Web UI" 404 by redirecting `/` to the active Dash ingress prefix.
- Remove static `webui` URL so Home Assistant uses ingress routing for the add-on UI.

## 0.1.1

- Fix Home Assistant ingress loading issue by adding Dash path-prefix support.
- Auto-detect ingress entry path from Supervisor in add-on startup.
- Add regression test coverage for ingress path-prefix normalization.

## 0.1.0

- Initial Home Assistant add-on packaging.
- Includes dashboard UI and in-UI collection trigger.
- Supports `amd64` and `aarch64`.
