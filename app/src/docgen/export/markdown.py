"""Markdown exporter for rendering WorkingDocuments as Markdown text."""

from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.export._naming import make_safe_filename
from docgen.export.protocol import RenderedFile
from docgen.formatting.schemas import FormattingTemplate

# Shared with html.py/docx.py/pdf.py's identical wording for the same
# situation: an image node with no locally-resolvable `src` (the dominant
# production case for AI-assembled documents, whose IMAGE nodes carry only
# `text` + provenance, never `data.src`).
_IMAGE_PLACEHOLDER_MESSAGE = "Изображение или схема"


class MarkdownExporter:
    """Exports WorkingDocuments to Markdown format."""

    def render(
        self, document: WorkingDocument, template: FormattingTemplate
    ) -> RenderedFile:
        """Render a document to Markdown format.

        Args:
            document: The WorkingDocument to render.
            template: The FormattingTemplate naming the id folded into the
                on-disk filename by ExportStorage.

        Returns:
            RenderedFile with Markdown content.
        """
        # Render all top-level nodes
        rendered_nodes = []
        for node in document.nodes:
            rendered = self._render_node(node)
            if rendered:
                rendered_nodes.append(rendered)

        # Join nodes with blank line separator
        content = "\n\n".join(rendered_nodes)

        # Ensure single trailing newline
        if content:
            content = content.rstrip() + "\n"
        else:
            content = "\n"

        # Create filesystem-safe filename from document title
        filename = make_safe_filename(
            document.title, ".md", reserved_suffix=f"-{template.id}"
        )

        return RenderedFile(
            filename=filename,
            media_type="text/markdown",
            content=content.encode("utf-8"),
        )

    def _render_node(self, node: DocumentNode) -> str:
        """Render a single node to Markdown.

        Args:
            node: The DocumentNode to render.

        Returns:
            Markdown representation of the node, or empty string if not supported.
        """
        if node.kind == NodeKind.HEADING:
            return self._render_heading(node)
        elif node.kind == NodeKind.PARAGRAPH:
            return self._render_paragraph(node)
        elif node.kind == NodeKind.LIST:
            return self._render_list(node)
        elif node.kind == NodeKind.TABLE:
            return self._render_table(node)
        elif node.kind == NodeKind.IMAGE:
            return self._render_image(node)
        elif node.kind == NodeKind.GAP:
            return self._render_gap(node)
        else:
            return ""

    def _render_children(self, node: DocumentNode) -> str:
        """Render all children of a node and format them appropriately.

        Args:
            node: The DocumentNode whose children to render.

        Returns:
            Formatted child content, or empty string if no children.
        """
        if not node.children:
            return ""

        child_content = []
        for child in node.children:
            rendered = self._render_node(child)
            if rendered:
                child_content.append(rendered)

        if child_content:
            return "\n\n" + "\n\n".join(child_content)
        return ""

    def _render_heading(self, node: DocumentNode) -> str:
        """Render a heading node."""
        level = self._coerce_heading_level(node.data.get("level", 1))
        prefix = "#" * level
        text = node.text or ""

        result = f"{prefix} {text}"
        result += self._render_children(node)

        return result

    @staticmethod
    def _coerce_heading_level(value: object) -> int:
        """Coerce a heading `level` to an int in [1, 6].

        `data` comes straight from the LLM's JSON payload -- its envelope is
        validated but `data` itself is not, so `level` can arrive as
        `"2"` (string) or `2.0` (float) as well as a plain int. The other
        three exporters already tolerate this (html.py/pdf.py via Jinja's
        `|int(1)` filter, docx.py via an isinstance check); unlike a bare
        `max(1, min(6, level))`, this must never raise `TypeError` on a
        non-numeric `level` and fail the whole export job.
        """
        try:
            level = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            level = 1
        return max(1, min(6, level))

    def _render_paragraph(self, node: DocumentNode) -> str:
        """Render a paragraph node."""
        text = node.text or ""

        result = text
        result += self._render_children(node)

        return result

    def _render_list(self, node: DocumentNode) -> str:
        """Render a list node (ordered or unordered)."""
        items = node.data.get("items", [])
        ordered = node.data.get("ordered", False)

        lines = []
        for idx, item in enumerate(items):
            if ordered:
                prefix = f"{idx + 1}."
            else:
                prefix = "-"
            lines.append(f"{prefix} {item}")

        result = "\n".join(lines)
        result += self._render_children(node)

        return result

    def _render_table(self, node: DocumentNode) -> str:
        """Render a table node as GFM table."""
        headers = node.data.get("headers", [])
        rows = node.data.get("rows", [])

        # Only skip rendering if both headers and rows are empty
        if not headers and not rows:
            result = ""
        else:
            # Escape pipes and newlines in content
            def escape_table_content(content: str) -> str:
                """Escape pipes and newlines in table content."""
                if not isinstance(content, str):
                    content = str(content)
                # Replace pipes with escaped pipes
                content = content.replace("|", "\\|")
                # Replace newlines with spaces
                content = content.replace("\n", " ")
                return content

            # Determine column count from headers or first row
            if headers:
                col_count = len(headers)
            elif rows and rows[0]:
                col_count = len(rows[0])
            else:
                col_count = 1

            lines = []

            # Build header row (use headers if present, otherwise empty row)
            if headers:
                header_cells = [escape_table_content(h) for h in headers]
            else:
                # No headers: create empty header row with correct column count
                header_cells = [""] * col_count
            header_row = "| " + " | ".join(header_cells) + " |"
            lines.append(header_row)

            # Build separator row
            separator_row = "| " + " | ".join(["---"] * col_count) + " |"
            lines.append(separator_row)

            # Build data rows
            for row in rows:
                # Pad row with empty cells if needed
                padded_row = list(row) + [""] * (col_count - len(row))
                # Take only the first col_count cells
                padded_row = padded_row[:col_count]
                cells = [escape_table_content(cell) for cell in padded_row]
                lines.append("| " + " | ".join(cells) + " |")

            result = "\n".join(lines)

        result += self._render_children(node)
        return result

    def _render_image(self, node: DocumentNode) -> str:
        """Render an image node.

        When `src` is present, renders a normal Markdown image link (the
        alt text is used as-is). Otherwise -- the dominant production case
        for AI-assembled documents, whose IMAGE nodes carry only `text` +
        provenance, never `data.src` -- a literal `![]()` would be a
        meaningless, broken link that silently drops the description
        entirely. Instead, mirror html.py/docx.py/pdf.py's placeholder
        convention and surface `node.text` as a caption underneath it, so
        the description always survives the export.
        """
        src = node.data.get("src", "")
        alt = node.data.get("alt", "")

        if isinstance(src, str) and src:
            image_part = f"![{alt}]({src})"
        else:
            image_part = f"*{_IMAGE_PLACEHOLDER_MESSAGE}*"

        caption = node.text or ""
        result = f"{image_part}\n\n{caption}" if caption else image_part
        result += self._render_children(node)

        return result

    def _render_gap(self, node: DocumentNode) -> str:
        """Render a gap node as a blockquote.

        Gap nodes represent missing content sections. Following the
        application's HTML template convention, always render the fixed
        message "Нет данных в источниках" regardless of node.text.
        """
        # Use fixed message per app's HTML rendering convention
        result = "> **Нет данных в источниках**"
        result += self._render_children(node)

        return result
