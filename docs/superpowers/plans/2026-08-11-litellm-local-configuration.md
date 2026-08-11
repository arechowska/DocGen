# LiteLLM Local Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Подключить текстовую модель DocGen к корпоративному LiteLLM через локальный корневой `.env`, не раскрывая и не коммитя API-ключ.

**Architecture:** Путь к `.env` вычисляется от расположения `docgen/config.py`, поэтому не зависит от рабочего каталога web или worker. Параметры интеграции хранятся только в игнорируемом `.env`; код и тесты не содержат значений секрета.

**Tech Stack:** Python 3.12+, pydantic-settings, pytest, FastAPI worker/web processes, OpenAI-compatible HTTP adapter.

## Global Constraints

- API-ключ хранится только в корневом `.env` и никогда не выводится в терминал, тесты, документацию или Git diff.
- Настраивается только текстовая модель; vision и Confluence не изменяются.
- Корпоративный AI host должен быть явно добавлен в `DOCGEN_TRUSTED_INTEGRATION_HOSTS`.
- Проверка модели не должна печатать содержимое запроса или ответа.

---

### Task 1: Независимая от CWD загрузка корневого `.env`

**Files:**
- Modify: `app/src/docgen/config.py`
- Test: `app/tests/test_config.py`
- Create locally, ignored: `.env`

**Interfaces:**
- Consumes: `Settings` на базе `pydantic_settings.BaseSettings`.
- Produces: абсолютный `REPOSITORY_ROOT_ENV: Path`, используемый `Settings.model_config["env_file"]`.

- [ ] **Step 1: Write the failing test**

Добавить тест, который меняет CWD на `app`, затем проверяет, что путь `Settings.model_config["env_file"]` абсолютный и равен `.env` в корне репозитория:

```python
def test_default_env_file_is_repository_root(monkeypatch: pytest.MonkeyPatch) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    monkeypatch.chdir(repository_root / "app")

    env_file = Path(Settings.model_config["env_file"])

    assert env_file.is_absolute()
    assert env_file == repository_root / ".env"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `app/.venv/Scripts/python.exe -m pytest app/tests/test_config.py::test_default_env_file_is_repository_root -v`

Expected: FAIL, потому что текущий путь равен относительному `Path(".env")`.

- [ ] **Step 3: Write minimal implementation**

В `app/src/docgen/config.py` вычислить путь относительно файла модуля:

```python
REPOSITORY_ROOT_ENV = Path(__file__).resolve().parents[3] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DOCGEN_",
        env_file=REPOSITORY_ROOT_ENV,
    )
```

- [ ] **Step 4: Run focused tests**

Run: `app/.venv/Scripts/python.exe -m pytest app/tests/test_config.py -v`

Expected: все тесты проходят.

- [ ] **Step 5: Save local integration settings**

Создать игнорируемый `.env` в корне репозитория с четырьмя переменными из подтверждённых пользователем параметров. Значение API-ключа взять непосредственно из текущего запроса пользователя и передать операции записи без повторного показа:

```dotenv
DOCGEN_LOCAL_TEXT_BASE_URL=https://ai.colvir.com
DOCGEN_LOCAL_TEXT_MODEL=qwen3.6-35b-256k
DOCGEN_TRUSTED_INTEGRATION_HOSTS=["ai.colvir.com"]
```

В тот же файл записать `DOCGEN_LOCAL_TEXT_API_KEY`, не включая его значение в план, команды проверки или вывод инструментов.

Не добавлять `.env` через `git add` и не выводить его содержимое.

- [ ] **Step 6: Verify configuration without exposing values**

Run a Python assertion that instantiates `Settings`, verifies the URL/model/key are present, calls `build_text_model(settings)`, and prints only `CONFIG_OK`. Then run `git check-ignore -v .env` and `git status --short`.

Expected: `CONFIG_OK`; `.env` ignored; Git status contains only code/test/plan changes.

- [ ] **Step 7: Restart and smoke-test**

Stop only DocGen web/worker processes whose command lines point into this repository. Restart worker with `DOCGEN_WORKER_ID=local-worker` and web on `127.0.0.1:8000`, then verify `GET /projects` returns HTTP 200. Send one minimal structured adapter request with stdout/stderr redirected to local ignored logs and report only success/failure and HTTP-safe error class, never payloads or response bodies.

- [ ] **Step 8: Run regression checks and commit safe files**

Run:

```powershell
app\.venv\Scripts\python.exe -m pytest app\tests\test_config.py app\tests\ai\test_client.py -q
app\.venv\Scripts\python.exe -m ruff check app\src\docgen\config.py app\tests\test_config.py
git diff --check
```

Expected: tests and Ruff pass; diff check exits 0. Commit only `app/src/docgen/config.py`, `app/tests/test_config.py`, the design, and this plan. Confirm `.env` is absent from `git ls-files`.
