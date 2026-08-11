from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from docgen.templates_catalog.schemas import SemanticTemplate


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
    def __init__(
        self,
        directory: Path | None = None,
        *,
        external_directory: Path | None = None,
    ) -> None:
        self._directory = directory
        self._external_directory = external_directory

    def list(self) -> list[SemanticTemplate]:
        templates = self._load_directory(self._base_directory())
        if self._external_directory is not None:
            templates.update(self._load_directory(self._external_directory))
        return [templates[template_id] for template_id in sorted(templates)]

    def get(self, template_id: str) -> SemanticTemplate:
        for template in self.list():
            if template.id == template_id:
                return template
        raise TemplateConfigurationError(f"Шаблон не найден: {template_id}")

    def _base_directory(self) -> Any:
        return self._directory or files("docgen.templates_catalog").joinpath("semantic")

    @classmethod
    def _load_directory(cls, directory: Any) -> dict[str, SemanticTemplate]:
        templates: dict[str, SemanticTemplate] = {}
        for resource in cls._yaml_resources(directory):
            template = cls._load_template(resource)
            if template.id in templates:
                raise TemplateConfigurationError(
                    f"Повторяющийся идентификатор шаблона: {template.id}"
                )
            templates[template.id] = template
        return templates

    @staticmethod
    def _yaml_resources(directory: Any) -> list[Any]:
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
