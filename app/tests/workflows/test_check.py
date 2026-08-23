from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from docgen.ai.grounding import GroundingValidator
from docgen.documents.operations import InsertNode, UpdateData
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
from docgen.jobs.models import Job, JobKind, JobStatus
from docgen.templates_catalog.loader import NO_TEMPLATE_ID, TemplateCatalog
from docgen.templates_catalog.schemas import SemanticRule, SemanticSection, SemanticTemplate
from docgen.workflows.assemble import WorkflowError
from docgen.workflows.check import (
    CheckWorkflow,
    _check_prompt,
    _merge_structure_check,
    _structure_mismatch_message,
    _target_document,
    _template_for_model_check,
    _validated_report,
    structure_gap_operations,
)
from docgen.workflows.normalize import NormalizedProject


@dataclass
class FakeProjects:
    def get(self, project_id: str) -> object | None:
        return object() if project_id == "p1" else None


@dataclass
class FakeCatalog:
    template: SemanticTemplate

    def get(self, template_id: str) -> SemanticTemplate:
        assert template_id == self.template.id
        return self.template


@dataclass
class FakeNormalization:
    block: NormalizedBlock | None
    events: list[str]
    call_gate: bool = True
    warnings: list[str] = field(default_factory=list)

    def run(self, project_id: str, before_extract: Any = None) -> NormalizedProject:
        assert project_id == "p1"
        if self.call_gate:
            before_extract()
            self.events.append("extract")
        blocks = [self.block] if self.block is not None else []
        return NormalizedProject(
            blocks=blocks,
            total_pages=1,
            warnings=self.warnings,
        )


@dataclass
class FakeModel:
    result: object
    events: list[str]
    system_prompt: str = ""
    user_prompt: str = ""

    def generate_json(self, system: str, user: str, schema: type[Any]) -> object:
        assert "CheckReport" in system
        assert "низкой уверенностью" in system
        assert schema is CheckReport
        self.events.append("model")
        self.system_prompt = system
        self.user_prompt = user
        return self.result


@dataclass
class FakeDocuments:
    document: WorkingDocument | None
    events: list[str]
    report: CheckReport | None = None

    def get_document(self, project_id: str) -> WorkingDocument | None:
        assert project_id == "p1"
        return self.document

    def get_document_with_revision(
        self, project_id: str
    ) -> tuple[WorkingDocument, int] | None:
        document = self.get_document(project_id)
        return None if document is None else (document, 1)

    def save_document(self, project_id: str, document: WorkingDocument) -> None:
        assert project_id == "p1"
        self.events.append("save-document")
        self.document = document

    def save_report(
        self,
        project_id: str,
        report: CheckReport,
        *,
        expected_document_revision: int | None = None,
        target_source_id: str | None = None,
    ) -> None:
        assert project_id == "p1"
        assert expected_document_revision == 1
        del target_source_id
        self.events.append("save")
        self.report = report

    def save_document_and_report(
        self, project_id: str, document: WorkingDocument, report: CheckReport
    ) -> None:
        assert project_id == "p1"
        self.events.extend(["save-document", "save"])
        self.document = document
        self.report = report

    def get_report(self, project_id: str) -> CheckReport | None:
        assert project_id == "p1"
        return self.report


@dataclass
class NoImageVision:
    def describe(self, image: bytes, media_type: str) -> object:
        del image, media_type
        raise AssertionError("text-only checking must not call vision")


@dataclass
class ProgressSpy:
    events: list[str]
    values: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __call__(self, value: int, status_message: str | None = None) -> None:
        del status_message
        self.values.append(value)
        self.events.append(f"progress:{value}")

    def checkpoint(self) -> None:
        self.events.append("checkpoint")

    def report_warnings(self, warnings: list[str]) -> None:
        if warnings:
            self.warnings.extend(warnings)
            self.events.append("warnings")


