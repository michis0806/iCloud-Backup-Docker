import secrets
from pathlib import Path

from pydantic_settings import BaseSettings


_SECRET_KEY_DEFAULT = "change-me-in-production"


class Settings(BaseSettings):
    secret_key: str = _SECRET_KEY_DEFAULT
    auth_password: str = ""
    icloud_secret_key: str = ""
    config_path: Path = Path("/config")
    backup_path: Path = Path("/backups")
    archive_path: Path = Path("/archive")
    cookie_directory: Path = Path("/config/sessions")
    log_level: str = "INFO"

    model_config = {"env_prefix": ""}

    def ensure_directories(self) -> None:
        self.config_path.mkdir(parents=True, exist_ok=True)
        self.backup_path.mkdir(parents=True, exist_ok=True)
        self.archive_path.mkdir(parents=True, exist_ok=True)
        self.cookie_directory.mkdir(parents=True, exist_ok=True)

    def get_auth_password(self) -> str:
        """Return AUTH_PASSWORD or generate a random one."""
        if self.auth_password:
            return self.auth_password
        if not hasattr(self, "_generated_password"):
            self._generated_password = secrets.token_urlsafe(16)
        return self._generated_password

    def get_secret_key(self) -> str:
        """Return SECRET_KEY, falling back to AUTH_PASSWORD if not explicitly set.

        Priority: SECRET_KEY env var > AUTH_PASSWORD > generated password.
        This ensures session cookies are always signed with a unique secret,
        even when the user doesn't set SECRET_KEY explicitly.
        """
        if self.secret_key != _SECRET_KEY_DEFAULT:
            return self.secret_key
        return self.get_auth_password()

    def get_icloud_secret_key(self) -> str:
        """Return the key used to encrypt stored iCloud passwords.

        Priority: ICLOUD_SECRET_KEY > SECRET_KEY > AUTH_PASSWORD. Only
        explicitly configured values qualify – the randomly generated
        per-start password would break decryption after a restart, so an
        empty string (feature disabled) is returned instead.
        """
        if self.icloud_secret_key:
            return self.icloud_secret_key
        if self.secret_key != _SECRET_KEY_DEFAULT:
            return self.secret_key
        return self.auth_password


settings = Settings()
