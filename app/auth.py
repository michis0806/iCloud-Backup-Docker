"""Simple cookie-based password authentication."""

import hashlib
import hmac
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.config import settings

# Routes that don't require authentication
_PUBLIC_PATHS = {"/health", "/login"}

_COOKIE_NAME = "icloud_session"
_SESSION_MAX_AGE = 86400 * 7  # 7 days


def _sign(value: str) -> str:
    """Create an HMAC signature for the given value."""
    return hmac.new(
        settings.get_secret_key().encode(), value.encode(), hashlib.sha256
    ).hexdigest()


def create_session_cookie() -> str:
    """Create a signed session cookie value."""
    ts = str(int(time.time()))
    sig = _sign(ts)
    return f"{ts}.{sig}"


def verify_session_cookie(cookie: str) -> bool:
    """Verify a signed session cookie."""
    try:
        ts, sig = cookie.rsplit(".", 1)
        if not hmac.compare_digest(sig, _sign(ts)):
            return False
        age = time.time() - int(ts)
        return 0 <= age <= _SESSION_MAX_AGE
    except (ValueError, TypeError):
        return False


class AuthMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated requests to /login."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Allow public paths and static files
        if path in _PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)

        # Check session cookie
        cookie = request.cookies.get(_COOKIE_NAME)
        if cookie and verify_session_cookie(cookie):
            return await call_next(request)

        # API requests get 401, browser requests get redirect
        if path.startswith("/api/"):
            from starlette.responses import JSONResponse
            return JSONResponse(
                {"detail": "Nicht authentifiziert."}, status_code=401
            )

        return RedirectResponse(url="/login", status_code=302)
