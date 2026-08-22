import pytest
from docgen.chat.adapters import FaqAdapter
from pydantic import ValidationError

from docgen.chat.schemas import FaqEntryDraft, FaqPlacement
from docgen.documents.operations import InsertNode
from docgen.documents.schemas import WorkingDocument


def test_faq_contract_requires_complete_question_answer_and_evidence() -> None:
    with pytest.raises(ValidationError):
        FaqEntryDraft.model_validate(
            {
                "question": "Каков лимит?",
                "answer": "",
                "placement": {"index": 0},
                "evidence_block_ids": [],
            }
        )


def test_faq_adapter_compiles_only_visible_pair_without_service_fields() -> None:
    document = WorkingDocument(title="FAQ", template_id="faq")
    draft = FaqEntryDraft(
        question="Каков лимит?",
        answer="10 000 рублей.",
        placement=FaqPlacement(index=0),
        evidence_block_ids=["s1:limit"],
    )

    item = FaqAdapter().compile(document, draft)

    assert isinstance(item.operation, InsertNode)
    assert item.operation.node.data == {
        "items": ["Вопрос: Каков лимит?\nОтвет: 10 000 рублей."]
    }
    assert item.evidence_block_ids == ["s1:limit"]
    assert "evidence" not in item.operation.node.data["items"][0]
