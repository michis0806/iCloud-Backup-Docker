"""Core backup logic for iCloud Drive and iCloud Photos."""

import gc
import json
import logging
import os
import shutil
import threading
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from shutil import copyfileobj

from app.config import settings
from app.models import SyncPolicy
from app.services import icloud_service

log = logging.getLogger("icloud-backup")


# ---------------------------------------------------------------------------
# Live progress tracking
# ---------------------------------------------------------------------------

_progress: dict[str, dict] = {}  # keyed by apple_id
_progress_lock = threading.Lock()
_cancel_events: dict[str, threading.Event] = {}


def get_progress(config_id: str) -> dict | None:
    with _progress_lock:
        return _progress.get(config_id)


def _set_progress(config_id: str, data: dict) -> None:
    with _progress_lock:
        _progress[config_id] = data


def _clear_progress(config_id: str) -> None:
    with _progress_lock:
        _progress.pop(config_id, None)
        _cancel_events.pop(config_id, None)


def request_cancel(config_id: str) -> bool:
    """Request cancellation of a running backup. Returns True if a backup was running."""
    with _progress_lock:
        ev = _cancel_events.get(config_id)
        if ev is None:
            return False
        ev.set()
        return True


def _is_cancelled(config_id: str | None) -> bool:
    """Check whether cancellation has been requested."""
    if config_id is None:
        return False
    with _progress_lock:
        ev = _cancel_events.get(config_id)
        return ev is not None and ev.is_set()


def _register_cancel_event(config_id: str) -> None:
    """Register a fresh cancel event for the given backup run."""
    with _progress_lock:
        _cancel_events[config_id] = threading.Event()


class BackupCancelled(Exception):
    """Raised when a running backup is cancelled by the user."""


def _check_cancel(config_id: str | None) -> None:
    """Raise BackupCancelled if cancellation was requested."""
    if _is_cancelled(config_id):
        raise BackupCancelled()


# ---------------------------------------------------------------------------
# Etag cache
# ---------------------------------------------------------------------------

def _cache_path(destination: str, folder_name: str) -> Path:
    """Return the path to the etag cache file for a given folder."""
    safe = folder_name.replace("/", "_")
    return settings.config_path / f".icloud-backup-state-{destination}-{safe}.json"


def _load_cache(destination: str, folder_name: str) -> dict:
    path = _cache_path(destination, folder_name)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            log.warning("Cache-Datei beschädigt, wird ignoriert: %s", path)
    return {}


def _save_cache(destination: str, folder_name: str, cache: dict) -> None:
    path = _cache_path(destination, folder_name)
    try:
        path.write_text(json.dumps(cache, indent=2))
    except Exception as exc:
        log.warning("Cache konnte nicht gespeichert werden: %s", exc)


# ---------------------------------------------------------------------------
# Exclusion helpers
# ---------------------------------------------------------------------------

def _is_glob(pattern: str) -> bool:
    return any(c in pattern for c in ("*", "?", "["))


def is_excluded(rel_path: str, excludes: list[str]) -> bool:
    """Check whether *rel_path* matches any exclusion pattern.

    Supported patterns:
      - Glob patterns on individual path components: ``*.tmp``, ``.git``
      - Simple names (no slash): matches any path component
      - Path patterns (with slash): ``Ablage/gescannte Alben`` matches if
        *rel_path* starts with or equals the pattern
    """
    if not excludes:
        return False

    parts = rel_path.split("/")
    for pattern in excludes:
        if _is_glob(pattern):
            if any(fnmatch(p, pattern) for p in parts):
                return True
        elif "/" in pattern:
            if rel_path.startswith(pattern) or rel_path == pattern:
                return True
        else:
            if pattern in parts:
                return True
    return False


