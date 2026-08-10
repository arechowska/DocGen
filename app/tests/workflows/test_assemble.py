from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from docgen.ai.client import ModelConfigurationError, VisionDescription
from docgen.ai.grounding import GroundingValidator
from docgen.config import Settings
from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.extraction.schemas import BlockKind, NormalizedBlock, Provenance
from docgen.jobs.models import Job, JobKind, JobStatus
from docgen.jobs.runner import WorkflowDependencies, build_workflows
from docgen.templates_catalog.loader import TemplateCatalog
from docgen.workflows.assemble import AssembleWorkflow, WorkflowError
from docgen.workflows.normalize import NormalizedProject


@dataclass
class FakeProjects:
    events: list[str]

    def get(self, project_id: str) -> object | None:
        self.events.append("project")
        return object() if project_id == "p1" else None


@dataclass
class FakeNormalization:
    blocks: list[NormalizedBlock]
    events: list[str]

    def run(self, project_id: str, before_extract: Any = None) -> NormalizedProject:
        assert project_id == "p1"
        assert before_extract is not None
        before_extract()
        self.events.append("extract")
        return NormalizedProject(blocks=self.blocks, total_pages=2, warnings=[])


@dataclass
class FakeTextModel:
    result: object
    events: list[str]
    user_prompt: str = ""

    def generate_json(self, system: str, user: str, schema: type[Any]) -> object:
        assert "только" in system.lower()
        assert schema is WorkingDocument
        self.events.append("text")
        self.user_prompt = user
        return self.result


@dataclass
class FakeVisionModel:
    events: list[str]

    def describe(self, image: bytes, media_type: str) -> VisionDescription:
        assert image == b"diagram"
        assert media_type == "image/png"
        self.events.append("vision")
        return VisionDescription(description="Схема подтверждает успешную оплату")


@dataclass
class FakeDocuments:
    events: list[str]
    documents: dict[str, WorkingDocument] = field(default_factory=dict)

    def save_document(self, project_id: str, document: WorkingDocument) -> None:
        self.events.append("save")
        self.documents[project_id] = document

    def get_document(self, project_id: str) -> WorkingDocument | None:
        return self.documents.get(project_id)


@dataclass
class ProgressSpy:
    events: list[str]
    values: list[int] = field(default_factory=list)

    def __call__(self, value: int, status_message: str | None = None) -> None:
        del status_message
        self.values.append(value)
        self.events.append(f"progress:{value}")


@pytest.fixture
def image_path(tmp_path: Path) -> Path:
    path = tmp_path / "diagram.png"
    path.write_bytes(b"diagram")
    return path


@pytest.fixture
def blocks(image_path: Path) -> list[NormalizedBlock]:
    return [
        _block("text-block", BlockKind.TEXT, "Пользователь оплачивает заказ"),
        _block(
            "image-block",
            BlockKind.IMAGE,
            "",
            data={"storage_path": str(image_path), "width": 10, "height": 20},
        ),
    ]


@pytest.fixture
def grounded_document() -> WorkingDocument:
    return WorkingDocument(
        title="Оплата заказа",
        template_id="use-case",
        nodes=[
            DocumentNode(
                id="n1",
                kind=NodeKind.PARAGRAPH,
                text="Пользователь оплачивает заказ",
                provenance=[Provenance(source_id="text-block", locator="paragraph:1")],
            ),
            DocumentNode(
                id="n2",
                kind=NodeKind.IMAGE,
                text="Схема успешной оплаты",
                provenance=[Provenance(source_id="image-block", locator="image:1")],
            ),
        ],
    )


@pytest.fixture
def assembled(
    blocks: list[NormalizedBlock], grounded_document: WorkingDocument
) -> tuple[AssembleWorkflow, FakeTextModel, FakeDocuments, ProgressSpy, list[str]]:
    events: list[str] = []
    model = FakeTextModel(grounded_document, events)
    documents = FakeDocuments(events)
    progress = ProgressSpy(events)
    workflow = AssembleWorkflow(
        projects=FakeProjects(events),
        normalization=FakeNormalization(blocks, events),
        templates=TemplateCatalog(),
        text_model=model,
        vision_model=FakeVisionModel(events),
        grounding=GroundingValidator(),
        documents=documents,
    )
    return workflow, model, documents, progress, events


