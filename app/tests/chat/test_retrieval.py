import pytest

from docgen.chat.errors import ChatError, ChatErrorCode
from docgen.chat.retrieval import (
    SourceSnapshot,
    SourceSnapshotCache,
    retrieve_relevant_blocks,
)
from docgen.extraction.schemas import BlockKind, NormalizedBlock


def _block(block_id: str, text: str) -> NormalizedBlock:
    return NormalizedBlock(
        id=block_id,
        kind=BlockKind.TEXT,
        text=text,
        confidence=1,
    )


def test_retrieval_returns_only_ranked_bounded_context() -> None:
    snapshot = SourceSnapshot(
        configured_source_count=1,
        blocks=[
            _block("intro", "Общее введение"),
            _block("limit", "Максимальный лимит составляет 10 000 рублей"),
            _block("other", "Правила авторизации пользователя"),
        ],
    )

    result = retrieve_relevant_blocks(snapshot, "добавь лимит", limit=1)

    assert [block.id for block in result.blocks] == ["limit"]
    assert result.total_blocks == 3


def test_retrieval_does_not_fall_back_to_unrelated_first_blocks() -> None:
    snapshot = SourceSnapshot(
        configured_source_count=1,
        blocks=[_block("intro", "Общее введение")],
    )

    with pytest.raises(ChatError) as caught:
        retrieve_relevant_blocks(snapshot, "комиссия эквайринга")

    assert caught.value.code is ChatErrorCode.RELEVANT_FRAGMENT_MISSING


@pytest.mark.parametrize(
    ("snapshot", "code"),
    [
        (SourceSnapshot(configured_source_count=0), ChatErrorCode.SOURCES_MISSING),
        (
            SourceSnapshot(
                configured_source_count=1,
                warnings=("Источник Confluence пропущен: HTTP 503",),
            ),
            ChatErrorCode.SOURCE_UNAVAILABLE,
        ),
    ],
)
def test_retrieval_reports_source_state_precisely(
    snapshot: SourceSnapshot,
    code: ChatErrorCode,
) -> None:
    with pytest.raises(ChatError) as caught:
        retrieve_relevant_blocks(snapshot, "лимит")

    assert caught.value.code is code
    assert caught.value.action


def test_source_snapshot_cache_invalidates_previous_project_identity() -> None:
    cache = SourceSnapshotCache(ttl_seconds=60)
    first = SourceSnapshot(configured_source_count=1, identity="source-a")
    second = SourceSnapshot(configured_source_count=2, identity="source-a|source-b")

    cache.put("project", first)
    assert cache.get("project", "source-a") is first

    cache.put("project", second)
    assert cache.get("project", "source-a") is None
    assert cache.get("project", "source-a|source-b") is second
