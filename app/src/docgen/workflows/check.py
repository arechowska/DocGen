from __future__ import annotations

import json

from pydantic import ValidationError

from docgen.ai.client import TextModel, VisionModel
from docgen.ai.grounding import GroundingValidator
from docgen.ai.prompts import DOCUMENT_SYSTEM_PROMPT
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import CheckFinding, CheckReport, Severity, WorkingDocument
from docgen.extraction.schemas import NormalizedBlock
from docgen.jobs.models import Job, JobKind
from docgen.jobs.runner import ProgressSink
from docgen.projects.repository import ProjectRepository
from docgen.templates_catalog.loader import TemplateCatalog
from docgen.templates_catalog.schemas import SemanticTemplate

from .assemble import WorkflowError, enrich_images, public_blocks
from .normalize import NormalizationWorkflow

_CHECK_KIND_ERROR = "Некорректный тип задания для проверки"
_PROJECT_NOT_FOUND = "Проект не найден"
_DOCUMENT_NOT_FOUND = "Документ для проверки не найден"
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
        document = self._documents.get_document(job.project_id)
        if document is None:
            raise WorkflowError(_DOCUMENT_NOT_FOUND)

        normalized = self._normalization.run(
            job.project_id,
            before_extract=lambda: progress(35, "Нормализация источников"),
        )
        blocks = enrich_images(normalized.blocks, self._vision_model, progress)
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
        self._documents.save_report(job.project_id, report)
        return report


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
