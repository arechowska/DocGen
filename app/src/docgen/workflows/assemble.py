from __future__ import annotations

import json
import mimetypes
from pathlib import Path

from pydantic import ValidationError

from docgen.ai.client import TextModel, VisionDescription, VisionModel
from docgen.ai.grounding import GroundingValidator
from docgen.ai.prompts import DOCUMENT_SYSTEM_PROMPT
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import WorkingDocument
from docgen.extraction.schemas import BlockKind, NormalizedBlock
from docgen.jobs.models import Job, JobKind
from docgen.jobs.runner import ProgressSink, UserSafeJobError
from docgen.projects.repository import ProjectRepository
from docgen.templates_catalog.loader import TemplateCatalog
from docgen.templates_catalog.schemas import SemanticTemplate

from .normalize import NormalizationWorkflow

_ASSEMBLE_KIND_ERROR = "Некорректный тип задания для сборки"
_PROJECT_NOT_FOUND = "Проект не найден"
_DOCUMENT_SCHEMA_ERROR = "Модель вернула некорректный документ"
_DOCUMENT_TEMPLATE_ERROR = "Модель вернула документ для другого шаблона"
_GROUNDING_ERROR = "Результат не прошёл проверку по источникам"


class WorkflowError(UserSafeJobError):
    """A validation failure safe to show for an assemble/check job."""


class AssembleWorkflow:
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

    def run(self, job: Job, progress: ProgressSink) -> WorkingDocument:
        if job.kind is not JobKind.ASSEMBLE:
            raise WorkflowError(_ASSEMBLE_KIND_ERROR)

        progress(10, "Загружены проект и шаблон")
        if self._projects.get(job.project_id) is None:
            raise WorkflowError(_PROJECT_NOT_FOUND)
        template = self._templates.get(job.template_id)

        normalized = self._normalization.run(
            job.project_id,
            before_extract=lambda: progress(35, "Нормализация источников"),
        )
        blocks = enrich_images(normalized.blocks, self._vision_model, progress)

        progress(90, "Сборка документа моделью")
        raw_document = self._text_model.generate_json(
            DOCUMENT_SYSTEM_PROMPT,
            _assemble_prompt(template, blocks),
            WorkingDocument,
        )
        try:
            document = WorkingDocument.model_validate(raw_document)
        except ValidationError as exc:
            raise WorkflowError(_DOCUMENT_SCHEMA_ERROR) from exc
        if document.template_id != template.id:
            raise WorkflowError(_DOCUMENT_TEMPLATE_ERROR)

        grounding_errors = self._grounding.validate(document, {block.id for block in blocks})
        if grounding_errors:
            raise WorkflowError(_GROUNDING_ERROR)

        progress(100, "Сохранение документа")
        self._documents.save_document(job.project_id, document)
        return document


def enrich_images(
    blocks: list[NormalizedBlock],
    vision_model: VisionModel,
    progress: ProgressSink,
) -> list[NormalizedBlock]:
    enriched: list[NormalizedBlock] = []
    described_images = 0
    for block in blocks:
        storage_path = block.data.get("storage_path") if block.kind is BlockKind.IMAGE else None
        if not isinstance(storage_path, str):
            enriched.append(block)
            continue

        image_path = Path(storage_path)
        image_bytes = image_path.read_bytes()
        media_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
        progress(70, "Обогащение изображений")
        raw_description = vision_model.describe(image_bytes, media_type)
        try:
            description = VisionDescription.model_validate(raw_description)
        except ValidationError as exc:
            raise WorkflowError("Модель вернула некорректное описание изображения") from exc
        enriched.append(block.model_copy(update={"text": description.description}))
        described_images += 1

    if described_images == 0:
        progress(70, "Изображения для обогащения отсутствуют")
    return enriched


def public_blocks(blocks: list[NormalizedBlock]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for block in blocks:
        data = {key: value for key, value in block.data.items() if key != "storage_path"}
        result.append(
            {
                "id": block.id,
                "kind": block.kind.value,
                "text": block.text,
                "data": data,
                "confidence": block.confidence,
            }
        )
    return result


def _assemble_prompt(template: SemanticTemplate, blocks: list[NormalizedBlock]) -> str:
    payload = {
        "задача": "Соберите один документ по шаблону и укажите id исходного блока в provenance.source_id каждого узла.",
        "шаблон": template.model_dump(mode="json"),
        "исходные_блоки": public_blocks(blocks),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


__all__ = ["AssembleWorkflow", "WorkflowError"]
