from __future__ import annotations

from pathlib import Path


def test_windows_launcher_starts_worker_and_limits_reload_scope() -> None:
    launcher = Path(__file__).resolve().parents[2] / "1.bat"

    script = launcher.read_text(encoding="utf-8")

    assert "DOCGEN_WORKER_ID" in script
    assert "-m docgen.jobs.worker" in script
    assert "--reload-dir src" in script
    assert "--reload\n" not in script
    assert "rmdir /s /q .venv" not in script
