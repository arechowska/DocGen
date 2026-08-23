from __future__ import annotations

import re
import time
from dataclasses import dataclass
from threading import Lock

from docgen.chat.errors import ChatError, ChatErrorCode
from docgen.extraction.schemas import BlockKind, NormalizedBlock


@dataclass(frozen=True)
class SourceReference:
    id: str
    display_name: str


@dataclass(frozen=True)
class SourceSnapshot:
    configured_source_count: int
    blocks: tuple[NormalizedBlock, ...] | list[NormalizedBlock] = ()
    warnings: tuple[str, ...] = ()
    identity: str | None = None
    sources: tuple[SourceReference, ...] = ()


@dataclass(frozen=True)
class RetrievalResult:
    blocks: list[NormalizedBlock]
    total_blocks: int


class SourceSnapshotCache:
    def __init__(self, ttl_seconds: float = 60.0) -> None:
        self._ttl_seconds = ttl_seconds
        self._items: dict[tuple[str, str], tuple[float, SourceSnapshot]] = {}
        self._lock = Lock()

    def get(self, project_id: str, identity: str) -> SourceSnapshot | None:
        now = time.monotonic()
        key = (project_id, identity)
        with self._lock:
            cached = self._items.get(key)
            if cached is None:
                return None
            created_at, snapshot = cached
            if now - created_at > self._ttl_seconds:
                del self._items[key]
                return None
            return snapshot

    def put(self, project_id: str, snapshot: SourceSnapshot) -> None:
        if snapshot.identity is None:
            return
        with self._lock:
            self._items = {
                key: value for key, value in self._items.items() if key[0] != project_id
            }
            self._items[(project_id, snapshot.identity)] = (time.monotonic(), snapshot)


_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_STOP_WORDS = {
    "блок",
    "вопрос",
    "добавь",
    "добавить",
    "документ",
    "как",
    "какой",
    "который",
    "ответ",
    "про",
    "сделай",
    "что",
}


def retrieve_relevant_blocks(
    snapshot: SourceSnapshot,
    query: str,
    *,
    limit: int = 12,
) -> RetrievalResult:
    if snapshot.configured_source_count == 0:
        raise ChatError(ChatErrorCode.SOURCES_MISSING)
    blocks = list(snapshot.blocks)
    if not blocks:
        raise ChatError(ChatErrorCode.SOURCE_UNAVAILABLE)

    query_tokens = _tokens(query)
    if not query_tokens:
        raise ChatError(
            ChatErrorCode.CLARIFICATION,
            message="Не удалось определить тему фактологической правки",
            action="Уточни, какой факт нужно найти или изменить.",
        )
    ranked = sorted(
        (
            (_score(query_tokens, _tokens(block.text)), index, block)
            for index, block in enumerate(blocks)
        ),
        key=lambda item: (-item[0], item[1]),
    )
    matched_indices = [index for score, index, _block in ranked if score > 0]
    if not matched_indices:
        raise ChatError(ChatErrorCode.RELEVANT_FRAGMENT_MISSING)
    selected_indices: list[int] = []
    for index in matched_indices:
        if index not in selected_indices:
            selected_indices.append(index)
        if blocks[index].kind is BlockKind.HEADING:
            following = index + 1
            while following < len(blocks) and blocks[following].kind is not BlockKind.HEADING:
                if following not in selected_indices:
                    selected_indices.append(following)
                following += 1
        if len(selected_indices) >= limit:
            break
    selected = [blocks[index] for index in selected_indices[:limit]]
    return RetrievalResult(blocks=selected, total_blocks=len(blocks))


def _score(query: list[str], content: list[str]) -> int:
    return sum(
        1
        for query_token in query
        if any(_matches(query_token, content_token) for content_token in content)
    )


def _tokens(value: str) -> list[str]:
    return [
        token
        for token in _WORD.findall(value.casefold().replace("ё", "е"))
        if len(token) > 2 and token not in _STOP_WORDS
    ]


def _matches(left: str, right: str) -> bool:
    if left == right:
        return True
    common = 0
    for left_character, right_character in zip(left, right, strict=False):
        if left_character != right_character:
            break
        common += 1
    return common >= 5 and common / min(len(left), len(right)) >= 0.7
