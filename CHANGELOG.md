# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.10.5] 2026-06-10

### Fixed
- **Notes attachments failed with "Gone (410)" on scheduled runs** – pyicloud
  caches attachment metadata including the signed CloudKit download URLs
  inside its notes service, and the app keeps the iCloud session alive
  between backup runs. The signed URLs expire after a few hours, so every
  follow-up run tried to download attachments through stale links and
  logged one `410 Gone` warning per attachment. The attachment cache is
  now cleared before each notes fetch so every run gets freshly signed
  URLs. Notes whose attachment download failed are also no longer recorded
  in the content-hash cache, so their attachments are retried on the next
  run instead of being skipped as "unchanged".

### Changed
- **Re-authorization works without password, like `icloudpd --auth-only`** –
  The "Neu autorisieren" button (which always demanded the Apple ID
  password first) is gone. "Verbindung prüfen" now drives the whole flow:
  it validates the saved session against Apple and, when the trust token
  has expired, directly triggers the device push and opens the 2FA dialog.
  The password form only appears in the rare case that Apple requires a
  full re-login. The account detail page starts this flow automatically
  when the account needs re-authorization.
- **Session is validated against Apple before every backup run** – Scheduled
  and manual backups now begin with a real connection check (reconnect from
  the saved cookies plus a lightweight API call) instead of trusting a
  cached in-memory session. An expired token aborts the run immediately
  with a clear message, sets the account to `requires_2fa` and sends the
  "Token abgelaufen" notification once instead of failing mid-backup.

### Added
- **"Alle prüfen" button on the dashboard** – Checks the connection of all
  accounts in one go, shows a compact summary (e.g. "2 OK, 1× 2FA nötig")
  and then walks through every account that needs re-authorization: the
  2FA dialog opens for each one in turn, right on the dashboard.

### Removed
- **Token-age heuristic** – The ~30-day lifetime estimate (token-age
  progress bar, "geschätzt noch ~N Tage gültig" labels, "läuft bald ab"
  warning notification) has been removed. The trust token cannot be
  renewed ahead of time anyway, and the real per-backup validation plus
  the expiry notification make the guesswork obsolete. The account pages
  still show the timestamp of the last token refresh.

## [0.10.4] 2026-06-10

- Bump pyicloud dependency to version 2.6.5

## [0.10.3] 2026-05-30

- Bump pyicloud dependency to version 2.6.4

## [0.10.2] 2026-05-28

- Bump pyicloud dependency to version 2.6.1

## [0.10.1] 2026-05-23

- Bump pyicloud dependency to version 2.6.0

## [0.10.0] - 2026-05-20

### Fixed
- **Misleading app-specific password hint** – The add-account form and the
  README recommended an app-specific password, but those do not work with
  pyicloud: the iCloud web-service login goes through the 2FA/HSA2
  challenge, which app-specific passwords cannot satisfy. The UI now asks
  for the regular Apple ID password and explains why.

### Added
- **iCloud Notes & Reminders backup** – Two new backup sources can be
  toggled per account. Reminders are written as JSON
  (`reminders/reminders.json` plus one `reminders/<list>.json` per list).
  Each note gets its own folder
  `notes/<folder>/<title>/` containing `note.html`, `note.txt` and an
  `attachments/` subfolder with the note's downloaded attachments, plus a
  complete `notes/notes.json` dump; locked notes are listed in the JSON
  but skipped for file export since they can't be decrypted server-side.
  Both use a content-hash cache for change detection (note attachments are
  part of the hash, so new attachments trigger a re-download) and appear
  in the per-run progress, the backup summary, and the local storage
  stats. Renamed/deleted notes have their stale folders cleaned up.

### Changed
- **Token-lifetime estimate lowered from 60 to 30 days** – Apple now expires
  iCloud session tokens after roughly 30 days, so the re-auth countdown,
  the token-age progress bar/colour thresholds on the account detail page,
  and the "~N Tage verbleibend" labels on the dashboard were all off by 2×.
  The expiry warning notification now fires when ~7 days of validity
  remain (previously ~10 days against the 60-day assumption).


## [0.9.23] - 2026-05-15

### Added
- **Backup mount disk-usage indicator** – The dashboard now shows a compact
  card with total / used / free space for the filesystem hosting
  `BACKUP_PATH` (default `/backups`), along with a coloured progress bar
  (green / amber / red as utilisation approaches 100 %). Handy when the
  backup target is a remote mount (CIFS, NFS) and free space matters.
  Powered by a new `GET /api/backup/disk-usage` endpoint that uses
  `shutil.disk_usage` under the hood.
