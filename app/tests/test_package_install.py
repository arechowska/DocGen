from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile


def test_wheel_clean_install_renders_pages_outside_source_tree(tmp_path: Path) -> None:
    app_root = Path(__file__).parents[1]
    wheel_dir = tmp_path / "wheel"
    install_dir = tmp_path / "installed"
    smoke_dir = tmp_path / "independent-cwd"
    wheel_dir.mkdir()
    smoke_dir.mkdir()
    (smoke_dir / ".env").write_text(
        "DOCGEN_DATABASE_URL=sqlite:///wheel-smoke.db\n"
        "DOCGEN_DATA_DIR=wheel-smoke-data\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheel_dir),
            ".",
        ],
        cwd=app_root,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(wheel_dir.glob("docgen-*.whl"))
    with ZipFile(wheel) as archive:
        members = set(archive.namelist())
    assert "docgen/templates/projects/index.html" in members
    assert "docgen/templates/projects/source_panel.html" in members
    assert "docgen/templates/projects/work_panel.html" in members
    assert "docgen/templates/projects/action_panel.html" in members
    assert "docgen/templates/editor/surface.html" in members
    assert "docgen/templates/generation/status.html" in members
    assert "docgen/static/vendor/htmx-2.0.8.min.js" in members
    assert "docgen/static/vendor/HTMX-LICENSE.txt" in members
    assert "docgen/static/vendor/TAILWIND-LICENSE.txt" in members
    assert "docgen/static/css/docgen.css" in members
    assert "docgen/static/js/docgen2-editor.js" in members

    # colvir.docx is the runtime asset DocxExporter actually loads; the
    # 236KB colvir_v3.dotx it was built from (tools/build_default_docx_
    # template.py) is a build-time-only input and must not bloat the
    # shipped wheel.
    assert "docgen/formatting/templates/colvir.docx" in members
    assert "docgen/formatting/templates/Akrobat-Bold.otf" in members
    assert "docgen/formatting/templates/Roboto-Regular.ttf" in members
    assert "docgen/formatting/templates/Roboto-Light.ttf" in members
    assert "docgen/formatting/templates/Roboto-Bold.ttf" in members
    assert not any(name.endswith(".dotx") for name in members)

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(install_dir),
            str(wheel),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    script = r'''
import os
import socket
from pathlib import Path

from fastapi.testclient import TestClient
import docgen
from docgen.config import Settings
from docgen.main import create_app

install_dir = Path(os.environ["DOCGEN_SMOKE_INSTALL_DIR"]).resolve()
smoke_dir = Path(os.environ["DOCGEN_SMOKE_ROOT"]).resolve()
assert Path(docgen.__file__).resolve().is_relative_to(install_dir)

def deny_external_network(*args, **kwargs):
    raise AssertionError("wheel smoke attempted an external network connection")

socket.create_connection = deny_external_network
original_connect = socket.socket.connect

def allow_loopback_only(sock, address):
    if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1", "localhost"}:
        return original_connect(sock, address)
    raise AssertionError("wheel smoke attempted an external network connection")

socket.socket.connect = allow_loopback_only
settings = Settings()
assert settings.database_url == "sqlite:///wheel-smoke.db"
assert settings.data_dir == Path("wheel-smoke-data")
with TestClient(create_app(settings)) as client:
    projects = client.get("/projects")
    assert projects.status_code == 200
    assert "/static/css/docgen.css" in projects.text
    created = client.post(
        "/projects",
        data={"name": "Wheel smoke"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    detail = client.get(created.headers["location"])
    assert detail.status_code == 200
    assert "Formatta" in detail.text
    assert "К выбору проекта" in detail.text
    assert "Wheel smoke" in detail.text
    templates = client.get(
        created.headers["location"] + "/export/templates?format=html"
    )
    assert templates.status_code == 200
    assert 'value="docgen-light"' in templates.text
    assert client.get("/static/vendor/htmx-2.0.8.min.js").status_code == 200
'''
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(install_dir)
    environment["DOCGEN_SMOKE_INSTALL_DIR"] = str(install_dir)
    environment["DOCGEN_SMOKE_ROOT"] = str(smoke_dir)
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=smoke_dir,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
