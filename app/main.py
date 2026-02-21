"""iCloud Backup Service – FastAPI application entry point."""

import hmac
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.auth import AuthMiddleware, _COOKIE_NAME, create_session_cookie
from app import config_store
from app.config import settings
from app.routers import accounts, backup
from app.services.log_handler import log_buffer
from app.services.scheduler import start_scheduler, stop_scheduler, sync_scheduled_jobs

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,  # Override uvicorn's default logging setup
)


class _HealthCheckFilter(logging.Filter):
    """Suppress noisy 'GET /health' access-log entries from uvicorn."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "GET /health" not in msg


logging.getLogger("uvicorn.access").addFilter(_HealthCheckFilter())

# Attach ring buffer handler to the root logger so all messages are captured
root_logger = logging.getLogger()
root_logger.addHandler(log_buffer)

log = logging.getLogger("icloud-backup")


def _build_info() -> dict[str, str]:
    """Return build metadata injected at image build time."""
    return {
        "version": os.getenv("APP_VERSION", "dev"),
        "commit": os.getenv("APP_COMMIT", "unknown"),
        "build_date": os.getenv("APP_BUILD_DATE", "unknown"),
    }

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_directories()
    # Reset any backup statuses stuck on "running" from a previous crash
    reset_count = config_store.reset_stale_running_states()
    if reset_count:
        log.warning(
            "%d Backup(s) waren beim letzten Stopp noch aktiv und wurden zurückgesetzt.",
            reset_count,
        )
    start_scheduler()
    await sync_scheduled_jobs()
    # Log the password if it was auto-generated
    if not settings.auth_password:
        log.info(
            "Kein AUTH_PASSWORD gesetzt. Generiertes Passwort: %s",
            settings.get_auth_password(),
        )
    build = _build_info()
    log.info(
        "iCloud Backup Service gestartet (version=%s, commit=%s, build_date=%s)",
        build["version"],
        build["commit"],
        build["build_date"],
    )
    yield
    stop_scheduler()
    log.info("iCloud Backup Service gestoppt")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="iCloud Backup Service",
    version="1.0.0",
    lifespan=lifespan,
)

# Static files & templates
static_dir = Path(__file__).parent / "static"
templates_dir = Path(__file__).parent / "templates"
static_dir.mkdir(exist_ok=True)
(static_dir / "css").mkdir(exist_ok=True)
(static_dir / "js").mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
templates = Jinja2Templates(directory=str(templates_dir))

# Authentication middleware
app.add_middleware(AuthMiddleware)

# API routers
app.include_router(accounts.router)
app.include_router(backup.router)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "build": _build_info()}


# ---------------------------------------------------------------------------
# Logs API
# ---------------------------------------------------------------------------
@app.get("/api/logs")
async def get_logs(after: int = 0, limit: int = 200):
    """Return recent log entries (for polling-based log viewer)."""
    return log_buffer.get_entries(after_id=after, limit=limit)


# ---------------------------------------------------------------------------
# Progress API
# ---------------------------------------------------------------------------
@app.get("/api/backup/progress/{apple_id}")
async def get_backup_progress(apple_id: str):
    """Return live progress for a running backup."""
    from app.services.backup_service import get_progress

    progress = get_progress(apple_id)
    if progress is None:
        return {"running": False}
    return {"running": True, **progress}


# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------
@app.get("/login")
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    if hmac.compare_digest(password, settings.get_auth_password()):
        response = RedirectResponse(url="/", status_code=302)
        response.set_cookie(
            _COOKIE_NAME,
            create_session_cookie(),
            max_age=86400 * 7,
            httponly=True,
            samesite="lax",
        )
        return response
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Falsches Passwort."}, status_code=401
    )


@app.post("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(_COOKIE_NAME)
    return response


# ---------------------------------------------------------------------------
# Web UI routes
# ---------------------------------------------------------------------------
@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(
        "index.html", {"request": request, "build": _build_info()}
    )


@app.get("/accounts/{apple_id}")
async def account_detail(request: Request, apple_id: str):
    return templates.TemplateResponse(
        "account_detail.html",
        {"request": request, "apple_id": apple_id, "build": _build_info()},
    )


@app.get("/logs")
async def logs_page(request: Request):
    return templates.TemplateResponse("logs.html", {"request": request, "build": _build_info()})
