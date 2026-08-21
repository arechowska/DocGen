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
from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.extraction.schemas import BlockKind, NormalizedBlock
from docgen.jobs.models import Job, JobKind
from docgen.jobs.runner import ProgressSink, UserSafeJobError
from docgen.projects.repository import ProjectRepository
from docgen.templates_catalog.loader import NO_TEMPLATE_ID, TemplateCatalog
from docgen.templates_catalog.schemas import SemanticTemplate

from .normalize import NormalizationWorkflow, NormalizedProject

_ASSEMBLE_KIND_ERROR = "Некорректный тип задания для сборки"
_PROJECT_NOT_FOUND = "Проект не найден"
_DOCUMENT_SCHEMA_ERROR = "Модель вернула некорректный документ"
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
        project = self._projects.get(job.project_id)
        if project is None:
            raise WorkflowError(_PROJECT_NOT_FOUND)

        normalized = normalize_sources(self._normalization, job.project_id, progress)
        if job.template_id == NO_TEMPLATE_ID:
            document = _document_without_template(
                normalized.blocks, getattr(project, "name", "Документ")
            )
            progress(100, "Сохранение документа")
            self._documents.save_document(job.project_id, document)
            return document

        template = self._templates.get(job.template_id)
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
        document = document.model_copy(
            update={
                "title": _compact_document_title(document.title),
                "template_id": template.id,
            }
        )
        document = _add_missing_required_sections(document, template)

        _validate_document_structure(document, template)
        grounding_errors = self._grounding.validate(
            document, {block.id: block for block in blocks}
        )
        if grounding_errors:
            raise WorkflowError(_GROUNDING_ERROR)

        progress(100, "Сохранение документа")
        self._documents.save_document(job.project_id, document)
        return document


def _document_without_template(
    blocks: list[NormalizedBlock], title: str
) -> WorkingDocument:
    node_kinds = {
        BlockKind.TEXT: NodeKind.PARAGRAPH,
        BlockKind.HEADING: NodeKind.HEADING,
        BlockKind.LIST: NodeKind.LIST,
        BlockKind.TABLE: NodeKind.TABLE,
        BlockKind.IMAGE: NodeKind.IMAGE,
    }
    nodes: list[DocumentNode] = []
    for block in blocks:
        data = dict(block.data)
        if block.kind is BlockKind.LIST and "items" not in data:
            data["items"] = [block.text]
        nodes.append(
            DocumentNode(
                id=f"document-node:{block.id}",
                kind=node_kinds[block.kind],
                text=block.text,
                data=data,
                provenance=list(block.provenance),
            )
        )
    return WorkingDocument(title=title, template_id=NO_TEMPLATE_ID, nodes=nodes)


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
        "Поле title должно быть коротким названием темы документа: не более 60 символов, "
        "без фраз вроде 'Вопросы и ответы по модулю' или 'Документ по материалам источников'. "
        "Используйте название продукта, модуля или процесса и только при необходимости его роль. "
        f"В nodes должен быть ровно один верхнеуровневый node для каждого section_id: "
        f"{section_ids}. не добавляйте section_id вне этого списка и не дублируйте section_id. "
        "Если section_id можно заполнить фактами из источников, node не должен быть kind='gap'. "
        "Для kind='gap' используйте section_id из списка разделов, flags=['missing-source-data'], "
        "без text, data, children и provenance. Для kind='list' кладите элементы списка в data.items "
        "как массив строк. Для kind='paragraph' кладите текст в поле text. Каждый негэповый node "
        "обязан иметь provenance с source_id, locator и quote, где quote является точной подстрокой "
        "соответствующего исходного блока."
    )


def _compact_document_title(title: str) -> str:
    compact = " ".join(title.split()).strip()
    if len(compact) <= 60:
        return compact or "Документ"
    shortened = compact[:60].rsplit(" ", 1)[0].rstrip(" ,:;—-\"")
    return shortened or compact[:60].rstrip()


