# AGENTS.md – Project Guide for AI Agents

## Project Overview

iCloud Backup Docker is a FastAPI-based web application that backs up iCloud Drive and iCloud Photos to local storage. It runs as a Docker container with a Bootstrap 5 / Alpine.js frontend.

## Tech Stack

- **Backend:** Python 3.12, FastAPI, Uvicorn
- **Frontend:** Jinja2 templates, Bootstrap 5, Alpine.js
- **iCloud API:** pyicloud
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
├── schemas.py           # Pydantic request/response schemas
├── routers/
│   ├── accounts.py      # /api/accounts – account CRUD, 2FA endpoints
│   └── backup.py        # /api/backup – config, trigger, progress
├── services/
│   ├── icloud_service.py    # pyicloud wrapper (auth, 2FA, 2SA, Drive)
│   ├── backup_service.py    # Core backup logic (Drive + Photos)
│   ├── scheduler.py         # APScheduler cron job management
│   ├── notification.py      # Synology DSM notifications (synodsmnotify)
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

- **2FA (HSA2):** Modern Apple accounts. Phone numbers come from `api._auth_data["trustedPhoneNumbers"]`. SMS is requested via `PUT /verify/phone`. Code is validated via `api.validate_2fa_code(code)`.
- **2SA (HSA1):** Legacy two-step. Devices come from `api.trusted_devices` (`/listDevices` endpoint). Code is sent via `api.send_verification_code(device)` and validated via `api.validate_verification_code(device, code)`.

The `icloud_service.py` handles both flows transparently.

### Photo Download

`photo.download()` returns **`bytes`** (not a Response/stream). Write directly with `fh.write(data)`. Do **not** use `copyfileobj(download.raw, fh)` – that only works for Drive downloads (`node.open(stream=True)` returns a Response).

### Backup Service

- **Drive backup:** Recursively walks iCloud Drive folders, downloads files via `node.open(stream=True)`, uses etag caching to skip unchanged folders.
- **Photos backup:** Iterates `api.photos.all`, organizes by date into `YYYY/MM/` directories, skips already-downloaded files by size comparison.
- Both support exclusion patterns (glob and path-based).

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
| `SECRET_KEY` | `change-me-in-production` | Session cookie signing |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `SYNOLOGY_NOTIFY` | `false` | Enable Synology DSM notifications |
| `TZ` | `Europe/Berlin` | Container timezone |

## Common Pitfalls

- **pyicloud `photo.download()`** returns `bytes`, not a streaming Response.
- **2FA vs 2SA:** Modern accounts use HSA2 (2FA). The old `trusted_devices` / `listDevices` API returns nothing for HSA2 accounts – use `_auth_data["trustedPhoneNumbers"]` instead.
- **Exclusion paths:** Must work for both top-level and subfolder paths (e.g. `Documents/subfolder`).
- **Log level changes:** `LOG_LEVEL` env var is applied at startup via `config.py`. The log handler uses a ring buffer (`log_handler.py`) that captures all levels.
