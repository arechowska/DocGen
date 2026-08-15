from __future__ import annotations

from pathlib import Path

import pytest

from docgen.formatting.catalog import FormattingCatalog, FormattingTemplateError
from docgen.formatting.schemas import OutputFormat


def write_yaml(path: Path, content: dict) -> None:
    """Write a dictionary as YAML to a file."""
    import yaml

    path.write_text(yaml.dump(content, default_flow_style=False), encoding="utf-8")


VALID_FORMATTING_TEMPLATE_DOCX = {
    "id": "docgen-light",
    "name": "Светлая тема",
    "format": "docx",
    "renderer": "docx",
    "assets": [],
}

VALID_FORMATTING_TEMPLATE_HTML = {
    "id": "docgen-light",
    "name": "Светлая тема",
    "format": "html",
    "renderer": "html",
    "assets": [],
}


def write_complete_catalog(path: Path) -> None:
    """Write a complete and valid formatting catalog."""
    write_yaml(path / "docx.yaml", VALID_FORMATTING_TEMPLATE_DOCX)
    write_yaml(path / "html.yaml", VALID_FORMATTING_TEMPLATE_HTML)


@pytest.fixture
def catalog(tmp_path: Path) -> FormattingCatalog:
    write_complete_catalog(tmp_path)
    return FormattingCatalog(tmp_path)


def test_catalog_filters_templates_by_format(catalog: FormattingCatalog) -> None:
    """Fails if the catalog cannot filter templates by output format."""
    assert [t.id for t in catalog.list(OutputFormat.DOCX)] == ["docgen-light"]
    assert catalog.get(OutputFormat.HTML, "docgen-light").renderer == "html"


def test_template_cannot_claim_another_format(tmp_path: Path) -> None:
    """Fails if templates with mismatched renderer and format are not rejected."""
    bad_template = {
        "id": "bad",
        "name": "Плохой шаблон",
        "format": "pdf",
        "renderer": "docx",
        "assets": [],
    }
    write_yaml(tmp_path / "bad.yaml", bad_template)
    with pytest.raises(
        FormattingTemplateError, match="Renderer docx несовместим с форматом pdf"
    ):
        FormattingCatalog(tmp_path).list()


def test_catalog_rejects_unknown_fields(tmp_path: Path) -> None:
    """Fails if templates with unknown fields are accepted."""
    bad_template = {
        "id": "bad",
        "name": "Плохой шаблон",
        "format": "docx",
        "renderer": "docx",
        "assets": [],
        "unexpected_field": "value",
    }
    write_yaml(tmp_path / "bad.yaml", bad_template)
    with pytest.raises(FormattingTemplateError, match="unexpected_field"):
        FormattingCatalog(tmp_path).list()


def test_catalog_rejects_duplicate_ids_in_format(tmp_path: Path) -> None:
    """Fails if duplicate template IDs in the same format are not rejected."""
    write_yaml(
        tmp_path / "template1.yaml",
        {
            "id": "docgen-light",
            "name": "Шаблон 1",
            "format": "docx",
            "renderer": "docx",
            "assets": [],
        },
    )
    write_yaml(
        tmp_path / "template2.yaml",
        {
            "id": "docgen-light",
            "name": "Шаблон 2",
            "format": "docx",
            "renderer": "docx",
            "assets": [],
        },
    )
    with pytest.raises(
        FormattingTemplateError,
        match="Повторяющийся идентификатор шаблона в формате docx",
    ):
        FormattingCatalog(tmp_path).list()


def test_catalog_accepts_same_id_in_different_formats(tmp_path: Path) -> None:
    """Fails if the same template ID in different formats is not allowed."""
    write_yaml(
        tmp_path / "template_docx.yaml",
        {
            "id": "docgen-light",
            "name": "Светлая тема",
            "format": "docx",
            "renderer": "docx",
            "assets": [],
        },
    )
    write_yaml(
        tmp_path / "template_html.yaml",
        {
            "id": "docgen-light",
            "name": "Светлая тема",
            "format": "html",
            "renderer": "html",
            "assets": [],
        },
    )
    templates = FormattingCatalog(tmp_path).list()
    assert len(templates) == 2
    assert {t.format for t in templates} == {"docx", "html"}


def test_catalog_rejects_absolute_asset_paths(tmp_path: Path) -> None:
    """Fails if absolute asset paths are accepted."""
    bad_template = {
        "id": "bad",
        "name": "Плохой шаблон",
        "format": "docx",
        "renderer": "docx",
        "assets": ["/absolute/path/file.txt"],
    }
    write_yaml(tmp_path / "bad.yaml", bad_template)
    with pytest.raises(
        FormattingTemplateError, match="Абсолютный путь в ассетах"
    ):
        FormattingCatalog(tmp_path).list()


def test_catalog_rejects_path_traversal_in_assets(tmp_path: Path) -> None:
    """Fails if path traversal (..) in assets is accepted."""
    bad_template = {
        "id": "bad",
        "name": "Плохой шаблон",
        "format": "docx",
        "renderer": "docx",
        "assets": ["../../../etc/passwd"],
    }
    write_yaml(tmp_path / "bad.yaml", bad_template)
    with pytest.raises(FormattingTemplateError, match="'..' в ассетах"):
        FormattingCatalog(tmp_path).list()


