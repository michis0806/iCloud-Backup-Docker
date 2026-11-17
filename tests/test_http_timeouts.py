"""Offline tests for finite defaults on pyicloud HTTP requests."""

from requests import Response
from requests.adapters import HTTPAdapter

from pyicloud import PyiCloudService

from app.services.apple_auth import (
    DEFAULT_HTTP_TIMEOUT,
    InteractiveICloudService,
    _DefaultTimeoutHTTPAdapter,
)


def _mock_transport(monkeypatch):
    seen_timeouts = []

    def send(_adapter, request, **kwargs):
        seen_timeouts.append(kwargs.get("timeout"))
        response = Response()
        response.status_code = 200
        response.url = request.url
        response.request = request
        response.headers["Content-Type"] = "text/plain"
        response._content = b"ok"
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    return seen_timeouts


def test_constructor_auth_requests_use_finite_defaults(tmp_path, monkeypatch):
    seen_timeouts = _mock_transport(monkeypatch)

    def authenticate(self, force_refresh=False, service=None, pause_2fa=None):
        self.session.get("https://icloud.test/auth")

    monkeypatch.setattr(PyiCloudService, "authenticate", authenticate)
    InteractiveICloudService(
        "test@example.com", "unused", cookie_directory=str(tmp_path),
    )

    assert seen_timeouts == [DEFAULT_HTTP_TIMEOUT]


def test_explicit_timeout_is_preserved_and_adapters_are_not_remounted(
    tmp_path, monkeypatch,
):
    seen_timeouts = _mock_transport(monkeypatch)
    monkeypatch.setattr(PyiCloudService, "authenticate", lambda *args, **kwargs: None)
    api = InteractiveICloudService(
        "test@example.com", cookie_directory=str(tmp_path), authenticate=False,
    )
    https_adapter = api.session.get_adapter("https://icloud.test/resource")
    http_adapter = api.session.get_adapter("http://icloud.test/resource")
    assert isinstance(https_adapter, _DefaultTimeoutHTTPAdapter)
    assert isinstance(http_adapter, _DefaultTimeoutHTTPAdapter)

    api.session.get("https://icloud.test/resource", timeout=(2, 15))
    api.authenticate()

    assert seen_timeouts == [(2, 15)]
    assert api.session.get_adapter("https://icloud.test/resource") is https_adapter
    assert api.session.get_adapter("http://icloud.test/resource") is http_adapter