@pytest.fixture
def semantic_template() -> SemanticTemplate:
    return SemanticTemplate(
        id="use-case",
        name="Сценарий использования",
        version="1",
        sections=(
            SemanticSection(id="actors", title="Участники", required=True, description="Роли участников."),
            SemanticSection(id="flow", title="Поток", required=True, description="Шаги основного потока."),
            SemanticSection(id="result", title="Результат", required=True, description="Итог выполнения."),
        ),
        rules=(
            _rule("structure-1", "structure", "error", "Проверьте структуру документа."),
            _rule("completeness-2", "completeness", "error", "Проверьте полноту сведений."),
            _rule("terminology-3", "terminology", "warning", "Проверьте единство терминологии."),
            _rule("contradiction-4", "contradiction", "error", "Проверьте отсутствие противоречий."),
            _rule("style-5", "style", "warning", "Проверьте деловой стиль текста."),
        ),
        style_rules=("Формулируйте шаги как наблюдаемые действия.",),
    )


@pytest.fixture
def document() -> WorkingDocument:
    return WorkingDocument(
        title="Оплата",
        template_id="use-case",
        nodes=[
            DocumentNode(
                id="node-1",
                kind=NodeKind.PARAGRAPH,
                text="Пользователь оплачивает заказ",
                provenance=[
                    Provenance(
                        source_id="block-1",
                        locator="paragraph:1",
                        quote="Пользователь оплачивает заказ",
                    )
                ],
            )
        ],
    )


@pytest.fixture
def model_report() -> CheckReport:
    return CheckReport(
        template_id="use-case",
        findings=[
            _finding("style", "style-5", "node-1", Severity.INFO, 0.4),
            _finding("structure", "structure-1", "node-1", Severity.INFO, 0.95),
            _finding("complete", "completeness-2", "node-1", Severity.WARNING, 0.9),
            _finding("contradiction", "contradiction-4", "node-1", Severity.WARNING, 0.8),
        ],
        unchecked_rules=["terminology-3"],
    )


@pytest.fixture
def checking(
    semantic_template: SemanticTemplate,
    document: WorkingDocument,
    model_report: CheckReport,
) -> tuple[CheckWorkflow, FakeModel, FakeDocuments, ProgressSpy, list[str]]:
    events: list[str] = []
    block = NormalizedBlock(
        id="block-1",
        kind=BlockKind.TEXT,
        text="Пользователь оплачивает заказ",
        provenance=[Provenance(source_id="source-1", locator="paragraph:1")],
        confidence=1,
    )
    model = FakeModel(model_report, events)
    documents = FakeDocuments(document, events)
    progress = ProgressSpy(events)
    workflow = CheckWorkflow(
        projects=FakeProjects(),
        normalization=FakeNormalization(block, events),
        templates=FakeCatalog(semantic_template),
        text_model=model,
        vision_model=NoImageVision(),
        grounding=GroundingValidator(),
        documents=documents,
    )
    return workflow, model, documents, progress, events


def test_check_separates_confirmed_and_low_confidence_findings_and_checks_every_rule(
    checking: tuple[CheckWorkflow, FakeModel, FakeDocuments, ProgressSpy, list[str]],
    semantic_template: SemanticTemplate,
) -> None:
    workflow, model, documents, progress, events = checking

    report = workflow.run(_job(), progress)

    assert report.findings[0].severity is Severity.ERROR
    assert report.findings[-1].confidence < 0.7
    assert report.findings[-1].severity is Severity.WARNING
    assert report.unchecked_rules == ["terminology-3"]
    assert report.passed_rule_ids == ()
    assert documents.get_report("p1") == report
    assert all(rule.id in model.user_prompt for rule in semantic_template.rules)
    assert "верхнеуровневым узлом" not in model.system_prompt
    assert progress.values == [10, 35, 70, 90, 100]
    assert events == [
        "progress:10",
        "progress:35",
        "checkpoint",
        "extract",
        "progress:70",
        "progress:90",
        "model",
        "progress:100",
        "checkpoint",
        "save",
    ]


