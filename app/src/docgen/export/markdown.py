"""Markdown exporter for rendering WorkingDocuments as Markdown text."""

import re
from pathlib import Path

from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.export.protocol import RenderedFile
from docgen.formatting.schemas import FormattingTemplate


class MarkdownExporter:
    """Exports WorkingDocuments to Markdown format."""

    def render(
        self, document: WorkingDocument, template: FormattingTemplate
    ) -> RenderedFile:
        """Render a document to Markdown format.

        Args:
            document: The WorkingDocument to render.
            template: The FormattingTemplate (currently unused but included for protocol compatibility).

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
        filename = self._make_safe_filename(document.title)

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

    def _render_heading(self, node: DocumentNode) -> str:
        """Render a heading node."""
        level = node.data.get("level", 1)
        # Clamp level between 1 and 6
        level = max(1, min(6, level))
        prefix = "#" * level
        text = node.text or ""

        result = f"{prefix} {text}"

        # Render children if present
        if node.children:
            child_content = []
            for child in node.children:
                rendered = self._render_node(child)
                if rendered:
                    child_content.append(rendered)
            if child_content:
                result += "\n\n" + "\n\n".join(child_content)

        return result

    def _render_paragraph(self, node: DocumentNode) -> str:
        """Render a paragraph node."""
        text = node.text or ""

        result = text

        # Render children if present
        if node.children:
            child_content = []
            for child in node.children:
                rendered = self._render_node(child)
                if rendered:
                    child_content.append(rendered)
            if child_content:
                result += "\n\n" + "\n\n".join(child_content)

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

        return "\n".join(lines)

    def _render_table(self, node: DocumentNode) -> str:
        """Render a table node as GFM table."""
        headers = node.data.get("headers", [])
        rows = node.data.get("rows", [])

        if not headers:
            return ""

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

        # Build header row
        header_cells = [escape_table_content(h) for h in headers]
        header_row = "| " + " | ".join(header_cells) + " |"

        # Build separator row
        separator_row = "| " + " | ".join(["---"] * len(headers)) + " |"

        # Build data rows
        data_rows = []
        for row in rows:
            # Pad row with empty cells if needed
            padded_row = list(row) + [""] * (len(headers) - len(row))
            # Take only the first len(headers) cells
            padded_row = padded_row[:len(headers)]
            cells = [escape_table_content(cell) for cell in padded_row]
            data_rows.append("| " + " | ".join(cells) + " |")

        # Combine all parts
        lines = [header_row, separator_row] + data_rows
        return "\n".join(lines)

    def _render_image(self, node: DocumentNode) -> str:
        """Render an image node."""
        src = node.data.get("src", "")
        alt = node.data.get("alt", "")

        return f"![{alt}]({src})"

    def _render_gap(self, node: DocumentNode) -> str:
        """Render a gap node as a blockquote."""
        text = node.text or ""
        # Render as bold text in blockquote
        return f"> **{text}**"

    def _make_safe_filename(self, title: str) -> str:
        """Create a filesystem-safe filename from a document title.

        Args:
            title: The document title.

        Returns:
            A filesystem-safe filename ending in .md
        """
        # Replace non-alphanumeric characters (except spaces and hyphens) with hyphens
        safe_name = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ\s\-]", "", title)
        # Replace multiple spaces with single hyphen
        safe_name = re.sub(r"\s+", "-", safe_name.strip())
        # Remove leading/trailing hyphens
        safe_name = safe_name.strip("-")

        # If name is empty, use default
        if not safe_name:
            safe_name = "document"

        # Truncate to reasonable length (255 - .md = 252 chars max)
        max_length = 240  # Leave some buffer
        if len(safe_name) > max_length:
            safe_name = safe_name[:max_length]

        return f"{safe_name}.md"
