from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class OutputFormat(str, Enum):
    """Supported output formats for document export."""

    DOCX = "docx"
    PDF = "pdf"
    HTML = "html"
    MARKDOWN = "markdown"


class FormattingTemplate(BaseModel):
    """A formatting template defines visual styles and rendering configuration for a specific output format."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9-]+$")
    name: str
    format: OutputFormat
    renderer: OutputFormat
    assets: list[str] = Field(default_factory=list)
    options: dict[str, str | int | float | bool] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """Ensure name is not empty."""
        if not value.strip():
            raise ValueError("Название шаблона не может быть пустым")
        return value

    @model_validator(mode="after")
    def validate_renderer_matches_format(self) -> FormattingTemplate:
        """Ensure renderer matches the format."""
        if self.renderer != self.format:
            raise ValueError(
                f"Renderer {self.renderer.value} несовместим с форматом {self.format.value}"
            )
        return self

    def validate_assets(self, catalog_dir: Path) -> None:
        """Validate that all asset paths exist relative to catalog directory and are safe."""
        for asset_path in self.assets:
            # Check for absolute paths
            if asset_path.startswith("/"):
                raise ValueError(
                    f"Абсолютный путь в ассетах не допускается: {asset_path}"
                )

            # Check for path traversal
            if ".." in asset_path:
                raise ValueError(
                    f"Путь с '..' в ассетах не допускается: {asset_path}"
                )

            # Check if file exists
            full_path = (catalog_dir / asset_path).resolve()

            # Ensure the resolved path is still within the catalog directory
            try:
                full_path.relative_to(catalog_dir.resolve())
            except ValueError:
                raise ValueError(
                    f"Ассет выходит за границы каталога: {asset_path}"
                )

            if not full_path.exists():
                raise ValueError(
                    f"Ассет не найден: {asset_path}"
                )
