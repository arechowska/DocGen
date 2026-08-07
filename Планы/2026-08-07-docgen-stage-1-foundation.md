# DocGen Stage 1 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Создать основу однопользовательского DocGen: проекты, локальное сохранение, файловые источники, ссылки Confluence и минимальный веб-интерфейс.

**Architecture:** FastAPI обслуживает HTML-страницы и HTMX-фрагменты из одного Python-приложения. Метаданные проектов и источников хранятся в SQLite через SQLAlchemy, содержимое файлов — в локальном каталоге данных; доступ к обоим хранилищам изолирован репозиториями и сервисами. Этап не извлекает содержимое документов и не вызывает AI-модели: сохранённые источники являются стабильным входным интерфейсом этапа 2.

**Tech Stack:** Python 3.12, FastAPI, Uvicorn, Jinja2, HTMX 2, Tailwind CSS CDN, SQLAlchemy 2, Pydantic Settings 2, pytest, FastAPI TestClient, Ruff.

## Global Constraints

- Интерфейс и пользовательские сообщения — на русском языке.
- Приложение однопользовательское; авторизация и роли отсутствуют.
- Данные хранятся локально внутри корпоративного контура.
- Поддерживаемые файловые расширения: `.docx`, `.pdf`, `.txt`, `.md`, `.png`, `.jpg`, `.jpeg`, `.webp`.
- Ссылочным источником на этом этапе может быть только HTTPS-страница Confluence на разрешённом хосте.
- Один источник принадлежит ровно одному проекту.
- Удаление проекта удаляет его записи и каталог файлов; удаление источника удаляет его запись и сохранённый файл.
- История версий, совместная работа, извлечение содержимого, AI-обработка и экспорт не входят в этап 1.
- Визуальные правила берутся из `Проекты/DocGen/DESIGN.md`: Tailwind CDN, светлая палитра, системный шрифт, просторные поверхности, основные CTA цвета `#0071e3`.

## File Structure

```text
Проекты/DocGen/app/
├── pyproject.toml                 # зависимости, pytest и Ruff
├── README.md                      # локальный запуск и команды проверки
├── src/docgen/
│   ├── __init__.py
│   ├── main.py                    # фабрика FastAPI и lifespan
│   ├── config.py                  # настройки из DOCGEN_* переменных
│   ├── db.py                      # engine, session factory, Base
│   ├── web.py                     # Jinja2Templates и общие web-зависимости
│   ├── projects/
│   │   ├── models.py              # SQLAlchemy Project
│   │   ├── repository.py          # CRUD проектов
│   │   ├── service.py             # правила проектов и очистка данных
│   │   └── routes.py              # страницы и HTMX endpoints проектов
│   ├── sources/
│   │   ├── models.py              # SQLAlchemy Source и SourceKind
│   │   ├── repository.py          # CRUD источников
│   │   ├── storage.py             # безопасное локальное файловое хранилище
│   │   ├── service.py             # валидация и операции с источниками
│   │   └── routes.py              # upload/link/delete endpoints
│   ├── templates/
│   │   ├── base.html
│   │   ├── projects/index.html
│   │   ├── projects/detail.html
│   │   └── sources/list.html
│   └── static/.gitkeep
└── tests/
    ├── conftest.py                # временная БД, storage и TestClient
    ├── test_health.py
    ├── projects/test_repository.py
    ├── projects/test_routes.py
    ├── sources/test_storage.py
    ├── sources/test_service.py
    └── sources/test_routes.py
```

---

### Task 1: Bootstrap the FastAPI application

**Files:**
- Create: `Проекты/DocGen/app/pyproject.toml`
- Create: `Проекты/DocGen/app/src/docgen/__init__.py`
- Create: `Проекты/DocGen/app/src/docgen/config.py`
- Create: `Проекты/DocGen/app/src/docgen/main.py`
- Create: `Проекты/DocGen/app/src/docgen/web.py`
- Create: `Проекты/DocGen/app/src/docgen/templates/base.html`
- Create: `Проекты/DocGen/app/src/docgen/static/.gitkeep`
- Create: `Проекты/DocGen/app/tests/conftest.py`
- Create: `Проекты/DocGen/app/tests/test_health.py`

