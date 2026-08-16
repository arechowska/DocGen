"""PDF exporter for rendering WorkingDocuments as print-styled PDF files.

Reuses HtmlExporter's safe node-rendering (the same `render_node` Jinja
macro and image-resolution contract used by html.py) to build a
print-specific HTML document, then rasterizes it to PDF with WeasyPrint.
docgen-light-pdf.html.j2 imports the `render_node` macro straight out of
docgen-light.html.j2 rather than re-implementing node dispatch, so this
exporter never duplicates HTML-escaping or table/gap/image edge-case
handling -- it only supplies a different template/CSS pair and a
print-specific rasterization step.
"""

from __future__ import annotations

from pathlib import Path

from docgen.documents.schemas import WorkingDocument
from docgen.export._naming import make_safe_filename
from docgen.export.html import HtmlExporter, ImageAsset, ImageLoader, local_storage_image_loader
from docgen.export.protocol import ExportError, RenderedFile
from docgen.formatting.schemas import FormattingTemplate

__all__ = [
    "ExportError",
    "ImageAsset",
    "ImageLoader",
    "PdfExporter",
    "local_storage_image_loader",
]

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "formatting" / "templates"

_MEDIA_TYPE = "application/pdf"


class PdfExporter:
    """Exports WorkingDocuments to print-styled PDF (.pdf) files.

    Delegates all node rendering to an internal HtmlExporter (same
    image-resolution contract: an unresolvable/missing src always falls
    back to the standard placeholder, never fabricated or fetched
    externally) configured with the PDF template's `.html.j2`/`.css`
    assets, then feeds the resulting self-contained HTML string to
    WeasyPrint. Rendering happens fully in memory; a WeasyPrint engine
    failure is mapped to ExportError and never touches project state.
    """

    def __init__(
        self,
        image_loader: ImageLoader | None = None,
        templates_dir: Path | None = None,
    ) -> None:
        """Create a PdfExporter.

        Args:
            image_loader: Resolves a node's image src to bytes + MIME type.
                When None (the default), all images render as placeholders.
            templates_dir: Directory containing the `.html.j2`/`.css` assets
                named by a FormattingTemplate's `assets` list. Defaults to
                the built-in formatting/templates catalog directory.
        """
        self._templates_dir = (templates_dir or _TEMPLATES_DIR).resolve()
        self._html_exporter = HtmlExporter(
            image_loader=image_loader, templates_dir=self._templates_dir
        )

    def render(
        self, document: WorkingDocument, template: FormattingTemplate
    ) -> RenderedFile:
        """Render a document to a print-styled PDF file.

        Args:
            document: The WorkingDocument to render.
            template: The FormattingTemplate naming the `.html.j2` and
                `.css` print assets to use.

        Returns:
            RenderedFile with the PDF content.

        Raises:
            ExportError: If the WeasyPrint engine fails to rasterize the
                rendered HTML. Nothing is written to disk in that case.
        """
        html_rendered = self._html_exporter.render(document, template)
        html = html_rendered.content.decode("utf-8")

        # Imported lazily (not at module scope) so that merely *importing*
        # this module -- which `export.service.default_exporters()` does at
        # worker startup for every job kind, not just EXPORT -- never
        # requires WeasyPrint's system libraries (Pango/HarfBuzz, loaded via
        # dlopen at import time) to be present. If they're ever missing in
        # some environment, only a PDF render fails here; ASSEMBLE/CHECK and
        # the other export formats are unaffected. The Dockerfile is the
        # primary fix (it installs those libraries); this is defense in
        # depth on top of that.
        try:
            from weasyprint import HTML

            pdf_bytes = HTML(string=html, base_url=str(self._templates_dir)).write_pdf()
        except Exception as exc:
            raise ExportError("Не удалось сформировать PDF") from exc

        return RenderedFile(
            filename=make_safe_filename(
                document.title, ".pdf", reserved_suffix=f"-{template.id}"
            ),
            media_type=_MEDIA_TYPE,
            content=pdf_bytes,
        )
