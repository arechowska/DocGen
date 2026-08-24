"""HTML exporter for rendering WorkingDocuments as standalone HTML."""

from __future__ import annotations

import base64
import mimetypes
from collections.abc import Callable
from pathlib import Path

from bs4 import BeautifulSoup, Comment
from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup, escape

from docgen.documents.schemas import WorkingDocument
from docgen.documents.style import normalized_style_attribute
from docgen.export._naming import make_safe_filename
from docgen.export.protocol import RenderedFile
from docgen.formatting.schemas import FormattingTemplate
from docgen.sources.storage import LocalStorage

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "formatting" / "templates"

_RICH_TAGS = frozenset(
    {"a", "b", "br", "code", "em", "i", "mark", "s", "span", "strong", "sub", "sup", "u"}
)
_BLOCKED_RICH_TAGS = frozenset({"script", "style", "template"})

ImageAsset = tuple[bytes, str]
"""Resolved image content and its MIME type, e.g. ``(b"...", "image/png")``."""

ImageLoader = Callable[[str], "ImageAsset | None"]
"""Resolves a node's ``data['src']`` reference to image bytes + MIME type.

Must return ``None`` when the reference cannot be safely resolved (missing,
outside storage, wrong type, etc.). Callers fall back to a placeholder in
that case -- they never fabricate content or fetch external resources.
"""


class HtmlExporter:
    """Exports WorkingDocuments to standalone, self-contained HTML.

    The rendered document embeds all styling inline (``<style>``) and, when
    an ``image_loader`` resolves a node's image reference, embeds image
    bytes as ``data:`` URLs. It never references external resources, so the
    output is a single portable HTML file.
    """

    def __init__(
        self,
        image_loader: ImageLoader | None = None,
        templates_dir: Path | None = None,
    ) -> None:
        """Create an HtmlExporter.

        Args:
            image_loader: Resolves a node's image src to bytes + MIME type.
                When None (the default), all images render as placeholders.
            templates_dir: Directory containing the Jinja/CSS assets named
                by a FormattingTemplate's `assets` list. Defaults to the
                built-in formatting/templates catalog directory.
        """
        self._image_loader = image_loader
        self._templates_dir = (templates_dir or _TEMPLATES_DIR).resolve()
        self._env = Environment(
            loader=FileSystemLoader(str(self._templates_dir)),
            autoescape=True,
        )

    def render(
        self, document: WorkingDocument, template: FormattingTemplate
    ) -> RenderedFile:
        """Render a document to standalone HTML.

        Args:
            document: The WorkingDocument to render.
            template: The FormattingTemplate naming the `.html.j2` and
                `.css` assets to use.

        Returns:
            RenderedFile with self-contained HTML content.
        """
        html_asset = self._asset_named(template, ".html.j2")
        css_asset = self._asset_named(template, ".css")

        jinja_template = self._env.get_template(html_asset)
        css_text = self._read_asset_text(css_asset)

        html = jinja_template.render(
            document=document,
            css=css_text,
            image_data_url=self._image_data_url,
            rich_html=safe_rich_html,
            style_attribute=safe_style_attribute,
        )

        return RenderedFile(
            filename=make_safe_filename(
                document.title, ".html", reserved_suffix=f"-{template.id}"
            ),
            media_type="text/html",
            content=html.encode("utf-8"),
        )

    def _asset_named(self, template: FormattingTemplate, suffix: str) -> str:
        """Find the first declared asset ending with `suffix`.

        Only assets listed on the template are ever loaded -- this exporter
        never reads an arbitrary path, only ones the catalog already
        validated for this template.
        """
        matches = [asset for asset in template.assets if asset.endswith(suffix)]
        if not matches:
            raise ValueError(
                f"Шаблон {template.id} не содержит ассет с суффиксом {suffix}"
            )
        return matches[0]

    def _read_asset_text(self, name: str) -> str:
        """Read a catalog-declared asset file, enforcing containment."""
        path = (self._templates_dir / name).resolve()
        if not path.is_relative_to(self._templates_dir):
            raise ValueError("Недопустимый путь ассета")
        return path.read_text(encoding="utf-8")

    def _image_data_url(self, src: object) -> str | None:
        """Resolve a node's image src to an embeddable data: URL, or None.

        Only locally-storage-resolvable references are embedded. A missing,
        unresolvable, or non-image reference returns None so the caller
        renders the standard placeholder instead of fabricating content or
        fetching an external resource.
        """
        if not isinstance(src, str) or not src or self._image_loader is None:
            return None
        asset = self._image_loader(src)
        if asset is None:
            return None
        content, media_type = asset
        if not media_type or not media_type.startswith("image/"):
            return None
        encoded = base64.b64encode(content).decode("ascii")
        return f"data:{media_type};base64,{encoded}"


def local_storage_image_loader(storage: LocalStorage) -> ImageLoader:
    """Build an ImageLoader backed by LocalStorage-managed files.

    Resolves `src` as a stored relative path using the same containment
    check LocalStorage.resolve() already applies elsewhere in the app, then
    verifies the file exists and has an image/* MIME type before reading
    it. Any failure (unsafe path, missing file, wrong type) returns None
    rather than raising, so callers can fall back to a placeholder.
    """

    def _load(src: str) -> ImageAsset | None:
        try:
            path = storage.resolve(src)
        except ValueError:
            return None
        if not path.is_file():
            return None
        media_type, _ = mimetypes.guess_type(path.name)
        if not media_type or not media_type.startswith("image/"):
            return None
        try:
            content = path.read_bytes()
        except OSError:
            return None
        return content, media_type

    return _load


def safe_rich_html(value: object, fallback: str = "") -> Markup:
    """Return a small, sanitized editor rich-text fragment."""
    if not isinstance(value, str) or not value:
        return Markup(escape(fallback))

    soup = BeautifulSoup(value, "html.parser")
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()
    for tag in list(soup.find_all(_BLOCKED_RICH_TAGS)):
        tag.decompose()
    for tag in list(soup.find_all(True)):
        if tag.name not in _RICH_TAGS:
            tag.unwrap()
            continue
        for attribute in list(tag.attrs):
            if attribute == "style":
                style = normalized_style_attribute(str(tag.attrs[attribute]))
                if style:
                    tag.attrs[attribute] = style
                else:
                    del tag.attrs[attribute]
                continue
            if tag.name == "a" and attribute in {"href", "title"}:
                if attribute == "href" and not _is_safe_rich_url(tag.attrs[attribute]):
                    del tag.attrs[attribute]
                continue
            del tag.attrs[attribute]
    return Markup("".join(str(item) for item in soup.contents))


def safe_style_attribute(value: object) -> Markup:
    """Serialize only style properties already supported by the editor."""
    if isinstance(value, dict):
        raw_style = ";".join(f"{key}:{item}" for key, item in value.items())
    elif isinstance(value, str):
        raw_style = value
    else:
        raw_style = ""
    style = normalized_style_attribute(raw_style)
    if not style:
        return Markup("")
    return Markup(f' style="{escape(style)}"')


def _is_safe_rich_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    return normalized.startswith(("#", "/", "http://", "https://", "mailto:"))