**Interfaces:**
- Produces: `docgen.main.create_app(settings: Settings | None = None) -> FastAPI`
- Produces: `docgen.config.Settings(database_url: str, data_dir: Path, confluence_hosts: tuple[str, ...])`
- Produces: `GET /health -> {"status": "ok"}`

- [ ] **Step 1: Create package metadata and test configuration**

```toml
[project]
name = "docgen"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115,<1",
  "uvicorn[standard]>=0.34,<1",
  "jinja2>=3.1,<4",
  "python-multipart>=0.0.20,<1",
  "sqlalchemy>=2.0,<3",
  "pydantic-settings>=2.7,<3",
]

[project.optional-dependencies]
dev = ["httpx>=0.28,<1", "pytest>=8.3,<9", "ruff>=0.9,<1"]

[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]

[tool.ruff]
target-version = "py312"
line-length = 100
```

- [ ] **Step 2: Write the failing health test**

```python
from fastapi.testclient import TestClient


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [ ] **Step 3: Run the test and verify the application does not exist**

Run: `cd Проекты/DocGen/app && python -m pytest tests/test_health.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'docgen'`.

- [ ] **Step 4: Implement settings and the application factory**

```python
# src/docgen/config.py
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DOCGEN_")
    database_url: str = "sqlite:///./var/docgen.db"
    data_dir: Path = Path("./var/data")
    confluence_hosts: tuple[str, ...] = ("confluence.local",)
```

For environment configuration, `DOCGEN_CONFLUENCE_HOSTS` uses a JSON array, for example `["wiki.example.test"]`, as required by Pydantic Settings for a tuple field.

```python
# src/docgen/main.py
from fastapi import FastAPI
from .config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="DocGen")
    app.state.settings = settings or Settings()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
```

Create `web.py` with a `Jinja2Templates` instance rooted at `src/docgen/templates`. Create `base.html` with Russian `lang`, system font, Tailwind CDN, HTMX 2 script, colors from `DESIGN.md`, and `{% block content %}`. Keep `__init__.py` empty.

- [ ] **Step 5: Add isolated test settings and TestClient**

```python
# tests/conftest.py
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from docgen.config import Settings
from docgen.main import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        data_dir=tmp_path / "data",
        confluence_hosts=("wiki.example.test",),
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client
```

- [ ] **Step 6: Run tests and lint**

Run: `cd Проекты/DocGen/app && python -m pytest -v && python -m ruff check .`

Expected: all tests PASS; Ruff reports no errors.

- [ ] **Step 7: Commit**

```bash
git add Проекты/DocGen/app
git commit -m "chore: bootstrap DocGen FastAPI app"
```

### Task 2: Add database lifecycle and project persistence

**Files:**
- Create: `Проекты/DocGen/app/src/docgen/db.py`
- Create: `Проекты/DocGen/app/src/docgen/projects/__init__.py`
- Create: `Проекты/DocGen/app/src/docgen/projects/models.py`
- Create: `Проекты/DocGen/app/src/docgen/projects/repository.py`
- Modify: `Проекты/DocGen/app/src/docgen/main.py`
- Modify: `Проекты/DocGen/app/tests/conftest.py`
- Create: `Проекты/DocGen/app/tests/projects/test_repository.py`

**Interfaces:**
- Consumes: `Settings.database_url`
- Produces: `Project(id: str, name: str, created_at: datetime, updated_at: datetime)`
- Produces: `ProjectRepository.create(name: str) -> Project`
- Produces: `ProjectRepository.list() -> list[Project]`
- Produces: `ProjectRepository.get(project_id: str) -> Project | None`
- Produces: `ProjectRepository.rename(project_id: str, name: str) -> Project`
- Produces: `ProjectRepository.delete(project_id: str) -> bool`

- [ ] **Step 1: Write failing repository tests**

```python
def test_create_list_and_rename_project(project_repository):
    project = project_repository.create("Use Case клиента")
    assert project.name == "Use Case клиента"
    assert project_repository.list()[0].id == project.id

    renamed = project_repository.rename(project.id, "Use Case банка")
    assert renamed.name == "Use Case банка"


