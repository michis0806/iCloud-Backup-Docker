# AGENTS.md – Project Guide for AI Agents

## Project Overview

iCloud Backup Docker is a FastAPI-based web application that backs up iCloud Drive, iCloud Photos, iCloud Contacts and iCloud Calendars to local storage. It runs as a Docker container with a Bootstrap 5 / Alpine.js frontend.

## Tech Stack

- **Backend:** Python 3.12, FastAPI, Uvicorn
- **Frontend:** Jinja2 templates, Bootstrap 5, Alpine.js
- **iCloud API:** pyicloud
- **Calendar ICS:** icalendar (>=6.0.0)
- **Scheduler:** APScheduler (AsyncIOScheduler)
- **Config:** Pydantic Settings + YAML persistence
- **Tests:** pytest + pytest-asyncio + httpx

## Project Structure

```
app/
├── main.py              # FastAPI app, lifespan (scheduler start, dir setup)
├── config.py            # Pydantic Settings (env vars → settings object)
├── config_store.py      # YAML-based persistent config (/config/config.yaml)
├── auth.py              # Session/cookie authentication middleware
├── models.py            # Enums (AccountStatus, BackupStatus, DriveConfigMode, SyncPolicy)
├── schemas.py           # Pydantic request/response schemas
├── routers/
│   ├── accounts.py      # /api/accounts – account CRUD, 2FA endpoints
│   └── backup.py        # /api/backup – config, trigger, progress
├── services/
│   ├── icloud_service.py    # pyicloud wrapper (auth, 2FA, 2SA, Drive, Contacts, Calendar)
│   ├── backup_service.py    # Core backup logic (Drive, Photos, Contacts, Calendar)
│   ├── scheduler.py         # APScheduler cron job management
│   ├── notification.py      # Pushover push notifications
│   └── log_handler.py       # Ring buffer for live log viewer
├── static/              # CSS, JS
└── templates/           # Jinja2 (login, dashboard, config, logs)

tests/
├── test_api.py          # API endpoint tests
├── test_etag_cache.py   # Etag cache tests
├── test_exclusions.py   # Glob/path exclusion tests
├── test_log_handler.py  # Log ring buffer tests
└── test_progress.py     # Progress tracking tests
```

## Key Concepts

### Authentication Flow (2FA / 2SA)

pyicloud distinguishes two auth modes:

- **2FA (HSA2):** Modern Apple accounts use `services/apple_auth.py` and the
  pinned pyicloud 2026.10.0 (calendar versioning). Login uses `pause_2fa=True`;
  automatic library delivery is disabled. `request_challenge("push" | "sms", phone_id)`
  requests only the selected channel, without automatic SMS fallback. Phone
  metadata may also be nested under `phoneNumberVerification`.
  `validate_selected_code(code)` retains the selected verifier and bridge
  context, completes trust once, and checks existing tokens if Apple's result
  is ambiguous. Do not add another `trust_session()` or start a fresh login
  during code validation.
- **Internal pyicloud hooks the adapter uses:** `request_2fa_code()` (overridden
  to return False), `_augment_webauthn_options()` (overridden as a no-op: since
  2026.10.0 pyicloud would send a second, JSON GET to the auth endpoint for every
  account without security keys), `_get_mfa_auth_options()`, `_auth_data`,
  `_auth_endpoint`, `_get_auth_headers()`, `_supports_trusted_device_bridge()`,
  `_trusted_device_bridge.start/validate_code`, `_trusted_device_bridge_state`
  (`uses_legacy_trusted_device_verifier`), `_current_hsa2_boot_context()`,
  `_clear_trusted_device_bridge_state()`, `_set_two_factor_delivery_state()`,
  `_two_factor_code_requested`, `_validate_token()`, `_requires_mfa`,
  `_update_state()`, `trust_session()`. `tests/test_apple_auth.py` asserts that
  they exist; re-check them on every pyicloud bump.
