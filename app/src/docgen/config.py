from pathlib import Path

from pydantic import AnyHttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DOCGEN_",
        env_file=Path(__file__).resolve().parents[3] / ".env",
    )

    database_url: str = "sqlite:///./var/docgen.db"
    data_dir: Path = Path("./var/data")
    confluence_hosts: tuple[str, ...] = ("confluence.local",)
    confluence_api_base: AnyHttpUrl | None = None
    confluence_token: SecretStr | None = None
