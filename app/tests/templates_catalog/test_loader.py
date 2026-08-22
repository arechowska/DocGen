from __future__ import annotations

from pathlib import Path

import pytest

from docgen.templates_catalog.loader import TemplateCatalog, TemplateConfigurationError
from docgen.templates_catalog.use_case import (
    USE_CASE_DESCRIPTION_ROWS,
    USE_CASE_FORM_HEADINGS,
    USE_CASE_HISTORY_HEADERS,
    USE_CASE_LINK_HEADERS,
    USE_CASE_METADATA_FIELDS,
    USE_CASE_TECHNICAL_HEADERS,
)

REQUIRED_TEMPLATE_IDS = {
    "faq",
    "use-case",
    "technical-spec",
    "release-notes",
    "api-docs",
}

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


def template_with_id(template_id: str) -> str:
    return VALID_TEMPLATE.replace("id: valid-template", f"id: {template_id}", 1)


def write_complete_catalog(path: Path) -> None:
    for template_id in REQUIRED_TEMPLATE_IDS:
        write_yaml(path / f"{template_id}.yaml", template_with_id(template_id))


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

    assert template.name == "Use Case"
    assert len(template.sections) >= 3


def test_use_case_structure_check_matches_corporate_export_form(
    catalog: TemplateCatalog,
) -> None:
    contract = catalog.get("use-case").structure_check
    assert contract is not None
    tables = {table.id: table.required_labels for table in contract.required_tables}

    assert contract.required_headings == USE_CASE_FORM_HEADINGS
    assert tables["metadata"] == USE_CASE_METADATA_FIELDS
    assert tables["description"] == tuple(
        dict.fromkeys(label for label, _section_id in USE_CASE_DESCRIPTION_ROWS)
    )
    assert tables["technical-specifications"] == USE_CASE_TECHNICAL_HEADERS
    assert tables["links"] == USE_CASE_LINK_HEADERS
    assert tables["change-history"] == USE_CASE_HISTORY_HEADERS


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
    write_complete_catalog(tmp_path)
    duplicate_rule = template_with_id("faq").replace(
        "- id: completeness-rule", "- id: structure-rule", 1
    )
    write_yaml(tmp_path / "faq.yaml", duplicate_rule)

    with pytest.raises(TemplateConfigurationError, match="Повторяющийся идентификатор правила"):
        TemplateCatalog(tmp_path).list()


def test_catalog_rejects_duplicate_template_ids(tmp_path: Path) -> None:
    """Fails if get() would have two candidates for the same template id."""
    write_complete_catalog(tmp_path)
    write_yaml(tmp_path / "duplicate.yaml", template_with_id("faq"))

    with pytest.raises(TemplateConfigurationError, match="Повторяющийся идентификатор шаблона"):
        TemplateCatalog(tmp_path).list()


def test_catalog_rejects_unknown_yaml_fields(tmp_path: Path) -> None:
    """Fails if a template typo silently becomes ignored configuration."""
    write_complete_catalog(tmp_path)
    write_yaml(tmp_path / "faq.yaml", f"{template_with_id('faq')}\nunexpected: value\n")

    with pytest.raises(TemplateConfigurationError, match="unexpected"):
        TemplateCatalog(tmp_path).list()


def test_catalog_accepts_a_custom_catalog_with_any_template_ids(tmp_path: Path) -> None:
    write_yaml(tmp_path / "custom.yaml", template_with_id("custom"))


    assert [template.id for template in TemplateCatalog(tmp_path).list()] == ["custom"]


def test_catalog_adds_external_template_to_bundled_templates(tmp_path: Path) -> None:
    write_yaml(tmp_path / "other.yaml", template_with_id("other"))

    assert {template.id for template in TemplateCatalog(external_directory=tmp_path).list()} == (
        REQUIRED_TEMPLATE_IDS | {"other"}
    )


def test_external_template_replaces_bundled_template_with_the_same_id(tmp_path: Path) -> None:
    replacement = template_with_id("faq").replace(
        "name: Корректный шаблон", "name: Корпоративный FAQ", 1
    )
    write_yaml(tmp_path / "faq.yaml", replacement)

    assert TemplateCatalog(external_directory=tmp_path).get("faq").name == "Корпоративный FAQ"