def test_check_prompt_requires_exact_template_id_and_report_shape(
    semantic_template: SemanticTemplate,
    document: WorkingDocument,
) -> None:
    block = NormalizedBlock(
        id="block-1",
        kind=BlockKind.TEXT,
        text="Пользователь оплачивает заказ",
        provenance=[Provenance(source_id="source-1", locator="paragraph:1")],
        confidence=1,
    )

    payload = json.loads(_check_prompt(semantic_template, [block], document))

    assert payload["формат_ответа"]["template_id"] == semantic_template.id
    assert payload["формат_ответа"]["findings"] == []
    assert payload["формат_ответа"]["passed_rule_ids"] == [
        rule.id for rule in semantic_template.rules
    ]
    assert "не faq_validation_result" in payload["важно"]


def test_check_publishes_normalization_warnings_before_model_call(
    checking: tuple[CheckWorkflow, FakeModel, FakeDocuments, ProgressSpy, list[str]],
) -> None:
    workflow, _model, _documents, progress, events = checking
    workflow._normalization.warnings = [  # type: ignore[attr-defined]
        "Страница 2 не содержит извлекаемого текста",
        "Обработка может занять более пяти минут",
    ]

    workflow.run(_job(), progress)

    assert progress.warnings == [
        "Страница 2 не содержит извлекаемого текста",
        "Обработка может занять более пяти минут",
    ]
    assert events.index("warnings") < events.index("model")


def test_report_rule_outcomes_form_an_exact_partition(
    semantic_template: SemanticTemplate,
    document: WorkingDocument,
) -> None:
    report = CheckReport(
        template_id="use-case",
        findings=[_finding("structure", "structure-1", "node-1", Severity.INFO, 0.9)],
        passed_rule_ids=["completeness-2", "terminology-3"],
        unchecked_rules=["contradiction-4", "style-5"],
    )

    validated = _validated_report(report, semantic_template, document)

    assert validated.passed_rule_ids == ("completeness-2", "terminology-3")


@pytest.mark.parametrize(
    "report",
    [
        CheckReport(template_id="use-case"),
        CheckReport(
            template_id="use-case",
            passed_rule_ids=["structure-1", "structure-1"],
            unchecked_rules=[
                "completeness-2",
                "terminology-3",
                "contradiction-4",
                "style-5",
            ],
        ),
        CheckReport(
            template_id="use-case",
            findings=[
                CheckFinding(
                    code="first",
                    rule_id="structure-1",
                    node_id="node-1",
                    severity=Severity.ERROR,
                    confidence=0.9,
                    message="Первая проблема",
                ),
                CheckFinding(
                    code="second",
                    rule_id="structure-1",
                    node_id="node-1",
                    severity=Severity.ERROR,
                    confidence=0.8,
                    message="Вторая проблема",
                ),
            ],
            unchecked_rules=[
                "completeness-2",
                "terminology-3",
                "contradiction-4",
                "style-5",
            ],
        ),
        CheckReport(
            template_id="use-case",
            passed_rule_ids=["structure-1"],
            unchecked_rules=["structure-1", "completeness-2", "terminology-3", "contradiction-4", "style-5"],
        ),
        CheckReport(
            template_id="use-case",
            passed_rule_ids=["unknown-rule"],
            unchecked_rules=["structure-1", "completeness-2", "terminology-3", "contradiction-4", "style-5"],
        ),
    ],
)
def test_report_rejects_incomplete_unknown_overlapping_or_duplicate_coverage(
    semantic_template: SemanticTemplate,
    document: WorkingDocument,
    report: CheckReport,
) -> None:
    with pytest.raises(WorkflowError, match="покрытие правил"):
        _validated_report(report, semantic_template, document)


