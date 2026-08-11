from __future__ import annotations

import base64
import binascii
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

from .normalize import NormalizationWorkflow, NormalizedProject

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

        normalized = normalize_sources(self._normalization, job.project_id, progress)
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

        _validate_document_structure(document, template)
        grounding_errors = self._grounding.validate(
            document, {block.id: block for block in blocks}
        )
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
    progress(70, "Обогащение изображений")
    for block in blocks:
        if block.kind is not BlockKind.IMAGE:
            enriched.append(block)
            continue

        storage_path = block.data.get("storage_path")
        attachment = block.data.get("attachment")
        if isinstance(storage_path, str):
            image_path = Path(storage_path)
            image_bytes = image_path.read_bytes()
            media_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
        elif isinstance(attachment, str):
            encoded_content = block.data.get("content_base64")
            media_type = block.data.get("media_type")
            if not isinstance(encoded_content, str) or not isinstance(media_type, str):
                raise WorkflowError("Не удалось прочитать вложение Confluence")
            try:
                image_bytes = base64.b64decode(encoded_content, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise WorkflowError("Не удалось прочитать вложение Confluence") from exc
            if not image_bytes or not media_type.startswith("image/"):
                raise WorkflowError("Не удалось прочитать вложение Confluence")
        else:
            enriched.append(block)
            continue

        cancellation_checkpoint(progress)
        raw_description = vision_model.describe(image_bytes, media_type)
        try:
            description = VisionDescription.model_validate(raw_description)
        except ValidationError as exc:
            raise WorkflowError("Модель вернула некорректное описание изображения") from exc
        sanitized_data = {
            key: value
            for key, value in block.data.items()
            if key not in {"content_base64", "storage_path"}
        }
        enriched.append(
            block.model_copy(
                update={"text": description.description, "data": sanitized_data}
            )
        )
    return enriched


def normalize_sources(
    normalization: NormalizationWorkflow,
    project_id: str,
    progress: ProgressSink,
) -> NormalizedProject:
    progress(35, "Нормализация источников")
    normalized = normalization.run(
        project_id,
        before_extract=lambda: cancellation_checkpoint(progress),
    )
    report_warnings = getattr(progress, "report_warnings", None)
    if callable(report_warnings):
        report_warnings(normalized.warnings)
    return normalized


def cancellation_checkpoint(progress: ProgressSink) -> None:
    checkpoint = getattr(progress, "checkpoint", None)
    if callable(checkpoint):
        checkpoint()


def public_blocks(blocks: list[NormalizedBlock]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for block in blocks:
        data = {
            key: value
            for key, value in block.data.items()
            if key not in {"content_base64", "storage_path"}
        }
        result.append(
            {
                "id": block.id,
                "kind": block.kind.value,
                "text": block.text,
                "data": data,
                "locators": [item.locator for item in block.provenance],
                "confidence": block.confidence,
            }
        )
    return result


def _assemble_prompt(template: SemanticTemplate, blocks: list[NormalizedBlock]) -> str:
    payload = {
        "задача": (
            "Соберите один непустой документ. Для каждого раздела шаблона создайте один "
            "верхнеуровневый узел с точным section_id. Для каждого фактического узла "
            "укажите id исходного блока в provenance.source_id, его исходный locator и "
            "точную непустую quote из текста блока. Если данных для раздела нет, создайте "
            "пустой gap с section_id и флагом missing-source-data. не требуйте, чтобы "
            "источник уже был написан в формате шаблона: преобразуйте подтверждённые факты "
            "из источников в нужную структуру, если каждый вывод можно подтвердить точной quote."
        ),
        "правила_json": _json_rules(template),
        "пример_формата": _format_example(template),
        "инструкция_для_шаблона": _template_instruction(template),
        "шаблон": template.model_dump(mode="json"),
        "исходные_блоки": public_blocks(blocks),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _json_rules(template: SemanticTemplate) -> str:
    section_ids = ", ".join(section.id for section in template.sections)
    return (
        f"Верните только объект WorkingDocument: title, template_id='{template.id}', nodes. "
        f"В nodes должен быть ровно один верхнеуровневый node для каждого section_id: "
        f"{section_ids}. не добавляйте section_id вне этого списка и не дублируйте section_id. "
        "Если section_id можно заполнить фактами из источников, node не должен быть kind='gap'. "
        "Для kind='gap' используйте section_id из списка разделов, flags=['missing-source-data'], "
        "без text, data, children и provenance. Для kind='list' кладите элементы списка в data.items "
        "как массив строк. Для kind='paragraph' кладите текст в поле text. Каждый негэповый node "
        "обязан иметь provenance с source_id, locator и quote, где quote является точной подстрокой "
        "соответствующего исходного блока."
    )


def _format_example(template: SemanticTemplate) -> str:
    if template.id == "faq":
        example = {
            "title": "FAQ по материалам источников",
            "template_id": "faq",
            "nodes": [
                {
                    "kind": "paragraph",
                    "section_id": "purpose",
                    "text": "Краткое назначение FAQ на основе источников.",
                    "provenance": [
                        {
                            "source_id": "<id исходного блока>",
                            "locator": "<locator исходного блока>",
                            "quote": "<точная цитата из блока>",
                        }
                    ],
                },
                {
                    "kind": "list",
                    "section_id": "questions",
                    "text": "Вопросы и ответы",
                    "data": {
                        "items": [
                            "Вопрос: ... Ответ: ...",
                            "Вопрос: ... Ответ: ...",
                        ]
                    },
                    "provenance": [
                        {
                            "source_id": "<id исходного блока>",
                            "locator": "<locator исходного блока>",
                            "quote": "<точная цитата из блока>",
                        }
                    ],
                },
                {
                    "kind": "list",
                    "section_id": "references",
                    "text": "Источники сведений",
                    "data": {"items": ["<название или id источника>"]},
                    "provenance": [
                        {
                            "source_id": "<id исходного блока>",
                            "locator": "<locator исходного блока>",
                            "quote": "<точная цитата из блока>",
                        }
                    ],
                },
            ],
        }
        return json.dumps(example, ensure_ascii=False, indent=2)
    return (
        "Верните WorkingDocument с nodes, где каждый верхнеуровневый node содержит "
        "kind, section_id, text или data, а также provenance для всех фактических данных."
    )


def _template_instruction(template: SemanticTemplate) -> str:
    if template.id == "faq":
        return (
            "Для FAQ преобразуйте описания, процедурные шаги, правила и ограничения из "
            "источников в вопросы и ответы от лица пользователя. Секция purpose должна быть "
            "paragraph с назначением FAQ. Секция questions должна быть list, где data.items "
            "содержит строки вида 'Вопрос: ... Ответ: ...'. Секция references должна быть list "
            "с перечислением использованных источников. Не создавайте gap только потому, что "
            "источник не содержит готовых вопросов. Каждый ответ должен быть основан на факте "
            "из исходных блоков и иметь provenance с точной quote. Если по конкретному вопросу "
            "нет подтверждения в источниках, не придумывайте ответ."
        )
    return (
        "Адаптируйте подтверждённые факты из источников под разделы шаблона. Создавайте gap "
        "только для разделов, для которых в исходных блоках нет подходящих подтверждённых фактов."
    )


def _validate_document_structure(
    document: WorkingDocument, template: SemanticTemplate
) -> None:
    section_ids = [node.section_id for node in document.nodes]
    known_section_ids = {section.id for section in template.sections}
    required_section_ids = {
        section.id for section in template.sections if section.required
    }
    actual_section_ids = {section_id for section_id in section_ids if section_id}
    if (
        not document.nodes
        or any(section_id is None or not section_id.strip() for section_id in section_ids)
        or len(actual_section_ids) != len(section_ids)
        or not actual_section_ids.issubset(known_section_ids)
        or not required_section_ids.issubset(actual_section_ids)
    ):
        raise WorkflowError(
            "Документ не содержит все обязательные разделы шаблона"
        )


__all__ = ["AssembleWorkflow", "WorkflowError"]
