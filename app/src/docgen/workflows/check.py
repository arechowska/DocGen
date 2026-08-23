from __future__ import annotations

import json
import re
from uuid import uuid4

from pydantic import ValidationError

from docgen.ai.client import TextModel, VisionModel
from docgen.ai.grounding import GroundingValidator
from docgen.ai.prompts import CHECK_SYSTEM_PROMPT
from docgen.documents.operations import DocumentOperation, InsertNode
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import (
    CheckFinding,
    CheckReport,
    DocumentNode,
    DocumentOrigin,
    NodeKind,
    Severity,
    WorkingDocument,
)
from docgen.extraction.schemas import BlockKind, NormalizedBlock, Provenance
from docgen.jobs.models import CheckTargetKind, Job, JobKind
from docgen.jobs.runner import ProgressSink
from docgen.projects.repository import ProjectRepository
from docgen.templates_catalog.loader import NO_TEMPLATE_ID, TemplateCatalog
from docgen.templates_catalog.schemas import (
    SemanticStructureCheck,
    SemanticTableCheck,
    SemanticTemplate,
)

from .assemble import enrich_images, normalize_sources, public_blocks
from .errors import WorkflowError
from .normalize import NormalizationWorkflow

_CHECK_KIND_ERROR = "Некорректный тип задания для проверки"
_PROJECT_NOT_FOUND = "Проект не найден"
_DOCUMENT_NOT_FOUND = "Документ для проверки не найден"
_SOURCE_UNAVAILABLE_ERROR = (
    "Не удалось получить выбранный источник; проверьте доступ и повторите проверку"
)
_SOURCE_EMPTY_ERROR = "В выбранном источнике нет извлекаемого содержимого"
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
        current = self._documents.get_document_with_revision(job.project_id)
        document_revision: int | None = None
        document = None
        if current is not None:
            current_document, current_revision = current
            if target_kind is CheckTargetKind.CURRENT or (
                target_kind is CheckTargetKind.SOURCE
                and current_document.origin is DocumentOrigin.IMPORTED
                and current_document.source_id == job.target_source_id
            ):
                document, document_revision = current_document, current_revision
        if target_kind is CheckTargetKind.CURRENT and document is None:
            raise WorkflowError(_DOCUMENT_NOT_FOUND)

        normalized = normalize_sources(self._normalization, job.project_id, progress)
        blocks = enrich_images(normalized.blocks, self._vision_model, progress)
        evidence_blocks = blocks
        if target_kind is CheckTargetKind.SOURCE:
            if job.target_source_id is None:
                raise WorkflowError(_DOCUMENT_NOT_FOUND)
            target_blocks = _blocks_for_source(job.target_source_id, blocks)
            if not target_blocks:
                if job.target_source_id in normalized.unavailable_source_ids:
                    raise WorkflowError(_SOURCE_UNAVAILABLE_ERROR)
                raise WorkflowError(_SOURCE_EMPTY_ERROR)
            evidence_blocks = target_blocks
            if document is None:
                document = _target_document(
                    job.target_source_id,
                    template.id,
                    evidence_blocks,
                )
        assert document is not None
        if self._grounding.validate(document, {block.id: block for block in blocks}):
            raise WorkflowError(_DOCUMENT_GROUNDING_ERROR)

        progress(90, "Проверка документа моделью")
        model_template = _template_for_model_check(template)
        raw_report = self._text_model.generate_json(
            CHECK_SYSTEM_PROMPT,
            _check_prompt(model_template, evidence_blocks, document),
            CheckReport,
        )
        try:
            model_report = CheckReport.model_validate(raw_report)
        except ValidationError as exc:
            raise WorkflowError(_REPORT_SCHEMA_ERROR) from exc
        report = _validated_report(model_report, model_template, document)
        report = _merge_structure_check(report, template, document)

        progress(100, "Сохранение отчёта")
        progress.checkpoint()
        if document_revision is not None:
            self._documents.save_report(
                job.project_id,
                report,
                expected_document_revision=document_revision,
                target_source_id=job.target_source_id,
            )
        elif current is None:
            self._documents.save_document_and_report(job.project_id, document, report)
        else:
            self._documents.save_report(
                job.project_id,
                report,
                expected_document_revision=current_revision,
                target_source_id=job.target_source_id,
            )
        return report


def _target_document(
    target_source_id: str,
    template_id: str,
    blocks: list[NormalizedBlock],
) -> WorkingDocument:
    del template_id
    target_blocks = _target_blocks(target_source_id, blocks)
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
        template_id=NO_TEMPLATE_ID,
        origin=DocumentOrigin.IMPORTED,
        source_id=target_source_id,
        nodes=[_node_from_block(block) for block in target_blocks],
    )