def test_standalone_check_maps_target_blocks_and_saves_document_only_with_validated_report(
    semantic_template: SemanticTemplate,
) -> None:
    events: list[str] = []
    blocks = [
        _target_block("heading", BlockKind.HEADING, "Раздел", {"level": 2}),
        _target_block("text", BlockKind.TEXT, "Описание"),
        _target_block("list", BlockKind.LIST, "Первый", {"items": ["Первый"]}),
        _target_block("table", BlockKind.TABLE, "Поле", {"rows": [["Поле"]]}),
        _target_block("image", BlockKind.IMAGE, "Схема"),
        NormalizedBlock(
            id="evidence-source:text",
            kind=BlockKind.TEXT,
            text="Контекст проекта",
            provenance=[Provenance(source_id="evidence-source", locator="line:1")],
            confidence=1,
        ),
    ]

    class TargetNormalization:
        def run(self, project_id: str, before_extract: Any = None) -> NormalizedProject:
            assert project_id == "p1"
            assert before_extract is not None
            before_extract()
            events.append("extract")
            return NormalizedProject(blocks=blocks, total_pages=1, warnings=[])

    model = FakeModel(
        CheckReport(
            template_id="use-case",
            unchecked_rules=[rule.id for rule in semantic_template.rules],
        ),
        events,
    )
    documents = FakeDocuments(None, events)
    progress = ProgressSpy(events)
    workflow = CheckWorkflow(
        projects=FakeProjects(),
        normalization=TargetNormalization(),
        templates=FakeCatalog(semantic_template),
        text_model=model,
        vision_model=NoImageVision(),
        grounding=GroundingValidator(),
        documents=documents,
    )

    report = workflow.run(_job(target_source_id="source-1"), progress)

    assert report.unchecked_rules == [rule.id for rule in semantic_template.rules]
    assert documents.document is not None
    assert documents.document.title == "Раздел"
    assert documents.document.template_id == "no-template"
    assert documents.document.origin is DocumentOrigin.IMPORTED
    assert documents.document.source_id == "source-1"
    assert documents.document.build_template_id is None
    assert [node.kind for node in documents.document.nodes] == [
        NodeKind.HEADING,
        NodeKind.PARAGRAPH,
        NodeKind.LIST,
        NodeKind.TABLE,
        NodeKind.IMAGE,
    ]
    assert [node.id for node in documents.document.nodes] == [
        f"document-node:source-1:{block_id}"
        for block_id in ("heading", "text", "list", "table", "image")
    ]
    assert all(
        node.provenance[0].source_id == block.id
        for node, block in zip(documents.document.nodes, blocks[:5], strict=True)
    )
    assert "Контекст проекта" not in model.user_prompt
    assert events.index("model") < events.index("save-document")
    assert events[-3:] == ["checkpoint", "save-document", "save"]


def test_standalone_check_reports_selected_source_without_content_before_model(
    semantic_template: SemanticTemplate,
) -> None:
    events: list[str] = []
    documents = FakeDocuments(None, events)
    workflow = CheckWorkflow(
        projects=FakeProjects(),
        normalization=FakeNormalization(
            _target_block("text", BlockKind.TEXT, "Описание"), events
        ),
        templates=FakeCatalog(semantic_template),
        text_model=FakeModel(CheckReport(template_id="use-case"), events),
        vision_model=NoImageVision(),
        grounding=GroundingValidator(),
        documents=documents,
    )

    with pytest.raises(WorkflowError, match="нет извлекаемого содержимого"):
        workflow.run(_job(target_source_id="other-source"), ProgressSpy(events))

    assert documents.document is None
    assert "model" not in events


def test_standalone_target_uses_source_provenance_not_block_id_prefix() -> None:
    block = NormalizedBlock(
        id="64a4ed5b-84a9-57a7-a38e-31bb2e93ca98",
        kind=BlockKind.TEXT,
        text="Описание из файла",
        provenance=[Provenance(source_id="source-1", locator="line:1")],
        confidence=1,
    )

    document = _target_document("source-1", "use-case", [block])

    assert [node.text for node in document.nodes] == ["Описание из файла"]


