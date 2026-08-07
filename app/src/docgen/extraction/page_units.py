from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from math import ceil

from docgen.extraction.schemas import BlockKind, NormalizedBlock


@dataclass(frozen=True)
class VirtualPageCalculator:
    chars_per_page: int = 1800

    def __post_init__(self) -> None:
        if self.chars_per_page <= 0:
            raise ValueError("chars_per_page must be positive")

    def from_text(self, text: str) -> int:
        non_whitespace_characters = sum(not character.isspace() for character in text)
        return ceil(max(1, non_whitespace_characters) / self.chars_per_page)

    def from_blocks(self, blocks: Iterable[NormalizedBlock]) -> int:
        block_list = list(blocks)
        text = "".join(block.text for block in block_list if block.kind is not BlockKind.IMAGE)
        return self.from_text(text) + sum(block.kind is BlockKind.IMAGE for block in block_list)