def _target_blocks(
    target_source_id: str,
    blocks: list[NormalizedBlock],
) -> list[NormalizedBlock]:
    target_blocks = _blocks_for_source(target_source_id, blocks)
    if not target_blocks:
        raise WorkflowError(_DOCUMENT_NOT_FOUND)
    return target_blocks


def _blocks_for_source(
    target_source_id: str,
    blocks: list[NormalizedBlock],
) -> list[NormalizedBlock]:
    return [
        block
        for block in blocks
        if any(item.source_id == target_source_id for item in block.provenance)
    ]


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
    if block.kind is BlockKind.LIST and "items" not in safe_data:
        safe_data["items"] = [block.text]
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


def _template_for_model_check(template: SemanticTemplate) -> SemanticTemplate:
    structure_check = template.structure_check
    if structure_check is None:
        return template
    return template.model_copy(
        update={
            "rules": tuple(
                rule for rule in template.rules if rule.id != structure_check.rule_id
            ),
            "structure_check": None,
        }
    )


def _merge_structure_check(
    report: CheckReport,
    template: SemanticTemplate,
    document: WorkingDocument,
) -> CheckReport:
    contract = template.structure_check
    if contract is None:
        return report
    rules = {rule.id: rule for rule in template.rules}
    rule = rules[contract.rule_id]
    message = _structure_mismatch_message(template, document)
    findings = list(report.findings)
    passed = set(report.passed_rule_ids)
    if message is None:
        passed.add(contract.rule_id)
    else:
        findings.insert(
            0,
            CheckFinding(
                code="template-structure-mismatch",
                severity=Severity(rule.severity),
                confidence=1.0,
                message=message,
                suggestion=_autofix_suggestion(document, template),
                rule_id=contract.rule_id,
            ),
        )
    return CheckReport(
        template_id=template.id,
        findings=findings,
        passed_rule_ids=tuple(rule.id for rule in template.rules if rule.id in passed),
        unchecked_rules=[
            rule.id for rule in template.rules if rule.id in report.unchecked_rules
        ],
    )


def _structure_mismatch_message(
    template: SemanticTemplate,
    document: WorkingDocument,
) -> str | None:
    contract = template.structure_check
    if contract is None:
        return None
    if (
        document.origin is DocumentOrigin.ASSEMBLED
        and document.build_template_id == template.id
    ):
        nodes_by_section = {
            node.section_id: node
            for node in document.nodes
            if node.kind is NodeKind.HEADING and node.section_id
        }
        missing_ids = [
            section_id
            for section_id in contract.assembled_section_ids
            if section_id not in nodes_by_section
            or not nodes_by_section[section_id].children
        ]
        if not missing_ids:
            return None
        titles = {section.id: section.title for section in template.sections}
        missing = ", ".join(
            f"«{titles.get(section_id, section_id)}»" for section_id in missing_ids
        )
        return f"В собранном документе «{template.name}» отсутствуют разделы: {missing}."

    headings = [
        _normalized_heading(node.text)
        for node in _walk_nodes(document.nodes)
        if node.kind is NodeKind.HEADING and node.text
    ]
    missing_headings = [
        heading
        for heading in contract.required_headings
        if _normalized_heading(heading) not in headings
    ]
    table_cells = _table_cells(document)
    differences: list[str] = []
    if missing_headings:
        differences.append(
            "отсутствуют разделы: "
            + ", ".join(f"«{heading}»" for heading in missing_headings)
        )
    for required_table in contract.required_tables:
        expected = {
            label: _normalized_label(label) for label in required_table.required_labels
        }
        best_matches: set[str] = set()
        for cells in table_cells:
            matches = {
                label
                for label, normalized in expected.items()
                if any(normalized in cell for cell in cells)
            }
            if len(matches) > len(best_matches):
                best_matches = matches
        if len(best_matches) == len(expected):
            continue
        missing_labels = [
            label for label in required_table.required_labels if label not in best_matches
        ]
        if not best_matches:
            differences.append(f"отсутствует таблица «{required_table.title}»")
        else:
            differences.append(
                f"в таблице «{required_table.title}» отсутствуют поля: "
                + ", ".join(f"«{label}»" for label in missing_labels)
            )
    if not differences:
        return None
    return (
        f"Структура документа не совпадает с полной формой «{template.name}»:\n"
        + "\n".join(differences)
        + "\n\nПустые значения допустимы — нужно добавить именно отсутствующие "
        "элементы формы."
    )


def structure_gap_operations(
    document: WorkingDocument,
    template: SemanticTemplate,
) -> list[DocumentOperation]:
    """Deterministically insert whatever the structure-check contract says
    is missing -- no model call, so a missing heading or table can never be
    rejected as "unconfirmed": it is a structural fact about the document,
    not a claim that needs source evidence."""
    contract = template.structure_check
    if contract is None:
        return []
    if document.origin is DocumentOrigin.ASSEMBLED and document.build_template_id == template.id:
        return _assembled_section_gap_operations(document, template, contract)
    return _form_gap_operations(document, contract)