def test_standalone_check_reports_unavailable_selected_source(
    semantic_template: SemanticTemplate,
) -> None:
    events: list[str] = []

    class UnavailableNormalization:
        def run(self, project_id: str, before_extract=None) -> NormalizedProject:
            del project_id, before_extract
            return NormalizedProject(
                blocks=[],
                total_pages=0,
                warnings=["Источник Confluence пропущен: нет доступа"],
                unavailable_source_ids=("source-1",),
            )

    workflow = CheckWorkflow(
        projects=FakeProjects(),
        normalization=UnavailableNormalization(),
        templates=FakeCatalog(semantic_template),
        text_model=FakeModel(CheckReport(template_id="use-case"), events),
        vision_model=NoImageVision(),
        grounding=GroundingValidator(),
        documents=FakeDocuments(None, events),
    )

    with pytest.raises(WorkflowError, match="проверьте доступ"):
        workflow.run(_job(target_source_id="source-1"), ProgressSpy(events))

    assert "model" not in events


def test_standalone_check_does_not_replace_current_document_when_report_is_invalid(
    semantic_template: SemanticTemplate,
    document: WorkingDocument,
) -> None:
    events: list[str] = []
    documents = FakeDocuments(document, events)
    workflow = CheckWorkflow(
        projects=FakeProjects(),
        normalization=FakeNormalization(
            _target_block("text", BlockKind.TEXT, "Новое описание"), events
        ),
        templates=FakeCatalog(semantic_template),
        text_model=FakeModel(
            CheckReport(
                template_id="use-case",
                findings=[
                    _finding(
                        "unknown",
                        "unknown-rule",
                        "document-node:source-1:text",
                        Severity.ERROR,
                        1,
                    )
                ],
            ),
            events,
        ),
        vision_model=NoImageVision(),
        grounding=GroundingValidator(),
        documents=documents,
    )

    with pytest.raises(WorkflowError, match="неизвестное правило"):
        workflow.run(_job(target_source_id="source-1"), ProgressSpy(events))

    assert documents.document is document
    assert "save-document" not in events


def test_standalone_check_attaches_report_without_replacing_unrelated_current_document(
    semantic_template: SemanticTemplate,
    document: WorkingDocument,
) -> None:
    """Checking a source must never overwrite an existing working document that
    belongs to a different source -- only the report is attached to it."""
    events: list[str] = []
    documents = FakeDocuments(document, events)
    workflow = CheckWorkflow(
        projects=FakeProjects(),
        normalization=FakeNormalization(
            _target_block("text", BlockKind.TEXT, "Новое описание"), events
        ),
        templates=FakeCatalog(semantic_template),
        text_model=FakeModel(
            CheckReport(
                template_id="use-case",
                passed_rule_ids=[rule.id for rule in semantic_template.rules],
            ),
            events,
        ),
        vision_model=NoImageVision(),
        grounding=GroundingValidator(),
        documents=documents,
    )

    report = workflow.run(_job(target_source_id="source-1"), ProgressSpy(events))

    assert documents.document is document
    assert "save-document" not in events
    assert "save" in events
    assert documents.report is report


def _finding(
    code: str,
    rule_id: str,
    node_id: str,
    severity: Severity,
    confidence: float,
) -> CheckFinding:
    return CheckFinding(
        code=code,
        severity=severity,
        confidence=confidence,
        message="Найдена проблема",
        node_id=node_id,
        rule_id=rule_id,
    )


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (
            {"template_id": "use-case", "findings": [{"severity": "critical"}]},
            "Модель вернула некорректный отчёт",
        ),
        (
            CheckReport(
                template_id="use-case",
                findings=[_finding("unknown", "unknown-rule", "node-1", Severity.ERROR, 1)],
                passed_rule_ids=[
                    "structure-1",
                    "completeness-2",
                    "terminology-3",
                    "contradiction-4",
                    "style-5",
                ],
            ),
            "неизвестное правило",
        ),
        (
            CheckReport(
                template_id="use-case",
                findings=[_finding("node", "structure-1", "missing-node", Severity.ERROR, 1)],
                passed_rule_ids=[
                    "completeness-2",
                    "terminology-3",
                    "contradiction-4",
                    "style-5",
                ],
            ),
            "неизвестный узел",
        ),
    ],
)
def test_check_rejects_invalid_or_ungrounded_report_without_replacing_previous_one(
    checking: tuple[CheckWorkflow, FakeModel, FakeDocuments, ProgressSpy, list[str]],
    result: object,
    message: str,
) -> None:
    workflow, model, documents, progress, _events = checking
    previous = CheckReport(template_id="use-case", unchecked_rules=["previous"])
    documents.report = previous
    model.result = result

    with pytest.raises(WorkflowError, match=message):
        workflow.run(_job(), progress)

    assert documents.get_report("p1") == previous


