from __future__ import annotations

import json

from pydantic import ValidationError

from docgen.ai.client import TextModel, VisionModel
from docgen.ai.grounding import GroundingValidator
from docgen.ai.prompts import DOCUMENT_SYSTEM_PROMPT
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import (
    CheckFinding,
    CheckReport,
    DocumentNode,
    NodeKind,
    Severity,
    WorkingDocument,
)
from docgen.extraction.schemas import BlockKind, NormalizedBlock, Provenance
from docgen.jobs.models import Job, JobKind
from docgen.jobs.runner import ProgressSink
from docgen.projects.repository import ProjectRepository
from docgen.templates_catalog.loader import TemplateCatalog
from docgen.templates_catalog.schemas import SemanticTemplate

from .assemble import WorkflowError, enrich_images, normalize_sources, public_blocks
from .normalize import NormalizationWorkflow

_CHECK_KIND_ERROR = "Некорректный тип задания для проверки"
_PROJECT_NOT_FOUND = "Проект не найден"
_DOCUMENT_NOT_FOUND = "Документ для проверки не найден"
_DOCUMENT_TEMPLATE_ERROR = "Документ создан для другого шаблона"
_REPORT_SCHEMA_ERROR = "Модель вернула некорректный отчёт"
_REPORT_TEMPLATE_ERROR = "Модель вернула отчёт для другого шаблона"
_DOCUMENT_GROUNDING_ERROR = "Документ не прошёл проверку по источникам"
_LOW_CONFIDENCE_THRESHOLD = 0.7


class CheckWorkflow:
    def __init__(
        self,
        *,
        projects: ProjectRepository,
        normalization: NormalizationWorkflow,
        templates: TemplateCatalog,
        text_model: TextModel,
        vision_model: VisionModel,
        grounding: GroundingValidator,
        documents: DocumentRepository,
    ) -> None:
        self._projects = projects
        self._normalization = normalization
        self._templates = templates
        self._text_model = text_model
        self._vision_model = vision_model
        self._grounding = grounding
        self._documents = documents

    def run(self, job: Job, progress: ProgressSink) -> CheckReport:
        if job.kind is not JobKind.CHECK:
            raise WorkflowError(_CHECK_KIND_ERROR)

        progress(10, "Загружены проект, шаблон и документ")
        if self._projects.get(job.project_id) is None:
            raise WorkflowError(_PROJECT_NOT_FOUND)
        template = self._templates.get(job.template_id)
        document = (
            self._documents.get_document(job.project_id)
            if job.target_source_id is None
            else None
        )
        if job.target_source_id is None:
            if document is None:
                raise WorkflowError(_DOCUMENT_NOT_FOUND)
            if document.template_id != template.id:
                raise WorkflowError(_DOCUMENT_TEMPLATE_ERROR)

        normalized = normalize_sources(self._normalization, job.project_id, progress)
        blocks = enrich_images(normalized.blocks, self._vision_model, progress)
        if job.target_source_id is not None:
            document = _target_document(job.target_source_id, template.id, blocks)
        assert document is not None
        if self._grounding.validate(document, {block.id for block in blocks}):
            raise WorkflowError(_DOCUMENT_GROUNDING_ERROR)

        progress(90, "Проверка документа моделью")
        raw_report = self._text_model.generate_json(
            DOCUMENT_SYSTEM_PROMPT
            + "\nПроверьте каждое правило шаблона. Отдельно отмечайте выводы с низкой уверенностью.",
            _check_prompt(template, blocks, document),
            CheckReport,
        )
        try:
            model_report = CheckReport.model_validate(raw_report)
        except ValidationError as exc:
            raise WorkflowError(_REPORT_SCHEMA_ERROR) from exc
        report = _validated_report(model_report, template, document)

        progress(100, "Сохранение отчёта")
        progress.checkpoint()
        if job.target_source_id is not None:
            self._documents.save_document(job.project_id, document)
        self._documents.save_report(job.project_id, report)
        return report