- **HTTP 409 with `securityCode.valid`:** pyicloud 2026.10.0 accepts such
  answers inside its own `validate_2fa_code()`, but only checks `valid`. The
  adapter does not call that method (it would use pyicloud's own phone choice
  and `pushMode`) and keeps its stricter `_code_accepted_in_conflict()`.
- **Error types (2026.10.0):** `PyiCloudConnectionException` (subclass of
  `PyiCloudAPIResponseException`, `code=None`) means iCloud was not reached; it
  is not a failed login and must not trigger a password prompt.
  `PyiCloudAccountLockedException` (subclass of `PyiCloudFailedLoginException`,
  service error `-20209`) means the Apple Account is locked; `icloud_service`
  reports `icloud.account_locked` and never retries with the stored password.
- **2SA (HSA1):** Legacy two-step. Devices come from `api.trusted_devices` (`/listDevices` endpoint). Code is sent via `api.send_verification_code(device)` and validated via `api.validate_verification_code(device, code)`.

The `icloud_service.py` handles both flows transparently.

The explicit 2FA adapter was developed together with the Unified Inbox
repository (`backend/unifiedinbox/integrations/apple_auth.py`). Both tools
keep their own Apple sessions and tokens, so the copies may diverge (this one
uses `app.i18n` for its messages). Port fixes to the Apple flow itself
(challenge, validation, trust) by hand when they apply to both.

### Photo Download

`_download_photo()` bypasses `photo.download()` (which loads everything into RAM) and instead **streams** the file directly to disk:

1. Fetch the download URL from `photo.versions["original"]["url"]`
2. Use `photo._service.session.get(url, stream=True)` for an authenticated streaming GET
3. Write to disk via `response.iter_content(chunk_size=256 * 1024)`

Drive downloads use `node.open(stream=True)` which returns a `Response` and is read via `copyfileobj(response.raw, fh)`.

### Backup Service

- **Drive backup:** Recursively walks iCloud Drive folders, downloads files via `_open_drive_node()` (wraps `node.open(stream=True)` with fallback logic for special characters), uses etag caching to skip unchanged folders.
- **Photos backup:** Iterates `api.photos.all`, organizes by date into `YYYY/MM/DD/` directories. Change detection uses a **multi-level approach**: (1) fingerprint cache (`resOriginalFingerprint` / `recordChangeTag`) as primary check, (2) file size comparison as fallback. The fingerprint cache is persisted per library in `/config/.icloud-photo-cache-*.json`.
- **Contacts backup:** Fetches all contacts via `api.contacts.all`, converts each to vCard 3.0 format (manual conversion in `_contact_to_vcard()`). Creates individual VCF files, a combined `all_contacts.vcf`, and a raw `contacts.json`. SHA-256 hash per `contactId` for change detection (cache: `/config/.icloud-contacts-cache-*.json`). Deleted contacts are handled via configurable sync policy (default: archive).
- **Calendar backup:** Fetches calendars via `api.calendar.get_calendars()` and events via `api.calendar.get_events()` (7-year window: 5 years back, 2 years forward). Apple's proprietary 7-element date arrays are converted to Python datetimes via `_apple_date_to_datetime()`. Events are converted to `icalendar.Event` objects via `_event_to_ical()` (handles all-day events, timezones, alarms, attendees). One `.ics` file per calendar. SHA-256 hash per calendar GUID for change detection (cache: `/config/.icloud-calendar-cache-*.json`).
- Drive and Photos support exclusion patterns (glob and path-based).

### Special Characters in Folder/File Names

Files in folders whose name contains URL-special characters (`#`, `%`, `?`, `&`, `+`) may fail to download because the iCloud document service returns 404 for their `docwsid`. The `_open_drive_node()` wrapper in `backup_service.py` handles this with two fallback strategies:

1. Re-fetch node metadata via `get_node_data(drivewsid)` to obtain a fresh `docwsid`
2. Try using the `drivewsid` as `document_id` for the download