def _autofix_suggestion(
    document: WorkingDocument,
    template: SemanticTemplate,
) -> str | None:
    """Describe exactly what the "Внести правку" button will do -- it must
    never promise more than structure_gap_operations actually performs."""
    if not structure_gap_operations(document, template):
        return None
    if document.origin is DocumentOrigin.ASSEMBLED and document.build_template_id == template.id:
        return "Добавить недостающие разделы формы"
    return (
        "Добавить недостающие таблицы формы (пустые заголовки разделов "
        "нужно расставить вручную — правильное место для существующего "
        "текста определить автоматически нельзя)"
    )


def _assembled_section_gap_operations(
    document: WorkingDocument,
    template: SemanticTemplate,
    contract: SemanticStructureCheck,
) -> list[DocumentOperation]:
    nodes_by_section = {
        node.section_id: node
        for node in document.nodes
        if node.kind is NodeKind.HEADING and node.section_id
    }
    titles = {section.id: section.title for section in template.sections}
    operations: list[DocumentOperation] = []
    next_index = len(document.nodes)
    for section_id in contract.assembled_section_ids:
        existing = nodes_by_section.get(section_id)
        if existing is not None and existing.children:
            continue
        if existing is not None:
            operations.append(_gap_insert(parent_id=existing.id))
            continue
        heading_id = f"section-{uuid4()}"
        operations.append(
            InsertNode(
                index=next_index,
                node=DocumentNode(
                    id=heading_id,
                    kind=NodeKind.HEADING,
                    text=titles.get(section_id, section_id),
                    section_id=section_id,
                    flags=["structural-edit"],
                ),
            )
        )
        next_index += 1
        operations.append(_gap_insert(parent_id=heading_id))
    return operations


def _form_gap_operations(
    document: WorkingDocument,
    contract: SemanticStructureCheck,
) -> list[DocumentOperation]:
    # Missing headings are deliberately NOT auto-inserted here: unlike an
    # assembled document (where a section_id ties every node to its slot),
    # an imported/free-form document's existing text is often already the
    # missing section's content, just not wrapped in a matching heading.
    # Blindly adding an empty "Описание" heading next to unlabeled content
    # that IS the description creates a duplicate, meaningless skeleton --
    # deciding where existing prose belongs needs judgment, not a rule.
    operations: list[DocumentOperation] = []
    next_index = len(document.nodes)
    table_cells = _table_cells(document)
    for required_table in contract.required_tables:
        expected = {
            label: _normalized_label(label) for label in required_table.required_labels
        }
        best_matches: set[str] = set()
        for cells in table_cells:
            matches = {
                label
                for label, normalized in expected.items()
                if any(normalized in cell for cell in cells)
            }
            if len(matches) > len(best_matches):
                best_matches = matches
        if best_matches:
            # Table exists with at least some required fields -- adding
            # columns to it automatically risks corrupting real content, so
            # only entirely absent tables are auto-filled.
            continue
        operations.append(
            InsertNode(
                index=next_index,
                node=DocumentNode(
                    kind=NodeKind.TABLE,
                    text=required_table.title,
                    data=_empty_table_data(required_table),
                    flags=["structural-edit"],
                ),
            )
        )
        next_index += 1
    return operations


def _empty_table_data(required_table: SemanticTableCheck) -> dict:
    if required_table.layout == "key_value":
        return {"rows": [[label, ""] for label in required_table.required_labels]}
    return {
        "headers": list(required_table.required_labels),
        "rows": [["" for _ in required_table.required_labels]],
    }


def _gap_insert(*, parent_id: str) -> InsertNode:
    return InsertNode(
        parent_id=parent_id,
        index=0,
        node=DocumentNode(kind=NodeKind.GAP, flags=["structural-edit"]),
    )


def _table_cells(document: WorkingDocument) -> list[list[str]]:
    """Per-table normalized cell text, gathered from both row values (a
    key-value form: label in one cell, value in the next) and header
    labels (a wide table: labels are column headers, not row content)."""
    cells: list[list[str]] = []
    for node in _walk_nodes(document.nodes):
        if node.kind is not NodeKind.TABLE:
            continue
        rows = node.data.get("rows", [])
        headers = node.data.get("headers", [])
        table_cells = [
            _normalized_label(cell)
            for row in rows
            if isinstance(row, list)
            for cell in row
        ]
        if isinstance(headers, list):
            table_cells.extend(_normalized_label(header) for header in headers)
        if table_cells:
            cells.append(table_cells)
    return cells


def _normalized_label(value: object) -> str:
    return " ".join(str(value).split()).casefold()


def _normalized_heading(value: object) -> str:
    return re.sub(r"^\d+(?:\.\d+)*[.)]?\s+", "", _normalized_label(value))


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


__all__ = ["CheckWorkflow", "structure_gap_operations"]
