from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DOCGEN_")

    database_url: str = "sqlite:///./var/docgen.db"
    data_dir: Path = Path("./var/data")
    confluence_hosts: tuple[str, ...] = ("confluence.local",)
