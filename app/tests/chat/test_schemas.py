from docgen.chat.schemas import ChatEditPlan
from docgen.documents.operations import UpdateData


def test_chat_edit_plan_accepts_bare_operation_list_as_noop_plan() -> None:
    plan = ChatEditPlan.model_validate([])

    assert plan.summary == "Нет правок"
    assert plan.operations == []


def test_chat_edit_plan_accepts_bare_update_data_operation_list() -> None:
    plan = ChatEditPlan.model_validate(
        [
            {
                "kind": "update_data",
                "node_id": "n1",
                "data": {"style": {"color": "green", "font-style": "italic"}},
            }
        ]
    )

    assert plan.summary
    operation = plan.operations[0].operation
    assert isinstance(operation, UpdateData)
    assert operation.node_id == "n1"
    assert operation.data == {"style": {"color": "green", "font-style": "italic"}}
    assert plan.operations[0].evidence_block_ids == []


def test_chat_edit_plan_accepts_dotted_data_style_update() -> None:
    plan = ChatEditPlan.model_validate(
        {
            "summary": "Formatted first paragraph",
            "operations": [
                {
                    "operation": {
                        "kind": "update_data",
                        "node_id": "n1",
                        "data.style": {
                            "color": "blue",
                            "font-weight": "700",
                        },
                    },
                    "evidence_block_ids": [],
                }
            ],
        }
    )

    operation = plan.operations[0].operation
    assert isinstance(operation, UpdateData)
    assert operation.data == {
        "style": {
            "color": "blue",
            "font-weight": "700",
        }
    }