def _adjust_excludes_for_folder(folder_name: str, excludes: list[str] | None) -> list[str]:
    """Strip *folder_name* prefix from path-based exclusion patterns.

    When syncing folder "Ablage", internal paths are relative to that folder
    (e.g. "gescannte Alben/file.pdf").  If the user set an exclusion like
    "Ablage/gescannte Alben", we need to strip the "Ablage/" prefix so the
    pattern becomes "gescannte Alben" which will match the relative path.
    """
    if not excludes:
        return []
    prefix = folder_name + "/"
    adjusted = []
    for pattern in excludes:
        if not _is_glob(pattern) and "/" in pattern and pattern.startswith(prefix):
            stripped = pattern[len(prefix):]
            if stripped:
                adjusted.append(stripped)
            # else: pattern == folder_name + "/" → skip entire folder (shouldn't happen)
        else:
            adjusted.append(pattern)
    return adjusted


# ---------------------------------------------------------------------------
# iCloud Drive backup
# ---------------------------------------------------------------------------

def _walk_remote(node, prefix: str = "", excludes: list[str] | None = None,
                 cache: dict | None = None):
    """Recursively yield ``(relative_path, node)`` for all files under *node*.

    When *cache* is provided, folders whose etag matches the cached value
    are skipped entirely.  Yields an additional sentinel
    ``(folder_rel_path, None, new_etag)`` for folders so the caller can
    update the cache after an error-free run.
    """
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
            # Etag-based skip
            child_etag = getattr(child, "etag", None)
            if cache is not None and child_etag:
                cached_etag = cache.get(rel)
                if cached_etag and cached_etag == child_etag:
                    log.debug("Cache-Hit (etag unverändert): %s", rel)
                    continue

            yield from _walk_remote(child, rel, excludes, cache)

            # Yield folder etag so caller can update cache
            if child_etag:
                yield rel, None, child_etag
        else:
            yield rel, child, None


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


def _apply_sync_policy(
    local_file: Path,
    rel_path: str,
    policy: str,
    archive_dest: Path,
    stats: dict,
) -> None:
    """Handle an orphaned local file according to the sync policy.

    *policy* is one of ``"keep"``, ``"delete"``, ``"archive"``.
    """
    if policy == SyncPolicy.KEEP:
        return

    if policy == SyncPolicy.ARCHIVE:
        target = archive_dest / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(local_file), str(target))
            log.info("Archiviert (remote entfernt): %s → %s", rel_path, target)
            stats["archived"] += 1
        except Exception as exc:
            log.error("Fehler beim Archivieren von %s: %s", rel_path, exc)
            stats["errors"] += 1
        return

    # policy == "delete"
    try:
        local_file.unlink()
        log.info("Gelöscht (remote entfernt): %s", rel_path)
        stats["deleted"] += 1
    except Exception as exc:
        log.error("Fehler beim Löschen von %s: %s", rel_path, exc)
        stats["errors"] += 1


