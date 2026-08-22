import pytest
from pydantic import ValidationError

from docgen.documents.schemas import CheckReport, DocumentOrigin, WorkingDocument


def test_legacy_template_id_migrates_to_assembled_identity() -> None:
    document = WorkingDocument.model_validate(
        {"title": "Сценарий", "template_id": "use-case", "nodes": []}
    )

    assert document.origin is DocumentOrigin.ASSEMBLED
    assert document.build_template_id == "use-case"
    assert document.source_id is None


def test_imported_document_has_source_but_no_build_template() -> None:
    document = WorkingDocument(
        title="Старый документ",
        template_id="no-template",
        origin=DocumentOrigin.IMPORTED,
        source_id="source-1",
        nodes=[],
    )

    assert document.build_template_id is None
    assert document.template_id == "no-template"


def test_imported_document_cannot_claim_build_template() -> None:
    with pytest.raises(ValidationError, match="Импортированный"):
        WorkingDocument(
            title="Старый документ",
            template_id="use-case",
            origin=DocumentOrigin.IMPORTED,
            source_id="source-1",
            build_template_id="use-case",
            nodes=[],
        )


def test_assembled_document_requires_matching_legacy_template_id() -> None:
    with pytest.raises(ValidationError, match="не совпадает"):
        WorkingDocument(
            title="Сценарий",
            template_id="faq",
            origin=DocumentOrigin.ASSEMBLED,
            build_template_id="use-case",
        )


def test_check_report_exposes_independent_profile_identity() -> None:
    report = CheckReport.model_validate(
        {"template_id": "use-case", "findings": []}
    )

    assert report.check_profile_id == "use-case"