def test_project_name_cannot_be_blank(project_repository):
    with pytest.raises(ValueError, match="Название проекта обязательно"):
        project_repository.create("   ")
```

Extend `tests/conftest.py` with a SQLite engine fixture, transactional `Session` fixture and `ProjectRepository` fixture.

- [ ] **Step 2: Run repository tests and verify failure**

Run: `cd Проекты/DocGen/app && python -m pytest tests/projects/test_repository.py -v`

Expected: FAIL because `docgen.projects.repository` does not exist.

- [ ] **Step 3: Implement the database and Project model**

```python
# src/docgen/db.py
from collections.abc import Iterator
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def build_session_factory(database_url: str) -> sessionmaker[Session]:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args)
    return sessionmaker(engine, expire_on_commit=False)
```

Implement `Project` with a UUID string primary key, required `name`, UTC `created_at`, and UTC `updated_at`. Implement the five repository methods with trimmed names, `ValueError("Название проекта обязательно")` for blank input, and `LookupError("Проект не найден")` when rename targets a missing project.

- [ ] **Step 4: Initialize the database in lifespan**

In `create_app`, build the session factory, create all tables during lifespan, store the factory on `app.state.session_factory`, and ensure `settings.data_dir.mkdir(parents=True, exist_ok=True)` runs before requests are served.

- [ ] **Step 5: Run repository and health tests**

Run: `cd Проекты/DocGen/app && python -m pytest tests/projects/test_repository.py tests/test_health.py -v`

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add Проекты/DocGen/app/src/docgen/db.py Проекты/DocGen/app/src/docgen/projects Проекты/DocGen/app/src/docgen/main.py Проекты/DocGen/app/tests
git commit -m "feat: persist DocGen projects"
```

### Task 3: Implement safe local file storage

**Files:**
- Create: `Проекты/DocGen/app/src/docgen/sources/__init__.py`
- Create: `Проекты/DocGen/app/src/docgen/sources/storage.py`
- Create: `Проекты/DocGen/app/tests/sources/test_storage.py`

**Interfaces:**
- Produces: `StoredFile(relative_path: str, size_bytes: int)`
- Produces: `LocalStorage.save(project_id: str, source_id: str, filename: str, stream: BinaryIO) -> StoredFile`
- Produces: `LocalStorage.delete(relative_path: str) -> None`
- Produces: `LocalStorage.delete_project(project_id: str) -> None`
- Produces: `LocalStorage.resolve(relative_path: str) -> Path`

- [ ] **Step 1: Write failing path-safety and lifecycle tests**

```python
from io import BytesIO
import pytest
from docgen.sources.storage import LocalStorage


def test_save_uses_ids_not_untrusted_filename(tmp_path):
    storage = LocalStorage(tmp_path)
    saved = storage.save("project-1", "source-1", "../../secret.txt", BytesIO(b"safe"))
    path = storage.resolve(saved.relative_path)
    assert path.read_bytes() == b"safe"
    assert path.is_relative_to(tmp_path.resolve())
    assert ".." not in saved.relative_path


def test_resolve_rejects_path_escape(tmp_path):
    storage = LocalStorage(tmp_path)
    with pytest.raises(ValueError, match="Недопустимый путь"):
        storage.resolve("../outside.txt")
```

- [ ] **Step 2: Run tests and verify failure**

Run: `cd Проекты/DocGen/app && python -m pytest tests/sources/test_storage.py -v`

Expected: FAIL because `LocalStorage` does not exist.

- [ ] **Step 3: Implement storage with atomic writes**

Store each upload at `<data_dir>/projects/<project_id>/sources/<source_id><lowercase-extension>`. Write to a sibling `.part` file and replace the final path only after the stream is fully copied. `resolve` must call `Path.resolve()` and reject paths outside `data_dir`. `delete` is idempotent. `delete_project` removes only `<data_dir>/projects/<validated-project-id>` after confirming the resolved path remains inside `<data_dir>/projects`.

