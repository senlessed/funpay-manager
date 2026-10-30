import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Non-secret bootstrap settings. Secrets never belong in this model."""

    model_config = SettingsConfigDict(env_prefix="FPTM_", extra="ignore")
    data_dir: Path = (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        / "FunPayTelegramManager"
    )

    def prepare(self) -> None:
        for directory in ("data", "logs", "backups", "config"):
            (self.data_dir / directory).mkdir(parents=True, exist_ok=True)

    @property
    def database(self) -> Path:
        return self.data_dir / "data" / "funpay.db"
