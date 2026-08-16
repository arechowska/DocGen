"""Export service: renders one exact document revision and stores it atomically.

Per the Global Constraints, export uses a specific saved document revision
and never runs the AI model, and an export failure never mutates or deletes
the project or working document. Rendering happens fully in memory (via the
format's `Exporter`) before anything is written, and the write itself is
atomic (`ExportStorage`), so a failure at any stage leaves the project, the
working document, and any previously-written successful export untouched.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from pydantic import BaseModel

from docgen.documents.repository import DocumentRepository
from docgen.export.protocol import Exporter, ExportError
from docgen.export.storage import ExportStorage
from docgen.formatting.catalog import FormattingCatalog
from docgen.formatting.schemas import OutputFormat

if TYPE_CHECKING:
    from pathlib import Path

    from docgen.export.html import ImageLoader

_REVISION_CHANGED_MESSAGE = "Документ изменён; запустите экспорт повторно"
_EMPTY_CONTENT_MESSAGE = "Экспорт вернул пустой файл"
_INVALID_CONTENT_MESSAGE = "Экспорт вернул повреждённый файл"
_UNSUPPORTED_FORMAT_MESSAGE = "Формат экспорта не поддерживается"

# Magic-byte signatures for binary formats. Text formats (HTML/Markdown) are
# instead verified by confirming their content decodes as UTF-8.
_BINARY_SIGNATURES: dict[OutputFormat, bytes] = {
    OutputFormat.DOCX: b"PK\x03\x04",
    OutputFormat.PDF: b"%PDF-",
}
_TEXT_FORMATS = frozenset({OutputFormat.HTML, OutputFormat.MARKDOWN})


class ExportRequest(BaseModel):
    """A request to export one exact document revision to a format/template."""

    project_id: str
    document_revision: int
    format: OutputFormat
    template_id: str


class ExportResult(BaseModel):
    """Metadata describing a successfully written export file."""

    relative_path: str
    filename: str
    media_type: str
    size_bytes: int
    document_revision: int


class ExportService:
    """Renders a `WorkingDocument` to a chosen format/template and stores it."""

    def __init__(
        self,
        documents: DocumentRepository,
        templates: FormattingCatalog,
        storage: ExportStorage,
        exporters: Mapping[OutputFormat, Exporter],
    ) -> None:
        self._documents = documents
        self._templates = templates
        self._storage = storage
        self._exporters = exporters

    def export(self, request: ExportRequest) -> ExportResult:
        document = self._documents.get_document_at_revision(
            request.project_id, request.document_revision
        )
        if document is None:
            raise ExportError(_REVISION_CHANGED_MESSAGE)

        template = self._templates.get(request.format, request.template_id)
        exporter = self._exporters.get(request.format)
        if exporter is None:
            raise ExportError(_UNSUPPORTED_FORMAT_MESSAGE)

        rendered = exporter.render(document, template)
        self._verify_rendered(request.format, rendered.content)

        stored = self._storage.save(
            request.project_id, request.format, request.template_id, rendered
        )
        return ExportResult(
            relative_path=stored.relative_path,
            filename=stored.filename,
            media_type=rendered.media_type,
            size_bytes=stored.size_bytes,
            document_revision=request.document_revision,
        )

    @staticmethod
    def _verify_rendered(format: OutputFormat, content: bytes) -> None:
        if not content:
            raise ExportError(_EMPTY_CONTENT_MESSAGE)

        signature = _BINARY_SIGNATURES.get(format)
        if signature is not None:
            if not content.startswith(signature):
                raise ExportError(_INVALID_CONTENT_MESSAGE)
            return

        if format in _TEXT_FORMATS:
            try:
                content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ExportError(_INVALID_CONTENT_MESSAGE) from exc


def default_exporters(
    *,
    image_loader: ImageLoader | None = None,
    templates_dir: Path | None = None,
) -> dict[OutputFormat, Exporter]:
    """Build the standard `OutputFormat` -> `Exporter` registry.

    Maps each `OutputFormat` to exactly one exporter, matching the
    `FormattingCatalog`'s format/renderer pairing. `image_loader` and
    `templates_dir` are forwarded to the exporters that embed images or load
    template assets from disk (all but Markdown).
    """
    from docgen.export.docx import DocxExporter
    from docgen.export.html import HtmlExporter
    from docgen.export.markdown import MarkdownExporter
    from docgen.export.pdf import PdfExporter

    return {
        OutputFormat.MARKDOWN: MarkdownExporter(),
        OutputFormat.HTML: HtmlExporter(image_loader=image_loader, templates_dir=templates_dir),
        OutputFormat.DOCX: DocxExporter(image_loader=image_loader, templates_dir=templates_dir),
        OutputFormat.PDF: PdfExporter(image_loader=image_loader, templates_dir=templates_dir),
    }


__all__ = ["ExportRequest", "ExportResult", "ExportService", "default_exporters"]
