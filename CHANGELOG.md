# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