- **Per-run archive folders with retention policy** – Files that the sync
  policy moves into the archive are now grouped per backup run under
  `/archive/YYYY-MM-DD_HH-MM/<account>/<type>/…` instead of being mixed
  into a single account-scoped directory. A new global setting
  *Aufbewahrung (Tage)* (default `30`, `0` disables pruning) controls how
  long these run folders are kept; entries older than the cutoff are
  removed automatically after every backup run. A "Jetzt aufräumen"
  button in the settings page triggers a one-shot prune via the new
  `POST /api/settings/archive/prune` endpoint. Empty per-run folders are
  cleaned up automatically so the archive root stays tidy when nothing
  was archived during a run.

### Fixed
- **Scheduled backups silently ran in UTC** – APScheduler was initialised
  without an explicit timezone and the `python:3.12-slim` base image ships
  without `tzdata`, so even with `TZ=Europe/Berlin` in the compose file
  `tzlocal` fell back to UTC. A cron like `0 2 * * *` therefore fired at
  02:00 UTC (03:00 / 04:00 local time) instead of 02:00 Berlin time. The
  image now installs `tzdata`, and the scheduler resolves the timezone
  explicitly from the `TZ` env var via `zoneinfo.ZoneInfo`, falling back
  to UTC with an explicit log warning when `TZ` is unset or unknown. The
  cron trigger is bound to the same timezone and the next run time is
  logged when the schedule is registered.

## [0.9.22] - 2026-05-15

### Fixed
- **Clunky 2FA flow when adding a new account** – Previously the dashboard
  simply closed the modal after the Apple ID and password were submitted,
  ignoring the `requires_2fa` status in the response. The push to the
  user's Apple devices was only triggered when opening the account detail
  page, which forced the user to enter the password a second time.
  `POST /api/accounts` now triggers the push notification directly after
  authentication (matching the `reconnect` behaviour), and the dashboard
  opens the 2FA modal automatically afterwards. Once the code is
  submitted, backup status and iCloud storage info are refreshed
  immediately.

## [0.9.21] - 2026-05-04

### Fixed
- **Zombie `[curl] <defunct>` Prozesse** – Der Container lief mit `uvicorn`
  als PID 1 ohne Init. Die Docker `HEALTHCHECK`-Aufrufe von `curl` (alle 30 s
  via `sh -c`) wurden nach Beenden ihrer Shell-Wrapper an PID 1 reparented,
  aber von uvicorn nicht reaped, sodass sich Hunderte Zombies ansammeln
  konnten. Das Image installiert jetzt `tini` und nutzt es als
  `ENTRYPOINT`, der Kindprozesse korrekt reaped. Zusätzlich aktiviert
  `docker-compose.yml` `init: true` als Defense-in-Depth.

## [0.9.20] - 2026-04-19

### Removed
- **Synology DSM notifications** – Removed completely. Even when invoked with
  the correct DSM 7.3 syntax, `synodsmnotify` consistently segfaulted inside
  the container because the bundled Synology C++ libraries require a DSM
  runtime environment that cannot be reproduced in a generic Docker image.
  Pushover remains the only supported notification backend. The `DSM_NOTIFY`
  environment variable, the `dsm_notify` config key, the DSM section in the
  *Einstellungen* UI, and the `synodsmnotify` / `/usr/lib` volume mounts in
  the Synology docker-compose example have all been removed. Any stored
  `dsm_notify` values in `/config/config.yaml` are ignored.

## [0.9.18] - 2026-04-19

### Fixed
- **DSM notifications call format updated for DSM 7.x** – `synodsmnotify` in
  DSM 7.3 requires the positional `title` argument to be a registered
  mail-string key and the `msg` argument to be a JSON object mapping
  placeholders to values. The service was updated to use
  `DSMSupportFormCustomMessage` with a `{"CUSTOM_MSG":"…"}` payload, fixing
  the previous `title: '…' is neither mail string key nor i18n format.`
  error. (Superseded by the full removal of DSM support in 0.9.19 after the
  binary still segfaulted inside the container despite the correct syntax.)

## [0.9.17] - 2026-04-19

### Changed
- **Cached iCloud storage usage** – The iCloud usage breakdown (photos, drive, backups, mail, …) is now refreshed automatically after every backup and persisted to `/config/.icloud-storage-cache-<account>.json`. The dashboard renders the breakdown from the cache instead of calling Apple on every page load. A manual refresh is available via `GET /api/accounts/<id>/icloud-storage?refresh=true`.
- **Notifications are configured in the web UI** – DSM and Pushover settings are no longer set via `DSM_NOTIFY` / `PUSHOVER_*` in `docker-compose.yml`; they live under the new **Einstellungen** menu and are stored in the existing `/config/config.yaml`. The API never returns stored secrets, and submitting an empty value preserves the previously saved secret.

