# iCloud Backup Docker

Ein Docker-basierter Backup-Service für **iCloud Drive** und **iCloud Fotos** mit einer benutzerfreundlichen Weboberfläche.

## Features

- **Multi-Account-Support** – Verwalten und sichern Sie mehrere iCloud-Accounts
- **iCloud Drive Backup** – Einfacher Modus (Ordner-Auswahl per Checkbox) oder erweiterter Modus (manuelle Pfad-Konfiguration)
- **iCloud Fotos Backup** – Inklusive optionaler Familienbibliothek
- **Exclusions** – Flexible Ausschlussmuster (Glob-Patterns, Pfade)
- **Automatische Backups** – Zeitplan per Cron-Ausdruck konfigurierbar
- **2FA-Support** – Zwei-Faktor-Authentifizierung direkt über die Weboberfläche
- **Dark Mode UI** – Moderne, responsive Weboberfläche
- **Etag-Caching** – Nur geänderte Ordner werden erneut gescannt
- **Live-Fortschritt** – Echtzeit-Fortschrittsanzeige während laufender Backups
- **Log-Viewer** – Integrierter Log-Viewer in der Weboberfläche

## Schnellstart

```bash
# Repository klonen
git clone https://github.com/michis0806/iCloud-Backup-Docker.git
cd iCloud-Backup-Docker

# .env erstellen
cp .env.example .env

# Container starten
docker compose up -d
```

Die Weboberfläche ist dann erreichbar unter: **http://localhost:8080**

## Konfiguration

### docker-compose.yml

| Variable | Standard | Beschreibung |
|----------|----------|--------------|
| `WEB_PORT` | `8080` | Port der Weboberfläche |
| `BACKUP_PATH` | `./backups` | Pfad für Backup-Dateien |
| `CONFIG_PATH` | `./config` | Pfad für Konfiguration & Sessions |
| `TZ` | `Europe/Berlin` | Zeitzone |
| `SECRET_KEY` | `change-me-in-production` | Geheimer Schlüssel |

### Volumes

| Container-Pfad | Beschreibung |
|----------------|--------------|
| `/backups` | Backup-Zielverzeichnis |
| `/config` | Konfiguration, Datenbank, Session-Tokens |

## Nutzung

### 1. Account hinzufügen
- Öffnen Sie die Weboberfläche
- Klicken Sie auf "Account hinzufügen"
- Geben Sie Ihre Apple-ID und Passwort ein (app-spezifisches Passwort empfohlen)
- Bestätigen Sie den 2FA-Code von Ihrem Apple-Gerät

### 2. Backup konfigurieren
- Klicken Sie auf "Konfigurieren" beim gewünschten Account
- Wählen Sie aus: iCloud Drive und/oder iCloud Fotos
- **Drive (Einfach):** Ordner per Checkbox auswählen
- **Drive (Erweitert):** Pfade manuell eingeben (ein Pfad pro Zeile)
- **Fotos:** Optional Familienbibliothek mitsichern
- Ausschlüsse definieren (z.B. `*.tmp`, `.DS_Store`, `node_modules`)

### 3. Backup starten
- Manuell: "Backup jetzt starten"
- Automatisch: Zeitplan aktivieren (Cron-Ausdruck, Standard: täglich 2:00 Uhr)

## Technologie

- **Backend:** Python / FastAPI
- **Frontend:** Bootstrap 5 / Alpine.js
- **iCloud API:** [pyicloud](https://github.com/picklepete/pyicloud)
- **Konfiguration:** YAML (`/config/config.yaml`)
- **Scheduler:** APScheduler

## Installation auf Synology NAS

### Variante 1: Container Manager (DSM 7.2+)

1. **Container Manager** im DSM-Paketmanager installieren (falls nicht vorhanden)

2. **Projekt erstellen:**
   - Container Manager öffnen → **Projekt** → **Erstellen**
   - Projektname: `icloud-backup`
   - Pfad: einen Ordner auf dem NAS wählen (z.B. `/volume1/docker/icloud-backup`)
   - Die `docker-compose.yml` aus diesem Repository als Quelle verwenden

3. **Volumes vorbereiten:**
   ```bash
   # Per SSH oder File Station erstellen:
   mkdir -p /volume1/docker/icloud-backup/config
   mkdir -p /volume1/docker/icloud-backup/backups
   ```

4. **docker-compose.yml anpassen** (Pfade für Synology):
   ```yaml
   version: "3.8"
   services:
     icloud-backup:
       build: .
       container_name: icloud-backup
       restart: unless-stopped
       ports:
         - "8080:8080"
       volumes:
         - /volume1/docker/icloud-backup/backups:/backups
         - /volume1/docker/icloud-backup/config:/config
       environment:
         - TZ=Europe/Berlin
   ```

5. **Projekt starten** → Container Manager baut und startet den Container

6. **Weboberfläche** aufrufen: `http://<NAS-IP>:8080`

### Variante 2: Docker per SSH (DSM 7.x)

```bash
# Per SSH auf die Synology verbinden
ssh admin@<NAS-IP>

# Repository klonen
cd /volume1/docker
git clone https://github.com/michis0806/iCloud-Backup-Docker.git
cd iCloud-Backup-Docker

# Container bauen und starten
sudo docker compose up -d
```

### Variante 3: Portainer (falls installiert)

1. Portainer öffnen → **Stacks** → **Add stack**
2. Name: `icloud-backup`
3. Den Inhalt der `docker-compose.yml` einfügen
4. Pfade anpassen (s.o.)
5. **Deploy the stack**

### Synology-Tipps

- **Ports:** Falls Port 8080 belegt ist, in der `docker-compose.yml` z.B. `"8085:8080"` setzen
- **Berechtigungen:** Der Container läuft als Root. Die Backup-Verzeichnisse müssen beschreibbar sein
- **Firewall:** Ggf. den Port in der DSM-Firewall freigeben (Systemsteuerung → Sicherheit → Firewall)
- **Autostart:** `restart: unless-stopped` sorgt dafür, dass der Container nach einem NAS-Neustart automatisch wieder startet
- **Backup-Ziel:** Am besten einen freigegebenen Ordner verwenden, der im Synology Hyper Backup mit gesichert wird – so haben Sie ein doppeltes Backup

## Hinweise

- **2FA-Tokens** laufen nach ca. 2 Monaten ab und müssen erneuert werden
- Es wird empfohlen, ein **app-spezifisches Passwort** zu verwenden
- Das Passwort wird **nicht gespeichert** – nur die Session-Tokens von pyicloud im `/config/sessions`-Verzeichnis
- Die **Etag-Cache-Dateien** liegen in `/config/` und beschleunigen wiederholte Backups erheblich

## Entwicklung

```bash
# Dev-Dependencies installieren
pip install -r requirements-dev.txt

# Tests ausführen
pytest
```

## Lizenz

MIT