def test_catalog_rejects_english_section_title(tmp_path: Path) -> None:
    """Fails if a user-facing section title does not contain Russian text."""
    write_complete_catalog(tmp_path)
    english_title = template_with_id("faq").replace("title: Обзор", "title: Overview", 1)
    write_yaml(tmp_path / "faq.yaml", english_title)

    with pytest.raises(TemplateConfigurationError, match="кириллицу"):
        TemplateCatalog(tmp_path).list()


@pytest.mark.parametrize("name", ["FAQ", "Release notes", "API Docs"])
def test_catalog_accepts_product_facing_template_name(tmp_path: Path, name: str) -> None:
    write_complete_catalog(tmp_path)
    product_name = template_with_id("faq").replace(
        "name: Корректный шаблон", f"name: {name}", 1
    )
    write_yaml(tmp_path / "faq.yaml", product_name)

    assert TemplateCatalog(tmp_path).get("faq").name == name


def test_catalog_rejects_english_section_description(tmp_path: Path) -> None:
    """Fails if a user-facing section description does not contain Russian text."""
    write_complete_catalog(tmp_path)
    english_description = template_with_id("faq").replace(
        "Кратко описывает назначение документа.", "Describes the document purpose.", 1
    )
    write_yaml(tmp_path / "faq.yaml", english_description)

    with pytest.raises(TemplateConfigurationError, match="кириллицу"):
        TemplateCatalog(tmp_path).list()


@pytest.mark.parametrize(
    ("source", "replacement"),
    [
        ("title: Обзор", "title: English title Я"),
        ("Кратко описывает назначение документа.", "English description Я"),
        ("Проверьте порядок обязательных разделов.", "English instruction Я"),
        (
            "Используйте нейтральный деловой стиль.",
            "English style instruction Я",
        ),
    ],
)
def test_catalog_rejects_text_without_cyrillic_majority(
    tmp_path: Path, source: str, replacement: str
) -> None:
    """Fails if one Cyrillic letter can disguise otherwise English template text."""
    write_complete_catalog(tmp_path)
    mixed_language_text = template_with_id("faq").replace(source, replacement, 1)
    write_yaml(tmp_path / "faq.yaml", mixed_language_text)

    with pytest.raises(TemplateConfigurationError, match="не менее 50%"):
        TemplateCatalog(tmp_path).list()


@pytest.mark.parametrize(
    ("source", "replacement"),
    [
        ("Кратко описывает назначение документа.", "Кратко"),
        ("Используйте нейтральный деловой стиль.", "Кратко"),
    ],
)
def test_catalog_accepts_short_russian_description_and_style_rule(
    tmp_path: Path, source: str, replacement: str
) -> None:
    """Fails if the concrete-instruction limit is applied outside rule instructions."""
    write_complete_catalog(tmp_path)
    short_russian_text = template_with_id("faq").replace(source, replacement, 1)
    write_yaml(tmp_path / "faq.yaml", short_russian_text)

    assert TemplateCatalog(tmp_path).get("faq").id == "faq"


def test_catalog_rejects_placeholder_rule_instruction(tmp_path: Path) -> None:
    """Fails if a rule ships with a placeholder instead of an evaluable instruction."""
    write_complete_catalog(tmp_path)
    placeholder_instruction = template_with_id("faq").replace(
        "Проверьте порядок обязательных разделов.", "TBD", 1
    )
    write_yaml(tmp_path / "faq.yaml", placeholder_instruction)

    with pytest.raises(TemplateConfigurationError, match="заполнитель"):
        TemplateCatalog(tmp_path).list()


def test_catalog_exposes_template_collections_as_immutable_tuples(catalog: TemplateCatalog) -> None:
    """Fails if callers can mutate a selected template's catalog data."""
    template = catalog.get("faq")

    assert isinstance(template.sections, tuple)
    assert isinstance(template.rules, tuple)
    assert isinstance(template.style_rules, tuple)
    with pytest.raises(AttributeError):
        template.sections.append(template.sections[0])
