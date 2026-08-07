from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from docgen.templates_catalog.schemas import SemanticTemplate

REQUIRED_TEMPLATE_IDS = frozenset(
    {"faq", "use-case", "technical-spec", "release-notes", "api-docs"}
)


class TemplateConfigurationError(ValueError):
    """Raised when a semantic-template catalog cannot be loaded safely."""


class _StrictYamlLoader(yaml.SafeLoader):
    pass


def _construct_mapping_without_duplicate_keys(
    loader: _StrictYamlLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise TemplateConfigurationError(f"Повторяющийся ключ YAML: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictYamlLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping_without_duplicate_keys
)


class TemplateCatalog:
    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory

    def list(self) -> list[SemanticTemplate]:
        templates: list[SemanticTemplate] = []
        template_ids: set[str] = set()
        for resource in self._yaml_resources():
            template = self._load_template(resource)
            if template.id in template_ids:
                raise TemplateConfigurationError(
                    f"Повторяющийся идентификатор шаблона: {template.id}"
                )
            template_ids.add(template.id)
            templates.append(template)
        if template_ids != REQUIRED_TEMPLATE_IDS:
            missing_ids = sorted(REQUIRED_TEMPLATE_IDS - template_ids)
            unexpected_ids = sorted(template_ids - REQUIRED_TEMPLATE_IDS)
            details: list[str] = []
            if missing_ids:
                details.append("отсутствуют: " + ", ".join(missing_ids))
            if unexpected_ids:
                details.append("лишние: " + ", ".join(unexpected_ids))
            raise TemplateConfigurationError(
                "Полный набор идентификаторов шаблонов должен быть "
                + ", ".join(sorted(REQUIRED_TEMPLATE_IDS))
                + "; "
                + "; ".join(details)
            )
        return templates

    def get(self, template_id: str) -> SemanticTemplate:
        for template in self.list():
            if template.id == template_id:
                return template
        raise TemplateConfigurationError(f"Шаблон не найден: {template_id}")

    def _yaml_resources(self) -> list[Any]:
        directory = self._directory or files("docgen.templates_catalog").joinpath("semantic")
        try:
            resources = [resource for resource in directory.iterdir() if resource.name.endswith(".yaml")]
        except FileNotFoundError as exc:
            raise TemplateConfigurationError(f"Каталог шаблонов не найден: {directory}") from exc
        return sorted(resources, key=lambda resource: resource.name)

    @staticmethod
    def _load_template(resource: Any) -> SemanticTemplate:
        try:
            raw_template = yaml.load(resource.read_text(encoding="utf-8"), Loader=_StrictYamlLoader)
        except (OSError, yaml.YAMLError, TemplateConfigurationError) as exc:
            raise TemplateConfigurationError(f"Некорректный YAML-шаблон {resource.name}: {exc}") from exc

        if not isinstance(raw_template, dict):
            raise TemplateConfigurationError(f"Шаблон {resource.name} должен содержать YAML-объект")

        try:
            return SemanticTemplate.model_validate(raw_template)
        except ValidationError as exc:
            raise TemplateConfigurationError(f"Некорректный шаблон {resource.name}: {exc}") from exc
