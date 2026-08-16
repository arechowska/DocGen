from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from docgen.db import Base
from docgen.documents.models import ProjectArtifact
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.export.protocol import ExportError, RenderedFile
from docgen.export.service import (
    ExportRequest,
    ExportResult,
    ExportService,
    default_exporters,
)
from docgen.export.storage import ExportStorage
from docgen.formatting.catalog import FormattingCatalog
from docgen.formatting.schemas import OutputFormat
from docgen.jobs.models import Job
from docgen.projects.models import Project
from docgen.projects.repository import ProjectRepository
from docgen.sources.models import Source


class _FakeExporter:
    """Test double standing in for a real Exporter: returns a fixed
    RenderedFile, or raises a given error, and counts invocations so tests
    can assert an exporter was (or was not) actually invoked."""

    def __init__(
        self,
        rendered: RenderedFile | None = None,
        error: Exception | None = None,
    ) -> None:
        self._rendered = rendered
        self._error = error
        self.calls = 0

    def render(self, document: WorkingDocument, template: object) -> RenderedFile:
        del document, template
        self.calls += 1
        if self._error is not None:
            raise self._error
        assert self._rendered is not None
        return self._rendered


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=(Project.__table__, Source.__table__, ProjectArtifact.__table__, Job.__table__),
    )
    database_session = Session(engine)
    yield database_session
    database_session.close()
    Base.metadata.drop_all(
        engine,
        tables=(Job.__table__, ProjectArtifact.__table__, Source.__table__, Project.__table__),
    )
    engine.dispose()


@pytest.fixture
def documents(session: Session) -> DocumentRepository:
    return DocumentRepository(session)


@pytest.fixture
def project_id(session: Session) -> str:
    return ProjectRepository(session).create("Проект").id


@pytest.fixture
def catalog_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "catalog"
    directory.mkdir()
    (directory / "asset.txt").write_text("заглушка", encoding="utf-8")
    (directory / "docgen-light-html.yaml").write_text(
        "id: docgen-light\n"
        "name: Облегченный HTML\n"
        "format: html\n"
        "renderer: html\n"
        "assets: [asset.txt]\n",
        encoding="utf-8",
    )
    (directory / "docgen-light-docx.yaml").write_text(
        "id: docgen-light\n"
        "name: Облегченный DOCX\n"
        "format: docx\n"
        "renderer: docx\n"
        "assets: [asset.txt]\n",
        encoding="utf-8",
    )
    return directory


@pytest.fixture
def catalog(catalog_dir: Path) -> FormattingCatalog:
    return FormattingCatalog(catalog_dir)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def storage(data_dir: Path) -> ExportStorage:
    return ExportStorage(data_dir)


def _document(title: str = "Документ") -> WorkingDocument:
    return WorkingDocument(
        title=title,
        template_id="docgen-light",
        nodes=[DocumentNode(kind=NodeKind.PARAGRAPH, text="Текст документа")],
    )


def _save_revisions(documents: DocumentRepository, project_id: str, count: int) -> int:
    """Save `count` document versions in a row, returning the final revision."""
    revision = 0
    for _ in range(count):
        revision = documents.save_document(project_id, _document())
    return revision


def test_export_uses_exact_document_revision(
    documents: DocumentRepository,
    project_id: str,
    catalog: FormattingCatalog,
    storage: ExportStorage,
) -> None:
    revision = _save_revisions(documents, project_id, 4)
    assert revision == 4
    rendered = RenderedFile(
        filename="document.html", media_type="text/html", content=b"<html>ok</html>"
    )
    exporter = _FakeExporter(rendered=rendered)
    service = ExportService(documents, catalog, storage, {OutputFormat.HTML: exporter})

    result = service.export(
        ExportRequest(
            project_id=project_id,
            document_revision=4,
            format=OutputFormat.HTML,
            template_id="docgen-light",
        )
    )

    assert isinstance(result, ExportResult)
    assert result.document_revision == 4
    assert result.media_type == "text/html"
    assert result.filename == "document-docgen-light.html"
    assert result.size_bytes == len(rendered.content)
    stored_path = storage.resolve(result.relative_path)
    assert stored_path.exists()
    assert stored_path.read_bytes() == rendered.content
    assert exporter.calls == 1


def test_export_rejects_stale_document_revision(
    documents: DocumentRepository,
    project_id: str,
    catalog: FormattingCatalog,
    storage: ExportStorage,
) -> None:
    _save_revisions(documents, project_id, 2)  # current revision is now 2
    exporter = _FakeExporter(
        rendered=RenderedFile(filename="d.html", media_type="text/html", content=b"<p>x</p>")
    )
    service = ExportService(documents, catalog, storage, {OutputFormat.HTML: exporter})

    with pytest.raises(ExportError, match="Документ изменён; запустите экспорт повторно"):
        service.export(
            ExportRequest(
                project_id=project_id,
                document_revision=1,
                format=OutputFormat.HTML,
                template_id="docgen-light",
            )
        )

    assert exporter.calls == 0
    _, current_revision = documents.get_document_with_revision(project_id)
    assert current_revision == 2