def test_check_emits_normalization_stage_once_when_project_has_no_sources(
    semantic_template: SemanticTemplate,
) -> None:
    events: list[str] = []
    progress = ProgressSpy(events)
    document = WorkingDocument(
        title="Нет исходных данных",
        template_id="use-case",
        nodes=[DocumentNode(kind=NodeKind.GAP, flags=["missing-source-data"])],
    )
    documents = FakeDocuments(document, events)
    workflow = CheckWorkflow(
        projects=FakeProjects(),
        normalization=FakeNormalization(None, events, call_gate=False),
        templates=FakeCatalog(semantic_template),
        text_model=FakeModel(
            CheckReport(
                template_id="use-case",
                unchecked_rules=[rule.id for rule in semantic_template.rules],
            ),
            events,
        ),
        vision_model=NoImageVision(),
        grounding=GroundingValidator(),
        documents=documents,
    )

    workflow.run(_job(), progress)

    assert progress.values == [10, 35, 70, 90, 100]


def test_check_allows_current_document_to_use_another_semantic_profile(
    checking: tuple[CheckWorkflow, FakeModel, FakeDocuments, ProgressSpy, list[str]],
) -> None:
    workflow, _model, documents, progress, events = checking
    documents.document = documents.document.model_copy(update={"template_id": "faq"})
    report = workflow.run(_job(), progress)

    assert report.template_id == "use-case"
    assert documents.document.template_id == "faq"
    assert events[-1] == "save"


def test_use_case_form_check_is_local_and_not_delegated_to_model() -> None:
    template = TemplateCatalog().get("use-case")

    model_template = _template_for_model_check(template)
    prompt = json.loads(
        _check_prompt(
            model_template,
            [],
            WorkingDocument(title="Исходник", template_id=NO_TEMPLATE_ID),
        )
    )

    assert "use-case-template-form" not in {
        rule["id"] for rule in prompt["правила"]
    }
    assert {rule.id for rule in model_template.rules} == {
        rule.id for rule in template.rules if rule.id != "use-case-template-form"
    }


def test_use_case_form_check_reports_missing_structure_even_when_model_passes() -> None:
    template = TemplateCatalog().get("use-case")
    model_template = _template_for_model_check(template)
    document = WorkingDocument(
        title="Исходный Use Case",
        template_id=NO_TEMPLATE_ID,
        origin=DocumentOrigin.IMPORTED,
        source_id="source-1",
        nodes=[DocumentNode(kind=NodeKind.PARAGRAPH, text="Описание сценария")],
    )
    model_report = CheckReport(
        template_id=template.id,
        passed_rule_ids=tuple(rule.id for rule in model_template.rules),
    )

    validated = _validated_report(model_report, model_template, document)
    report = _merge_structure_check(validated, template, document)

    finding = next(
        finding
        for finding in report.findings
        if finding.rule_id == "use-case-template-form"
    )
    assert finding.code == "template-structure-mismatch"
    assert finding.confidence == 1
    assert "«Общие сведения»" in finding.message
    assert "«История изменений»" in finding.message
    assert "use-case-template-form" not in report.passed_rule_ids
    # Each gap (missing headings, each missing/incomplete table) is its own
    # line -- a single run-on "; "-joined sentence is unreadable once there
    # are several gaps.
    assert finding.message.count("\n") >= 2
    assert "; " not in finding.message
    # The user does not have to locate an external "full form": the selected
    # template already owns it, so the next step is phrased as a system action.
    assert finding.suggestion is not None
    assert "Добавить отсутствующие поля" in finding.suggestion
    assert "полной форм" not in finding.suggestion
    assert "вручную" not in finding.suggestion


