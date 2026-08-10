from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

import yaml

from docgen.ai.grounding import GroundingValidator
from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.extraction.schemas import BlockKind, NormalizedBlock, Provenance
from docgen.extraction.text import TextExtractor
from docgen.models import Source, SourceKind
from docgen.templates_catalog.loader import TemplateCatalog


@dataclass(frozen=True)
class QualityScore:
    requirement_coverage: float
    ungrounded_claims: int
    processing_seconds: float
    maximum_processing_seconds: float


@dataclass(frozen=True)
class QualityArtifact:
    document: WorkingDocument
    checked_rule_ids: frozenset[str]


class DeterministicFakeModel:
    """A frozen local model double; it performs no I/O and has no random state."""

    def generate(self, blocks: list[NormalizedBlock], template_id: str) -> QualityArtifact:
        nodes = [_node_from_block(block) for block in blocks]
        if any("канал отдельного уведомления" in block.text.lower() for block in blocks):
            nodes.append(
                DocumentNode(
                    id="gap-notification-channel",
                    kind=NodeKind.GAP,
                    flags=["missing-source-data"],
                )
            )
        template = TemplateCatalog().get(template_id)
        return QualityArtifact(
            document=WorkingDocument(
                title="Перевод между своими счетами",
                template_id=template_id,
                nodes=nodes,
            ),
            checked_rule_ids=frozenset(rule.id for rule in template.rules),
        )


def deterministic_fake_model() -> DeterministicFakeModel:
    return DeterministicFakeModel()


def evaluate_case(case_dir: Path, model: DeterministicFakeModel) -> QualityScore:
    started_at = perf_counter()
    expected = _load_expectations(case_dir / "expected.yaml")
    source_path = case_dir / "sources" / "input.md"
    source = Source(
        id="quality-source",
        project_id="quality-project",
        kind=SourceKind.FILE,
        display_name=source_path.name,
        media_type="text/markdown",
        storage_path=str(source_path),
        status="ready",
    )
    blocks = TextExtractor().extract(source, source_path).blocks
    artifact = model.generate(blocks, expected["template_id"])

    applicable_rules = set(expected["applicable_rule_ids"])
    catalog_rules = {
        rule.id for rule in TemplateCatalog().get(expected["template_id"]).rules
    }
    covered_rules = artifact.checked_rule_ids & applicable_rules & catalog_rules

    actual_nodes = {
        (node.kind.value, node.text)
        for node in _walk_nodes(artifact.document.nodes)
        if node.text
    }
    required_nodes = {
        (item["kind"], item["title"]) for item in expected["required_nodes"]
    }
    covered_nodes = actual_nodes & required_nodes

    actual_gaps = {
        node.id
        for node in _walk_nodes(artifact.document.nodes)
        if node.kind is NodeKind.GAP
    }
    expected_gaps = {item["id"] for item in expected["expected_gaps"]}
    covered_gaps = actual_gaps & expected_gaps

    requirement_count = len(applicable_rules) + len(required_nodes) + len(expected_gaps)
    covered_count = len(covered_rules) + len(covered_nodes) + len(covered_gaps)
    coverage = covered_count / requirement_count if requirement_count else 1.0

    block_ids = {block.id for block in blocks}
    grounding_errors = GroundingValidator().validate(artifact.document, block_ids)
    rendered_text = "\n".join(
        node.text or "" for node in _walk_nodes(artifact.document.nodes)
    ).lower()
    forbidden_claims = sum(
        claim.lower() in rendered_text for claim in expected["forbidden_claims"]
    )

    return QualityScore(
        requirement_coverage=coverage,
        ungrounded_claims=len(grounding_errors) + forbidden_claims,
        processing_seconds=perf_counter() - started_at,
        maximum_processing_seconds=float(expected["max_processing_seconds"]),
    )


def _load_expectations(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("Quality expectations must be a mapping")
    return data


def _node_from_block(block: NormalizedBlock) -> DocumentNode:
    node_kinds = {
        BlockKind.TEXT: NodeKind.PARAGRAPH,
        BlockKind.HEADING: NodeKind.HEADING,
        BlockKind.LIST: NodeKind.LIST,
        BlockKind.TABLE: NodeKind.TABLE,
        BlockKind.IMAGE: NodeKind.IMAGE,
    }
    locator = block.provenance[0].locator if block.provenance else block.id
    return DocumentNode(
        id=f"quality-node:{block.id}",
        kind=node_kinds[block.kind],
        text=block.text,
        data=block.data,
        provenance=[Provenance(source_id=block.id, locator=locator)],
    )


def _walk_nodes(nodes: list[DocumentNode]) -> list[DocumentNode]:
    result: list[DocumentNode] = []
    for node in nodes:
        result.append(node)
        result.extend(_walk_nodes(node.children))
    return result


__all__ = [
    "QualityScore",
    "deterministic_fake_model",
    "evaluate_case",
]
