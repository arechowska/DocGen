# Stage 2 final review fix report

Дата: 2026-08-10

База проверки: `6160bb164ba3038ce95bee70a345c2fd1aed2281`

Статус: **DONE**

## Итог

Все 12 разделов `final-fix-brief.md` реализованы и проверены. Дополнительно во внутреннем независимом review были найдены и устранены пять связанных Important edge cases: terminal FK на удалённый target и silent retry как CURRENT, recovery lease только при старте worker, multipart spool до сервисного лимита, подмена отчёта более новым отчётом той же document revision и fragment-only ошибки обычных HTML-форм. После исправлений независимый reviewer не нашёл Critical/Important замечаний.

## Реализация по разделам brief

### 1. Полное покрытие правил

- В `CheckReport` добавлен immutable tuple `passed_rule_ids`.
- Проверка отчёта требует точного непересекающегося разбиения всех правил на passed, failed findings и unchecked.
- Отклоняются пустое/неполное покрытие, неизвестные ID и дубли во всех трёх группах, включая повторные `finding.rule_id`.
- UI отдельно и честно показывает passed, failed и unchecked; пустой отчёт не утверждает, что все правила проверены. У каждого finding виден `rule_id`.

### 2. Ресурсы установленного пакета

- `pyproject.toml` включает Jinja templates, semantic YAML и локальные static assets в wheel.
- Добавлен isolated wheel → clean target install → source-CWD-independent TestClient smoke.
- Smoke импортирует именно установленный пакет, рендерит `/projects`, project/generation page и локальные static assets при запрете внешних socket-соединений.

### 3. Сериализованный SQLite bootstrap

- Web и worker используют единый `initialize_database()`.
- Для SQLite функция берёт `BEGIN IMMEDIATE` до schema inspection/create/migrations, выполняет один commit и rollback при ошибке.
- Добавлен детерминированный concurrent first-start test с двумя engine на свежей БД; DDL вне writer lock не наблюдается.

### 4. SQLite referential integrity

- `PRAGMA foreign_keys=ON` устанавливается на каждом application SQLite connection.
- Fresh jobs schema использует `target_source_id ... ON DELETE RESTRICT` и сохраняет явный `check_target_kind`.
- Миграция fail-ит queued/running dangling targets безопасным публичным сообщением до очистки FK; terminal rows очищаются без reinterpretation intent.
- Enqueue/delete сериализованы writer lock и повторно проверяют project/target/active jobs.
- Удаление источника очищает только terminal job references атомарно, сохраняя `check_target_kind=source`; такой job больше не предлагает retry как CURRENT.
- Добавлены FK check, migration и две детерминированные enqueue/delete race-регрессии.

### 5. Worker lease и process identity

- Stable `worker_id` дополнен уникальным UUID instance token и configurable lease (`30` секунд по умолчанию).
- Claim хранит slot, token и expiry; progress, cancellation checkpoints, warnings и отдельный background heartbeat продлевают lease.
- Owned transitions проверяют slot и token. Recovery затрагивает только expired claims stable slot.
- Recovery выполняется на каждом poll-цикле: replacement, стартовавший при живом predecessor, подберёт истёкший claim без собственного рестарта.
- Lost transition/deleted job не завершает worker loop.

### 6. Trusted outbound origins

- Добавлен `DOCGEN_TRUSTED_INTEGRATION_HOSTS` как JSON-массив; default: `localhost`, `127.0.0.1`, `::1`.
- AI и Confluence API endpoints принимают только HTTP(S), без credentials, fragments и backslash, с точным host из allowlist.
- Confluence page host отдельно проверяется через `DOCGEN_CONFLUENCE_HOSTS`; bearer token отправляется только после проверки API origin.
- Ошибки конфигурации публичные и безопасные на русском языке.

### 7. Видимые normalization warnings

- Job хранит deduplicated warnings JSON; owned update одновременно обновляет lease.
- Warnings видны в running и terminal status/result/report, сохраняются после polling/restart.
- Покрыты exact warning для 101–150 страниц и extractor warning пустой PDF-страницы.
- Stage 2 journey создаёт реальный 101+ virtual-page Markdown input и подтверждает warning до и после app restart.

