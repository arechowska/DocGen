from __future__ import annotations

from docgen.documents.schemas import (
    DocumentNode,
    DocumentOrigin,
    NodeKind,
    WorkingDocument,
)
from docgen.templates_catalog.schemas import SemanticTemplate

from .errors import WorkflowError

_BARE_GAP_MESSAGE = "нет данных в источниках"
_FLOW_SECTIONS = {"main-flow", "alternative-flow", "exceptions"}


def prepare_assembled_document(
    document: WorkingDocument,
    template: SemanticTemplate,
) -> WorkingDocument:
    """Apply deterministic structure owned by a semantic-template adapter."""
    identity = {
        "origin": DocumentOrigin.ASSEMBLED,
        "source_id": None,
        "build_template_id": template.id,
        "template_id": template.id,
    }
    if template.id != "use-case":
        return WorkingDocument.model_validate(
            {**document.model_dump(mode="python"), **identity}
        )

    by_section: dict[str, DocumentNode] = {}
    for node in document.nodes:
        if node.section_id is None or node.section_id in by_section:
            continue
        by_section[node.section_id] = node

    structured: list[DocumentNode] = []
    for section in template.sections:
        content = by_section.get(section.id)
        if content is None and not section.required:
            continue
        if content is None:
            content = DocumentNode(
                kind=NodeKind.GAP,
                flags=["missing-source-data"],
            )
        elif content.kind is NodeKind.HEADING:
            children = content.children
            if not children:
                children = [
                    DocumentNode(
                        kind=NodeKind.GAP,
                        flags=["missing-source-data"],
                    )
                ]
            content = DocumentNode(
                kind=NodeKind.HEADING,
                section_id=section.id,
                text=section.title,
                data={"level": 2},
                children=children,
                flags=["template-structure"],
            )
            structured.append(content)
            continue
        else:
            content = content.model_copy(update={"section_id": None})

        _validate_use_case_content(section.id, section.title, content)
        if section.id in _FLOW_SECTIONS and content.kind is NodeKind.LIST:
            content = content.model_copy(
                update={"data": {**content.data, "ordered": True}}
            )
        structured.append(
            DocumentNode(
                kind=NodeKind.HEADING,
                section_id=section.id,
                text=section.title,
                data={"level": 2},
                children=[content],
                flags=["template-structure"],
            )
        )

    prepared = WorkingDocument.model_validate(
        {
            **document.model_dump(mode="python"),
            **identity,
            "nodes": structured,
        }
    )
    _validate_use_case_required_sections(prepared, template)
    return prepared


def _validate_use_case_content(
    section_id: str,
    section_title: str,
    content: DocumentNode,
) -> None:
    if (
        content.kind is NodeKind.PARAGRAPH
        and (content.text or "").strip().casefold() == _BARE_GAP_MESSAGE
    ):
        raise WorkflowError(
            f"Раздел «{section_title}» содержит недопустимую фразу «Нет данных»"
        )
    if section_id == "main-flow" and content.kind not in {
        NodeKind.LIST,
        NodeKind.GAP,
    }:
        raise WorkflowError(
            "Основной поток должен быть нумерованным списком отдельных шагов"
        )
    if content.kind is NodeKind.LIST:
        items = content.data.get("items")
        if not isinstance(items, list) or any(
            not isinstance(item, str) or not item.strip() for item in items
        ):
            raise WorkflowError(f"Раздел «{section_title}» содержит некорректный список")


def _validate_use_case_required_sections(
    document: WorkingDocument,
    template: SemanticTemplate,
) -> None:
    sections = {node.section_id: node for node in document.nodes}
    for section in template.sections:
        if not section.required:
            continue
        node = sections.get(section.id)
        if (
            node is None
            or node.kind is not NodeKind.HEADING
            or node.text != section.title
            or not node.children
        ):
            raise WorkflowError(
                f"Документ не содержит обязательный раздел «{section.title}»"
            )
