"""Core backup logic for iCloud Drive and iCloud Photos."""

import json
import logging
import os
import time
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from shutil import copyfileobj

from app.config import settings
from app.services import icloud_service

log = logging.getLogger("icloud-backup")


# ---------------------------------------------------------------------------
# Exclusion helpers
# ---------------------------------------------------------------------------

def _is_glob(pattern: str) -> bool:
    return any(c in pattern for c in ("*", "?", "["))


def is_excluded(rel_path: str, excludes: list[str]) -> bool:
    """Check whether *rel_path* matches any exclusion pattern.

    Supported patterns (same semantics as the original backup.py):
      - Glob patterns on individual path components: ``*.tmp``, ``.git``
      - Relative paths: ``Projects``  (matches any component)
      - Absolute paths from drive root: ``Documents/Projects``
    """
    if not excludes:
        return False

    parts = rel_path.split("/")
    for pattern in excludes:
        if _is_glob(pattern):
            # Match each component individually
            if any(fnmatch(p, pattern) for p in parts):
                return True
        elif "/" in pattern:
            # Absolute-style path match
            if rel_path.startswith(pattern) or rel_path == pattern:
                return True
        else:
            # Simple name match against any component
            if pattern in parts:
                return True
    return False


# ---------------------------------------------------------------------------
# iCloud Drive backup
# ---------------------------------------------------------------------------

def _walk_remote(node, prefix: str = "", excludes: list[str] | None = None):
    """Recursively yield ``(relative_path, node)`` for all files under *node*."""
    excludes = excludes or []
    try:
        children = node.dir()
    except Exception:
        children = []

    for name in children:
        child = node[name]
        rel = f"{prefix}/{name}" if prefix else name

        if is_excluded(rel, excludes):
            log.debug("Excluded: %s", rel)
            continue

        if child.type == "folder":
            yield from _walk_remote(child, rel, excludes)
        else:
            yield rel, child


def _file_needs_update(node, local_path: Path) -> bool:
    """Return True when the local file is missing or outdated."""
    if not local_path.exists():
        return True
    try:
        remote_size = node.size or 0
        local_size = local_path.stat().st_size
        if remote_size != local_size:
            return True
        remote_mtime = node.date_modified.timestamp() if node.date_modified else 0
        local_mtime = local_path.stat().st_mtime
        if abs(remote_mtime - local_mtime) > 2:
            return True
    except Exception:
        return True
    return False


