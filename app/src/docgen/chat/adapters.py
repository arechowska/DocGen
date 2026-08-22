from __future__ import annotations

from uuid import uuid4

from docgen.chat.errors import ChatError, ChatErrorCode
from docgen.chat.schemas import ChatEditOperation, FaqEntryDraft
from docgen.documents.operations import InsertNode
from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument


class FaqAdapter:
    action = "faq.add_entry"

    def compile(
        self,
        document: WorkingDocument,
        draft: FaqEntryDraft,
    ) -> ChatEditOperation:
        placement = draft.placement
        if placement.parent_id is not None:
            raise ChatError(
                ChatErrorCode.INVALID_OPERATION,
                message="FAQ-запись можно добавить только на верхний уровень документа",
                action="Повтори запрос и укажи позицию между разделами FAQ.",
            )
        if placement.index > len(document.nodes):
            raise ChatError(ChatErrorCode.INVALID_OPERATION)
        visible_pair = f"Вопрос: {draft.question}\nОтвет: {draft.answer}"
        return ChatEditOperation(
            operation=InsertNode(
                index=placement.index,
                node=DocumentNode(
                    id=f"faq-{uuid4()}",
                    kind=NodeKind.LIST,
                    data={"items": [visible_pair]},
                    flags=["grounded"],
                ),
            ),
            evidence_block_ids=draft.evidence_block_ids,
        )