### Added
- **Test buttons for DSM and Pushover** – The settings page now offers a "Testbenachrichtigung senden" button per backend that triggers a one-off notification (bypassing the enabled toggle) and shows the result inline. Backed by `POST /api/settings/notifications/test` with `{"backend": "dsm"|"pushover"}`.
- Unit tests for the new storage cache (`tests/test_storage_cache.py`) and the notification settings API (`tests/test_notifications_settings.py`).

### Removed
- Environment variables `DSM_NOTIFY`, `PUSHOVER_ENABLED`, `PUSHOVER_API_TOKEN`, `PUSHOVER_USER_KEY`, and `PUSHOVER_DEVICES` are no longer effective. Any values left in an existing `docker-compose.yml` are ignored – please re-enter them once in the web UI.

## [0.9.16] - 2026-04-19

### Fixed
- **SMS 2FA shows phone numbers again** – The SMS tab now loads trusted phone numbers for modern Apple accounts (HSA2) more reliably and falls back to Apple's auth options when `pyicloud` metadata is empty.
- **SMS code verification fixed for HSA2** – Confirming an SMS-delivered 2FA code now goes through Apple's phone verification endpoint instead of the device-code path.

### Added
- Unit tests for phone-number lookup, SMS dispatch, and SMS code verification in the HSA2 flow.

## [0.9.14] 2026-04-04

- Bump pyicloud dependency to version 2.5.0

## [0.9.13] 2026-03-17

### Fixed
- **Reconnect-Button funktioniert jetzt** – Der „Erneut verbinden"-Button auf dem Dashboard hat bei abgelaufener Session nichts getan, weil `authenticate()` ohne Passwort aufgerufen wurde. Der Button zeigt jetzt einen Lade-Spinner und öffnet bei Bedarf ein Passwort-Eingabefeld.
- Bessere Fehlermeldungen bei abgelaufenen Sessions (statt kryptischem „No password set")

### Added
- **Neu autorisieren auf der Detailseite** – Neuer „Neu autorisieren"-Button mit Passwort-Eingabe und 2FA-Dialog direkt auf der Account-Konfigurationsseite
- „Verbindung prüfen" blendet bei abgelaufenem Token automatisch das Reauth-Formular ein

## [0.9.12] 2026-03-16

### Added
- **Pushover-Benachrichtigungen** – Push-Notifications bei fehlerhaften Backups, ablaufenden und abgelaufenen Tokens über [Pushover](https://pushover.net). Konfiguration über `PUSHOVER_ENABLED`, `PUSHOVER_API_TOKEN`, `PUSHOVER_USER_KEY` und optional `PUSHOVER_DEVICES` (kommaseparierte Gerätenamen). Funktioniert parallel zu den bestehenden Synology DSM-Benachrichtigungen.

## [0.9.11] 2026-03-02

### Added
- **iCloud Contacts Backup** – Back up all iCloud contacts as individual VCF files (vCard 3.0), a combined `all_contacts.vcf`, and raw JSON. Includes SHA-256 hash caching for change detection.
- **iCloud Calendar Backup** – Back up all iCloud calendars as standard `.ics` files (one per calendar) using `pyicloud` for data retrieval and the `icalendar` library for standards-compliant ICS generation. 7-year date range (5 years back, 2 years forward).
- **Contacts sync policy** – Configurable handling of deleted contacts: keep locally, delete, or archive to `/archive/` (default: archive). Matches the existing Drive/Photos sync policy pattern.
- New UI configuration cards for Contacts and Calendar backup toggles
- Backup stats now include contacts and calendar results (written, skipped, archived, deleted, errors)

## [0.9.10] 2026-03-02

### Fixed
- Scheduled backup job was occasionally skipped if the scheduler missed the trigger time by a few seconds (APScheduler `misfire_grace_time` increased from 1s to 3600s)

## [0.9.9] 2026-02-24

- Bump pyicloud dependency to version 2.4.1

## [0.9.8] 2026-02-25

- Bump pyicloud dependency to version 2.4.1

## [0.9.6] 2026-02-23

- Check token expiry before manual backups (not just scheduled ones)
- Send DSM notification when token has expired during backup (auto-detect via session check)
- Fix ghost re-downloads of .sparsebundle and other macOS package files (package_token size mismatch)

## [0.9.5] 2026-02-23

- Add debug logging for repeated file downloads to diagnose ghost files and persistent metadata mismatches

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