def sync_drive_folder(
    apple_id: str,
    folder_name: str,
    destination: Path,
    excludes: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Synchronise a single iCloud Drive folder to *destination*.

    Returns stats dict: ``{downloaded, deleted, skipped, errors}``.
    """
    stats = {"downloaded": 0, "deleted": 0, "skipped": 0, "errors": 0}

    api = icloud_service.get_session(apple_id)
    if api is None:
        log.error("Keine Sitzung für %s", apple_id)
        return {**stats, "errors": 1}

    try:
        folder_node = api.drive[folder_name]
    except (KeyError, Exception) as exc:
        log.error("Ordner '%s' nicht gefunden: %s", folder_name, exc)
        return {**stats, "errors": 1}

    dest = destination / folder_name
    dest.mkdir(parents=True, exist_ok=True)

    # Track remote files so we can detect local-only files for deletion
    remote_files: set[str] = set()

    for rel_path, node in _walk_remote(folder_node, excludes=excludes):
        remote_files.add(rel_path)
        local_path = dest / rel_path

        if not _file_needs_update(node, local_path):
            stats["skipped"] += 1
            continue

        if dry_run:
            log.info("[DRY RUN] Würde herunterladen: %s", rel_path)
            stats["downloaded"] += 1
            continue

        local_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = local_path.with_suffix(local_path.suffix + ".tmp")

        try:
            with open(tmp_path, "wb") as fh:
                response = node.open(stream=True)
                copyfileobj(response.raw, fh)
            tmp_path.rename(local_path)

            # Preserve modification time
            if node.date_modified:
                mtime = node.date_modified.timestamp()
                os.utime(local_path, (mtime, mtime))

            log.info("Heruntergeladen: %s", rel_path)
            stats["downloaded"] += 1
        except Exception as exc:
            log.error("Fehler beim Herunterladen von %s: %s", rel_path, exc)
            if tmp_path.exists():
                tmp_path.unlink()
            stats["errors"] += 1

    # Delete local files that no longer exist remotely
    if not dry_run:
        for local_file in dest.rglob("*"):
            if local_file.is_file() and not local_file.name.endswith(".tmp"):
                rel = str(local_file.relative_to(dest))
                if rel not in remote_files:
                    try:
                        local_file.unlink()
                        log.info("Gelöscht (remote entfernt): %s", rel)
                        stats["deleted"] += 1
                    except Exception as exc:
                        log.error("Fehler beim Löschen von %s: %s", rel, exc)
                        stats["errors"] += 1

        # Clean up empty directories
        for dirpath in sorted(dest.rglob("*"), reverse=True):
            if dirpath.is_dir() and not any(dirpath.iterdir()):
                dirpath.rmdir()

    return stats


def run_drive_backup(
    apple_id: str,
    folders: list[str],
    destination: str,
    excludes: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Run a full iCloud Drive backup for the given folders.

    Returns aggregated stats dict.
    """
    dest_path = settings.backup_path / destination / "drive"
    dest_path.mkdir(parents=True, exist_ok=True)

    total = {"downloaded": 0, "deleted": 0, "skipped": 0, "errors": 0}
    for folder in folders:
        log.info("Synchronisiere Drive-Ordner: %s → %s", folder, dest_path)
        stats = sync_drive_folder(apple_id, folder, dest_path, excludes, dry_run)
        for k in total:
            total[k] += stats[k]

    return total


# ---------------------------------------------------------------------------
# iCloud Photos backup
# ---------------------------------------------------------------------------

def run_photos_backup(
    apple_id: str,
    destination: str,
    include_family: bool = False,
    excludes: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Download iCloud Photos (and optionally family library) to *destination*.

    Returns stats dict.
    """
    stats = {"downloaded": 0, "skipped": 0, "errors": 0}

    api = icloud_service.get_session(apple_id)
    if api is None:
        log.error("Keine Sitzung für %s", apple_id)
        return {**stats, "errors": 1}

    dest_path = settings.backup_path / destination / "photos"
    dest_path.mkdir(parents=True, exist_ok=True)

    albums_to_backup = ["All Photos"]
    if include_family:
        # Attempt to include shared/family albums
        try:
            for album in api.photos.albums:
                if "family" in album.lower() or "shared" in album.lower():
                    albums_to_backup.append(album)
        except Exception as exc:
            log.warning("Konnte Familien-Alben nicht abrufen: %s", exc)

    for album_name in albums_to_backup:
        try:
            album = api.photos.albums.get(album_name)
            if album is None:
                log.warning("Album '%s' nicht gefunden, überspringe.", album_name)
                continue

            log.info("Sichere Album: %s", album_name)
            album_dest = dest_path / _safe_dirname(album_name)
            album_dest.mkdir(parents=True, exist_ok=True)

            for photo in album:
                try:
                    filename = photo.filename
                    if not filename:
                        continue

                    # Check exclusions
                    if excludes and is_excluded(filename, excludes):
                        continue

                    local_path = album_dest / filename
                    if local_path.exists():
                        # Skip if sizes match
                        if hasattr(photo, "size") and photo.size:
                            if local_path.stat().st_size == photo.size:
                                stats["skipped"] += 1
                                continue

                    if dry_run:
                        log.info("[DRY RUN] Würde herunterladen: %s", filename)
                        stats["downloaded"] += 1
                        continue

                    download = photo.download()
                    if download is None:
                        stats["errors"] += 1
                        continue

                    tmp_path = local_path.with_suffix(local_path.suffix + ".tmp")
                    with open(tmp_path, "wb") as fh:
                        copyfileobj(download.raw, fh)
                    tmp_path.rename(local_path)

                    log.info("Foto heruntergeladen: %s", filename)
                    stats["downloaded"] += 1

                except Exception as exc:
                    log.error("Fehler bei Foto %s: %s", getattr(photo, "filename", "?"), exc)
                    stats["errors"] += 1

        except Exception as exc:
            log.error("Fehler beim Album '%s': %s", album_name, exc)
            stats["errors"] += 1

    return stats


def _safe_dirname(name: str) -> str:
    """Create a filesystem-safe directory name."""
    return "".join(c if c.isalnum() or c in (" ", "-", "_") else "_" for c in name).strip()


# ---------------------------------------------------------------------------
# Combined backup runner
# ---------------------------------------------------------------------------

def run_backup(
    apple_id: str,
    backup_drive: bool = False,
    backup_photos: bool = False,
    drive_folders: list[str] | None = None,
    photos_include_family: bool = False,
    destination: str = "",
    excludes: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Run a complete backup for one account based on its configuration.

    Returns a result dict with drive_stats and photos_stats.
    """
    result = {"drive_stats": None, "photos_stats": None, "success": True, "message": ""}

    if not destination:
        # Derive from apple_id
        destination = apple_id.replace("@", "_at_").replace(".", "_")

    if backup_drive and drive_folders:
        log.info("Starte iCloud Drive Backup für %s", apple_id)
        drive_stats = run_drive_backup(apple_id, drive_folders, destination, excludes, dry_run)
        result["drive_stats"] = drive_stats
        if drive_stats["errors"] > 0:
            result["success"] = False

    if backup_photos:
        log.info("Starte iCloud Fotos Backup für %s", apple_id)
        photos_stats = run_photos_backup(
            apple_id, destination, photos_include_family, excludes, dry_run
        )
        result["photos_stats"] = photos_stats
        if photos_stats["errors"] > 0:
            result["success"] = False

    parts = []
    if result["drive_stats"]:
        d = result["drive_stats"]
        parts.append(
            f"Drive: {d['downloaded']} heruntergeladen, "
            f"{d['deleted']} gelöscht, {d['skipped']} übersprungen, "
            f"{d['errors']} Fehler"
        )
    if result["photos_stats"]:
        p = result["photos_stats"]
        parts.append(
            f"Fotos: {p['downloaded']} heruntergeladen, "
            f"{p['skipped']} übersprungen, {p['errors']} Fehler"
        )

    result["message"] = " | ".join(parts) if parts else "Nichts zu sichern."
    return result
