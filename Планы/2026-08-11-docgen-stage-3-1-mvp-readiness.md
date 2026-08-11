# DocGen Stage 3.1 MVP Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать этапы 2–3 пригодными для реального пользовательского запуска без изменения этапа 4.

**Architecture:** Web проверяет только зависимости, нужные для постановки задания в очередь; worker лениво создаёт vision-клиент при первом блоке изображения. Каталог шаблонов объединяет пять встроенных стартовых YAML с внешним административным каталогом: внешний `id` заменяет встроенный, новый `id` добавляется. UI использует термин «Документ», а бизнес-сущность `Project` и БД остаются совместимыми.

**Tech Stack:** FastAPI, SQLAlchemy, Pydantic Settings, PyYAML, HTTPX, pytest, Ruff.

## Global Constraints

- Все AI-вызовы остаются внутри разрешённых локальных HTTP(S) endpoints.
- Нельзя выводить токены, исходники или внутренние исключения в интерфейс.
- Нельзя менять `template_id` у уже сохранённых документов; администратор может заменять или добавлять шаблоны, сохраняя необходимые ID для существующих документов.
- Для каждого исправления сначала добавляется падающий регрессионный тест.

---

### Task 1: Конфигурация и text-only задания

**Files:** `app/src/docgen/config.py`, `app/src/docgen/generation/routes.py`, `app/src/docgen/jobs/runner.py`, `app/tests/test_config.py`, `app/tests/generation/test_routes.py`, `app/tests/workflows/test_assemble.py`.

- [ ] Написать тест: корневой `.env` читается при запуске из `app`; text-only сборка с настроенной text-моделью ставится в очередь без vision.
- [ ] Запустить тест и подтвердить отказ `503` до изменения.
- [ ] Вычислять путь `.env` от репозитория; убрать безусловную проверку vision на web-слое; создавать vision-клиент только при обработке image-блока.
- [ ] Запустить целевые тесты, затем полный набор.

### Task 2: Диагностика и grounding

**Files:** `app/src/docgen/generation/routes.py`, `app/src/docgen/templates/generation/status.html`, `app/src/docgen/chat/routes.py`, `app/tests/generation/test_routes.py`, `app/tests/chat/test_routes.py`.

- [ ] Написать тесты: failed job показывает безопасное сохранённое сообщение; production chat получает блоки нормализованных источников.
- [ ] Запустить тесты и подтвердить текущую общую ошибку и document-based evidence.
- [ ] Ограничить отображаемое сообщение безопасными типами ошибок; связать chat route с нормализацией и source repository.
- [ ] Запустить целевые тесты и полный набор.

### Task 3: Пользовательский поток документа и внешний YAML-каталог

**Files:** `app/src/docgen/templates_catalog/loader.py`, `app/src/docgen/config.py`, `app/src/docgen/projects/routes.py`, `app/src/docgen/templates/projects/*.html`, `app/tests/templates_catalog/test_loader.py`, `app/tests/projects/test_routes.py`.

- [ ] Написать тесты: внешний YAML добавляется или переопределяет встроенный шаблон; имя документа предлагается по имени источника; проверка выбранного источника не требует второго выбора.
- [ ] Запустить тесты и подтвердить текущие ограничения каталога и UI.
- [ ] Добавить `DOCGEN_TEMPLATE_DIR`, объединение каталогов с приоритетом внешних YAML по `id`, copy и терминологию «Документ», автоматический target проверки.
- [ ] Запустить целевые тесты, полный набор и Ruff.

### Task 4: Документация и проверка запуска

**Files:** `app/README.md`, `app/pyproject.toml`, `app/tests/test_package_install.py`.

- [ ] Написать тест или обновить существующую проверку чистой dev-установки с `setuptools`.
- [ ] Добавить `setuptools>=75` в dev extra и описать web/worker, `.env`, text-only/vision и внешний каталог шаблонов.
- [ ] Выполнить полный `pytest`, `ruff check .`, `git diff --check`.