def test_catalog_rejects_missing_asset_files(tmp_path: Path) -> None:
    """Fails if references to missing asset files are accepted."""
    bad_template = {
        "id": "bad",
        "name": "Плохой шаблон",
        "format": "docx",
        "renderer": "docx",
        "assets": ["missing_file.txt"],
    }
    write_yaml(tmp_path / "bad.yaml", bad_template)
    with pytest.raises(FormattingTemplateError, match="Ассет не найден"):
        FormattingCatalog(tmp_path).list()


def test_catalog_accepts_valid_asset_files(tmp_path: Path) -> None:
    """Fails if valid asset references are rejected."""
    # Create asset file
    asset_file = tmp_path / "style.css"
    asset_file.write_text("body { color: black; }", encoding="utf-8")

    template = {
        "id": "good",
        "name": "Хороший шаблон",
        "format": "docx",
        "renderer": "docx",
        "assets": ["style.css"],
    }
    write_yaml(tmp_path / "good.yaml", template)
    templates = FormattingCatalog(tmp_path).list()
    assert len(templates) == 1
    assert templates[0].id == "good"


def test_catalog_sorts_by_name_then_id(tmp_path: Path) -> None:
    """Fails if templates are not sorted by name then by id."""
    write_yaml(
        tmp_path / "first.yaml",
        {
            "id": "z-id",
            "name": "Альфа",
            "format": "docx",
            "renderer": "docx",
            "assets": [],
        },
    )
    write_yaml(
        tmp_path / "second.yaml",
        {
            "id": "a-id",
            "name": "Альфа",
            "format": "html",
            "renderer": "html",
            "assets": [],
        },
    )
    write_yaml(
        tmp_path / "third.yaml",
        {
            "id": "middle-id",
            "name": "Зета",
            "format": "markdown",
            "renderer": "markdown",
            "assets": [],
        },
    )

    templates = FormattingCatalog(tmp_path).list()
    # Sorted by name (Альфа, Зета), then by id within same name
    assert [t.id for t in templates] == ["a-id", "z-id", "middle-id"]


def test_catalog_get_by_format_and_id(tmp_path: Path) -> None:
    """Fails if get() method doesn't retrieve templates by format and id."""
    write_yaml(
        tmp_path / "template.yaml",
        {
            "id": "test-template",
            "name": "Тестовый шаблон",
            "format": "docx",
            "renderer": "docx",
            "assets": [],
        },
    )

    catalog = FormattingCatalog(tmp_path)
    template = catalog.get(OutputFormat.DOCX, "test-template")
    assert template.id == "test-template"
    assert template.name == "Тестовый шаблон"


def test_catalog_get_raises_error_for_nonexistent_template(
    tmp_path: Path,
) -> None:
    """Fails if get() doesn't raise an error for nonexistent templates."""
    write_yaml(
        tmp_path / "template.yaml",
        {
            "id": "test-template",
            "name": "Тестовый шаблон",
            "format": "docx",
            "renderer": "docx",
            "assets": [],
        },
    )

    catalog = FormattingCatalog(tmp_path)
    with pytest.raises(FormattingTemplateError, match="Шаблон не найден"):
        catalog.get(OutputFormat.HTML, "nonexistent")


def test_template_id_pattern_validation(tmp_path: Path) -> None:
    """Fails if invalid template ID patterns are accepted."""
    bad_template = {
        "id": "Invalid_ID",  # Contains uppercase and underscore
        "name": "Плохой шаблон",
        "format": "docx",
        "renderer": "docx",
        "assets": [],
    }
    write_yaml(tmp_path / "bad.yaml", bad_template)
    with pytest.raises(FormattingTemplateError, match="String should match pattern"):
        FormattingCatalog(tmp_path).list()


def test_template_rejects_english_name(tmp_path: Path) -> None:
    """Fails if non-Russian names are accepted."""
    bad_template = {
        "id": "bad",
        "name": "English Template",
        "format": "docx",
        "renderer": "docx",
        "assets": [],
    }
    write_yaml(tmp_path / "bad.yaml", bad_template)
    with pytest.raises(FormattingTemplateError, match="кириллицу"):
        FormattingCatalog(tmp_path).list()


def test_template_rejects_name_with_placeholder_markers(tmp_path: Path) -> None:
    """Fails if placeholder markers in names are accepted."""
    bad_template = {
        "id": "bad",
        "name": "TBD",
        "format": "docx",
        "renderer": "docx",
        "assets": [],
    }
    write_yaml(tmp_path / "bad.yaml", bad_template)
    with pytest.raises(FormattingTemplateError, match="заполнитель"):
        FormattingCatalog(tmp_path).list()


def test_template_accepts_valid_russian_name(tmp_path: Path) -> None:
    """Fails if valid Russian names are rejected."""
    good_template = {
        "id": "good",
        "name": "Корпоративный темный стиль",
        "format": "docx",
        "renderer": "docx",
        "assets": [],
    }
    write_yaml(tmp_path / "good.yaml", good_template)
    templates = FormattingCatalog(tmp_path).list()
    assert len(templates) == 1
    assert templates[0].name == "Корпоративный темный стиль"
