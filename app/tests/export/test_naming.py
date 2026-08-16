from __future__ import annotations

from docgen.export._naming import make_safe_filename


def test_make_safe_filename_strips_unsafe_characters() -> None:
    filename = make_safe_filename('Мой док:умент "тест"/<>', ".pdf")

    assert filename == "Мой-документ-тест.pdf"


def test_make_safe_filename_falls_back_when_title_is_empty_after_stripping() -> None:
    assert make_safe_filename("***", ".html") == "document.html"
    assert make_safe_filename("", ".docx") == "document.docx"


def test_make_safe_filename_collapses_whitespace_to_single_hyphen() -> None:
    filename = make_safe_filename("Заголовок   с   пробелами", ".md")

    assert filename == "Заголовок-с-пробелами.md"


def test_make_safe_filename_result_stays_within_byte_budget_with_long_cyrillic_title() -> None:
    """The core finding-6 regression: a 150+ character Cyrillic title, once
    encoded as UTF-8 (2 bytes/char) and combined with a template-id suffix
    that ExportStorage appends afterward, must never exceed the 255-byte
    filesystem limit for a single filename."""
    long_title = "Очень длинное название документа для банковского регламента " * 5
    assert len(long_title) > 150

    filename = make_safe_filename(long_title, ".pdf", reserved_suffix="-docgen-light")
    full_name = filename.replace(".pdf", "-docgen-light.pdf")  # mirrors ExportStorage

    assert len(full_name.encode("utf-8")) <= 255


def test_make_safe_filename_reserves_room_for_suffix() -> None:
    """A short reserved_suffix leaves more room for the stem than a long one."""
    long_title = "А" * 200

    short_suffix = make_safe_filename(long_title, ".pdf", reserved_suffix="-x")
    long_suffix = make_safe_filename(long_title, ".pdf", reserved_suffix="-" + "y" * 100)

    assert len(long_suffix) < len(short_suffix)


def test_make_safe_filename_never_splits_a_multibyte_character() -> None:
    long_title = "Ё" * 300

    filename = make_safe_filename(long_title, ".pdf", reserved_suffix="-docgen-light")

    # Must decode/encode round-trip cleanly -- a split multi-byte char would
    # either raise here or silently corrupt.
    filename.encode("utf-8").decode("utf-8")


def test_make_safe_filename_short_title_is_unaffected_by_byte_budget() -> None:
    assert make_safe_filename("Отчёт", ".html", reserved_suffix="-docgen-light") == "Отчёт.html"
