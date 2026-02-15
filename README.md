# iCloud Backup Docker

A Docker-based backup service for **iCloud Drive** and **iCloud Photos** with a user-friendly web interface.

## Features

- **Multi-Account Support** – Manage and back up multiple iCloud accounts
- **iCloud Drive Backup** – Simple mode (folder selection via checkboxes) or advanced mode (manual path configuration)
- **iCloud Photos Backup** – Including optional family library
- **Exclusions** – Flexible exclusion patterns (glob patterns, paths)
- **Scheduled Backups** – Configurable via cron expressions
- **2FA Support** – Two-factor authentication directly through the web UI (device push & SMS)
- **Synology Notifications** – Optional notifications via `synodsmnotify` on Synology NAS
- **Password Protection** – Web UI secured with password authentication
- **Dark Mode UI** – Modern, responsive web interface
- **Etag Caching** – Only changed folders are re-scanned
- **Live Progress** – Real-time progress display during running backups
- **Log Viewer** – Built-in log viewer in the web interface

## Quick Start

```bash
# Clone the repository
git clone https://github.com/michis0806/iCloud-Backup-Docker.git
cd iCloud-Backup-Docker

# Start the container
docker compose up -d
```

The web interface is available at: **http://localhost:8080**

On first startup, a random password is logged to the console:
```
docker logs icloud-backup
# Look for: "Kein AUTH_PASSWORD gesetzt. Generiertes Passwort: <your-password>"
```

To set a fixed password, add `AUTH_PASSWORD` to your environment (see [Configuration](#configuration)).

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTH_PASSWORD` | *(random, logged)* | Password for the web UI. If not set, a random password is generated and printed to the log on startup. |
| `SECRET_KEY` | `change-me-in-production` | Secret key for session cookie signing. Change this in production! |
| `WEB_PORT` | `8080` | Web UI port |
| `BACKUP_PATH` | `./backups` | Host path for backup files |
| `CONFIG_PATH` | `./config` | Host path for configuration & sessions |
| `LOG_LEVEL` | `INFO` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `SYNOLOGY_NOTIFY` | `false` | Enable Synology DSM notifications via `synodsmnotify` (`true`/`false`) |
| `TZ` | `Europe/Berlin` | Timezone |

### Volumes

| Container Path | Description |
|----------------|-------------|
| `/backups` | Backup destination directory |
| `/config` | Configuration, session tokens, etag caches |

### docker-compose.yml

```yaml
version: "3.8"
services:
  icloud-backup:
    build: .
    # Or use the pre-built image:
    # image: michis0806/icloud-backup-docker:latest
    container_name: icloud-backup
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - ./backups:/backups
      - ./config:/config
    environment:
      - TZ=Europe/Berlin
      - AUTH_PASSWORD=my-secure-password
      - SECRET_KEY=change-me-in-production
      # - LOG_LEVEL=INFO
      # - SYNOLOGY_NOTIFY=true  # Enable on Synology NAS
```

## Usage

### 1. Add an Account
- Open the web interface and log in
- Click "Account hinzufügen" (Add Account)
- Enter your Apple ID and password (app-specific password recommended)
- Confirm the 2FA code from your Apple device, or request an SMS code to a trusted phone number

### 2. Configure Backup
- Click "Konfigurieren" (Configure) on the desired account
- Select: iCloud Drive and/or iCloud Photos
- **Drive (Simple):** Select folders via checkboxes
- **Drive (Advanced):** Enter paths manually (one path per line)
- **Photos:** Optionally include family library
- Define exclusions (e.g. `*.tmp`, `.DS_Store`, `node_modules`)

### 3. Run Backup
- Manual: Click "Backup jetzt starten" (Start backup now)
- Automatic: Enable schedule with a cron expression (default: daily at 2:00 AM)

## Installation on Synology NAS

### Option 1: Container Manager (DSM 7.2+)

1. Install **Container Manager** from the DSM Package Center (if not already installed)

2. **Create a project:**
   - Open Container Manager → **Project** → **Create**
   - Project name: `icloud-backup`
   - Path: choose a folder on the NAS (e.g. `/volume1/docker/icloud-backup`)
   - Use the `docker-compose.yml` from this repository

3. **Prepare volumes:**
   ```bash
   # Via SSH or File Station:
   mkdir -p /volume1/docker/icloud-backup/config
   mkdir -p /volume1/docker/icloud-backup/backups
   ```

4. **Adjust docker-compose.yml** (paths for Synology):
   ```yaml
   version: "3.8"
   services:
     icloud-backup:
       image: michis0806/icloud-backup-docker:latest
       container_name: icloud-backup
       restart: unless-stopped
       mem_limit: 512m
       cpus: 1.0
       ports:
         - "8080:8080"
       volumes:
         - /volume1/docker/icloud-backup/backups:/backups
         - /volume1/docker/icloud-backup/config:/config
       environment:
         - TZ=Europe/Berlin
         - AUTH_PASSWORD=my-secure-password
         - SECRET_KEY=my-secret-key
         - SYNOLOGY_NOTIFY=true
   ```

5. **Start the project** → Container Manager builds and starts the container

6. **Open the web interface** at `http://<NAS-IP>:8080`

### Option 2: Docker via SSH (DSM 7.x)

```bash
# Connect via SSH
ssh admin@<NAS-IP>

# Create directory and docker-compose.yml
mkdir -p /volume1/docker/icloud-backup && cd /volume1/docker/icloud-backup
# Create docker-compose.yml with the content above

# Start the container
sudo docker compose up -d
```

### Option 3: Portainer (if installed)

1. Open Portainer → **Stacks** → **Add stack**
2. Name: `icloud-backup`
3. Paste the `docker-compose.yml` contents
4. Adjust paths as needed
5. **Deploy the stack**

### Synology Tips

- **Ports:** If port 8080 is already in use, change it in `docker-compose.yml` e.g. `"8085:8080"`
- **Permissions:** The container runs as root. Backup directories must be writable.
- **Firewall:** You may need to allow the port in the DSM firewall (Control Panel → Security → Firewall)
- **Auto-start:** `restart: unless-stopped` ensures the container restarts automatically after a NAS reboot
- **Backup target:** Consider using a shared folder that is also backed up with Synology Hyper Backup for a double backup

## Notes

- **2FA tokens** expire after approximately 2 months and need to be renewed
- It is recommended to use an **app-specific password** ([create one here](https://support.apple.com/en-us/102654))
- The password is **not stored** – only pyicloud's session tokens in the `/config/sessions` directory
- **Etag cache files** are stored in `/config/` and significantly speed up repeated backups

## Tech Stack

- **Backend:** Python / FastAPI
- **Frontend:** Bootstrap 5 / Alpine.js
- **iCloud API:** [pyicloud](https://github.com/picklepete/pyicloud)
- **Configuration:** YAML (`/config/config.yaml`)
- **Scheduler:** APScheduler

## License

MIT
