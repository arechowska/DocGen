from io import BytesIO
from pathlib import Path

import pytest

from docgen.sources.storage import LocalStorage


def test_save_uses_ids_not_untrusted_filename(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)

    saved = storage.save("project-1", "source-1", "../../secret.txt", BytesIO(b"safe"))

    path = storage.resolve(saved.relative_path)
    assert path.read_bytes() == b"safe"
    assert path.is_relative_to(tmp_path.resolve())
    assert ".." not in saved.relative_path
    assert saved.relative_path == "projects/project-1/sources/source-1.txt"
    assert saved.size_bytes == 4


def test_resolve_rejects_path_escape(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)

    with pytest.raises(ValueError, match="Недопустимый путь"):
        storage.resolve("../outside.txt")


@pytest.mark.parametrize("project_id", ("../project", "nested/project", ""))
def test_save_rejects_unsafe_project_id(tmp_path: Path, project_id: str) -> None:
    storage = LocalStorage(tmp_path)

    with pytest.raises(ValueError, match="Недопустимый идентификатор"):
        storage.save(project_id, "source-1", "file.pdf", BytesIO(b"safe"))


@pytest.mark.parametrize("source_id", ("../source", "nested/source", ""))
def test_save_rejects_unsafe_source_id(tmp_path: Path, source_id: str) -> None:
    storage = LocalStorage(tmp_path)

    with pytest.raises(ValueError, match="Недопустимый идентификатор"):
        storage.save("project-1", source_id, "file.pdf", BytesIO(b"safe"))


def test_save_replaces_part_file_only_after_complete_copy(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    destination = tmp_path / "projects" / "project-1" / "sources" / "source-1.txt"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"previous")

    class FailingStream(BytesIO):
        def read(self, size: int = -1) -> bytes:
            if self.tell():
                raise OSError("copy failed")
            return super().read(size)

    with pytest.raises(OSError, match="copy failed"):
        storage.save("project-1", "source-1", "file.txt", FailingStream(b"new content"))

    assert destination.read_bytes() == b"previous"
    assert not destination.with_suffix(".txt.part").exists()


def test_delete_is_idempotent_and_scoped_to_data_dir(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    saved = storage.save("project-1", "source-1", "file.pdf", BytesIO(b"safe"))

    storage.delete(saved.relative_path)
    storage.delete(saved.relative_path)

    assert not (tmp_path / saved.relative_path).exists()
    with pytest.raises(ValueError, match="Недопустимый путь"):
        storage.delete("../outside.txt")


def test_delete_project_removes_only_validated_project_directory(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    saved = storage.save("project-1", "source-1", "file.pdf", BytesIO(b"safe"))
    other_saved = storage.save("project-2", "source-2", "file.pdf", BytesIO(b"other"))

    storage.delete_project("project-1")
    storage.delete_project("project-1")

    assert not (tmp_path / saved.relative_path).exists()
    assert storage.resolve(other_saved.relative_path).read_bytes() == b"other"
    with pytest.raises(ValueError, match="Недопустимый идентификатор"):
        storage.delete_project("../project-2")