### 8. Offline UI и package security

- CDN удалены. HTMX 2.0.8 vendored локально; Tailwind CSS 3.4.17 сгенерирован и закоммичен локально.
- Static files обслуживаются приложением; CSP запрещает внешние scripts/styles, object/embed и framing.
- Локальный CSS явно реализует `.htmx-indicator`, поэтому CSP не требует inline style injection.
- Формы имеют `action`/`method`; обычный браузер поддерживает create/upload/start/cancel/retry/delete.
- Ошибки non-HTMX source forms возвращают полный project page с ошибкой и рабочими формами, а HTMX сохраняет targeted fragment behavior.

### 9. Claim-level grounding и required structure

- Evidence содержит exact nonempty `quote` вместе с normalized block ID и locator.
- Grounding получает mapping normalized blocks и проверяет block ID, locator и наличие quote в тексте referenced block.
- Каждый non-gap node обязан иметь evidence; gap остаётся пустым и имеет `missing-source-data`.
- Generated document обязан быть непустым и содержать уникальные известные top-level `section_id` для всех required template sections.
- Prompts и deterministic quality fakes обновлены под stable section IDs и exact quotes.
- Standalone target mapping использует provenance source identity, а не предположение о формате block ID.

### 10. Локальные resource budgets

- Добавлены и документированы defaults:
  - `DOCGEN_MAX_UPLOAD_BYTES=52428800`;
  - `DOCGEN_MAX_PROJECT_STORAGE_BYTES=524288000`;
  - `DOCGEN_MAX_IMAGE_PIXELS=40000000`;
  - `DOCGEN_MAX_ARCHIVE_ENTRIES=10000`;
  - `DOCGEN_MAX_ARCHIVE_UNCOMPRESSED_BYTES=209715200`;
  - `DOCGEN_MAX_MODEL_REQUEST_BYTES=20971520`;
  - `DOCGEN_MAX_JOB_SECONDS=300`.
- Custom streaming multipart parser считает file bytes до записи следующего chunk в `SpooledTemporaryFile`; chunked request без `Content-Length` не может разрастись выше лимита. Exact boundary принимается.
- Destination storage повторно ограничивает streaming write и удаляет `.part` при отказе; project aggregate повторно проверяется под writer lock.
- PDF page count, DOCX entry/expanded size, image dimensions/pixels и text/file bytes проверяются до дорогого полного чтения/декодирования.
- Serialized UTF-8 JSON payload модели проверяется до HTTP.
- Job deadline проверяется на cooperative stage/call gates.

### 11. Согласованность document/report revisions

- Artifact хранит monotonic `document_revision`, bound `report_revision` и отдельный monotonic `report_generation` как identity отчёта.
- Замена документа увеличивает revision и атомарно очищает отчёт.
- Current-document report сохраняется CAS по ожидаемой document revision.
- Standalone check публикует документ и отчёт атомарно на одной revision.
- Job хранит result document revision, report revision и report generation; старый check job не может показать более новый отчёт того же документа.
- Journey подтверждает invalidation R(A) после assemble B, retry и restart.

### 12. Локализованные DOCX styles

- Heading распознаётся по stable built-in `style_id` Heading1–Heading9 и inherited outline level.
- Lists распознаются по stable list style IDs и inherited numbering, а не English display name.
- Добавлены Cyrillic-renamed heading/list fixtures.

## Миграции

- Bootstrap добавляет artifact document/report revisions и report generation.
- Legacy report без доказуемой document binding очищается.
- Jobs migration добавляет explicit target kind, instance token, lease expiry, warnings, result revisions/generation и перестраивает таблицу с restrictive target FK.
- Dangling project artifacts/jobs/sources ремонтируются до `PRAGMA foreign_key_check`.
- Миграция идемпотентна и выполняется под тем же serialized transaction, что create schema.

## RED → GREEN evidence

