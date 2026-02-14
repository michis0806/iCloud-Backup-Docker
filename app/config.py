import secrets
from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    secret_key: str = "change-me-in-production"
    auth_password: str = ""
    config_path: Path = Path("/config")
    backup_path: Path = Path("/backups")
    cookie_directory: Path = Path("/config/sessions")
    log_level: str = "INFO"

    model_config = {"env_prefix": ""}

    def ensure_directories(self) -> None:
        self.config_path.mkdir(parents=True, exist_ok=True)
        self.backup_path.mkdir(parents=True, exist_ok=True)
        self.cookie_directory.mkdir(parents=True, exist_ok=True)

    def get_auth_password(self) -> str:
        """Return AUTH_PASSWORD or generate a random one."""
        if self.auth_password:
            return self.auth_password
        if not hasattr(self, "_generated_password"):
            self._generated_password = secrets.token_urlsafe(16)
        return self._generated_password


settings = Settings()
