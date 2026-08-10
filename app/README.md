# DocGen

DocGen — локальное веб-приложение для проектов с источниками, сборки документов по смысловым шаблонам и проверки уже загруженных или собранных документов. Веб-процесс ставит длительные операции в SQLite-очередь, а отдельный worker выполняет их и сохраняет документ и отчёт.

## Установка

Команды POSIX (из корня репозитория):

```bash
cd app
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Команды Windows PowerShell (из корня репозитория):

```powershell
cd app
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Запуск web и worker

Web и worker должны работать одновременно в двух терминалах. Сначала задайте стабильный и уникальный для этого worker-процесса `DOCGEN_WORKER_ID` в окружении процесса.

POSIX, терминал web:

```bash
cd app
.venv/bin/uvicorn docgen.main:app --port 8000
```

POSIX, терминал worker:

```bash
cd app
.venv/bin/python -m docgen.jobs.worker
```

Windows PowerShell, терминал web:

```powershell
cd app
.\.venv\Scripts\python.exe -m uvicorn docgen.main:app --port 8000
```

Windows PowerShell, терминал worker:

```powershell
cd app
.\.venv\Scripts\python.exe -m docgen.jobs.worker
```

Интерфейс доступен по `http://127.0.0.1:8000/projects`.

## Конфигурация

`Settings` автоматически загружает файл `.env` из корня репозитория. Переменные окружения процесса имеют приоритет. Вручную выполнять `. ./.env` для полей `Settings` не требуется. `DOCGEN_WORKER_ID` читается worker напрямую из окружения процесса и не является полем `Settings`.

Поддерживаются все следующие имена:

- `DOCGEN_DATABASE_URL` — URL базы SQLAlchemy;
- `DOCGEN_DATA_DIR` — каталог загруженных файлов;
- `DOCGEN_CONFLUENCE_HOSTS` — JSON-массив разрешённых имён хостов Confluence;
- `DOCGEN_CONFLUENCE_API_BASE` — базовый URL Confluence REST API;
- `DOCGEN_CONFLUENCE_TOKEN` — токен Confluence;
- `DOCGEN_LOCAL_TEXT_BASE_URL` — базовый URL OpenAI-совместимой текстовой модели;
- `DOCGEN_LOCAL_TEXT_MODEL` — имя текстовой модели;
- `DOCGEN_LOCAL_VISION_BASE_URL` — базовый URL OpenAI-совместимой мультимодальной модели;
- `DOCGEN_LOCAL_VISION_MODEL` — имя мультимодальной модели;
- `DOCGEN_WORKER_ID` — стабильный уникальный идентификатор worker-слота.

Секреты хранятся только в корневом `.env`. Не печатайте его содержимое, не копируйте значения в команды, код, конфигурацию или заметки и не добавляйте `.env` в Git.

Web запускается без настроенных моделей и Confluence. Worker также может собрать зависимости без этих необязательных интеграций, но не запускается без `DOCGEN_WORKER_ID`. Запуск сборки или проверки возвращает `503`, пока не настроены все четыре переменные текстовой и мультимодальной моделей. Для проекта с источником Confluence операция дополнительно возвращает `503`, пока не настроены API и токен Confluence. Задание не ставится в очередь при такой проверке зависимостей.

## Текущие ограничения

- Поддерживаются `.docx`, `.pdf`, `.txt`, `.md`, `.png`, `.jpg`, `.jpeg` и `.webp`. Для автономной проверки целевым документом могут быть только DOCX, PDF, TXT или Markdown.
- Общий объём проекта ограничен 150 расчётными страницами. Начиная со 101 страницы показывается предупреждение о возможной обработке дольше пяти минут.
- Ответ локальной модели ограничен 10 МБ, тайм-аут запроса — 120 секунд.
- Ответ Confluence ограничен 5 МБ, суммарный объём его загруженных вложений-изображений — 5 МБ, тайм-аут каждого запроса — 30 секунд.
- Отдельного лимита размера локального загружаемого файла сейчас нет: файл потоково сохраняется на диск и ограничен доступным хранилищем; лимит 150 страниц применяется при обработке.
- Для одного проекта одновременно допускается только одно задание в очереди или в работе. Отмена кооперативная между внешними этапами; после ошибки или отмены интерфейс предлагает повторный запуск.

## Тесты качества и приёмка Stage 2

Тесты используют только синтетические данные, локальные детерминированные fake-модели и `MockTransport`; они не обращаются к сети, реальным моделям или корпоративному Confluence. Замороженный банковский пример вымышлен и не содержит реальных корпоративных данных.

POSIX:

```bash
cd app
.venv/bin/python -m pytest tests/quality tests/test_stage2_journey.py -v
.venv/bin/python -m pytest -v
.venv/bin/python -m ruff check .
```

Windows PowerShell:

```powershell
cd app
.\.venv\Scripts\python.exe -m pytest tests\quality tests\test_stage2_journey.py -v
.\.venv\Scripts\python.exe -m pytest -v
.\.venv\Scripts\python.exe -m ruff check .
```

На Windows без права создания символических ссылок два теста безопасности хранилища могут завершиться до выполнения кода приложения с `WinError 1314`. Для такой среды разрешённый полный прогон исключает только:

- `tests/sources/test_storage.py::test_save_rejects_projects_directory_symlinked_outside_data_dir`;
- `tests/sources/test_storage.py::test_delete_project_rejects_symlink_to_another_project`.

## Сброс локальных данных

Остановите web и worker. Затем удалите только фактические пути базы и данных, заданные `DOCGEN_DATABASE_URL` и `DOCGEN_DATA_DIR`. Сначала разрешите и проверьте точные абсолютные пути; не используйте широкий рекурсивный путь, glob или корень workspace.

## Восстановление после ошибки очистки файлов

Удаление проекта или источника фиксирует транзакцию базы до удаления локальных файлов. Если последний шаг не удался, DocGen пишет `project_cleanup_failed` или `source_cleanup_failed` с идентификаторами и относительным путём.

После исправления доступа остановите процессы и удалите только записанный путь внутри `DOCGEN_DATA_DIR`. Для источника это точный относительный путь из журнала; для проекта — каталог `projects/<project-id>`. Убедитесь, что разрешённый путь остаётся внутри `DOCGEN_DATA_DIR`, затем перезапустите DocGen.
