"""Tests for the pre-backup session validation in run_backup."""

from app.services import backup_service


def test_run_backup_aborts_when_token_expired(monkeypatch):
    monkeypatch.setattr(
        backup_service.icloud_service,
        "check_connection",
        lambda apple_id: {
            "valid": False,
            "message": "Token abgelaufen – Zwei-Faktor-Authentifizierung erforderlich.",
            "requires_2fa": True,
        },
    )

    result = backup_service.run_backup(apple_id="expired@icloud.com")

    assert result["success"] is False
    assert result["auth_expired"] is True
    assert "Token abgelaufen" in result["message"]


def test_run_backup_aborts_on_connection_error_without_auth_flag(monkeypatch):
    monkeypatch.setattr(
        backup_service.icloud_service,
        "check_connection",
        lambda apple_id: {
            "valid": False,
            "message": "Verbindungsfehler: timeout",
            "requires_2fa": False,
        },
    )

    result = backup_service.run_backup(apple_id="offline@icloud.com")

    assert result["success"] is False
    assert "auth_expired" not in result
    assert "Verbindungsfehler" in result["message"]
