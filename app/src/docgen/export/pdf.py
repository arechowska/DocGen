"""PDF exporter for rendering WorkingDocuments as PDF files.

HTML/CSS templates retain the existing WeasyPrint pipeline. Templates based
on a real DOCX asset are rendered by :class:`DocxExporter` first and converted
with LibreOffice, which keeps the supplied corporate Word layout intact.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from docgen.documents.schemas import WorkingDocument
from docgen.export._naming import make_safe_filename
from docgen.export.docx import DocxExporter
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
    """Exports WorkingDocuments to PDF.

    HTML/CSS templates use the existing WeasyPrint renderer. DOCX templates
    are rendered through the same DOCX exporter used for Word downloads and
    converted with LibreOffice, so Word and PDF have one source of layout.
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
            templates_dir: Directory containing the assets named by a
                FormattingTemplate. Defaults to the built-in catalog.
        """
        self._templates_dir = (templates_dir or _TEMPLATES_DIR).resolve()
        self._image_loader = image_loader
        self._html_exporter = HtmlExporter(
            image_loader=image_loader, templates_dir=self._templates_dir
        )

    def render(
        self, document: WorkingDocument, template: FormattingTemplate
    ) -> RenderedFile:
        """Render a document to a print-styled PDF file.

        Args:
            document: The WorkingDocument to render.
            template: The FormattingTemplate naming either HTML/CSS or DOCX
                assets.

        Returns:
            RenderedFile with the PDF content.

        Raises:
            ExportError: If the selected rendering engine fails. Nothing is
                written to project storage in that case.
        """
        if any(asset.endswith(".docx") for asset in template.assets):
            return self._render_from_docx_template(document, template)

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

    def _render_from_docx_template(
        self, document: WorkingDocument, template: FormattingTemplate
    ) -> RenderedFile:
        """Convert the Word-template render so PDF matches its source layout."""
        office = shutil.which("soffice") or shutil.which("libreoffice")
        if office is None:
            raise ExportError("Не найден LibreOffice для формирования PDF")

        rendered_docx = DocxExporter(
            image_loader=self._image_loader,
            templates_dir=self._templates_dir,
        ).render(document, template)
        with TemporaryDirectory(prefix="docgen-pdf-") as temporary_directory:
            workspace = Path(temporary_directory)
            docx_path = workspace / "document.docx"
            output_dir = workspace / "output"
            profile_dir = workspace / "profile"
            output_dir.mkdir()
            profile_dir.mkdir()
            docx_path.write_bytes(rendered_docx.content)
            try:
                subprocess.run(
                    [
                        office,
                        "--headless",
                        f"-env:UserInstallation={profile_dir.as_uri()}",
                        "--convert-to",
                        "pdf:writer_pdf_Export",
                        "--outdir",
                        str(output_dir),
                        str(docx_path),
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=90,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ExportError("Не удалось сформировать PDF из Word-шаблона") from exc
            pdf_path = output_dir / "document.pdf"
            if not pdf_path.is_file():
                raise ExportError("Не удалось сформировать PDF из Word-шаблона")
            pdf_bytes = pdf_path.read_bytes()

        return RenderedFile(
            filename=make_safe_filename(
                document.title, ".pdf", reserved_suffix=f"-{template.id}"
            ),
            media_type=_MEDIA_TYPE,
            content=pdf_bytes,
        )
