"""iCloud Backup Service – FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.database import init_db
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
)

# Attach ring buffer handler to the root logger so all messages are captured
root_logger = logging.getLogger()
root_logger.addHandler(log_buffer)

log = logging.getLogger("icloud-backup")


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_directories()
    await init_db()
    start_scheduler()
    await sync_scheduled_jobs()
    log.info("iCloud Backup Service gestartet")
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

# API routers
app.include_router(accounts.router)
app.include_router(backup.router)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok"}


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
@app.get("/api/backup/progress/{config_id}")
async def get_backup_progress(config_id: int):
    """Return live progress for a running backup."""
    from app.services.backup_service import get_progress

    progress = get_progress(config_id)
    if progress is None:
        return {"running": False}
    return {"running": True, **progress}


# ---------------------------------------------------------------------------
# Web UI routes
# ---------------------------------------------------------------------------
@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/accounts/{account_id}")
async def account_detail(request: Request, account_id: int):
    return templates.TemplateResponse(
        "account_detail.html", {"request": request, "account_id": account_id}
    )


@app.get("/logs")
async def logs_page(request: Request):
    return templates.TemplateResponse("logs.html", {"request": request})