def _target_document(
    target_source_id: str,
    template_id: str,
    blocks: list[NormalizedBlock],
) -> WorkingDocument:
    target_prefix = f"{target_source_id}:"
    target_blocks = [block for block in blocks if block.id.startswith(target_prefix)]
    if not target_blocks:
        raise WorkflowError(_DOCUMENT_NOT_FOUND)
    title = next(
        (
            block.text
            for block in target_blocks
            if block.kind is BlockKind.HEADING and block.text.strip()
        ),
        "Документ для проверки",
    )
    return WorkingDocument(
        title=title,
        template_id=template_id,
        nodes=[_node_from_block(block) for block in target_blocks],
    )


def _node_from_block(block: NormalizedBlock) -> DocumentNode:
    node_kinds = {
        BlockKind.TEXT: NodeKind.PARAGRAPH,
        BlockKind.HEADING: NodeKind.HEADING,
        BlockKind.LIST: NodeKind.LIST,
        BlockKind.TABLE: NodeKind.TABLE,
        BlockKind.IMAGE: NodeKind.IMAGE,
    }
    safe_data = {
        key: value
        for key, value in block.data.items()
        if key not in {"content_base64", "storage_path"}
    }
    locators = [item.locator for item in block.provenance] or [block.id]
    return DocumentNode(
        id=f"document-node:{block.id}",
        kind=node_kinds[block.kind],
        text=block.text,
        data=safe_data,
        provenance=[
            Provenance(source_id=block.id, locator=locator) for locator in locators
        ],
    )


def _validated_report(
    model_report: CheckReport,
    template: SemanticTemplate,
    document: WorkingDocument,
) -> CheckReport:
    if model_report.template_id != template.id:
        raise WorkflowError(_REPORT_TEMPLATE_ERROR)

    rules = {rule.id: rule for rule in template.rules}
    unchecked = set(model_report.unchecked_rules)
    unknown_unchecked = unchecked - rules.keys()
    if unknown_unchecked:
        raise WorkflowError("Отчёт содержит неизвестное правило")

    node_ids = {node.id for node in _walk_nodes(document.nodes)}
    findings: list[CheckFinding] = []
    for finding in model_report.findings:
        if finding.rule_id not in rules:
            raise WorkflowError("Отчёт содержит неизвестное правило")
        if finding.node_id is not None and finding.node_id not in node_ids:
            raise WorkflowError("Отчёт содержит неизвестный узел документа")
        if finding.rule_id in unchecked:
            raise WorkflowError("Правило не может одновременно иметь находку и быть непроверенным")
        severity = Severity(rules[finding.rule_id].severity)
        findings.append(finding.model_copy(update={"severity": severity}))

    confirmed = [finding for finding in findings if finding.confidence >= _LOW_CONFIDENCE_THRESHOLD]
    low_confidence = [
        finding for finding in findings if finding.confidence < _LOW_CONFIDENCE_THRESHOLD
    ]
    ordered_unchecked = [rule.id for rule in template.rules if rule.id in unchecked]
    try:
        return CheckReport.model_validate(
            {
                "template_id": template.id,
                "findings": confirmed + low_confidence,
                "unchecked_rules": ordered_unchecked,
            }
        )
    except ValidationError as exc:
        raise WorkflowError(_REPORT_SCHEMA_ERROR) from exc


def _walk_nodes(nodes: list) -> list:
    result = []
    for node in nodes:
        result.append(node)
        result.extend(_walk_nodes(node.children))
    return result


def _check_prompt(
    template: SemanticTemplate,
    blocks: list[NormalizedBlock],
    document: WorkingDocument,
) -> str:
    payload = {
        "задача": "Проверьте каждое правило; непроверенные id запишите в unchecked_rules.",
        "правила": [rule.model_dump(mode="json") for rule in template.rules],
        "стилевые_правила": list(template.style_rules),
        "исходные_блоки": public_blocks(blocks),
        "документ": document.model_dump(mode="json"),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


__all__ = ["CheckWorkflow"]