def test_structure_gap_operations_do_not_guess_where_absent_tables_belong() -> None:
    template = TemplateCatalog().get("use-case")
    contract = template.structure_check
    assert contract is not None
    document = WorkingDocument(
        title="Исходный Use Case",
        template_id=NO_TEMPLATE_ID,
        origin=DocumentOrigin.IMPORTED,
        source_id="source-1",
        nodes=[DocumentNode(kind=NodeKind.PARAGRAPH, text="Описание сценария")],
    )

    operations = structure_gap_operations(document, template)
    assert operations == []


def test_structure_gap_operations_never_auto_insert_missing_headings() -> None:
    """A missing heading in an imported/free-form document is never
    auto-filled: existing unlabeled text is often already that section's
    content, and deciding where it belongs needs judgment, not a rule."""
    template = TemplateCatalog().get("use-case")
    document = WorkingDocument(
        title="Исходный Use Case",
        template_id=NO_TEMPLATE_ID,
        origin=DocumentOrigin.IMPORTED,
        source_id="source-1",
        nodes=[
            DocumentNode(
                kind=NodeKind.PARAGRAPH,
                text="1. Пользователь открывает задачу «Ведение картотеки клиентов».",
            )
        ],
    )

    operations = structure_gap_operations(document, template)

    assert not any(
        isinstance(operation, InsertNode) and operation.node.kind is NodeKind.HEADING
        for operation in operations
    )


def test_structure_gap_operations_do_not_insert_an_absent_wide_table() -> None:
    template = TemplateCatalog().get("use-case")
    contract = template.structure_check
    assert contract is not None
    technical_specs = next(
        table for table in contract.required_tables if table.id == "technical-specifications"
    )
    assert technical_specs.layout == "header_row"
    document = WorkingDocument(
        title="Исходный Use Case",
        template_id=NO_TEMPLATE_ID,
        origin=DocumentOrigin.IMPORTED,
        source_id="source-1",
        nodes=[DocumentNode(kind=NodeKind.PARAGRAPH, text="Описание сценария")],
    )

    operations = structure_gap_operations(document, template)
    assert operations == []


def test_structure_gap_operations_append_only_missing_key_value_rows() -> None:
    template = TemplateCatalog().get("use-case")
    contract = template.structure_check
    assert contract is not None
    metadata_table = next(table for table in contract.required_tables if table.id == "metadata")
    document = WorkingDocument(
        title="Исходный Use Case",
        template_id=NO_TEMPLATE_ID,
        origin=DocumentOrigin.IMPORTED,
        source_id="source-1",
        nodes=[
            DocumentNode(
                id="partial-metadata",
                kind=NodeKind.TABLE,
                data={"rows": [[metadata_table.required_labels[0], "УК-1"]]},
            )
        ],
    )

    operations = structure_gap_operations(document, template)

    update = next(
        operation
        for operation in operations
        if isinstance(operation, UpdateData) and operation.node_id == "partial-metadata"
    )
    assert update.data["rows"][0] == [metadata_table.required_labels[0], "УК-1"]
    assert [row[0] for row in update.data["rows"][1:]] == list(
        metadata_table.required_labels[1:]
    )
    assert all(row[1] == "" for row in update.data["rows"][1:])


def test_structure_gap_operations_append_columns_without_changing_existing_cells() -> None:
    template = TemplateCatalog().get("use-case")
    contract = template.structure_check
    assert contract is not None
    table_contract = next(
        table for table in contract.required_tables if table.id == "technical-specifications"
    )
    original_headers = list(table_contract.required_labels[:2])
    original_row = ["Название API", "REST"]
    document = WorkingDocument(
        title="Документ",
        template_id=NO_TEMPLATE_ID,
        nodes=[
            DocumentNode(
                id="spec-table",
                kind=NodeKind.TABLE,
                data={"headers": original_headers, "rows": [original_row]},
            )
        ],
    )

    operations = structure_gap_operations(document, template)

    update = next(
        operation
        for operation in operations
        if isinstance(operation, UpdateData) and operation.node_id == "spec-table"
    )
    assert update.data["headers"][:2] == original_headers
    assert update.data["headers"][2:] == list(table_contract.required_labels[2:])
    assert update.data["rows"][0][:2] == original_row
    assert update.data["rows"][0][2:] == [""] * len(table_contract.required_labels[2:])