def test_assemble_saves_grounded_document_after_gated_external_calls(
    assembled: tuple[AssembleWorkflow, FakeTextModel, FakeDocuments, ProgressSpy, list[str]],
) -> None:
    workflow, model, documents, progress, events = assembled

    document = workflow.run(_job(JobKind.ASSEMBLE), progress)

    assert document.template_id == "use-case"
    assert documents.get_document("p1") == document
    assert progress.values == [10, 35, 70, 90, 100]
    assert events == [
        "progress:10",
        "project",
        "progress:35",
        "extract",
        "progress:70",
        "vision",
        "progress:90",
        "text",
        "progress:100",
        "save",
    ]
    assert "Схема подтверждает успешную оплату" in model.user_prompt
    assert "storage_path" not in model.user_prompt


def test_assemble_rejects_ungrounded_output_without_replacing_document(
    assembled: tuple[AssembleWorkflow, FakeTextModel, FakeDocuments, ProgressSpy, list[str]],
    grounded_document: WorkingDocument,
) -> None:
    workflow, model, documents, progress, _events = assembled
    documents.documents["p1"] = grounded_document
    model.result = WorkingDocument(
        title="Выдуманный результат",
        template_id="use-case",
        nodes=[
            DocumentNode(
                kind=NodeKind.PARAGRAPH,
                text="Нет в источниках",
                provenance=[Provenance(source_id="unknown", locator="paragraph:1")],
            )
        ],
    )

    with pytest.raises(WorkflowError, match="Результат не прошёл проверку по источникам"):
        workflow.run(_job(JobKind.ASSEMBLE), progress)

    assert documents.get_document("p1") == grounded_document


def test_assemble_validates_model_schema_before_saving(
    assembled: tuple[AssembleWorkflow, FakeTextModel, FakeDocuments, ProgressSpy, list[str]],
) -> None:
    workflow, model, documents, progress, _events = assembled
    model.result = {"title": "Ошибка", "template_id": "use-case", "nodes": [{"kind": "bogus"}]}

    with pytest.raises(WorkflowError, match="Модель вернула некорректный документ"):
        workflow.run(_job(JobKind.ASSEMBLE), progress)

    assert documents.get_document("p1") is None


def test_production_builder_never_falls_back_to_fake_models(
    blocks: list[NormalizedBlock], tmp_path: Path
) -> None:
    events: list[str] = []
    documents = FakeDocuments(events)
    dependencies = WorkflowDependencies(
        projects=FakeProjects(events),
        normalization=FakeNormalization([blocks[0]], events),
        templates=TemplateCatalog(),
        documents=documents,
    )
    workflows = build_workflows(Settings(data_dir=tmp_path), dependencies)

    with pytest.raises(ModelConfigurationError, match="Локальные модели не настроены"):
        workflows[JobKind.ASSEMBLE].run(_job(JobKind.ASSEMBLE), ProgressSpy(events))

    assert documents.get_document("p1") is None


def _block(
    block_id: str,
    kind: BlockKind,
    text: str,
    *,
    data: dict[str, object] | None = None,
) -> NormalizedBlock:
    return NormalizedBlock(
        id=block_id,
        kind=kind,
        text=text,
        data=data or {},
        provenance=[Provenance(source_id="source-1", locator=f"{kind.value}:1")],
        confidence=1.0,
    )


def _job(kind: JobKind) -> Job:
    return Job(
        id=f"job-{kind.value}",
        project_id="p1",
        kind=kind,
        template_id="use-case",
        status=JobStatus.RUNNING,
        progress=0,
        status_message="Выполняется",
        cancel_requested=False,
    )
