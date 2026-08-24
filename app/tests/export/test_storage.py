from __future__ import annotations

from pathlib import Path

import pytest

from docgen.export.protocol import RenderedFile
from docgen.export.storage import ExportStorage
from docgen.formatting.schemas import OutputFormat


@pytest.fixture
def storage(tmp_path: Path) -> ExportStorage:
    return ExportStorage(tmp_path / "data")


def test_save_writes_atomically_and_returns_metadata(storage: ExportStorage) -> None:
    rendered = RenderedFile(
        filename="Отчёт.html", media_type="text/html", content=b"<html></html>"
    )

    stored = storage.save("proj-1", OutputFormat.HTML, "docgen-light", rendered)

    resolved = storage.resolve(stored.relative_path)
    assert resolved.read_bytes() == rendered.content
    assert stored.filename == "Отчёт-docgen-light.html"
    assert stored.size_bytes == len(rendered.content)
    assert stored.relative_path == f"projects/proj-1/exports/{stored.filename}"
    # no leftover .part file after a successful write
    assert not (resolved.parent / ".html-docgen-light.part").exists()


def test_save_replaces_previous_export_for_same_format_and_template(
    storage: ExportStorage,
) -> None:
    first = RenderedFile(filename="doc.html", media_type="text/html", content=b"first")
    second = RenderedFile(
        filename="doc.html", media_type="text/html", content=b"second, longer content"
    )

    stored_first = storage.save("proj-1", OutputFormat.HTML, "docgen-light", first)
    stored_second = storage.save("proj-1", OutputFormat.HTML, "docgen-light", second)

    assert stored_first.relative_path == stored_second.relative_path
    assert storage.resolve(stored_second.relative_path).read_bytes() == second.content


def test_save_conversion_uses_a_name_not_shared_with_editor_exports(
    storage: ExportStorage,
) -> None:
    rendered = RenderedFile(
        filename="document.html",
        media_type="text/html",
        content=b"direct conversion",
    )

    conversion = storage.save_conversion(
        "proj-1", OutputFormat.HTML, "docgen-light", rendered
    )
    editor_export = storage.save(
        "proj-1", OutputFormat.HTML, "docgen-light", rendered
    )

    assert conversion.filename == "document-conversion-docgen-light.html"
    assert conversion.relative_path != editor_export.relative_path
    assert storage.resolve(conversion.relative_path).read_bytes() == rendered.content


def test_latest_returns_newest_matching_project_export(storage: ExportStorage) -> None:
    older = storage.save(
        "proj-1",
        OutputFormat.HTML,
        "docgen-light",
        RenderedFile(filename="older.html", media_type="text/html", content=b"older"),
    )
    newer = storage.save(
        "proj-1",
        OutputFormat.HTML,
        "docgen-light",
        RenderedFile(filename="newer.html", media_type="text/html", content=b"newer"),
    )

    latest = storage.latest("proj-1", OutputFormat.HTML, "docgen-light")

    assert latest is not None
    assert latest.relative_path == newer.relative_path
    assert latest.filename == newer.filename
    assert latest.size_bytes == len(b"newer")
    assert latest.relative_path != older.relative_path


def test_latest_returns_none_without_matching_export(storage: ExportStorage) -> None:
    assert storage.latest("proj-1", OutputFormat.HTML, "docgen-light") is None


def test_latest_conversion_excludes_regular_export_from_legacy_files(
    storage: ExportStorage,
) -> None:
    legacy_conversion = storage.save(
        "proj-1",
        OutputFormat.HTML,
        "docgen-light",
        RenderedFile(
            filename="source.html",
            media_type="text/html",
            content=b"saved source",
        ),
    )
    editor_export = storage.save(
        "proj-1",
        OutputFormat.HTML,
        "docgen-light",
        RenderedFile(
            filename="editor.html",
            media_type="text/html",
            content=b"rebuilt editor",
        ),
    )

    latest = storage.latest_conversion(
        "proj-1",
        OutputFormat.HTML,
        "docgen-light",
        legacy_export_paths={editor_export.relative_path},
    )

    assert latest == legacy_conversion


def test_latest_conversion_prefers_new_conversion_namespace(
    storage: ExportStorage,
) -> None:
    storage.save(
        "proj-1",
        OutputFormat.HTML,
        "docgen-light",
        RenderedFile(
            filename="legacy.html", media_type="text/html", content=b"legacy"
        ),
    )
    current = storage.save_conversion(
        "proj-1",
        OutputFormat.HTML,
        "docgen-light",
        RenderedFile(
            filename="current.html", media_type="text/html", content=b"current"
        ),
    )

    assert (
        storage.latest_conversion("proj-1", OutputFormat.HTML, "docgen-light")
        == current
    )


def test_save_deletes_part_file_and_preserves_previous_target_on_write_failure(
    storage: ExportStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = RenderedFile(filename="doc.html", media_type="text/html", content=b"good content")
    stored = storage.save("proj-1", OutputFormat.HTML, "docgen-light", good)
    destination = storage.resolve(stored.relative_path)
    part_path = destination.parent / ".html-docgen-light.part"

    def failing_fsync(fd: int) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr("docgen.export.storage.os.fsync", failing_fsync)

    bad = RenderedFile(filename="doc.html", media_type="text/html", content=b"bad content")
    with pytest.raises(OSError):
        storage.save("proj-1", OutputFormat.HTML, "docgen-light", bad)

    # the previous successful export is completely untouched
    assert destination.read_bytes() == b"good content"
    # the failed attempt's .part file was cleaned up, not left behind
    assert not part_path.exists()


def test_save_rejects_unsafe_template_id(storage: ExportStorage) -> None:
    rendered = RenderedFile(filename="doc.html", media_type="text/html", content=b"x")

    with pytest.raises(ValueError):
        storage.save("proj-1", OutputFormat.HTML, "../evil", rendered)


def test_save_rejects_unsafe_project_id(storage: ExportStorage) -> None:
    rendered = RenderedFile(filename="doc.html", media_type="text/html", content=b"x")

    with pytest.raises(ValueError):
        storage.save("../evil", OutputFormat.HTML, "docgen-light", rendered)


def test_resolve_rejects_path_traversal(storage: ExportStorage) -> None:
    with pytest.raises(ValueError):
        storage.resolve("../../etc/passwd")


def test_resolve_rejects_absolute_path(storage: ExportStorage) -> None:
    with pytest.raises(ValueError):
        storage.resolve("/etc/passwd")
