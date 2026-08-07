from __future__ import annotations

from pathlib import Path

import pytest

from docgen.templates_catalog.loader import TemplateCatalog, TemplateConfigurationError

VALID_TEMPLATE = """
id: valid-template
name: Корректный шаблон
version: "1.0"
sections:
  - id: overview
    title: Обзор
    required: true
    description: Кратко описывает назначение документа.
  - id: details
    title: Детали
    required: true
    description: Содержит проверяемые сведения из исходных материалов.
  - id: outcome
    title: Результат
    required: true
    description: Фиксирует ожидаемый результат.
rules:
  - id: structure-rule
    dimension: structure
    severity: error
    instruction: Проверьте порядок обязательных разделов.
  - id: completeness-rule
    dimension: completeness
    severity: error
    instruction: Проверьте, что сведения для обязательных разделов не пропущены.
  - id: terminology-rule
    dimension: terminology
    severity: warning
    instruction: Проверьте единообразие терминов из исходных материалов.
  - id: contradiction-rule
    dimension: contradiction
    severity: error
    instruction: Проверьте отсутствие противоречий между утверждениями документа.
  - id: style-rule
    dimension: style
    severity: warning
    instruction: Проверьте нейтральный деловой стиль формулировок.
style_rules:
  - Используйте нейтральный деловой стиль.
"""


def write_yaml(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def catalog() -> TemplateCatalog:
    return TemplateCatalog()


def test_catalog_loads_five_templates_in_file_name_order(catalog: TemplateCatalog) -> None:
    """Fails if a bundled template is absent or filesystem ordering leaks into the API."""
    templates = catalog.list()

    assert [template.id for template in templates] == [
        "api-docs",
        "faq",
        "release-notes",
        "technical-spec",
        "use-case",
    ]


def test_catalog_get_returns_template_by_id(catalog: TemplateCatalog) -> None:
    """Fails if lookup does not return the loaded semantic template."""
    template = catalog.get("use-case")

    assert template.name == "Сценарий использования"
    assert len(template.sections) >= 3


def test_catalog_default_path_is_independent_from_current_directory(
    catalog: TemplateCatalog, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fails if the default loader relies on the process working directory."""
    monkeypatch.chdir(tmp_path)

    assert {template.id for template in catalog.list()} == {
        "faq",
        "use-case",
        "technical-spec",
        "release-notes",
        "api-docs",
    }


def test_catalog_rejects_duplicate_rule_ids(tmp_path: Path) -> None:
    """Fails if a checker cannot unambiguously identify a duplicate rule."""
    duplicate_rule = VALID_TEMPLATE.replace(
        "- id: completeness-rule", "- id: structure-rule", 1
    )
    write_yaml(tmp_path / "bad.yaml", duplicate_rule)

    with pytest.raises(TemplateConfigurationError, match="Повторяющийся идентификатор правила"):
        TemplateCatalog(tmp_path).list()


def test_catalog_rejects_duplicate_template_ids(tmp_path: Path) -> None:
    """Fails if get() would have two candidates for the same template id."""
    write_yaml(tmp_path / "first.yaml", VALID_TEMPLATE)
    write_yaml(tmp_path / "second.yaml", VALID_TEMPLATE)

    with pytest.raises(TemplateConfigurationError, match="Повторяющийся идентификатор шаблона"):
        TemplateCatalog(tmp_path).list()


def test_catalog_rejects_unknown_yaml_fields(tmp_path: Path) -> None:
    """Fails if a template typo silently becomes ignored configuration."""
    write_yaml(tmp_path / "bad.yaml", f"{VALID_TEMPLATE}\nunexpected: value\n")

    with pytest.raises(TemplateConfigurationError, match="unexpected"):
        TemplateCatalog(tmp_path).list()