def sync_drive_folder(
    apple_id: str,
    folder_name: str,
    destination_path: Path,
    destination_key: str,
    excludes: list[str] | None = None,
    dry_run: bool = False,
    config_id: str | None = None,
    sync_policy: str = SyncPolicy.DELETE,
) -> dict:
    """Synchronise a single iCloud Drive folder to *destination_path*.

    Returns stats dict: ``{downloaded, deleted, archived, skipped, errors}``.
    """
    stats = {"downloaded": 0, "deleted": 0, "archived": 0, "skipped": 0, "errors": 0}

    api = icloud_service.get_session(apple_id)
    if api is None:
        log.error("Keine Sitzung für %s", apple_id)
        return {**stats, "errors": 1}

    try:
        folder_node = api.drive[folder_name]
    except (KeyError, Exception) as exc:
        log.error("Ordner '%s' nicht gefunden: %s", folder_name, exc)
        return {**stats, "errors": 1}

    dest = destination_path / folder_name
    dest.mkdir(parents=True, exist_ok=True)

    # Adjust exclusion patterns: strip folder_name prefix from path patterns
    # so "Ablage/gescannte Alben" becomes "gescannte Alben" inside the Ablage walk
    adjusted_excludes = _adjust_excludes_for_folder(folder_name, excludes)

    # Load etag cache
    cache = _load_cache(destination_key, folder_name)
    new_etags: dict[str, str] = {}

    remote_files: set[str] = set()

    for rel_path, node, etag in _walk_remote(folder_node, excludes=adjusted_excludes, cache=cache):
        _check_cancel(config_id)

        # Folder etag sentinel
        if node is None and etag:
            new_etags[rel_path] = etag
            continue

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

        # Update progress
        if config_id is not None:
            _set_progress(config_id, {
                "phase": "drive",
                "folder": folder_name,
                "current_file": rel_path,
                **stats,
            })

    # Handle local files that no longer exist remotely
    if not dry_run and sync_policy != SyncPolicy.KEEP:
        archive_dest = settings.archive_path / destination_key / "drive" / folder_name
        for local_file in dest.rglob("*"):
            if local_file.is_file() and not local_file.name.endswith(".tmp"):
                rel = str(local_file.relative_to(dest))
                if rel not in remote_files:
                    _apply_sync_policy(local_file, rel, sync_policy, archive_dest, stats)

        # Clean up empty directories
        for dirpath in sorted(dest.rglob("*"), reverse=True):
            if dirpath.is_dir() and not any(dirpath.iterdir()):
                dirpath.rmdir()

    # Save etag cache only when no errors occurred
    if stats["errors"] == 0:
        cache.update(new_etags)
        _save_cache(destination_key, folder_name, cache)

    return stats


def _resolve_drive_folders(apple_id: str, folders: list[str]) -> list[str]:
    """Resolve the ``__ALL__`` marker to every top-level Drive folder."""
    if "__ALL__" not in folders:
        return folders
    all_folders = icloud_service.get_drive_folders(apple_id)
    return [f["name"] for f in all_folders if f["type"] == "folder"]


def run_drive_backup(
    apple_id: str,
    folders: list[str],
    destination: str,
    excludes: list[str] | None = None,
    dry_run: bool = False,
    config_id: str | None = None,
    sync_policy: str = SyncPolicy.DELETE,
) -> dict:
    """Run a full iCloud Drive backup for the given folders."""
    folders = _resolve_drive_folders(apple_id, folders)

    dest_path = settings.backup_path / destination / "drive"
    dest_path.mkdir(parents=True, exist_ok=True)

    total = {"downloaded": 0, "deleted": 0, "archived": 0, "skipped": 0, "errors": 0}
    for folder in folders:
        _check_cancel(config_id)
        log.info("Synchronisiere Drive-Ordner: %s → %s", folder, dest_path)
        if config_id is not None:
            _set_progress(config_id, {
                "phase": "drive",
                "folder": folder,
                "current_file": "",
                **total,
            })
        stats = sync_drive_folder(
            apple_id, folder, dest_path, destination, excludes, dry_run, config_id,
            sync_policy=sync_policy,
        )
        for k in total:
            total[k] += stats.get(k, 0)

    return total


# ---------------------------------------------------------------------------
# iCloud Photos backup
# ---------------------------------------------------------------------------

def _photo_date(photo) -> datetime | None:
    """Extract the best available date from a photo asset."""
    for attr in ("asset_date", "created", "added_date"):
        dt = getattr(photo, attr, None)
        if dt is not None:
            return dt
    return None


