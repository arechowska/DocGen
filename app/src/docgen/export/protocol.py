"""Export protocol for rendering documents to various formats."""

from typing import Protocol

from pydantic import BaseModel

from docgen.documents.schemas import WorkingDocument
from docgen.formatting.schemas import FormattingTemplate


class ExportError(Exception):
    """Raised when rendering a document to an output format fails.

    Exporters raise this for renderer/engine-level failures (e.g. a PDF
    engine crash) instead of letting the underlying exception type leak.
    Per the Global Constraints, an export failure must never mutate or
    delete the project or working document -- rendering happens fully in
    memory before any output is written, so raising this never leaves
    partial state behind.
    """


class RenderedFile(BaseModel):
    """Result of rendering a document to a specific format."""

    filename: str
    """Filesystem-safe filename for the rendered content."""

    media_type: str
    """MIME type of the rendered content.

    Examples: 'text/markdown',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document'.
    """

    content: bytes
    """Raw bytes of the rendered content, encoded according to the format."""


class Exporter(Protocol):
    """Protocol for exporting a WorkingDocument to a specific format."""

    def render(
        self, document: WorkingDocument, template: FormattingTemplate
    ) -> RenderedFile:
        """Render a document using the given formatting template.

        Args:
            document: The WorkingDocument to render.
            template: The FormattingTemplate that defines styling and rendering options.

        Returns:
            RenderedFile containing the rendered content, filename, and media type.
        """
        ...