- [ ] **Step 4: Run storage tests**

Run: `cd Проекты/DocGen/app && python -m pytest tests/sources/test_storage.py -v`

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add Проекты/DocGen/app/src/docgen/sources Проекты/DocGen/app/tests/sources
git commit -m "feat: add safe local source storage"
```

### Task 4: Persist and validate file and Confluence sources

**Files:**
- Create: `Проекты/DocGen/app/src/docgen/sources/models.py`
- Create: `Проекты/DocGen/app/src/docgen/sources/repository.py`
- Create: `Проекты/DocGen/app/src/docgen/sources/service.py`
- Modify: `Проекты/DocGen/app/src/docgen/projects/models.py`
- Create: `Проекты/DocGen/app/src/docgen/projects/service.py`
- Create: `Проекты/DocGen/app/tests/sources/test_service.py`

**Interfaces:**
- Consumes: `Project`, `LocalStorage`, `Settings.confluence_hosts`
- Produces: `SourceKind.FILE`, `SourceKind.CONFLUENCE`
- Produces: `Source(id, project_id, kind, display_name, media_type, size_bytes, storage_path, url, status, created_at)`
- Produces: `SourceService.add_file(project_id: str, filename: str, media_type: str, stream: BinaryIO) -> Source`
- Produces: `SourceService.add_confluence(project_id: str, url: str) -> Source`
- Produces: `SourceService.list(project_id: str) -> list[Source]`
- Produces: `SourceService.delete(project_id: str, source_id: str) -> None`

- [ ] **Step 1: Write failing source validation tests**

```python
def test_add_supported_file(source_service):
    source = source_service.add_file("project-1", "case.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", BytesIO(b"docx"))
    assert source.kind is SourceKind.FILE
    assert source.display_name == "case.docx"
    assert source.status == "stored"


def test_reject_unsupported_extension(source_service):
    with pytest.raises(ValueError, match="Формат файла не поддерживается"):
        source_service.add_file("project-1", "archive.zip", "application/zip", BytesIO(b"zip"))


def test_reject_non_confluence_host(source_service):
    with pytest.raises(ValueError, match="Разрешены только ссылки Confluence"):
        source_service.add_confluence("project-1", "https://example.org/page")
```

Also test that `http://wiki.example.test/page`, URLs with credentials, missing projects, and deleting another project's source are rejected.

- [ ] **Step 2: Run service tests and verify failure**

Run: `cd Проекты/DocGen/app && python -m pytest tests/sources/test_service.py -v`

Expected: FAIL because source models and service do not exist.

- [ ] **Step 3: Implement source persistence**

Create a `SourceKind(str, Enum)` and a `Source` model. File sources require `storage_path`; Confluence sources require `url`. Add `Project.sources` with cascade delete-orphan. Implement repository methods `create_file`, `create_confluence`, `list_for_project`, `get`, and `delete`.

- [ ] **Step 4: Implement SourceService validation and cleanup**

Use an immutable extension set:

```python
ALLOWED_EXTENSIONS = frozenset({".docx", ".pdf", ".txt", ".md", ".png", ".jpg", ".jpeg", ".webp"})
```

Normalize extensions to lowercase. For Confluence links require `https`, no username/password, a host in `Settings.confluence_hosts`, and a non-empty path. If DB persistence fails after a file is stored, delete the stored file. If a file source is deleted, delete its file after the DB transaction succeeds.

- [ ] **Step 5: Add project deletion service**

Create `ProjectService.delete(project_id: str) -> None`. Delete the project record transactionally, then call `LocalStorage.delete_project(project_id)`. Missing projects raise `LookupError("Проект не найден")`.

- [ ] **Step 6: Run service and repository tests**