def _unique_path(path: Path) -> Path:
    """Return *path* if it does not exist, else append a counter to avoid collisions."""
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _download_photo(photo, local_path: Path, stats: dict) -> None:
    """Download a single photo asset to *local_path* with true streaming.

    Bypasses photo.download() which reads the entire file into RAM.
    Instead, we get the download URL and stream directly to disk in chunks.
    """
    fname = getattr(photo, "filename", "?")

    # Get download URL from photo versions (avoids photo.download() which
    # calls response.raw.read() and loads everything into memory).
    try:
        versions = photo.versions
        version_info = versions.get("original")
        if not version_info or not version_info.get("url"):
            log.warning("Keine Download-URL für %s", fname)
            stats["errors"] += 1
            return
        url = version_info["url"]
    except Exception as exc:
        log.error("Fehler beim Abrufen der Version für %s: %s", fname, exc)
        stats["errors"] += 1
        return

    # Stream download via the iCloud session (handles auth cookies)
    response = None
    try:
        response = photo._service.session.get(url, stream=True)
        response.raise_for_status()
    except Exception as exc:
        log.error("Download-Fehler für %s: %s", fname, exc)
        stats["errors"] += 1
        if response is not None:
            response.close()
        return

    tmp_path = local_path.with_suffix(local_path.suffix + ".tmp")
    try:
        with open(tmp_path, "wb") as fh:
            for chunk in response.iter_content(chunk_size=256 * 1024):
                if chunk:
                    fh.write(chunk)
        tmp_path.rename(local_path)
    except Exception as exc:
        log.error("Schreibfehler für %s: %s", local_path.name, exc)
        if tmp_path.exists():
            tmp_path.unlink()
        stats["errors"] += 1
        return
    finally:
        response.close()

    # Preserve photo timestamp
    dt = _photo_date(photo)
    if dt:
        try:
            mtime = dt.timestamp()
            os.utime(local_path, (mtime, mtime))
        except Exception:
            pass

    log.info("Foto heruntergeladen: %s", local_path.name)
    stats["downloaded"] += 1


def _process_photo(photo, dest_path: Path, excludes: list[str] | None,
                   stats: dict, dry_run: bool) -> tuple[str | None, bool]:
    """Process a single photo: check exclusions, skip/download.

    Returns ``(filename, was_skipped)`` – *was_skipped* is True when the
    photo already existed locally and was not re-downloaded.
    """
    filename = getattr(photo, "filename", None)
    if not filename:
        return None, False

    if excludes and is_excluded(filename, excludes):
        return filename, False

    # Organise into date-based subfolders: YYYY/MM/DD
    dt = _photo_date(photo)
    if dt:
        sub = dest_path / f"{dt:%Y}" / f"{dt:%m}" / f"{dt:%d}"
    else:
        sub = dest_path / "unknown_date"
    sub.mkdir(parents=True, exist_ok=True)

    local_path = sub / filename

    # Skip if already downloaded with matching size
    if local_path.exists():
        remote_size = getattr(photo, "size", None)
        if remote_size is not None:
            if local_path.stat().st_size == remote_size:
                stats["skipped"] += 1
                return filename, True
            # Size mismatch → re-download (handled below)
        else:
            # No remote size available → trust file existence
            log.debug("Kein remote_size für %s, überspringe (Datei existiert)", filename)
            stats["skipped"] += 1
            return filename, True

    if dry_run:
        log.info("[DRY RUN] Würde herunterladen: %s", filename)
        stats["downloaded"] += 1
        return filename, False

    # Handle filename collisions (different photo, same name)
    if local_path.exists():
        local_path = _unique_path(local_path)

    _download_photo(photo, local_path, stats)
    return filename, False


def _reconcile_photos(
    local_dir: Path,
    remote_files: set[str],
    sync_policy: str,
    archive_dest: Path,
    stats: dict,
    dry_run: bool,
) -> None:
    """Remove / archive local photos that no longer exist in iCloud."""
    if dry_run or sync_policy == SyncPolicy.KEEP:
        return
    if not local_dir.exists():
        return

    for local_file in local_dir.rglob("*"):
        if not local_file.is_file() or local_file.name.endswith(".tmp"):
            continue
        rel = str(local_file.relative_to(local_dir))
        if rel not in remote_files:
            _apply_sync_policy(local_file, rel, sync_policy, archive_dest, stats)

    # Clean up empty directories
    for dirpath in sorted(local_dir.rglob("*"), reverse=True):
        if dirpath.is_dir() and not any(dirpath.iterdir()):
            dirpath.rmdir()


