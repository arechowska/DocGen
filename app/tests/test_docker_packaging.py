from __future__ import annotations

from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_dockerfile_packages_docgen_runtime() -> None:
    dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM python:3.12-slim" in dockerfile
    assert "WORKDIR /app" in dockerfile
    assert "COPY app/pyproject.toml ./pyproject.toml" in dockerfile
    assert "COPY app/src ./src" in dockerfile
    assert 'CMD ["uvicorn", "docgen.main:app", "--host", "0.0.0.0", "--port", "8000"]' in dockerfile


def test_docker_compose_runs_web_and_worker_from_one_image() -> None:
    compose = yaml.safe_load((REPOSITORY_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

    services = compose["services"]
    assert sorted(services) == ["web", "worker"]
    assert services["web"]["build"]["context"] == "."
    assert services["web"]["ports"] == ["8000:8000"]
    assert services["web"]["env_file"] == [".env"]
    assert services["web"]["environment"]["DOCGEN_DATABASE_URL"] == "sqlite:////app/var/docgen.db"
    assert services["web"]["environment"]["DOCGEN_DATA_DIR"] == "/app/var/data"
    assert "docgen-var:/app/var" in services["web"]["volumes"]

    assert services["worker"]["build"]["context"] == "."
    assert services["worker"]["command"] == ["python", "-m", "docgen.jobs.worker"]
    assert services["worker"]["env_file"] == [".env"]
    assert services["worker"]["environment"]["DOCGEN_WORKER_ID"] == "docker-worker-1"
    assert services["worker"]["depends_on"] == ["web"]
    assert "docgen-var:/app/var" in services["worker"]["volumes"]

    assert "docgen-var" in compose["volumes"]


def test_dokploy_compose_keeps_web_internal_and_worker_ready() -> None:
    compose = yaml.safe_load(
        (REPOSITORY_ROOT / "docker-compose.dokploy.yml").read_text(encoding="utf-8")
    )

    services = compose["services"]
    web = services["web"]
    worker = services["worker"]

    assert sorted(services) == ["web", "worker"]
    assert web["build"]["context"] == "."
    assert web["expose"] == ["8000"]
    assert "ports" not in web
    assert web["healthcheck"]["test"] == [
        "CMD",
        "python",
        "-c",
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')",
    ]
    assert web["environment"]["DOCGEN_DATABASE_URL"] == "sqlite:////app/var/docgen.db"
    assert "formatta-var:/app/var" in web["volumes"]

    assert worker["command"] == ["python", "-m", "docgen.jobs.worker"]
    assert worker["environment"]["DOCGEN_WORKER_ID"] == "dockploy-worker-1"
    assert worker["depends_on"]["web"]["condition"] == "service_healthy"
    assert "formatta-var:/app/var" in worker["volumes"]
    assert "formatta-var" in compose["volumes"]


def test_dockerignore_excludes_secrets_runtime_data_and_local_artifacts() -> None:
    patterns = {
        line.strip()
        for line in (REPOSITORY_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }

    assert ".env" in patterns
    assert "app/var/" in patterns
    assert "app/.venv/" in patterns
    assert ".git/" in patterns
    assert "**/__pycache__/" in patterns


def test_readme_documents_docker_startup() -> None:
    readme = (REPOSITORY_ROOT / "app" / "README.md").read_text(encoding="utf-8")

    assert "docker compose up --build" in readme
    assert "http://127.0.0.1:8000/projects" in readme
    assert "DOCGEN_LOCAL_TEXT_API_KEY" in readme


def test_readme_documents_dokploy_deployment() -> None:
    readme = (REPOSITORY_ROOT / "app" / "README.md").read_text(encoding="utf-8")

    assert "docker-compose.dokploy.yml" in readme
    assert "сервису `web`" in readme
    assert "внутренний порт контейнера `8000`" in readme
    assert "http://<домен>/health" in readme
