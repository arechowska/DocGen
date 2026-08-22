from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.extraction.schemas import Provenance


class UpdateText(BaseModel):
    kind: Literal["update_text"] = "update_text"
    node_id: str
    text: str


class InsertNode(BaseModel):
    kind: Literal["insert_node"] = "insert_node"
    parent_id: str | None = None
    index: int
    node: DocumentNode


class DeleteNode(BaseModel):
    kind: Literal["delete_node"] = "delete_node"
    node_id: str


class MoveNode(BaseModel):
    kind: Literal["move_node"] = "move_node"
    node_id: str
    parent_id: str | None = None
    index: int


class UpdateData(BaseModel):
    kind: Literal["update_data"] = "update_data"
    node_id: str
    data: dict


class UpdateProvenance(BaseModel):
    kind: Literal["update_provenance"] = "update_provenance"
    node_id: str
    provenance: list[Provenance]


DocumentOperation = Annotated[
    UpdateText | InsertNode | DeleteNode | MoveNode | UpdateData | UpdateProvenance,
    Field(discriminator="kind"),
]


class EditValidationError(ValueError):
    pass


def find_node(document: WorkingDocument, node_id: str) -> DocumentNode | None:
    for node in _walk(document.nodes):
        if node.id == node_id:
            return node
    return None


def apply_operations(
    document: WorkingDocument, operations: list[DocumentOperation]
) -> WorkingDocument:
    candidate = document.model_copy(deep=True)
    for operation in operations:
        candidate = _apply_one(candidate, operation)
    _validate_tree(candidate)
    return candidate


def _apply_one(document: WorkingDocument, operation: DocumentOperation) -> WorkingDocument:
    if isinstance(operation, UpdateText):
        return _replace_node(
            document,
            operation.node_id,
            lambda node: node.model_copy(update={"text": operation.text}),
        )
    if isinstance(operation, UpdateData):
        return _replace_node(
            document,
            operation.node_id,
            lambda node: node.model_copy(update={"data": operation.data}),
        )
    if isinstance(operation, UpdateProvenance):
        return _replace_node(
            document,
            operation.node_id,
            lambda node: node.model_copy(update={"provenance": operation.provenance}),
        )
    if isinstance(operation, InsertNode):
        _validate_index(operation.index)
        _ensure_unique_insert_id(document, operation.node.id)
        if operation.parent_id is None:
            nodes = list(document.nodes)
            if operation.index > len(nodes):
                raise EditValidationError("Индекс вставки вне документа")
            nodes.insert(operation.index, operation.node)
            return document.model_copy(update={"nodes": nodes})
        return _replace_node(
            document,
            operation.parent_id,
            lambda parent: _insert_child(parent, operation.index, operation.node),
        )
    if isinstance(operation, DeleteNode):
        return _delete_node(document, operation.node_id)
    if isinstance(operation, MoveNode):
        return _move_node(document, operation)
    raise EditValidationError("Неподдерживаемая операция")


def _replace_node(
    document: WorkingDocument,
    node_id: str,
    replace: Callable[[DocumentNode], DocumentNode],
) -> WorkingDocument:
    nodes, changed = _replace_in_nodes(document.nodes, node_id, replace)
    if not changed:
        raise EditValidationError(f"Блок {node_id} не найден")
    return document.model_copy(update={"nodes": nodes})


def _replace_in_nodes(
    nodes: list[DocumentNode],
    node_id: str,
    replace: Callable[[DocumentNode], DocumentNode],
) -> tuple[list[DocumentNode], bool]:
    result: list[DocumentNode] = []
    changed = False
    for node in nodes:
        if node.id == node_id:
            result.append(replace(node))
            changed = True
            continue
        children, child_changed = _replace_in_nodes(node.children, node_id, replace)
        if child_changed:
            result.append(node.model_copy(update={"children": children}))
            changed = True
        else:
            result.append(node)
    return result, changed


def _insert_child(parent: DocumentNode, index: int, node: DocumentNode) -> DocumentNode:
    _validate_index(index)
    children = list(parent.children)
    if index > len(children):
        raise EditValidationError("Индекс вставки вне блока")
    children.insert(index, node)
    return parent.model_copy(update={"children": children})


def _delete_node(document: WorkingDocument, node_id: str) -> WorkingDocument:
    if len(document.nodes) == 1 and document.nodes[0].id == node_id:
        raise EditValidationError("Нельзя удалить последний блок документа")
    nodes, removed = _delete_from_nodes(document.nodes, node_id)
    if not removed:
        raise EditValidationError(f"Блок {node_id} не найден")
    return document.model_copy(update={"nodes": nodes})


def _delete_from_nodes(nodes: list[DocumentNode], node_id: str) -> tuple[list[DocumentNode], bool]:
    result: list[DocumentNode] = []
    removed = False
    for node in nodes:
        if node.id == node_id:
            removed = True
            continue
        children, child_removed = _delete_from_nodes(node.children, node_id)
        if child_removed:
            result.append(node.model_copy(update={"children": children}))
            removed = True
        else:
            result.append(node)
    return result, removed


def _move_node(document: WorkingDocument, operation: MoveNode) -> WorkingDocument:
    _validate_index(operation.index)
    moving = find_node(document, operation.node_id)
    if moving is None:
        raise EditValidationError(f"Блок {operation.node_id} не найден")
    if operation.parent_id is not None:
        parent = find_node(document, operation.parent_id)
        if parent is None:
            raise EditValidationError(f"Блок {operation.parent_id} не найден")
        if _contains_node(moving, operation.parent_id):
            raise EditValidationError("Нельзя переместить блок внутрь его потомка")

    without_node = _delete_node(document, operation.node_id)
    return _apply_one(
        without_node,
        InsertNode(parent_id=operation.parent_id, index=operation.index, node=moving),
    )


def _validate_index(index: int) -> None:
    if index < 0:
        raise EditValidationError("Индекс не может быть отрицательным")


def _ensure_unique_insert_id(document: WorkingDocument, node_id: str) -> None:
    if find_node(document, node_id) is not None:
        raise EditValidationError(f"Блок {node_id} уже существует")


def _validate_tree(document: WorkingDocument) -> None:
    seen: set[str] = set()
    for node in _walk(document.nodes):
        if node.id in seen:
            raise EditValidationError(f"Блок {node.id} уже существует")
        seen.add(node.id)
        if node.kind is NodeKind.HEADING and not (node.text or "").strip():
            raise EditValidationError("Заголовок не может быть пустым")
        if node.kind is NodeKind.TABLE:
            _validate_table(node)


def _validate_table(node: DocumentNode) -> None:
    rows = node.data.get("rows")
    if rows is None:
        return
    if not isinstance(rows, list) or not rows or not all(isinstance(row, list) for row in rows):
        raise EditValidationError("Таблица должна содержать строки")
    width = len(rows[0])
    if width == 0:
        raise EditValidationError("Таблица должна содержать ячейки")
    if any(len(row) != width for row in rows):
        raise EditValidationError("Все строки таблицы должны иметь одинаковое число ячеек")


def _contains_node(node: DocumentNode, node_id: str) -> bool:
    return any(child.id == node_id or _contains_node(child, node_id) for child in node.children)


def _walk(nodes: list[DocumentNode]):
    for node in nodes:
        yield node
        yield from _walk(node.children)