def _backup_photo_library(
    api,
    library_photos,
    dest_dir: Path,
    label: str,
    excludes: list[str] | None,
    stats: dict,
    dry_run: bool,
    config_id: str | None,
    sync_policy: str,
    archive_base: Path,
) -> tuple[int, set[str]]:
    """Download all photos from a library/album iterator to *dest_dir*.

    Returns ``(processed_count, remote_files_set)``.
    """
    current_file = ""
    processed = 0
    remote_files: set[str] = set()

    try:
        for photo in library_photos:
            _check_cancel(config_id)
            fname, was_skipped = _process_photo(photo, dest_dir, excludes, stats, dry_run)
            processed += 1
            current_file = fname or current_file
            if fname:
                dt = _photo_date(photo)
                if dt:
                    rel = f"{dt:%Y}/{dt:%m}/{dt:%d}/{fname}"
                else:
                    rel = f"unknown_date/{fname}"
                remote_files.add(rel)
            if processed % 50 == 0:
                gc.collect()
            if config_id is not None:
                _set_progress(config_id, {
                    "phase": "photos",
                    "folder": label,
                    "current_file": current_file,
                    "processed": processed,
                    **stats,
                })
    except BackupCancelled:
        raise
    except Exception as exc:
        log.error("Fehler beim Iterieren von %s: %s", label, exc)
        stats["errors"] += 1

    log.info(
        "%s abgeschlossen: %d verarbeitet, %d heruntergeladen, "
        "%d übersprungen, %d Fehler",
        label, processed, stats["downloaded"], stats["skipped"], stats["errors"],
    )

    if processed > 0 and sync_policy != SyncPolicy.KEEP:
        _reconcile_photos(dest_dir, remote_files, sync_policy, archive_base, stats, dry_run)

    return processed, remote_files


def run_photos_backup(
    apple_id: str,
    destination: str,
    include_family: bool = False,
    shared_library_id: str | None = None,
    excludes: list[str] | None = None,
    dry_run: bool = False,
    config_id: str | None = None,
    sync_policy: str = SyncPolicy.KEEP,
) -> dict:
    """Download iCloud Photos (and optionally shared/family library) to *destination*."""
    stats = {"downloaded": 0, "skipped": 0, "deleted": 0, "archived": 0, "errors": 0}

    api = icloud_service.get_session(apple_id)
    if api is None:
        log.error("Keine Sitzung für %s", apple_id)
        return {**stats, "errors": 1}

    dest_path = settings.backup_path / destination / "photos"
    dest_path.mkdir(parents=True, exist_ok=True)

    # ---- Personal library via api.photos.all ----
    log.info("Sichere iCloud Fotos (Mediathek) für %s", apple_id)

    mediathek_dir = dest_path / "Mediathek"
    archive_mediathek = settings.archive_path / destination / "photos" / "Mediathek"
    processed, _ = _backup_photo_library(
        api, api.photos.all, mediathek_dir, "Mediathek",
        excludes, stats, dry_run, config_id, sync_policy, archive_mediathek,
    )

    # ---- Shared / family library ----
    if include_family and shared_library_id and shared_library_id.startswith("SharedSync-"):
        log.info("Sichere geteilte Mediathek (%s) für %s", shared_library_id, apple_id)
        try:
            libraries = api.photos.libraries
            shared_lib = libraries.get(shared_library_id)
            if shared_lib is None:
                log.warning(
                    "Geteilte Bibliothek %s nicht gefunden für %s",
                    shared_library_id, apple_id,
                )
            else:
                shared_dir = dest_path / "Geteilte Mediathek"
                archive_shared = settings.archive_path / destination / "photos" / "Geteilte Mediathek"
                _backup_photo_library(
                    api, shared_lib.all if hasattr(shared_lib, "all") else [],
                    shared_dir, "Geteilte Mediathek",
                    excludes, stats, dry_run, config_id, sync_policy, archive_shared,
                )
        except BackupCancelled:
            raise
        except Exception as exc:
            log.error("Fehler bei geteilter Bibliothek für %s: %s", apple_id, exc)
            stats["errors"] += 1

    stats["processed"] = processed
    return stats