If all fallbacks fail, a warning is logged advising the user to rename the folder.

### Backup Status & Timing

`config_store.update_backup_status()` tracks backup lifecycle:

| Field | Set when | Description |
|-------|----------|-------------|
| `last_backup_status` | start / end | `idle`, `running`, `success`, `error` |
| `last_backup_started_at` | start | UTC ISO timestamp when backup began |
| `last_backup_at` | end | UTC ISO timestamp when backup **finished** (not started!) |
| `last_backup_duration_seconds` | end | Wall-clock duration in seconds |
| `last_backup_message` | end | Human-readable result / error message |
| `last_backup_stats` | end | Dict with `drive`, `photos`, `contacts`, `calendar`, `storage` sub-dicts |

All timestamps use `datetime.now(timezone.utc).isoformat()` which produces an explicit `+00:00` suffix. This ensures JavaScript `new Date(iso)` correctly interprets them as UTC, and `toLocaleString('de-DE')` renders them in the browser's local timezone.

### Sync Policy (SyncPolicy enum)

Each backup type (Drive / Photos / Contacts) has a configurable sync policy that determines what happens to local files when they are deleted in iCloud:

| Policy | Drive Default | Photos Default | Contacts Default | Behaviour |
|--------|:---:|:---:|:---:|---|
| `keep` | | **X** | | Local files remain untouched |
| `delete` | **X** | | | Local files are removed |
| `archive` | | | **X** | Files are moved to `/archive/{destination}/…`, preserving the folder structure |

The shared helper `_apply_sync_policy()` in `backup_service.py` implements all three policies and is used by Drive, Photos, and Contacts reconciliation.

- **Drive:** After downloading, `sync_drive_folder()` compares the `remote_files` set against local files and applies the policy.
- **Photos:** `run_photos_backup()` collects remote filenames during iteration, then `_reconcile_photos()` applies the policy per library/album.
- **Contacts:** `run_contacts_backup()` tracks written filenames and compares against existing VCF files after processing all contacts. Orphaned files are handled via `_apply_sync_policy()`.
- **Archive mount:** `/archive` is a dedicated Docker volume (`ARCHIVE_PATH` env var). Files are moved via `shutil.move()` with the relative path preserved.

### Configuration

- Environment variables are read by `app/config.py` (Pydantic `BaseSettings`).
- Account configs and backup settings are persisted in `/config/config.yaml` via `config_store.py`.
- Session tokens are stored in `/config/sessions/<apple_id>/`.

## Development

```bash
# Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Run tests
pytest

# Start dev server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTH_PASSWORD` | *(random)* | Web UI password |
| `SECRET_KEY` | *(uses AUTH_PASSWORD)* | Session cookie HMAC signing; falls back to `AUTH_PASSWORD` if not set |
| `ICLOUD_SECRET_KEY` | *(uses SECRET_KEY / AUTH_PASSWORD)* | Fernet key source for opt-in stored iCloud passwords (`app/crypto.py`); without any explicitly set key the feature is disabled |
| `BACKUP_PATH` | `./backups` | Host path for backup files |
| `CONFIG_PATH` | `./config` | Host path for configuration & sessions |
| `ARCHIVE_PATH` | `./archive` | Host path for archived files (sync policy = "archive") |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `UI_LANGUAGE` | `de` | UI language: `de`, `en`, `fr`, `it` or `es` |
| `TZ` | `Europe/Berlin` | Container timezone |

> Pushover notification settings are stored in `/config/config.yaml` and
> configured via the web UI (Einstellungen), not via environment variables.

## UI Language (i18n)