def test_export_rejects_when_no_document_saved(
    documents: DocumentRepository,
    project_id: str,
    catalog: FormattingCatalog,
    storage: ExportStorage,
) -> None:
    exporter = _FakeExporter(
        rendered=RenderedFile(filename="d.html", media_type="text/html", content=b"<p>x</p>")
    )
    service = ExportService(documents, catalog, storage, {OutputFormat.HTML: exporter})

    with pytest.raises(ExportError, match="Документ изменён; запустите экспорт повторно"):
        service.export(
            ExportRequest(
                project_id=project_id,
                document_revision=1,
                format=OutputFormat.HTML,
                template_id="docgen-light",
            )
        )

    assert exporter.calls == 0


def test_failed_export_preserves_previous_file_and_document(
    documents: DocumentRepository,
    project_id: str,
    catalog: FormattingCatalog,
    storage: ExportStorage,
) -> None:
    revision = _save_revisions(documents, project_id, 4)
    previous_document = documents.get_document(project_id)

    previous_render = RenderedFile(
        filename="document.html", media_type="text/html", content=b"old content"
    )
    seeded = storage.save(project_id, OutputFormat.HTML, "docgen-light", previous_render)
    seeded_path = storage.resolve(seeded.relative_path)
    assert seeded_path.read_bytes() == b"old content"

    failing_exporter = _FakeExporter(error=ExportError("Не удалось сформировать файл"))
    service = ExportService(documents, catalog, storage, {OutputFormat.HTML: failing_exporter})

    with pytest.raises(ExportError):
        service.export(
            ExportRequest(
                project_id=project_id,
                document_revision=revision,
                format=OutputFormat.HTML,
                template_id="docgen-light",
            )
        )

    # the previously-successful export file is byte-for-byte untouched
    assert seeded_path.read_bytes() == b"old content"
    # no leftover partial file from the failed attempt
    part_path = seeded_path.parent / ".html-docgen-light.part"
    assert not part_path.exists()
    # the project's working document and revision are completely untouched
    assert documents.get_document(project_id) == previous_document
    _, current_revision = documents.get_document_with_revision(project_id)
    assert current_revision == revision


def test_export_rejects_empty_rendered_content(
    documents: DocumentRepository,
    project_id: str,
    catalog: FormattingCatalog,
    storage: ExportStorage,
    data_dir: Path,
) -> None:
    revision = _save_revisions(documents, project_id, 1)
    exporter = _FakeExporter(
        rendered=RenderedFile(filename="d.html", media_type="text/html", content=b"")
    )
    service = ExportService(documents, catalog, storage, {OutputFormat.HTML: exporter})

    with pytest.raises(ExportError, match="Экспорт вернул пустой файл"):
        service.export(
            ExportRequest(
                project_id=project_id,
                document_revision=revision,
                format=OutputFormat.HTML,
                template_id="docgen-light",
            )
        )

    # nothing was written -- the exports directory was never even created
    assert not (data_dir / "projects" / project_id / "exports").exists()


def test_export_rejects_invalid_binary_signature(
    documents: DocumentRepository,
    project_id: str,
    catalog: FormattingCatalog,
    storage: ExportStorage,
) -> None:
    revision = _save_revisions(documents, project_id, 1)
    exporter = _FakeExporter(
        rendered=RenderedFile(
            filename="d.docx",
            media_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            content=b"not actually a zip archive",
        )
    )
    service = ExportService(documents, catalog, storage, {OutputFormat.DOCX: exporter})

    with pytest.raises(ExportError, match="Экспорт вернул повреждённый файл"):
        service.export(
            ExportRequest(
                project_id=project_id,
                document_revision=revision,
                format=OutputFormat.DOCX,
                template_id="docgen-light",
            )
        )


def test_export_rejects_missing_exporter_for_format(
    documents: DocumentRepository,
    project_id: str,
    catalog: FormattingCatalog,
    storage: ExportStorage,
) -> None:
    revision = _save_revisions(documents, project_id, 1)
    service = ExportService(documents, catalog, storage, {})

    with pytest.raises(ExportError, match="Формат экспорта не поддерживается"):
        service.export(
            ExportRequest(
                project_id=project_id,
                document_revision=revision,
                format=OutputFormat.HTML,
                template_id="docgen-light",
            )
        )


def test_default_exporters_maps_every_output_format() -> None:
    registry = default_exporters()

    assert set(registry) == set(OutputFormat)
