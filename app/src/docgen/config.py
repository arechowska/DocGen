from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DOCGEN_",
        env_file=repository_root() / ".env",
    )

    database_url: str = "sqlite:///./var/docgen.db"
    data_dir: Path = Path("./var/data")
    confluence_hosts: tuple[str, ...] = ("confluence.local",)
    confluence_api_base: str | None = None
    confluence_token: SecretStr | None = None
    local_text_base_url: str | None = None
    local_text_model: str | None = None
    local_vision_base_url: str | None = None
    local_vision_model: str | None = None
    trusted_integration_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "::1")
    worker_lease_seconds: int = Field(default=30, gt=0)
    max_upload_bytes: int = Field(default=52_428_800, gt=0)
    max_project_storage_bytes: int = Field(default=524_288_000, gt=0)
    max_image_pixels: int = Field(default=40_000_000, gt=0)
    max_archive_entries: int = Field(default=10_000, gt=0)
    max_archive_uncompressed_bytes: int = Field(default=209_715_200, gt=0)
    max_model_request_bytes: int = Field(default=20_971_520, gt=0)
    max_job_seconds: int = Field(default=300, gt=0)
