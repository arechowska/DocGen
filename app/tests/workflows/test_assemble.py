from __future__ import annotations

import json
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from docgen.ai.client import ModelConfigurationError, VisionDescription
from docgen.ai.grounding import GroundingValidator
from docgen.config import Settings
from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.extraction.confluence import ConfluenceClient
from docgen.extraction.registry import ExtractorRegistry
from docgen.extraction.schemas import BlockKind, NormalizedBlock, Provenance
from docgen.jobs.models import Job, JobKind, JobStatus
from docgen.jobs.runner import WorkflowDependencies, build_workflows
from docgen.models import Source, SourceKind
from docgen.templates_catalog.loader import TemplateCatalog
from docgen.workflows.assemble import AssembleWorkflow, WorkflowError, _assemble_prompt
from docgen.workflows.normalize import NormalizationWorkflow, NormalizedProject


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
    call_gate: bool = True
    warnings: list[str] = field(default_factory=list)

    def run(self, project_id: str, before_extract: Any = None) -> NormalizedProject:
        assert project_id == "p1"
        assert before_extract is not None
        if self.call_gate:
            before_extract()
            self.events.append("extract")
        return NormalizedProject(
            blocks=self.blocks, total_pages=2, warnings=self.warnings
        )


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
    warnings: list[str] = field(default_factory=list)

    def __call__(self, value: int, status_message: str | None = None) -> None:
        del status_message
        self.values.append(value)
        self.events.append(f"progress:{value}")

    def checkpoint(self) -> None:
        self.events.append("checkpoint")

    def report_warnings(self, warnings: list[str]) -> None:
        self.warnings.extend(warnings)


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
                section_id="actors",
                text="Пользователь оплачивает заказ",
                provenance=[
                    Provenance(
                        source_id="text-block",
                        locator="text:1",
                        quote="Пользователь оплачивает заказ",
                    )
                ],
            ),
            DocumentNode(
                id="n2",
                kind=NodeKind.IMAGE,
                section_id="main-flow",
                text="Схема успешной оплаты",
                provenance=[
                    Provenance(
                        source_id="image-block",
                        locator="image:1",
                        quote="Схема подтверждает успешную оплату",
                    )
                ],
            ),
            DocumentNode(
                id="gap-preconditions",
                kind=NodeKind.GAP,
                section_id="preconditions",
                flags=["missing-source-data"],
            ),
            DocumentNode(
                id="gap-result",
                kind=NodeKind.GAP,
                section_id="result",
                flags=["missing-source-data"],
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
        "checkpoint",
        "extract",
        "progress:70",
        "checkpoint",
        "vision",
        "progress:90",
        "text",
        "progress:100",
        "save",
    ]
    assert "Схема подтверждает успешную оплату" in model.user_prompt
    assert "storage_path" not in model.user_prompt


def test_assemble_prompt_tells_faq_to_convert_supported_facts_into_answers(
    blocks: list[NormalizedBlock],
) -> None:
    template = TemplateCatalog().get("faq")

    payload = json.loads(_assemble_prompt(template, blocks))

    assert "не требуйте, чтобы источник уже был написан в формате шаблона" in payload[
        "задача"
    ].lower()
    assert "ровно один верхнеуровневый node" in payload["правила_json"]
    assert (
        "general_questions, getting_started, working_with_system, errors_and_limitations"
        in payload["правила_json"]
    )
    assert "не добавляйте section_id" in payload["правила_json"].lower()
    assert "data.items" in payload["правила_json"]
    assert '"section_id": "general_questions"' in payload["пример_формата"]
    assert '"section_id": "getting_started"' in payload["пример_формата"]
    assert '"section_id": "working_with_system"' in payload["пример_формата"]
    assert '"section_id": "errors_and_limitations"' in payload["пример_формата"]
    assert '"section_id": "purpose"' not in payload["пример_формата"]
    assert '"section_id": "references"' not in payload["пример_формата"]
    assert "процедур" in payload["инструкция_для_шаблона"]
    assert "вопросы и ответы" in payload["инструкция_для_шаблона"]
    assert "Секция purpose" not in payload["инструкция_для_шаблона"]
    assert "Секция references" not in payload["инструкция_для_шаблона"]
    assert "точной quote" in payload["инструкция_для_шаблона"]