def get_backup_storage_stats(destination: str) -> dict:
    """Scan local backup directories and return file counts and sizes.

    Returns::

        {
            "photos": {"count": 1234, "size_bytes": 567890},
            "drive":  {"count": 56,   "size_bytes": 123456},
        }
    """
    base = settings.backup_path / destination
    result = {}
    for subdir in ("photos", "drive"):
        path = base / subdir
        if not path.exists():
            result[subdir] = {"count": 0, "size_bytes": 0}
            continue
        count = 0
        total_size = 0
        for f in path.rglob("*"):
            if f.is_file():
                count += 1
                try:
                    total_size += f.stat().st_size
                except OSError:
                    pass
        result[subdir] = {"count": count, "size_bytes": total_size}
    return result


# ---------------------------------------------------------------------------
# Combined backup runner
# ---------------------------------------------------------------------------

def run_backup(
    apple_id: str,
    backup_drive: bool = False,
    backup_photos: bool = False,
    drive_folders: list[str] | None = None,
    photos_include_family: bool = False,
    shared_library_id: str | None = None,
    destination: str = "",
    exclusions: list[str] | None = None,
    dry_run: bool = False,
    config_id: str | None = None,
    drive_sync_policy: str = SyncPolicy.DELETE,
    photos_sync_policy: str = SyncPolicy.KEEP,
) -> dict:
    """Run a complete backup for one account based on its configuration."""
    result = {"drive_stats": None, "photos_stats": None, "success": True, "message": ""}

    if not destination:
        destination = apple_id.replace("@", "_at_").replace(".", "_")

    if config_id is not None:
        _register_cancel_event(config_id)
        _set_progress(config_id, {
            "phase": "starting",
            "folder": "",
            "current_file": "",
            "downloaded": 0, "skipped": 0, "errors": 0,
        })

    cancelled = False
    try:
        if backup_drive and drive_folders:
            log.info("Starte iCloud Drive Backup für %s", apple_id)
            drive_stats = run_drive_backup(
                apple_id, drive_folders, destination, exclusions, dry_run, config_id,
                sync_policy=drive_sync_policy,
            )
            result["drive_stats"] = drive_stats
            if drive_stats["errors"] > 0:
                result["success"] = False

        if backup_photos:
            log.info("Starte iCloud Fotos Backup für %s", apple_id)
            photos_stats = run_photos_backup(
                apple_id, destination, photos_include_family,
                shared_library_id=shared_library_id,
                excludes=exclusions, dry_run=dry_run, config_id=config_id,
                sync_policy=photos_sync_policy,
            )
            result["photos_stats"] = photos_stats
            if photos_stats["errors"] > 0:
                result["success"] = False
    except BackupCancelled:
        cancelled = True
        log.info("Backup für %s wurde vom Benutzer abgebrochen.", apple_id)
        result["success"] = False
    finally:
        if config_id is not None:
            _clear_progress(config_id)

    parts = []
    if cancelled:
        parts.append("Abgebrochen durch Benutzer")
    if result["drive_stats"]:
        d = result["drive_stats"]
        summary = f"Drive: {d['downloaded']} heruntergeladen"
        if d.get('deleted'):
            summary += f", {d['deleted']} gelöscht"
        if d.get('archived'):
            summary += f", {d['archived']} archiviert"
        summary += f", {d['skipped']} übersprungen, {d['errors']} Fehler"
        parts.append(summary)
    if result["photos_stats"]:
        p = result["photos_stats"]
        summary = f"Fotos: {p['downloaded']} heruntergeladen"
        if p.get('deleted'):
            summary += f", {p['deleted']} gelöscht"
        if p.get('archived'):
            summary += f", {p['archived']} archiviert"
        summary += f", {p['skipped']} übersprungen, {p['errors']} Fehler"
        parts.append(summary)

    result["message"] = " | ".join(parts) if parts else "Nichts zu sichern."
    return result
