from __future__ import annotations

import json

from pydantic import ValidationError

from docgen.ai.client import TextModel, VisionModel
from docgen.ai.grounding import GroundingValidator
from docgen.ai.prompts import CHECK_SYSTEM_PROMPT
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
from docgen.jobs.models import CheckTargetKind, Job, JobKind
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
        target_kind = job.check_target_kind or (
            CheckTargetKind.SOURCE
            if job.target_source_id is not None
            else CheckTargetKind.CURRENT
        )
        document_revision: int | None = None
        document = None
        if target_kind is CheckTargetKind.CURRENT:
            current = self._documents.get_document_with_revision(job.project_id)
            if current is not None:
                document, document_revision = current
        if target_kind is CheckTargetKind.CURRENT:
            if document is None:
                raise WorkflowError(_DOCUMENT_NOT_FOUND)
            if document.template_id != template.id:
                raise WorkflowError(_DOCUMENT_TEMPLATE_ERROR)

        normalized = normalize_sources(self._normalization, job.project_id, progress)
        blocks = enrich_images(normalized.blocks, self._vision_model, progress)
        if target_kind is CheckTargetKind.SOURCE:
            if job.target_source_id is None:
                raise WorkflowError(_DOCUMENT_NOT_FOUND)
            document = _target_document(job.target_source_id, template.id, blocks)
        assert document is not None
        if self._grounding.validate(document, {block.id: block for block in blocks}):
            raise WorkflowError(_DOCUMENT_GROUNDING_ERROR)

        progress(90, "Проверка документа моделью")
        raw_report = self._text_model.generate_json(
            CHECK_SYSTEM_PROMPT,
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
        if target_kind is CheckTargetKind.SOURCE:
            self._documents.save_document_and_report(job.project_id, document, report)
        else:
            assert document_revision is not None
            self._documents.save_report(
                job.project_id,
                report,
                expected_document_revision=document_revision,
            )
        return report


def _target_document(
    target_source_id: str,
    template_id: str,
    blocks: list[NormalizedBlock],
) -> WorkingDocument:
    target_blocks = [
        block
        for block in blocks
        if any(item.source_id == target_source_id for item in block.provenance)
    ]
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
            Provenance(source_id=block.id, locator=locator, quote=block.text)
            for locator in locators
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
    passed_ids = list(model_report.passed_rule_ids)
    unchecked_ids = list(model_report.unchecked_rules)
    finding_rule_ids = [finding.rule_id for finding in model_report.findings]
    node_ids = {node.id for node in _walk_nodes(document.nodes)}
    for finding in model_report.findings:
        if finding.rule_id not in rules:
            raise WorkflowError("Отчёт содержит неизвестное правило")
        if finding.node_id is not None and finding.node_id not in node_ids:
            raise WorkflowError("Отчёт содержит неизвестный узел документа")
    if (
        any(not rule_id or not rule_id.strip() for rule_id in passed_ids)
        or any(not rule_id or not rule_id.strip() for rule_id in unchecked_ids)
        or any(not rule_id or not rule_id.strip() for rule_id in finding_rule_ids)
        or len(passed_ids) != len(set(passed_ids))
        or len(unchecked_ids) != len(set(unchecked_ids))
        or len(finding_rule_ids) != len(set(finding_rule_ids))
    ):
        raise WorkflowError("Отчёт содержит некорректное покрытие правил")

    passed = set(passed_ids)
    unchecked = set(unchecked_ids)
    failed = set(finding_rule_ids)
    unknown_coverage = (passed | unchecked) - rules.keys()
    if unknown_coverage:
        raise WorkflowError("Отчёт содержит некорректное покрытие правил")
    if passed & unchecked or passed & failed or unchecked & failed:
        raise WorkflowError("Отчёт содержит некорректное покрытие правил")
    if passed | failed | unchecked != rules.keys():
        raise WorkflowError("Отчёт содержит некорректное покрытие правил")

    unknown_unchecked = unchecked - rules.keys()
    if unknown_unchecked:
        raise WorkflowError("Отчёт содержит неизвестное правило")

    findings: list[CheckFinding] = []
    for finding in model_report.findings:
        severity = Severity(rules[finding.rule_id].severity)
        findings.append(finding.model_copy(update={"severity": severity}))

    confirmed = [finding for finding in findings if finding.confidence >= _LOW_CONFIDENCE_THRESHOLD]
    low_confidence = [
        finding for finding in findings if finding.confidence < _LOW_CONFIDENCE_THRESHOLD
    ]
    ordered_unchecked = [rule.id for rule in template.rules if rule.id in unchecked]
    ordered_passed = tuple(rule.id for rule in template.rules if rule.id in passed)
    try:
        return CheckReport.model_validate(
            {
                "template_id": template.id,
                "findings": confirmed + low_confidence,
                "passed_rule_ids": ordered_passed,
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
        "задача": (
            "Проверьте каждое правило ровно один раз: правила без проблем запишите в "
            "passed_rule_ids, правила с проблемами представьте одной finding, а правила, "
            "которые невозможно проверить, запишите в unchecked_rules. Эти три группы "
            "должны быть непересекающимся полным покрытием всех rule id."
        ),
        "важно": (
            f"template_id в ответе должен быть ровно '{template.id}', не "
            f"{template.id}_validation_result и не faq_validation_result. Верните только "
            "CheckReport: template_id, findings, passed_rule_ids, unchecked_rules."
        ),
        "формат_ответа": {
            "template_id": template.id,
            "findings": [],
            "passed_rule_ids": [rule.id for rule in template.rules],
            "unchecked_rules": [],
        },
        "правила": [rule.model_dump(mode="json") for rule in template.rules],
        "стилевые_правила": list(template.style_rules),
        "исходные_блоки": public_blocks(blocks),
        "документ": document.model_dump(mode="json"),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


__all__ = ["CheckWorkflow"]