def test_structure_gap_operations_skip_ambiguous_generic_table() -> None:
    template = TemplateCatalog().get("use-case")
    document = WorkingDocument(
        title="Документ",
        template_id=NO_TEMPLATE_ID,
        nodes=[
            DocumentNode(
                id="ambiguous-table",
                kind=NodeKind.TABLE,
                data={"rows": [["Примечание", "Сохранить"]]},
            )
        ],
    )

    assert structure_gap_operations(document, template) == []


def test_structure_check_recognizes_a_header_row_table_by_its_headers() -> None:
    """A table whose labels live in "headers" (a real corporate-form wide
    table: one header row, data rows below) must count as present -- the
    labels are never duplicated into row values in that layout."""
    template = TemplateCatalog().get("use-case")
    contract = template.structure_check
    assert contract is not None
    links_table = next(table for table in contract.required_tables if table.id == "links")
    document = WorkingDocument(
        title="Исходный Use Case",
        template_id=NO_TEMPLATE_ID,
        origin=DocumentOrigin.IMPORTED,
        source_id="source-1",
        nodes=[
            DocumentNode(
                kind=NodeKind.TABLE,
                data={
                    "headers": list(links_table.required_labels),
                    "rows": [["" for _ in links_table.required_labels]],
                },
            )
        ],
    )

    message = _structure_mismatch_message(template, document)

    assert message is not None
    assert "отсутствует таблица «Ссылки»" not in message
    assert "в таблице «Ссылки» отсутствуют" not in message


def test_use_case_form_check_accepts_empty_values_in_complete_form() -> None:
    template = TemplateCatalog().get("use-case")
    contract = template.structure_check
    assert contract is not None
    nodes = [
        DocumentNode(kind=NodeKind.HEADING, text=heading)
        for heading in contract.required_headings
    ]
    nodes.extend(
        DocumentNode(
            kind=NodeKind.TABLE,
            data={"rows": [[label, ""] for label in table.required_labels]},
        )
        for table in contract.required_tables
    )
    document = WorkingDocument(
        title="Корпоративный Use Case",
        template_id=NO_TEMPLATE_ID,
        origin=DocumentOrigin.IMPORTED,
        source_id="source-1",
        nodes=nodes,
    )
    model_template = _template_for_model_check(template)
    model_report = CheckReport(
        template_id=template.id,
        passed_rule_ids=tuple(rule.id for rule in model_template.rules),
    )

    validated = _validated_report(model_report, model_template, document)
    report = _merge_structure_check(validated, template, document)

    assert report.findings == []
    assert report.passed_rule_ids == tuple(rule.id for rule in template.rules)


def _rule(rule_id: str, dimension: Any, severity: Any, instruction: str) -> SemanticRule:
    return SemanticRule(
        id=rule_id,
        dimension=dimension,
        severity=severity,
        instruction=instruction,
    )


def _target_block(
    block_id: str,
    kind: BlockKind,
    text: str,
    data: dict | None = None,
) -> NormalizedBlock:
    return NormalizedBlock(
        id=f"source-1:{block_id}",
        kind=kind,
        text=text,
        data=data or {},
        provenance=[Provenance(source_id="source-1", locator=f"block:{block_id}")],
        confidence=1,
    )


def _job(target_source_id: str | None = None) -> Job:
    return Job(
        id="job-check",
        project_id="p1",
        kind=JobKind.CHECK,
        template_id="use-case",
        status=JobStatus.RUNNING,
        progress=0,
        status_message="Выполняется",
        cancel_requested=False,
        target_source_id=target_source_id,
    )
