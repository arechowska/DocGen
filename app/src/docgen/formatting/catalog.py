from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from docgen.formatting.schemas import FormattingTemplate, OutputFormat


class FormattingTemplateError(ValueError):
    """Raised when a formatting template catalog cannot be loaded safely."""


def default_templates_dir() -> Path:
    """Return the built-in formatting template catalog directory.

    This is the same `formatting/templates` directory the export renderers
    (`docgen.export.html`/`docx`/`pdf`) fall back to for their own assets, so
    routes and the export workflow can build a `FormattingCatalog` over
    exactly the templates those renderers know how to load, unless an
    operator overrides it via `Settings.formatting_template_dir`.
    """
    return Path(__file__).resolve().parent / "templates"


class _StrictYamlLoader(yaml.SafeLoader):
    pass


def _construct_mapping_without_duplicate_keys(
    loader: _StrictYamlLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise FormattingTemplateError(f"Повторяющийся ключ YAML: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictYamlLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping_without_duplicate_keys
)


class FormattingCatalog:
    """Loads and manages formatting templates from YAML manifests."""

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory)
        self._templates: dict[OutputFormat, dict[str, FormattingTemplate]] | None = None

    def list(self, format: OutputFormat | None = None) -> list[FormattingTemplate]:
        """List formatting templates, optionally filtered by output format.

        Results are sorted by name, then by id.
        """
        templates = self._load_templates()

        if format is not None:
            result = [t for t in templates if t.format == format]
        else:
            result = templates

        # Sort by name, then by id
        return sorted(result, key=lambda t: (t.name, t.id))

    def get(self, format: OutputFormat, template_id: str) -> FormattingTemplate:
        """Get a specific formatting template by format and id."""
        templates = self._load_templates()

        for template in templates:
            if template.format == format and template.id == template_id:
                return template

        raise FormattingTemplateError(
            f"Шаблон не найден: формат {format.value}, id {template_id}"
        )

    def _load_templates(self) -> list[FormattingTemplate]:
        """Load and validate all formatting templates from YAML files."""
        if self._templates is None:
            self._load_directory()

        # Flatten templates from all formats
        templates: list[FormattingTemplate] = []
        for format_templates in self._templates.values():
            templates.extend(format_templates.values())

        return templates

    def _load_directory(self) -> None:
        """Load all YAML files from the directory and validate them."""
        self._templates = {}

        try:
            yaml_resources = sorted(
                [r for r in self._directory.iterdir() if r.name.endswith(".yaml")],
                key=lambda r: r.name,
            )
        except FileNotFoundError as exc:
            raise FormattingTemplateError(
                f"Каталог шаблонов не найден: {self._directory}"
            ) from exc

        for resource in yaml_resources:
            template = self._load_template(resource)

            # Check for duplicate IDs within the same format
            format_key = template.format
            if format_key not in self._templates:
                self._templates[format_key] = {}

            if template.id in self._templates[format_key]:
                raise FormattingTemplateError(
                    f"Повторяющийся идентификатор шаблона в формате {format_key.value}: {template.id}"
                )

            self._templates[format_key][template.id] = template

    def _load_template(self, resource: Path) -> FormattingTemplate:
        """Load and validate a single formatting template from a YAML file."""
        try:
            raw_template = yaml.load(
                resource.read_text(encoding="utf-8"), Loader=_StrictYamlLoader
            )
        except (OSError, yaml.YAMLError, FormattingTemplateError) as exc:
            raise FormattingTemplateError(
                f"Некорректный YAML-шаблон {resource.name}: {exc}"
            ) from exc

        if not isinstance(raw_template, dict):
            raise FormattingTemplateError(
                f"Шаблон {resource.name} должен содержать YAML-объект"
            )

        try:
            template = FormattingTemplate.model_validate(raw_template)
        except ValidationError as exc:
            raise FormattingTemplateError(
                f"Некорректный шаблон {resource.name}: {exc}"
            ) from exc

        # Validate asset files exist
        try:
            template.validate_assets(self._directory)
        except ValueError as exc:
            raise FormattingTemplateError(
                f"Ошибка в ассетах шаблона {resource.name}: {exc}"
            ) from exc

        return template
