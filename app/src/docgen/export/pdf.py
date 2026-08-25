"""PDF exporter for rendering WorkingDocuments as PDF files.

HTML/CSS templates retain the existing WeasyPrint pipeline. Templates based
on a real DOCX asset are rendered by :class:`DocxExporter` first and converted
with LibreOffice, which keeps the supplied corporate Word layout intact.
"""

from __future__ import annotations

import shutil
import subprocess
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

import pymupdf
from lxml import etree

from docgen.documents.schemas import WorkingDocument
from docgen.export._naming import make_safe_filename
from docgen.export.docx import DocxExporter, filename_title
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
_WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_WORD_NAMESPACES = {"w": _WORD_NAMESPACE}


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
            self._convert_docx_to_pdf(
                office, docx_path, output_dir, profile_dir
            )
            pdf_path = output_dir / "document.pdf"
            if not pdf_path.is_file():
                raise ExportError("Не удалось сформировать PDF из Word-шаблона")
            pdf_bytes = pdf_path.read_bytes()
            page_numbers = _toc_destination_page_labels(pdf_bytes)
            updated_docx = _replace_cached_toc_page_numbers(
                rendered_docx.content, page_numbers
            )
            if updated_docx is not None:
                docx_path.write_bytes(updated_docx)
                second_profile_dir = workspace / "profile-final"
                second_profile_dir.mkdir()
                self._convert_docx_to_pdf(
                    office, docx_path, output_dir, second_profile_dir
                )
                if not pdf_path.is_file():
                    raise ExportError("Не удалось сформировать PDF из Word-шаблона")
                pdf_bytes = pdf_path.read_bytes()

        return RenderedFile(
            filename=make_safe_filename(
                filename_title(document), ".pdf", reserved_suffix=f"-{template.id}"
            ),
            media_type=_MEDIA_TYPE,
            content=pdf_bytes,
        )

    @staticmethod
    def _convert_docx_to_pdf(
        office: str,
        docx_path: Path,
        output_dir: Path,
        profile_dir: Path,
    ) -> None:
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


def _toc_destination_page_labels(pdf_bytes: bytes) -> list[str]:
    """Read TOC destinations in their visual order from LibreOffice's PDF."""
    pdf = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        destinations: list[tuple[int, float, float, int]] = []
        for source_page, page in enumerate(pdf):
            for link in page.get_links():
                destination = link.get("page")
                source_rect = link.get("from")
                if (
                    link.get("kind") != pymupdf.LINK_GOTO
                    or not isinstance(destination, int)
                    or destination < 0
                    or destination >= len(pdf)
                    or source_rect is None
                ):
                    continue
                destinations.append(
                    (source_page, float(source_rect.y0), float(source_rect.x0), destination)
                )
        destinations.sort(key=lambda item: item[:3])
        return [pdf[destination].get_label() or str(destination + 1) for *_, destination in destinations]
    finally:
        pdf.close()


def _replace_cached_toc_page_numbers(
    docx_bytes: bytes, page_numbers: list[str]
) -> bytes | None:
    """Replace cached PAGEREF results when every TOC destination was resolved."""
    source = BytesIO(docx_bytes)
    with ZipFile(source) as package:
        document_xml = package.read("word/document.xml")
        root = etree.fromstring(document_xml)
        instructions = root.xpath(
            ".//w:instrText[contains(normalize-space(.), 'PAGEREF')]",
            namespaces=_WORD_NAMESPACES,
        )
        if not instructions or len(instructions) != len(page_numbers):
            return None

        for instruction, page_number in zip(instructions, page_numbers, strict=True):
            run = instruction.getparent()
            container = run.getparent()
            found_separator = False
            updated = False
            for sibling in list(container)[container.index(run) + 1 :]:
                field_char = sibling.find("w:fldChar", namespaces=_WORD_NAMESPACES)
                if field_char is not None and field_char.get(
                    f"{{{_WORD_NAMESPACE}}}fldCharType"
                ) == "separate":
                    found_separator = True
                    continue
                if not found_separator:
                    continue
                text = sibling.find("w:t", namespaces=_WORD_NAMESPACES)
                if text is not None:
                    text.text = page_number
                    updated = True
                    break
            if not updated:
                return None

        updated_xml = etree.tostring(
            root, xml_declaration=True, encoding="UTF-8", standalone=True
        )
        output = BytesIO()
        with ZipFile(output, "w", ZIP_DEFLATED) as updated_package:
            for item in package.infolist():
                content = updated_xml if item.filename == "word/document.xml" else package.read(item)
                updated_package.writestr(item, content)
        return output.getvalue()