def test_assemble_publishes_normalization_warnings_before_model_call(
    assembled: tuple[AssembleWorkflow, FakeTextModel, FakeDocuments, ProgressSpy, list[str]],
) -> None:
    workflow, _model, _documents, progress, events = assembled
    workflow._normalization.warnings = [  # type: ignore[attr-defined]
        "Страница 2 не содержит извлекаемого текста",
        "Обработка может занять более пяти минут",
    ]

    workflow.run(_job(JobKind.ASSEMBLE), progress)

    assert progress.warnings == [
        "Страница 2 не содержит извлекаемого текста",
        "Обработка может занять более пяти минут",
    ]
    assert events.index("extract") < events.index("text")


def test_assemble_saves_model_document_without_strict_grounding(
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
                section_id="actors",
                kind=NodeKind.PARAGRAPH,
                text="Нет в источниках",
                provenance=[
                    Provenance(
                        source_id="unknown",
                        locator="paragraph:1",
                        quote="Нет в источниках",
                    )
                ],
            ),
            DocumentNode(kind=NodeKind.GAP, section_id="preconditions", flags=["missing-source-data"]),
            DocumentNode(kind=NodeKind.GAP, section_id="main-flow", flags=["missing-source-data"]),
            DocumentNode(kind=NodeKind.GAP, section_id="result", flags=["missing-source-data"]),
        ],
    )

    document = workflow.run(_job(JobKind.ASSEMBLE), progress)

    assert document.title == "Выдуманный результат"
    assert documents.get_document("p1") == document


def test_assemble_rejects_empty_document_before_save(
    assembled: tuple[AssembleWorkflow, FakeTextModel, FakeDocuments, ProgressSpy, list[str]],
) -> None:
    workflow, model, documents, progress, _events = assembled
    model.result = WorkingDocument(title="Пустой", template_id="use-case")

    with pytest.raises(WorkflowError, match="обязательные разделы"):
        workflow.run(_job(JobKind.ASSEMBLE), progress)

    assert documents.get_document("p1") is None


def test_assemble_rejects_missing_required_section_ids(
    assembled: tuple[AssembleWorkflow, FakeTextModel, FakeDocuments, ProgressSpy, list[str]],
) -> None:
    workflow, model, documents, progress, _events = assembled
    model.result = WorkingDocument(
        title="Неполный",
        template_id="use-case",
        nodes=[
            DocumentNode(
                id="n1",
                kind=NodeKind.PARAGRAPH,
                text="Пользователь оплачивает заказ",
                section_id="actors",
                provenance=[
                    Provenance(
                        source_id="text-block",
                        locator="paragraph:1",
                        quote="Пользователь оплачивает заказ",
                    )
                ],
            )
        ],
    )

    with pytest.raises(WorkflowError, match="обязательные разделы"):
        workflow.run(_job(JobKind.ASSEMBLE), progress)

    assert documents.get_document("p1") is None


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
    workflows = build_workflows(Settings(_env_file=None, data_dir=tmp_path), dependencies)

    with pytest.raises(ModelConfigurationError, match="Локальные модели не настроены"):
        workflows[JobKind.ASSEMBLE].run(_job(JobKind.ASSEMBLE), ProgressSpy(events))

    assert documents.get_document("p1") is None


def test_assemble_emits_normalization_stage_once_when_project_has_no_sources() -> None:
    events: list[str] = []
    document = WorkingDocument(
        title="Нет исходных данных",
        template_id="use-case",
        nodes=[
            DocumentNode(kind=NodeKind.GAP, section_id=section_id, flags=["missing-source-data"])
            for section_id in ("actors", "preconditions", "main-flow", "result")
        ],
    )
    progress = ProgressSpy(events)
    workflow = AssembleWorkflow(
        projects=FakeProjects(events),
        normalization=FakeNormalization([], events, call_gate=False),
        templates=TemplateCatalog(),
        text_model=FakeTextModel(document, events),
        vision_model=FakeVisionModel(events),
        grounding=GroundingValidator(),
        documents=FakeDocuments(events),
    )

    workflow.run(_job(JobKind.ASSEMBLE), progress)

    assert progress.values == [10, 35, 70, 90, 100]