- All user-facing strings (templates, Alpine/JS code, API messages, status and log text) live in `app/locales/<lang>.json` (`de`, `en`, `fr`, `it`, `es`). German is the source and the fallback for missing keys. Never hardcode UI text in code.
- Python: `from app.i18n import t`, then `t("key", name=value)` with `{name}` placeholders. Templates have `t()` as a Jinja global. Call `t()` at call time, not in module-level constants, so the active language applies.
- Frontend: keys starting with `js.` are sent to the browser as `window.I18N`; use the global `t('js.key', {name: value})` in inline scripts and Alpine expressions (no Jinja inside `<script>` blocks). Dates use `t('js.date_locale')`.
- Add every new key to **all** catalogs, in the same order, with identical `{placeholders}` and `%s`/`%d` log specifiers; `tests/test_i18n.py` enforces this. Never put a literal `%` into a log format string.
- A new language needs its catalog plus an entry in the `ui_language` `Literal` in `app/config.py`; a test checks that both match.
- Tests run in German: `tests/conftest.py` pins `settings.ui_language = "de"`. Tests for English set it themselves via `monkeypatch`.
- Message text stored in `/config/config.yaml` (e.g. `status_message`) keeps the language it was written in.

## Changelog & Release Process

- **CHANGELOG.md** follows the [Keep a Changelog](https://keepachangelog.com/) format.
- New features and fixes go under `## [Unreleased]` until a version tag is created.
- When tagging a release (`git tag v1.0.0`), rename `[Unreleased]` to `[1.0.0] - YYYY-MM-DD` and add a new empty `[Unreleased]` section above.
- The CI workflow (`docker-publish.yml`) automatically combines `DOCKERHUB.md` + the two most recent changelog sections into the Docker Hub description. The full changelog links back to GitHub.
- Docker Hub has a 25,000 character limit for the full description.

## Common Pitfalls

- **pyicloud `photo.download()`** loads the entire file into RAM. Use `_download_photo()` instead, which streams via `session.get(url, stream=True)` + `iter_content()`.
- **pyicloud exception hierarchy:** Since 2026.10.0 the service errors
  (`CloudKitApiError`, `NotesError`, `RemindersApiError`, their `*AuthError`
  siblings, …) derive from `PyiCloudException`, but not from
  `PyiCloudAPIResponseException` or the login/2FA exceptions. `_execute_step()`
  re-raises only `_AUTH_EXCEPTIONS` (failed login incl. account locked, 2FA,
  2SA); everything else, including `PyiCloudConnectionException`, is deferred
  and retried once. `tests/test_deferred_retry.py` pins these assumptions.
- **`rich` is not a dependency:** pyicloud 2026.10.0 imports it lazily (only
  for debug output of the Notes HTML exporter, which this app does not use).
- **Shared Drive folders:** pyicloud 2026.10.0 `node.open()` adds the owner to
  the download path (`/download/by_id/<ownerRecordName>`) when the node has an
  `owner` dict. `_open_drive_node()` keeps its own fallbacks for the 404 case:
  the same owner path with the owner from `shareID`, then the owner-qualified
  zone, `shareID` query parameters and alternative document ids.
- **2FA vs 2SA:** Modern accounts use HSA2 (2FA). The old `trusted_devices` / `listDevices` API returns nothing for HSA2 accounts – use `_auth_data["trustedPhoneNumbers"]` instead.
- **Exclusion paths:** Must work for both top-level and subfolder paths (e.g. `Documents/subfolder`).
- **Log level changes:** `LOG_LEVEL` env var is applied at startup via `config.py`. The log handler uses a ring buffer (`log_handler.py`) that captures all levels.
- **Special characters in folder names:** `#`, `%`, `?`, `&`, `+` in iCloud Drive folder or file names can cause 404 errors during download. `_open_drive_node()` provides fallback strategies, but renaming the folder is the safest fix.
- **Health endpoint:** `/health` checks that `/backups`, `/config` and `/archive` are readable (in a worker thread with a 3 s timeout, reusing a still-pending check) and returns HTTP 503 otherwise. Keep it cheap: read at most one directory entry per path, never walk the tree.
- **Notifications:** Only Pushover is supported. Configuration lives in `/config/config.yaml` (managed via the web UI under *Einstellungen*), not in environment variables.
