from pathlib import Path

from docgen.models import Source, SourceKind

_CHECK_TARGET_EXTENSIONS = frozenset({".docx", ".pdf", ".txt", ".md"})


def is_supported_check_target(source: Source) -> bool:
    return (
        source.kind is SourceKind.FILE
        and Path(source.display_name).suffix.lower() in _CHECK_TARGET_EXTENSIONS
    )


def supported_check_targets(sources: list[Source]) -> list[Source]:
    return [source for source in sources if is_supported_check_target(source)]


__all__ = ["is_supported_check_target", "supported_check_targets"]