def test_assemble_enriches_native_confluence_attachment_with_gates_and_hides_base64() -> None:
    events: list[str] = []
    image_buffer = BytesIO()
    Image.new("RGB", (1, 1)).save(image_buffer, format="PNG")
    attachment_content = image_buffer.getvalue()
    source = Source(
        id="source-confluence",
        project_id="p1",
        kind=SourceKind.CONFLUENCE,
        display_name="Architecture",
        url="https://wiki.example.test/pages/viewpage.action?pageId=42",
        status="linked",
    )

    class Sources:
        def list_for_project(self, project_id: str) -> list[Source]:
            assert project_id == "p1"
            return [source]

    class Storage:
        def resolve(self, relative_path: str) -> Path:
            raise AssertionError(f"Confluence must not resolve local path {relative_path}")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret"
        if request.url.path == "/rest/api/content/42":
            events.append("http:page")
            return httpx.Response(
                200,
                json={
                    "body": {
                        "storage": {
                            "value": '<ac:image><ri:attachment ri:filename="architecture.png" /></ac:image>'
                        }
                    }
                },
            )
        if request.url.path == "/rest/api/content/42/child/attachment":
            events.append("http:metadata")
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "architecture.png",
                            "metadata": {"mediaType": "image/png"},
                            "_links": {"download": "/download/attachments/42/architecture.png"},
                        }
                    ]
                },
            )
        events.append("http:download")
        return httpx.Response(200, content=attachment_content)

    class AttachmentVision:
        def describe(self, image: bytes, media_type: str) -> VisionDescription:
            assert image == attachment_content
            assert media_type == "image/png"
            events.append("vision")
            return VisionDescription(description="Архитектурная схема системы")

    class GroundedModel:
        def generate_json(self, system: str, user: str, schema: type[Any]) -> WorkingDocument:
            del system
            assert schema is WorkingDocument
            payload = json.loads(user)
            source_block = payload["исходные_блоки"][0]
            assert source_block["text"] == "Архитектурная схема системы"
            assert "content_base64" not in source_block["data"]
            events.append("text")
            return WorkingDocument(
                title="Архитектура",
                template_id="use-case",
                nodes=[
                    DocumentNode(
                        kind=NodeKind.IMAGE,
                        section_id="actors",
                        text="Архитектурная схема системы",
                        provenance=[
                            Provenance(
                                source_id=source_block["id"],
                                locator=source_block["locators"][0],
                                quote="Архитектурная схема системы",
                            )
                        ],
                    ),
                    DocumentNode(
                        kind=NodeKind.GAP,
                        section_id="preconditions",
                        flags=["missing-source-data"],
                    ),
                    DocumentNode(
                        kind=NodeKind.GAP,
                        section_id="main-flow",
                        flags=["missing-source-data"],
                    ),
                    DocumentNode(
                        kind=NodeKind.GAP,
                        section_id="result",
                        flags=["missing-source-data"],
                    ),
                ],
            )

    documents = FakeDocuments(events)
    progress = ProgressSpy(events)
    normalization = NormalizationWorkflow(
        Sources(),
        Storage(),
        ExtractorRegistry.default(),
        ConfluenceClient(
            api_base="https://wiki.example.test/rest/api",
            token="secret",
            transport=httpx.MockTransport(handler),
        ),
    )
    workflow = AssembleWorkflow(
        projects=FakeProjects(events),
        normalization=normalization,
        templates=TemplateCatalog(),
        text_model=GroundedModel(),
        vision_model=AttachmentVision(),
        grounding=GroundingValidator(),
        documents=documents,
    )

    workflow.run(_job(JobKind.ASSEMBLE), progress)

    assert progress.values == [10, 35, 70, 90, 100]
    assert events == [
        "progress:10",
        "project",
        "progress:35",
        "checkpoint",
        "http:page",
        "checkpoint",
        "http:metadata",
        "checkpoint",
        "http:download",
        "progress:70",
        "checkpoint",
        "vision",
        "progress:90",
        "text",
        "progress:100",
        "save",
    ]


def test_assemble_rejects_malformed_confluence_attachment_payload_without_saving() -> None:
    events: list[str] = []
    attachment = _block(
        "attachment-block",
        BlockKind.IMAGE,
        "architecture.png",
        data={
            "attachment": "architecture.png",
            "content_base64": "not base64!",
            "media_type": "image/png",
        },
    )
    documents = FakeDocuments(events)
    workflow = AssembleWorkflow(
        projects=FakeProjects(events),
        normalization=FakeNormalization([attachment], events),
        templates=TemplateCatalog(),
        text_model=FakeTextModel(
            WorkingDocument(title="Не используется", template_id="use-case"), events
        ),
        vision_model=FakeVisionModel(events),
        grounding=GroundingValidator(),
        documents=documents,
    )

    with pytest.raises(WorkflowError, match="Не удалось прочитать вложение Confluence"):
        workflow.run(_job(JobKind.ASSEMBLE), ProgressSpy(events))

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
