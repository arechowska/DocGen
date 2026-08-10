from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from docgen.ai.grounding import GroundingValidator
from docgen.documents.schemas import (
    CheckFinding,
    CheckReport,
    DocumentNode,
    NodeKind,
    Severity,
    WorkingDocument,
)
from docgen.extraction.schemas import BlockKind, NormalizedBlock, Provenance
from docgen.jobs.models import Job, JobKind, JobStatus
from docgen.templates_catalog.schemas import SemanticRule, SemanticSection, SemanticTemplate
from docgen.workflows.assemble import WorkflowError
from docgen.workflows.check import CheckWorkflow
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
    block: NormalizedBlock
    events: list[str]

    def run(self, project_id: str, before_extract: Any = None) -> NormalizedProject:
        assert project_id == "p1"
        before_extract()
        self.events.append("extract")
        return NormalizedProject(blocks=[self.block], total_pages=1, warnings=[])


@dataclass
class FakeModel:
    result: object
    events: list[str]
    user_prompt: str = ""

    def generate_json(self, system: str, user: str, schema: type[Any]) -> object:
        assert "низкой уверенностью" in system
        assert schema is CheckReport
        self.events.append("model")
        self.user_prompt = user
        return self.result


@dataclass
class FakeDocuments:
    document: WorkingDocument
    events: list[str]
    report: CheckReport | None = None

    def get_document(self, project_id: str) -> WorkingDocument | None:
        assert project_id == "p1"
        return self.document

    def save_report(self, project_id: str, report: CheckReport) -> None:
        assert project_id == "p1"
        self.events.append("save")
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

    def __call__(self, value: int, status_message: str | None = None) -> None:
        del status_message
        self.values.append(value)
        self.events.append(f"progress:{value}")


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
                provenance=[Provenance(source_id="block-1", locator="paragraph:1")],
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
    assert documents.get_report("p1") == report
    assert all(rule.id in model.user_prompt for rule in semantic_template.rules)
    assert progress.values == [10, 35, 70, 90, 100]
    assert events == [
        "progress:10",
        "progress:35",
        "extract",
        "progress:70",
        "progress:90",
        "model",
        "progress:100",
        "save",
    ]


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
            ),
            "неизвестное правило",
        ),
        (
            CheckReport(
                template_id="use-case",
                findings=[_finding("node", "structure-1", "missing-node", Severity.ERROR, 1)],
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


def _rule(rule_id: str, dimension: Any, severity: Any, instruction: str) -> SemanticRule:
    return SemanticRule(
        id=rule_id,
        dimension=dimension,
        severity=severity,
        instruction=instruction,
    )


def _job() -> Job:
    return Job(
        id="job-check",
        project_id="p1",
        kind=JobKind.CHECK,
        template_id="use-case",
        status=JobStatus.RUNNING,
        progress=0,
        status_message="Выполняется",
        cancel_requested=False,
    )