Run: `cd Проекты/DocGen/app && python -m pytest tests/projects tests/sources -v`

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add Проекты/DocGen/app/src/docgen/projects Проекты/DocGen/app/src/docgen/sources Проекты/DocGen/app/tests
git commit -m "feat: manage DocGen sources"
```

### Task 5: Build project pages and autosave

**Files:**
- Create: `Проекты/DocGen/app/src/docgen/projects/routes.py`
- Create: `Проекты/DocGen/app/src/docgen/templates/projects/index.html`
- Create: `Проекты/DocGen/app/src/docgen/templates/projects/detail.html`
- Create: `Проекты/DocGen/app/src/docgen/templates/projects/name_form.html`
- Modify: `Проекты/DocGen/app/src/docgen/main.py`
- Create: `Проекты/DocGen/app/tests/projects/test_routes.py`

**Interfaces:**
- Consumes: `ProjectRepository` and `ProjectService`
- Produces: `GET /projects`, `POST /projects`, `GET /projects/{project_id}`, `PATCH /projects/{project_id}`, `DELETE /projects/{project_id}`
- Produces: HTMX project-name form fragment with inline validation

- [ ] **Step 1: Write failing route tests**

```python
def test_create_project_redirects_to_detail(client):
    response = client.post("/projects", data={"name": "Новый Use Case"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/projects/")


def test_blank_autosave_returns_inline_error(client, existing_project):
    response = client.patch(
        f"/projects/{existing_project.id}",
        data={"name": "   "},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 422
    assert "Название проекта обязательно" in response.text
```

Also test project listing, missing project 404, successful rename, and delete redirect.

- [ ] **Step 2: Run route tests and verify failure**

Run: `cd Проекты/DocGen/app && python -m pytest tests/projects/test_routes.py -v`

Expected: FAIL with 404 because routes are not registered.

- [ ] **Step 3: Implement repository/service dependencies and routes**

Create one session per request from `request.app.state.session_factory`. Render Russian pages. `POST /projects` creates a project and returns `303` to its detail page. `PATCH` renders `name_form.html`; return 422 with the entered value and error text for blank input. `DELETE` calls `ProjectService.delete` and returns `303` to `/projects`.

- [ ] **Step 4: Build project templates**

`index.html` contains project cards and primary button «Создать проект». `detail.html` contains the HTMX name form with `hx-patch`, `hx-trigger="change delay:500ms"`, `hx-target="this"`, and the empty sources area. Follow the colors, spacing and pill-button rules from `DESIGN.md`; do not add custom CSS.

- [ ] **Step 5: Register routes and run tests**

Include the project router from `create_app` and run:

`cd Проекты/DocGen/app && python -m pytest tests/projects -v && python -m ruff check .`

Expected: all tests PASS; Ruff reports no errors.

- [ ] **Step 6: Commit**

```bash
git add Проекты/DocGen/app/src/docgen Проекты/DocGen/app/tests/projects
git commit -m "feat: add DocGen project workspace"
```

### Task 6: Add source upload and Confluence UI

**Files:**
- Create: `Проекты/DocGen/app/src/docgen/sources/routes.py`
- Create: `Проекты/DocGen/app/src/docgen/templates/sources/list.html`
- Create: `Проекты/DocGen/app/src/docgen/templates/sources/error.html`
- Modify: `Проекты/DocGen/app/src/docgen/templates/projects/detail.html`
- Modify: `Проекты/DocGen/app/src/docgen/main.py`
- Create: `Проекты/DocGen/app/tests/sources/test_routes.py`

**Interfaces:**
- Consumes: `SourceService`
- Produces: `POST /projects/{project_id}/sources/files`
- Produces: `POST /projects/{project_id}/sources/confluence`
- Produces: `DELETE /projects/{project_id}/sources/{source_id}`
- Produces: `sources/list.html` HTMX fragment

- [ ] **Step 1: Write failing upload and link route tests**

```python
def test_upload_file_returns_updated_source_list(client, existing_project):
    response = client.post(
        f"/projects/{existing_project.id}/sources/files",
        files={"file": ("case.md", b"# Case", "text/markdown")},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert "case.md" in response.text


def test_add_confluence_link_returns_updated_source_list(client, existing_project):
    response = client.post(
        f"/projects/{existing_project.id}/sources/confluence",
        data={"url": "https://wiki.example.test/display/DOC/Page"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert "wiki.example.test" in response.text
```

Also test unsupported upload 422, invalid URL 422, missing project 404, source deletion, and cross-project deletion rejection.

- [ ] **Step 2: Run route tests and verify failure**

Run: `cd Проекты/DocGen/app && python -m pytest tests/sources/test_routes.py -v`

Expected: FAIL with 404 because source routes are not registered.

- [ ] **Step 3: Implement source routes**

Map `ValueError` to a 422 `sources/error.html` fragment and `LookupError` to 404. After each successful add or delete, render `sources/list.html` with the complete current list. Close every `UploadFile` in `finally`.

- [ ] **Step 4: Implement HTMX source controls**

Add a multipart file form with accepted extensions, a Confluence URL form, a source list with kind/status labels, and delete buttons. During requests show «Загрузка…» through `htmx-indicator`. Keep both forms and existing sources visible when one operation fails.

- [ ] **Step 5: Register routes and run all tests**

Run: `cd Проекты/DocGen/app && python -m pytest -v && python -m ruff check .`

Expected: all tests PASS; Ruff reports no errors.

- [ ] **Step 6: Commit**

```bash
git add Проекты/DocGen/app/src/docgen Проекты/DocGen/app/tests/sources
git commit -m "feat: add source management interface"
```

### Task 7: Verify the complete Stage 1 user journey and document operation

**Files:**
- Create: `Проекты/DocGen/app/tests/test_stage1_journey.py`
- Create: `Проекты/DocGen/app/README.md`

**Interfaces:**
- Consumes: all Stage 1 HTTP routes and persistence interfaces
- Produces: documented commands for setup, launch, tests and local data reset

- [ ] **Step 1: Write the end-to-end integration test**

```python
def test_stage1_journey_survives_app_restart(app_factory):
    first_client = app_factory()
    created = first_client.post("/projects", data={"name": "ТЗ платежей"}, follow_redirects=False)
    project_url = created.headers["location"]
    first_client.post(f"{project_url}/sources/files", files={"file": ("input.txt", b"data", "text/plain")})
    first_client.post(f"{project_url}/sources/confluence", data={"url": "https://wiki.example.test/display/DOC/Page"})
    first_client.close()

    second_client = app_factory()
    response = second_client.get(project_url)
    assert response.status_code == 200
    assert "ТЗ платежей" in response.text
    assert "input.txt" in response.text
    assert "wiki.example.test" in response.text
    second_client.close()
```

Make `app_factory` reuse the same temporary SQLite file and data directory across client restarts.

- [ ] **Step 2: Run the journey test and fix only integration defects**

Run: `cd Проекты/DocGen/app && python -m pytest tests/test_stage1_journey.py -v`

Expected: PASS. If it fails, change only wiring, transaction or lifespan code; keep public interfaces from Tasks 1–6 unchanged.

- [ ] **Step 3: Write operational documentation**

Document these exact workflows in `README.md`:

```bash
cd Проекты/DocGen/app
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/uvicorn docgen.main:app --reload
.venv/bin/pytest -v
.venv/bin/ruff check .
```

Document `DOCGEN_DATABASE_URL`, `DOCGEN_DATA_DIR`, and JSON-array `DOCGEN_CONFLUENCE_HOSTS`; state that `.env` values and secrets must never be committed. Explain that stage 1 stores Confluence URLs but stage 2 will retrieve and interpret their content.

- [ ] **Step 4: Run the complete verification suite**

Run: `cd Проекты/DocGen/app && python -m pytest -v && python -m ruff check .`

Expected: all tests PASS; Ruff reports no errors.

- [ ] **Step 5: Manually smoke-test the web flow**

Run: `cd Проекты/DocGen/app && python -m uvicorn docgen.main:app --port 8000`

Verify in the browser: create a project, rename it, upload each supported extension, reject `.zip`, add a permitted Confluence URL, reject an external URL, delete a source, restart the server, reopen the project, and confirm its remaining sources persist.

- [ ] **Step 6: Commit**

```bash
git add Проекты/DocGen/app/tests/test_stage1_journey.py Проекты/DocGen/app/README.md
git commit -m "test: verify DocGen stage one journey"
```
