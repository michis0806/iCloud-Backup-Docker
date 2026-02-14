from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    secret_key: str = "change-me-in-production"
    config_path: Path = Path("/config")
    backup_path: Path = Path("/backups")
    cookie_directory: Path = Path("/config/sessions")
    log_level: str = "INFO"

    model_config = {"env_prefix": ""}

    def ensure_directories(self) -> None:
        self.config_path.mkdir(parents=True, exist_ok=True)
        self.backup_path.mkdir(parents=True, exist_ok=True)
        self.cookie_directory.mkdir(parents=True, exist_ok=True)


settings = Settings()
