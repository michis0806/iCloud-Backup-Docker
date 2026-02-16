# iCloud Backup Docker

Back up your **iCloud Drive** and **iCloud Photos** automatically with a simple web interface.

## Features

- Multi-account support with 2FA (device push & SMS)
- iCloud Drive backup (folder selection or manual paths)
- iCloud Photos backup (including family library)
- Configurable sync policy per backup type: **keep**, **delete**, or **archive** removed files
- Scheduled backups via cron expressions
- Password-protected web UI
- Exclusion patterns (glob, paths)
- Etag caching for fast incremental backups
- Live progress, backup duration & built-in log viewer
- Synology DSM notifications via `synodsmnotify`
- Multi-arch: `linux/amd64` and `linux/arm64`

## Quick Start

```yaml
# docker-compose.yml
version: "3.8"
services:
  icloud-backup:
    image: michis0806/icloud-backup-docker:latest
    container_name: icloud-backup
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - ./backups:/backups
      - ./config:/config
      - ./archive:/archive    # for sync policy "archive"
    environment:
      - TZ=Europe/Berlin
      - AUTH_PASSWORD=my-secure-password
      - SECRET_KEY=change-me-in-production
```

```bash
docker compose up -d
```

Open **http://localhost:8080** and log in.

> If `AUTH_PASSWORD` is not set, a random password is generated and printed to the container log on startup.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTH_PASSWORD` | *(random)* | Web UI password. Random if not set (check logs). |
| `SECRET_KEY` | `change-me-in-production` | Secret for cookie signing. |
| `LOG_LEVEL` | `INFO` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `SYNOLOGY_NOTIFY` | `false` | Enable Synology DSM notifications (`true`/`false`). |
| `ARCHIVE_PATH` | `./archive` | Host path for archived files (sync policy "archive"). |
| `TZ` | `Europe/Berlin` | Container timezone. |

## Volumes

| Path | Description |
|------|-------------|
| `/backups` | Backup destination |
| `/config` | Config file, session tokens, caches |
| `/archive` | Archived files when sync policy is set to "archive" |

## How It Works

1. **Add account** – Enter your Apple ID and app-specific password, confirm 2FA (device push or SMS)
2. **Configure** – Select folders/photos, set exclusions, choose schedule and sync policy
3. **Back up** – Run manually or let the scheduler handle it

### Sync Policy

For each backup type (Drive / Photos) you can choose independently what happens when files are deleted in iCloud:

- **Keep** – local files remain untouched (default for Photos)
- **Delete** – local files are removed (default for Drive)
- **Archive** – files are moved to `/archive`, preserving the folder structure

Session tokens are cached so you don't need to re-authenticate on every run. 2FA tokens typically last ~2 months.

## Synology NAS

Works great on Synology NAS via Container Manager, SSH, or Portainer. See the [full documentation](https://github.com/michis0806/iCloud-Backup-Docker) for detailed Synology setup instructions.

## Links

- [GitHub Repository](https://github.com/michis0806/iCloud-Backup-Docker)
- [Create an App-Specific Password](https://support.apple.com/en-us/102654)