- Semantic coverage/grounding/revision wave: RED `15 failed, 28 passed` → GREEN `43 passed`.
- Trusted origins: RED `8 failed` → AI/Confluence GREEN `39 passed`; config defaults/JSON/positive validation GREEN `10 passed`.
- Resource extraction: RED `6 failed` → GREEN `29 passed`; storage/source boundaries and cleanup GREEN `34 passed, 2 deselected`; model request boundary GREEN `14 passed`.
- Job deadline: initial RED `TypeError` → focused GREEN.
- Offline UI: RED `4 failed` → GREEN `4 passed`.
- Localized DOCX: RED `3 failed` → GREEN `9 passed`.
- Delete conflict routes: RED `2 failed` → GREEN `2 passed`; deterministic delete/enqueue races GREEN `2 passed`.
- Heartbeat overlap initially allowed recovery of a live claim; focused heartbeat GREEN after lease ownership fix.
- Final audit regressions:
  - hidden HTMX indicator under CSP: RED `1 failed` → GREEN `1 passed`;
  - duplicate finding coverage: RED one parametrized case → check suite GREEN `16 passed`;
  - real source provenance mapping: RED `1 failed` → check suite GREEN `17 passed`;
  - warning journey: RED `1 failed` → GREEN `1 passed`;
  - terminal target FK/intent: RED `2 failed` → GREEN `2 passed`;
  - overlap-then-expire periodic recovery: RED `1 failed` → GREEN `2 passed` focused worker checks;
  - chunked pre-spool limit: RED spool reached 5 bytes with limit 4 → GREEN `6 passed` focused upload checks and recorded spool `<=4`;
  - same-document report replacement: RED `1 failed` → GREEN with `report_generation`;
  - non-HTMX error fallback: RED bare fragment → source routes GREEN `15 passed`.

## Vendored assets

- `htmx.org@2.0.8`, Zero-Clause BSD license in `HTMX-LICENSE.txt`.
  - SHA-256: `22283ef68cb7545914f0a88a1bdedc7256a703d1d580c1d255217d0a50d31313`.
- `tailwindcss@3.4.17`, MIT license in `TAILWIND-LICENSE.txt`.
  - Generated CSS SHA-256: `81a01a080cc63ff46474bf0967d58895ff3a457542aec8fc541e0907ddf1c746`.
  - Rebuild command is pinned in `static/vendor/VERSIONS.md`.
- Template scan for `cdn.tailwind`, `unpkg`, `hx-on` and inline ` style=` returned no matches.

## Browser и package verification

- Browser CLI: `npx --yes agent-browser` version `0.27.0`.
- Local browser flow: create project → upload Markdown → assemble queued → cancel → retry queued.
- Browser console/page errors: none.
- External resource entries: `[]`; all resources came from `http://127.0.0.1:8765/`.
- CSP-local indicator computed opacity when idle: `0`.
- Temporary browser server and browser session were stopped after verification.
- Isolated wheel test: `1 passed in 5.81s`; external sockets blocked except local process/self-pipe needs.

## Финальная verification

```text
python -m pytest -q \
  --deselect tests/sources/test_storage.py::test_save_rejects_projects_directory_symlinked_outside_data_dir \
  --deselect tests/sources/test_storage.py::test_delete_project_rejects_symlink_to_another_project
312 passed, 2 deselected in 22.05s

python -m ruff check .
All checks passed!

git diff --check
exit 0 (только информационные LF→CRLF notices рабочей копии)

python -m pytest tests/test_package_install.py -q
1 passed in 5.81s
```

Разрешённые исключения — только два Windows symlink test из brief; других deselect/skip не добавлялось.

## Ограничения и остаточные риски

- Critical/Important findings после независимого review отсутствуют.
- `DOCGEN_MAX_JOB_SECONDS` намеренно кооперативный: он проверяется до/после стадий и внешних вызовов, но не выполняет принудительное убийство уже исполняющегося библиотечного вызова. HTTP transport сохраняет собственный timeout.
- Pinned Tailwind build сообщил только informational warning об outdated Browserslist dataset; committed CSS детерминирован и hash зафиксирован.
- `.env` не читался, не печатался и не изменялся.