def _format_example(template: SemanticTemplate) -> str:
    if template.id == "faq":
        example = {
            "title": "FAQ по материалам источников",
            "template_id": "faq",
            "nodes": [
                {
                    "kind": "list",
                    "section_id": section.id,
                    "text": section.title,
                    "data": {"items": ["Вопрос: ... Ответ: ..."]},
                    "provenance": [
                        {
                            "source_id": "<id исходного блока>",
                            "locator": "<locator исходного блока>",
                            "quote": "<точная цитата из блока>",
                        }
                    ],
                }
                for section in template.sections
            ],
        }
        return json.dumps(example, ensure_ascii=False, indent=2)
    if template.id == "use-case":
        example = {
            "title": "Название сценария по материалам источников",
            "template_id": "use-case",
            "nodes": [
                {
                    "kind": "paragraph",
                    "section_id": "overview",
                    "text": "Краткое назначение и границы сценария, подтверждённые источниками.",
                    "provenance": [
                        {
                            "source_id": "<id исходного блока>",
                            "locator": "<locator исходного блока>",
                            "quote": "<точная цитата из блока>",
                        }
                    ],
                },
                {
                    "kind": "paragraph",
                    "section_id": "scope",
                    "text": "Процесс, продукт или система, к которым относится сценарий.",
                    "provenance": [
                        {
                            "source_id": "<id исходного блока>",
                            "locator": "<locator исходного блока>",
                            "quote": "<точная цитата из блока>",
                        }
                    ],
                },
                {
                    "kind": "paragraph",
                    "section_id": "primary-actor",
                    "text": "Основное действующее лицо: подтверждённая роль или система.",
                    "provenance": [
                        {
                            "source_id": "<id исходного блока>",
                            "locator": "<locator исходного блока>",
                            "quote": "<точная цитата из блока>",
                        }
                    ],
                },
                {
                    "kind": "paragraph",
                    "section_id": "actor-goal",
                    "text": "Цель основного действующего лица: наблюдаемый результат, подтверждённый источниками.",
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
                    "section_id": "actors",
                    "text": "Участники и интересы",
                    "data": {"items": ["Участник — подтверждённый интерес или ответственность"]},
                    "provenance": [
                        {
                            "source_id": "<id исходного блока>",
                            "locator": "<locator исходного блока>",
                            "quote": "<точная цитата из блока>",
                        }
                    ],
                },
                {
                    "kind": "paragraph",
                    "section_id": "preconditions",
                    "text": "Условие, которое должно быть выполнено до начала сценария.",
                    "provenance": [
                        {
                            "source_id": "<id исходного блока>",
                            "locator": "<locator исходного блока>",
                            "quote": "<точная цитата из блока>",
                        }
                    ],
                },
                {
                    "kind": "paragraph",
                    "section_id": "minimum-guarantees",
                    "text": "Минимальное безопасное состояние, подтверждённое источником.",
                    "provenance": [
                        {
                            "source_id": "<id исходного блока>",
                            "locator": "<locator исходного блока>",
                            "quote": "<точная цитата из блока>",
                        }
                    ],
                },
                {
                    "kind": "paragraph",
                    "section_id": "trigger",
                    "text": "Событие или запрос, инициирующий сценарий.",
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
                    "section_id": "main-flow",
                    "text": "Основной поток",
                    "data": {"items": ["1. Участник выполняет действие. 2. Система отвечает подтверждённым действием."]},
                    "provenance": [
                        {
                            "source_id": "<id исходного блока>",
                            "locator": "<locator исходного блока>",
                            "quote": "<точная цитата из блока>",
                        }
                    ],
                },
                {
                    "kind": "paragraph",
                    "section_id": "result",
                    "text": "Проверяемое состояние после успешного выполнения сценария.",
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
                    "section_id": "alternative-flow",
                    "text": "Альтернативный вариант",
                    "data": {"items": ["A1. Условие ветвления. Действие участника. Реакция системы. Возврат к шагу основного потока или завершение."]},
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
                    "section_id": "exceptions",
                    "text": "Исключения",
                    "data": {"items": ["E1. Ошибочное условие. Реакция системы. Безопасное состояние после прерывания."]},
                    "provenance": [
                        {
                            "source_id": "<id исходного блока>",
                            "locator": "<locator исходного блока>",
                            "quote": "<точная цитата из блока>",
                        }
                    ],
                },
                {
                    "kind": "paragraph",
                    "section_id": "open-questions",
                    "text": "Неразрешённый вопрос или недостающий факт из источников.",
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
                    "section_id": "terms",
                    "text": "Термины и определения",
                    "data": {"items": ["Термин — подтверждённое определение"]},
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
        section_ids = ", ".join(section.id for section in template.sections)
        return (
            "Правила сборки FAQ:\n"
            "1. Извлеките из исходных блоков КАЖДЫЙ отдельный факт, процедуру, правило и "
            "ограничение как отдельный вопрос-ответ от лица пользователя. Не "
            "останавливайтесь на нескольких показательных примерах и не объединяйте "
            "несколько разных фактов в один ответ. Если один исходный блок описывает "
            "несколько отдельных тем, создайте по вопросу на каждую тему.\n"
            "2. Формулируйте вопрос так, как его задал бы конечный пользователь, а не как "
            "заголовок раздела руководства.\n"
            f"3. Распределяйте вопросы по разделам шаблона: {section_ids}. Каждый раздел "
            "делайте list, где data.items содержит строки вида 'Вопрос: ... Ответ: ...'.\n"
            "4. Начинайте ответ с результата или прямого действия; подробности и "
            "ограничения размещайте после. Для процедурного вопроса включайте в текст "
            "ответа предварительные условия, пронумерованные действия и проверяемый "
            "результат.\n"
            "5. Каждый ответ обязан быть основан на факте из исходных блоков и иметь "
            "provenance с точной quote. Если источник поднимает тему, но не даёт полных "
            "деталей, всё равно создайте вопрос и прямо укажите в ответе, что нужна "
            "проверка администратором или службой поддержки. Не создавайте вопросы на "
            "темы, которых источники вообще не затрагивают, и не придумывайте недостающие "
            "детали.\n"
            "6. Используйте единые официальные названия экранов, полей, кнопок, статусов "
            "и объектов из источников; при первом употреблении кратко поясните термин.\n"
            "7. При расхождении источников сначала уточните версию, роль или настройку, к "
            "которой относится каждый вариант; не объединяйте взаимоисключающие "
            "инструкции — создайте отдельный вопрос, явно называющий неразрешённое "
            "расхождение.\n"
            "8. Не предлагайте повторное проведение, отмену, удаление или обход "
            "контрольных проверок без явно подтверждённого источником безопасного "
            "сценария.\n"
            "9. Пишите спокойным деловым языком, обращайтесь на «вы», используйте "
            "короткие предложения и не более одного действия на пункт списка.\n"
            "10. Не включайте в готовый FAQ перечень источников, идентификаторы блоков "
            "или служебные комментарии."
        )
    if template.id == "use-case":
        section_ids = ", ".join(section.id for section in template.sections)
        return (
            "Правила сборки корпоративного сценария использования:\n"
            f"1. Распределяйте подтверждённые факты только по разделам шаблона: {section_ids}. "
            "Не создавайте содержание для административных полей, диаграмм, тест-кейсов, "
            "приложений, ссылок и показателей, если они не являются текстовой частью сценария.\n"
            "2. В основном потоке нумеруйте шаги в порядке выполнения. Один шаг описывает "
            "одно наблюдаемое действие участника или системы; после действия участника "
            "указывайте подтверждённую источником реакцию системы.\n"
            "3. Свяжите «Основное действующее лицо» с «Участниками и интересами»: сначала "
            "назовите инициирующую роль и её цель, затем в списке участников укажите её "
            "интерес один раз и добавьте только других подтверждённых участников. Не "
            "дублируйте одну роль разными формулировками.\n"
            "4. Для альтернативного варианта обязательно назовите шаг основного потока, "
            "после которого возникает ветвление, и условие ветвления. Нумеруйте его шаги "
            "как A1, A2; в конце явно укажите возврат к шагу основного потока либо отдельное "
            "успешное завершение.\n"
            "5. Для исключения обязательно назовите ошибочное или аварийное условие, реакцию "
            "системы и безопасное конечное состояние. Нумеруйте его шаги как E1, E2. Не "
            "смешивайте исключение с альтернативой: альтернатива сохраняет цель сценария, "
            "а исключение описывает её недостижение или безопасное прерывание.\n"
            "6. Проверяйте согласованность основного потока, альтернатив, исключений, "
            "предусловий, минимальных гарантий и результата. Не объединяйте несовместимые "
            "варианты из разных источников; при расхождении укажите версию, роль или настройку "
            "и вынесите нерешённое противоречие в открытые вопросы.\n"
            "7. Если источник затрагивает часть сценария, но не описывает её полностью, не "
            "придумывайте детали: создайте gap с флагом missing-source-data или конкретный "
            "пункт в «Открытых вопросах». Не выдавайте домысел за подтверждённый факт.\n"
            "8. Используйте единые официальные названия ролей, систем, операций, экранов, "
            "полей и статусов из источников; при первом употреблении кратко поясняйте термин, "
            "если это необходимо для понимания сценария.\n"
            "9. Каждый фактический node обязан иметь provenance с точной quote из исходного "
            "блока. Не создавайте разделы или шаги для тем, которых источники вообще не затрагивают.\n"
            "10. Не включайте в готовый сценарий ссылки на источники, идентификаторы блоков, "
            "служебные комментарии, внутренние id или технические указания для модели."
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


def _add_missing_required_sections(
    document: WorkingDocument, template: SemanticTemplate
) -> WorkingDocument:
    if not document.nodes:
        return document

    present_section_ids = {node.section_id for node in document.nodes}
    missing_nodes = [
        DocumentNode(
            kind="gap",
            section_id=section.id,
            flags=["missing-source-data"],
        )
        for section in template.sections
        if section.required and section.id not in present_section_ids
    ]
    return document.model_copy(update={"nodes": [*document.nodes, *missing_nodes]})


__all__ = ["AssembleWorkflow", "WorkflowError"]
