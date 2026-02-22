# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.9.4] 2026-02-22

- Filter health check requests from uvicorn access log

## [0.9.3] 2026-02-19

- Fix `KeyError` when Apple API omits the `displayColor` field, which caused
  the entire storage info to return `None` (no bar shown at all)
- Fix hex color values without `#` prefix (e.g. `"5EB0EF"`) being passed as
  invalid CSS `background-color`, causing transparent/invisible segments
- Add `_css_color()` helper that validates hex format, ensures `#` prefix, and
  falls back to the palette colors when the API value is unusable

## [0.9.2] 2026-02-18

Three fixes for the storage usage bar display:
- Add explicit height:100% to .icloud-storage-segment (CSS flex child could collapse to 0px height in some browsers)
- Use background-color instead of background shorthand to avoid conflicts with Bootstrap dark theme CSS resets
- Reassign icloudStorage object instead of mutating it in-place to ensure Alpine.js reactivity triggers properly
- Add fallback colors in backend when Apple API returns no displayColor


## [0.9.1] 2026-02-18

### Added
- iCloud storage usage display with Apple-style bar on dashboard
- Apple token refresh tracking and connection check feature
- DSM notifications for token expiry (limit backup notifications to errors)
- Build version metadata exposed in app, image labels, and `/health` endpoint
- CloudKit-based download fallback for shared-with-you folders
- Shared folder detection: skip shared-with-you folders, grey out in GUI, exclude from backup
- Photo-level fingerprint cache to avoid redundant downloads
- Backup trigger buttons and cancel button on dashboard
- Backup end time, duration, and correct timezone display
- Smart shared library detection and cross-account dedup
- Dashboard storage stats (file count + size) per account
- "Backup all folders" option for iCloud Drive
- Configurable sync policy per backup type: keep, delete, or archive
- Centralized backup schedule (one schedule for all accounts)
- SMS-based 2FA (2SA) verification option
- Synology DSM notification support via `synodsmnotify`
- Password-protected web UI with session management
- Multi-arch Docker images (`linux/amd64`, `linux/arm64`)
- Exclusion patterns (glob, paths) for Drive backups
- Etag caching for fast incremental backups
- Live progress tracking and built-in log viewer
- Dark mode support

### Fixed
- Shared folder downloads: owner-qualified zone, shareID support, fallback parameters
- Download fallback for files in folders with URL-special characters (`#`, `%`, `?`, `&`, `+`)
- Exclusion patterns with globs (e.g. `Medien/*`) were ignored for Drive folders
- `SECRET_KEY` now falls back to `AUTH_PASSWORD` when not explicitly set
- Streaming photo downloads to prevent OOM crash on large libraries
- Prevent unnecessary re-downloads of already-existing photos
- Reset stale "running" backup states after container restart
- Synology host lib mount for `synodsmnotify` shared library resolution
- Docker build cache restored by moving build ARGs after install layers
- Photo backup rewritten to use `api.photos.all` instead of album lookup
- Path-based exclusions now work for subfolder paths
- SMS 2FA API fix and empty photo file downloads
- Logging broken after LogLevel change

### Changed
- Sort shared-not-owned folders to the bottom of the Drive list
- Improved user record detection for shared folder ownership
- Improved backup stats readability in dark mode
