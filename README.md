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
- **Datenbank:** SQLite
- **Scheduler:** APScheduler

## Hinweise

- **2FA-Tokens** laufen nach ca. 2 Monaten ab und müssen erneuert werden
- Es wird empfohlen, ein **app-spezifisches Passwort** zu verwenden
- Session-Tokens werden im `/config/sessions`-Verzeichnis gespeichert

## Lizenz

MIT
