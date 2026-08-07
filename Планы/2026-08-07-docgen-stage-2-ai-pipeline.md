# DocGen Stage 2 AI Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить извлечение и нормализацию источников, локальные AI-модели, смысловые шаблоны и два фоновых режима — сборку документа и проверку по шаблону.

**Architecture:** Каждый источник преобразуется адаптером в нормализованные блоки с внутренней привязкой к происхождению. Однопроцессный DB-backed worker забирает задания из SQLite, вызывает локальные OpenAI-совместимые текстовую и мультимодальную модели, сохраняет структурированный рабочий документ или отчёт проверки и обновляет прогресс. Маршруты FastAPI только создают задания и показывают их состояние, поэтому длительная обработка не блокирует HTTP-запросы.

**Tech Stack:** стек этапа 1 плюс PyMuPDF, python-docx, Pillow, markdown-it-py, Beautiful Soup 4, PyYAML, HTTPX, Pydantic 2 и pytest.

## Global Constraints

- Все AI-вызовы идут только к локальным endpoints внутри корпоративного контура.
- Генерация использует только извлечённые источники; общие знания модели запрещены системным контрактом и проверкой происхождения.
- Поддерживаемый максимум задания — 150 страниц; для 101–150 страниц показывается предупреждение о возможном превышении пяти минут.
- Задания из эталонного набора до 100 страниц имеют целевое время не более пяти минут.
- Язык источников, шаблонов, промптов и результатов — русский.
- Один запуск создаёт один рабочий документ или один отчёт проверки.
- Смысловые шаблоны задаются YAML-конфигурациями администратора; пользователь выбирает, но не редактирует их.
- Источники с ошибкой не удаляются; подтверждённый частичный результат не обозначается как готовый.
- История версий, чат, ручной редактор и экспорт остаются за границами этапа 2.

## File Structure

```text
Проекты/DocGen/app/
├── pyproject.toml
├── src/docgen/
│   ├── config.py
│   ├── main.py
│   ├── documents/
│   │   ├── schemas.py             # Pydantic-модель рабочего документа
│   │   ├── models.py              # сохранённый результат и отчёт
│   │   └── repository.py
│   ├── extraction/
│   │   ├── schemas.py             # NormalizedBlock и Provenance
│   │   ├── registry.py            # выбор extractor по источнику
│   │   ├── text.py
│   │   ├── docx.py
│   │   ├── pdf.py
│   │   ├── image.py
│   │   └── confluence.py
│   ├── templates_catalog/
│   │   ├── schemas.py
│   │   ├── loader.py
│   │   └── semantic/*.yaml
│   ├── ai/
│   │   ├── client.py              # локальный OpenAI-совместимый клиент
│   │   ├── prompts.py
│   │   └── grounding.py
│   ├── jobs/
│   │   ├── models.py
│   │   ├── repository.py
│   │   ├── runner.py
│   │   └── worker.py
│   ├── workflows/
│   │   ├── normalize.py
│   │   ├── assemble.py
│   │   └── check.py
│   ├── generation/routes.py
│   └── templates/generation/{setup,status,result,report}.html
└── tests/
    ├── extraction/
    ├── templates_catalog/
    ├── ai/
    ├── jobs/
    ├── workflows/
    └── generation/
```

---

### Task 1: Define normalized sources and structured documents

**Files:**
- Modify: `Проекты/DocGen/app/pyproject.toml`
- Create: `Проекты/DocGen/app/src/docgen/extraction/schemas.py`
- Create: `Проекты/DocGen/app/src/docgen/documents/schemas.py`
- Create: `Проекты/DocGen/app/src/docgen/documents/models.py`
- Create: `Проекты/DocGen/app/src/docgen/documents/repository.py`
- Modify: `Проекты/DocGen/app/src/docgen/projects/models.py`
- Create: `Проекты/DocGen/app/tests/documents/test_schemas.py`
- Create: `Проекты/DocGen/app/tests/documents/test_repository.py`

**Interfaces:**
- Produces: `Provenance(source_id: str, locator: str)`
- Produces: `NormalizedBlock(id: str, kind: BlockKind, text: str, data: dict, provenance: list[Provenance], confidence: float)`
- Produces: `DocumentNode(id: str, kind: NodeKind, text: str | None, data: dict, children: list[DocumentNode], provenance: list[Provenance], flags: list[str])`
- Produces: `WorkingDocument(title: str, template_id: str, nodes: list[DocumentNode])`
- Produces: `CheckFinding(code, severity, confidence, message, node_id, rule_id)` and `CheckReport(template_id, findings, unchecked_rules)`
- Produces: `DocumentRepository.save_document(project_id, document)`, `get_document(project_id)`, `save_report(project_id, report)`, `get_report(project_id)`

- [ ] **Step 1: Add parsing dependencies and write schema tests**

Add dependencies: `pymupdf>=1.25,<2`, `python-docx>=1.1,<2`, `pillow>=11,<12`, `markdown-it-py>=3,<4`, `beautifulsoup4>=4.12,<5`, `pyyaml>=6,<7`.

```python
def test_document_round_trip_preserves_provenance():
    document = WorkingDocument(
        title="Use Case",
        template_id="use-case",
        nodes=[DocumentNode(id="n1", kind=NodeKind.PARAGRAPH, text="Оплата", provenance=[Provenance(source_id="s1", locator="page:2")])],
    )
    assert WorkingDocument.model_validate_json(document.model_dump_json()) == document


def test_confidence_is_bounded():
    with pytest.raises(ValidationError):
        NormalizedBlock(id="b1", kind=BlockKind.TEXT, text="x", confidence=1.1)
```

- [ ] **Step 2: Run tests and verify failure**

Run: `cd Проекты/DocGen/app && python -m pytest tests/documents -v`

Expected: FAIL because document schemas do not exist.

- [ ] **Step 3: Implement immutable Pydantic schemas**

Use `ConfigDict(frozen=True)`, UUID-string IDs, `confidence: float = Field(ge=0, le=1)`, enums for kinds, and `Field(default_factory=list/dict)` for containers. Node kinds: `heading`, `paragraph`, `list`, `table`, `image`, `gap`. Severity values: `error`, `warning`, `info`.

```python
class Provenance(BaseModel):
    model_config = ConfigDict(frozen=True)
    source_id: str
    locator: str


class WorkingDocument(BaseModel):
    title: str
    template_id: str
    nodes: list[DocumentNode] = Field(default_factory=list)
```

- [ ] **Step 4: Persist one current document and report per project**

Create `ProjectArtifact` with `project_id` unique FK, `document_json`, `report_json`, and `updated_at`. Repository writes JSON with `model_dump_json()` and replaces only the current value; no history table is created.

- [ ] **Step 5: Run document tests and commit**

Run: `cd Проекты/DocGen/app && python -m pytest tests/documents -v`

Expected: PASS.

```bash
git add Проекты/DocGen/app
git commit -m "feat: define structured DocGen documents"
```

### Task 2: Extract supported local files

**Files:**
- Create: `Проекты/DocGen/app/src/docgen/extraction/registry.py`
- Create: `Проекты/DocGen/app/src/docgen/extraction/text.py`
- Create: `Проекты/DocGen/app/src/docgen/extraction/docx.py`
- Create: `Проекты/DocGen/app/src/docgen/extraction/pdf.py`
- Create: `Проекты/DocGen/app/src/docgen/extraction/image.py`
- Create: `Проекты/DocGen/app/tests/extraction/test_registry.py`
- Create: `Проекты/DocGen/app/tests/extraction/test_text.py`
- Create: `Проекты/DocGen/app/tests/extraction/test_docx.py`
- Create: `Проекты/DocGen/app/tests/extraction/test_pdf.py`

**Interfaces:**
- Produces: `ExtractionResult(blocks: list[NormalizedBlock], page_units: int, warnings: list[str])`
- Produces: protocol `Extractor.extract(source: Source, path: Path) -> ExtractionResult`
- Produces: `ExtractorRegistry.for_source(source: Source) -> Extractor`

- [ ] **Step 1: Write failing extraction contract tests**

```python
def test_txt_has_stable_line_locators(txt_source, tmp_path):
    path = tmp_path / "input.txt"
    path.write_text("Первая строка\nВторая строка", encoding="utf-8")
    result = TextExtractor().extract(txt_source, path)
    assert [b.provenance[0].locator for b in result.blocks] == ["lines:1-1", "lines:2-2"]


def test_registry_selects_pdf_extractor(pdf_source):
    assert isinstance(ExtractorRegistry.default().for_source(pdf_source), PdfExtractor)
```

Generate minimal DOCX/PDF fixtures inside tests with `python-docx` and PyMuPDF; do not commit opaque binary fixtures.

- [ ] **Step 2: Verify the tests fail**

Run: `cd Проекты/DocGen/app && python -m pytest tests/extraction -v`

Expected: FAIL because extractors do not exist.

- [ ] **Step 3: Implement TXT and Markdown extraction**

Decode UTF-8 with BOM support; return a clear `ExtractionError("Не удалось прочитать текстовый файл в UTF-8")` on failure. TXT paragraphs use line-range locators. Markdown uses `markdown-it-py` tokens and preserves heading/list/table intent in `BlockKind` and `data`.

- [ ] **Step 4: Implement DOCX and PDF extraction**

DOCX walks paragraphs and tables in document order, derives heading/list/table kinds, and uses locators `paragraph:<index>` and `table:<index>`. PDF uses PyMuPDF blocks per page, `locator="page:<n>/block:<n>"`, and `page_units=page_count`. Empty PDF pages add warning `Страница <n> не содержит извлекаемого текста`.

- [ ] **Step 5: Register raster images for multimodal extraction**

`ImageExtractor` validates the image with Pillow, returns one `BlockKind.IMAGE` block containing width, height and local storage path, locator `image:1`, and `page_units=1`. It does not call the model; Task 6 enriches this block.

- [ ] **Step 6: Run tests and commit**

Run: `cd Проекты/DocGen/app && python -m pytest tests/extraction -v && python -m ruff check .`

Expected: PASS; no Ruff errors.

```bash
git add Проекты/DocGen/app/src/docgen/extraction Проекты/DocGen/app/tests/extraction Проекты/DocGen/app/pyproject.toml
git commit -m "feat: extract DocGen file sources"
```

### Task 3: Retrieve and normalize Confluence pages

**Files:**
- Modify: `Проекты/DocGen/app/src/docgen/config.py`
- Create: `Проекты/DocGen/app/src/docgen/extraction/confluence.py`
- Create: `Проекты/DocGen/app/tests/extraction/test_confluence.py`

**Interfaces:**
- Consumes: `Settings.confluence_api_base`, `Settings.confluence_token`, allowed hosts
- Produces: `ConfluenceClient.fetch(url: str) -> ExtractionResult`

- [ ] **Step 1: Write failing client tests with HTTPX MockTransport**

```python
def test_fetch_confluence_page_maps_headings_tables_and_images(mock_transport):
    client = ConfluenceClient(api_base="https://wiki.example.test/rest/api", token="secret", transport=mock_transport)
    result = client.fetch("https://wiki.example.test/pages/viewpage.action?pageId=42")
    assert result.blocks[0].kind is BlockKind.HEADING
    assert result.blocks[0].provenance[0].locator == "confluence:42#heading-1"


def test_unauthorized_is_user_safe(mock_401_transport):
    with pytest.raises(ExtractionError, match="Нет доступа к странице Confluence"):
        ConfluenceClient(
            api_base="https://wiki.example.test/rest/api",
            token="secret",
            transport=mock_401_transport,
        ).fetch("https://wiki.example.test/pages/viewpage.action?pageId=42")
```

- [ ] **Step 2: Run the tests and verify failure**

Run: `cd Проекты/DocGen/app && python -m pytest tests/extraction/test_confluence.py -v`

Expected: FAIL because `ConfluenceClient` does not exist.

- [ ] **Step 3: Implement REST retrieval and HTML normalization**

Extract a numeric page ID from supported Confluence URLs, call `/content/{id}?expand=body.storage,version`, send `Authorization: Bearer <token>`, use a 30-second timeout, and never log the token. Parse storage HTML with Beautiful Soup into heading, paragraph, list, table and image blocks. Use stable locators `confluence:<page-id>#<element-index>` and estimate virtual pages from normalized character count using the same calculator as Task 4.

- [ ] **Step 4: Add secret-backed configuration**

Add optional `confluence_api_base: AnyHttpUrl | None` and `confluence_token: SecretStr | None`. Raise `ExtractionError("Интеграция Confluence не настроена")` before an HTTP call if either is missing. Values are supplied only through `DOCGEN_*` environment variables or root `.env`; tests use direct settings and never print secrets.

- [ ] **Step 5: Run tests and commit**

Run: `cd Проекты/DocGen/app && python -m pytest tests/extraction/test_confluence.py -v`

Expected: PASS.

```bash
git add Проекты/DocGen/app/src/docgen/config.py Проекты/DocGen/app/src/docgen/extraction/confluence.py Проекты/DocGen/app/tests/extraction/test_confluence.py
git commit -m "feat: retrieve Confluence sources"
```

### Task 4: Enforce the 150-page limit

**Files:**
- Create: `Проекты/DocGen/app/src/docgen/workflows/normalize.py`
- Create: `Проекты/DocGen/app/tests/workflows/test_normalize.py`

**Interfaces:**
- Produces: `VirtualPageCalculator.from_text(text: str) -> int`
- Produces: `NormalizedProject(blocks, total_pages, warnings)`
- Produces: `NormalizationWorkflow.run(project_id: str) -> NormalizedProject`

- [ ] **Step 1: Write boundary tests**

```python
def test_page_limit_accepts_150_and_rejects_151(workflow, sources):
    sources.results = [ExtractionResult(blocks=[], page_units=150, warnings=[])]
    assert workflow.run("p1").total_pages == 150
    sources.results = [ExtractionResult(blocks=[], page_units=151, warnings=[])]
    with pytest.raises(PageLimitExceeded, match="Максимальный объём — 150 страниц"):
        workflow.run("p1")


def test_virtual_page_count_is_deterministic():
    assert VirtualPageCalculator(chars_per_page=1800).from_text("а" * 1801) == 2
```

- [ ] **Step 2: Run and verify failure**

Run: `cd Проекты/DocGen/app && python -m pytest tests/workflows/test_normalize.py -v`

Expected: FAIL because normalization workflow does not exist.

- [ ] **Step 3: Implement deterministic aggregation**

Use `ceil(max(1, len(non_whitespace_text)) / 1800)` for non-paginated text; images count as one page. Extract sources in creation order, prefix block IDs with source ID, sum `page_units`, stop before model calls when the sum exceeds 150, and add warning `Обработка может занять более пяти минут` when total is 101–150.

- [ ] **Step 4: Run tests and commit**

Run: `cd Проекты/DocGen/app && python -m pytest tests/workflows/test_normalize.py -v`

Expected: PASS.

```bash
git add Проекты/DocGen/app/src/docgen/workflows Проекты/DocGen/app/tests/workflows
git commit -m "feat: normalize projects with page limits"
```

### Task 5: Load and validate semantic templates

**Files:**
- Create: `Проекты/DocGen/app/src/docgen/templates_catalog/schemas.py`
- Create: `Проекты/DocGen/app/src/docgen/templates_catalog/loader.py`
- Create: `Проекты/DocGen/app/src/docgen/templates_catalog/semantic/faq.yaml`
- Create: `Проекты/DocGen/app/src/docgen/templates_catalog/semantic/use-case.yaml`
- Create: `Проекты/DocGen/app/src/docgen/templates_catalog/semantic/technical-spec.yaml`
- Create: `Проекты/DocGen/app/src/docgen/templates_catalog/semantic/release-notes.yaml`
- Create: `Проекты/DocGen/app/src/docgen/templates_catalog/semantic/api-docs.yaml`
- Create: `Проекты/DocGen/app/tests/templates_catalog/test_loader.py`

**Interfaces:**
- Produces: `SemanticTemplate(id, name, version, sections, rules, style_rules)`
- Produces: `TemplateCatalog.list() -> list[SemanticTemplate]`
- Produces: `TemplateCatalog.get(template_id: str) -> SemanticTemplate`

- [ ] **Step 1: Write strict loader tests**

```python
def test_catalog_loads_five_templates(catalog):
    assert {t.id for t in catalog.list()} == {"faq", "use-case", "technical-spec", "release-notes", "api-docs"}


def test_duplicate_rule_ids_are_rejected(tmp_path):
    write_yaml(tmp_path / "bad.yaml", VALID_TEMPLATE_WITH_DUPLICATE_RULE)
    with pytest.raises(TemplateConfigurationError, match="Повторяющийся идентификатор правила"):
        TemplateCatalog(tmp_path).list()
```

- [ ] **Step 2: Run and verify failure**

Run: `cd Проекты/DocGen/app && python -m pytest tests/templates_catalog -v`

Expected: FAIL because catalog code does not exist.

- [ ] **Step 3: Implement strict YAML schemas and loader**

Forbid unknown keys with `ConfigDict(extra="forbid")`. Each section has `id`, Russian `title`, `required`, and `description`; each rule has unique `id`, `dimension` (`structure`, `completeness`, `terminology`, `contradiction`, `style`), `severity`, and a concrete Russian instruction. Sort files by name for deterministic output and reject duplicate template IDs.

- [ ] **Step 4: Add five minimal but valid templates**

Each YAML must contain at least three required sections and at least one rule for every checking dimension. Do not use placeholders; write domain-neutral rules that can be evaluated from supplied sources, such as checking that each Use Case has actors, preconditions, main flow and result.

- [ ] **Step 5: Run tests and commit**

Run: `cd Проекты/DocGen/app && python -m pytest tests/templates_catalog -v`

Expected: PASS.

```bash
git add Проекты/DocGen/app/src/docgen/templates_catalog Проекты/DocGen/app/tests/templates_catalog
git commit -m "feat: add semantic template catalog"
```

### Task 6: Add local text and multimodal model adapters

**Files:**
- Modify: `Проекты/DocGen/app/src/docgen/config.py`
- Create: `Проекты/DocGen/app/src/docgen/ai/client.py`
- Create: `Проекты/DocGen/app/src/docgen/ai/prompts.py`
- Create: `Проекты/DocGen/app/src/docgen/ai/grounding.py`
- Create: `Проекты/DocGen/app/tests/ai/test_client.py`
- Create: `Проекты/DocGen/app/tests/ai/test_grounding.py`

**Interfaces:**
- Produces: protocol `TextModel.generate_json(system: str, user: str, schema: type[T]) -> T`
- Produces: protocol `VisionModel.describe(image: bytes, media_type: str) -> VisionDescription`
- Produces: `OpenAICompatibleTextModel` and `OpenAICompatibleVisionModel`
- Produces: `GroundingValidator.validate(document, source_block_ids) -> list[str]`

- [ ] **Step 1: Write contract tests with MockTransport**

```python
def test_text_model_parses_structured_response(mock_transport):
    model = OpenAICompatibleTextModel(base_url=LOCAL_URL, model="local-text", transport=mock_transport)
    result = model.generate_json("system", "user", WorkingDocument)
    assert result.template_id == "use-case"


def test_grounding_rejects_unknown_block_reference():
    errors = GroundingValidator().validate(document_with_source("missing"), {"known"})
    assert errors == ["Узел n1 ссылается на неизвестный блок missing"]
```

- [ ] **Step 2: Run and verify failure**

Run: `cd Проекты/DocGen/app && python -m pytest tests/ai -v`

Expected: FAIL because AI adapters do not exist.

- [ ] **Step 3: Implement OpenAI-compatible local clients**

POST to `<base_url>/chat/completions`, require JSON response format, use configured model name and 120-second timeout, parse content into the requested Pydantic schema, and map network, HTTP and schema failures to `ModelError` with Russian user-safe messages. Never log prompts, source content, tokens or response bodies.

- [ ] **Step 4: Implement grounding contract and prompts**

All generated content nodes must include at least one known normalized block ID unless `kind == gap`. Gap nodes must have no invented content and carry flag `missing-source-data`. System prompts state: write in Russian; use only numbered source blocks; never fill missing facts; emit a gap node; separate low-confidence findings.

- [ ] **Step 5: Add model configuration**

Add required-at-runtime `local_text_base_url`, `local_text_model`, `local_vision_base_url`, and `local_vision_model`. App startup remains possible without them, but job creation returns `503` with `Локальные модели не настроены` until all selected workflow dependencies are configured.

- [ ] **Step 6: Run tests and commit**

Run: `cd Проекты/DocGen/app && python -m pytest tests/ai -v`

Expected: PASS.

```bash
git add Проекты/DocGen/app/src/docgen/ai Проекты/DocGen/app/src/docgen/config.py Проекты/DocGen/app/tests/ai
git commit -m "feat: integrate local DocGen models"
```

### Task 7: Implement persistent background jobs

**Files:**
- Create: `Проекты/DocGen/app/src/docgen/jobs/models.py`
- Create: `Проекты/DocGen/app/src/docgen/jobs/repository.py`
- Create: `Проекты/DocGen/app/src/docgen/jobs/runner.py`
- Create: `Проекты/DocGen/app/src/docgen/jobs/worker.py`
- Create: `Проекты/DocGen/app/tests/jobs/test_repository.py`
- Create: `Проекты/DocGen/app/tests/jobs/test_runner.py`

**Interfaces:**
- Produces: `JobKind.ASSEMBLE`, `JobKind.CHECK`; `JobStatus.QUEUED/RUNNING/SUCCEEDED/FAILED/CANCELLED`
- Produces: `JobRepository.enqueue(project_id, kind, template_id) -> Job`
- Produces: `JobRepository.claim_next() -> Job | None`
- Produces: `JobRepository.request_cancel(job_id) -> None`
- Produces: `JobRunner.run_once() -> bool`
- Produces: CLI `python -m docgen.jobs.worker`

- [ ] **Step 1: Write state-machine and claim tests**

```python
def test_claim_next_is_fifo_and_marks_running(job_repository):
    first = job_repository.enqueue("p1", JobKind.ASSEMBLE, "use-case")
    job_repository.enqueue("p2", JobKind.CHECK, "use-case")
    claimed = job_repository.claim_next()
    assert claimed.id == first.id
    assert claimed.status is JobStatus.RUNNING


def test_cancelled_job_is_not_claimed(job_repository):
    job = job_repository.enqueue("p1", JobKind.ASSEMBLE, "faq")
    job_repository.request_cancel(job.id)
    assert job_repository.claim_next() is None
```

- [ ] **Step 2: Run and verify failure**

Run: `cd Проекты/DocGen/app && python -m pytest tests/jobs -v`

Expected: FAIL because job models do not exist.

- [ ] **Step 3: Implement the SQLite job state machine**

Persist timestamps, integer progress 0–100, Russian `status_message`, user-safe `error_message`, and `cancel_requested`. Allow transitions queued→running→succeeded/failed/cancelled and running→failed/cancelled only. Use a SQLite `BEGIN IMMEDIATE` transaction while claiming to prevent two workers taking the same row.

- [ ] **Step 4: Implement runner and worker loop**

`JobRunner` receives a mapping from `JobKind` to callable workflow. It commits progress between stages, checks cancellation before every extractor/model call, stores only user-safe errors, and returns whether a job was processed. `worker.py` polls every 0.5 seconds, handles SIGTERM, and marks jobs left running by the same interrupted worker as failed with `Обработка была прервана; запустите её повторно` on next startup.

- [ ] **Step 5: Run tests and commit**

Run: `cd Проекты/DocGen/app && python -m pytest tests/jobs -v`

Expected: PASS.

```bash
git add Проекты/DocGen/app/src/docgen/jobs Проекты/DocGen/app/tests/jobs
git commit -m "feat: run persistent DocGen jobs"
```

### Task 8: Build assemble and check workflows

**Files:**
- Create: `Проекты/DocGen/app/src/docgen/workflows/assemble.py`
- Create: `Проекты/DocGen/app/src/docgen/workflows/check.py`
- Modify: `Проекты/DocGen/app/src/docgen/jobs/runner.py`
- Create: `Проекты/DocGen/app/tests/workflows/test_assemble.py`
- Create: `Проекты/DocGen/app/tests/workflows/test_check.py`

**Interfaces:**
- Consumes: normalization, template catalog, text/vision models, grounding validator, document repository
- Produces: `AssembleWorkflow.run(job: Job, progress: ProgressSink) -> WorkingDocument`
- Produces: `CheckWorkflow.run(job: Job, progress: ProgressSink) -> CheckReport`

- [ ] **Step 1: Write grounded assembly tests**

```python
def test_assemble_saves_grounded_document(workflow, fake_model):
    fake_model.result = grounded_use_case_document()
    document = workflow.run(assemble_job(), progress_spy)
    assert document.template_id == "use-case"
    assert document_repository.get_document("p1") == document
    assert progress_spy.values == [10, 35, 70, 90, 100]


def test_assemble_fails_on_ungrounded_output(workflow, fake_model):
    fake_model.result = document_with_source("unknown")
    with pytest.raises(WorkflowError, match="Результат не прошёл проверку по источникам"):
        workflow.run(assemble_job(), progress_spy)
```

- [ ] **Step 2: Write checking tests**

```python
def test_check_separates_confirmed_and_low_confidence_findings(check_workflow, fake_model):
    report = check_workflow.run(check_job(), progress_spy)
    assert report.findings[0].severity is Severity.ERROR
    assert report.findings[-1].confidence < 0.7
    assert report.unchecked_rules == ["terminology-3"]
```

- [ ] **Step 3: Run and verify failure**

Run: `cd Проекты/DocGen/app && python -m pytest tests/workflows/test_assemble.py tests/workflows/test_check.py -v`

Expected: FAIL because workflows do not exist.

- [ ] **Step 4: Implement deterministic workflow stages**

Assembly stages: load project/template 10%; normalize text files 35%; enrich images through vision model 70%; call text model and validate grounding 90%; save artifact 100%. Checking uses the same normalized sources plus the uploaded document, evaluates every template rule, marks confidence below 0.7 as low-confidence, records unevaluated rule IDs, and saves the report only after schema validation.

- [ ] **Step 5: Register workflows in JobRunner and run tests**

Run: `cd Проекты/DocGen/app && python -m pytest tests/workflows tests/jobs -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add Проекты/DocGen/app/src/docgen/workflows Проекты/DocGen/app/src/docgen/jobs/runner.py Проекты/DocGen/app/tests/workflows
git commit -m "feat: assemble and check DocGen documents"
```

### Task 9: Add generation and checking screens

**Files:**
- Create: `Проекты/DocGen/app/src/docgen/generation/routes.py`
- Create: `Проекты/DocGen/app/src/docgen/templates/generation/setup.html`
- Create: `Проекты/DocGen/app/src/docgen/templates/generation/status.html`
- Create: `Проекты/DocGen/app/src/docgen/templates/generation/result.html`
- Create: `Проекты/DocGen/app/src/docgen/templates/generation/report.html`
- Modify: `Проекты/DocGen/app/src/docgen/templates/projects/detail.html`
- Modify: `Проекты/DocGen/app/src/docgen/main.py`
- Create: `Проекты/DocGen/app/tests/generation/test_routes.py`

**Interfaces:**
- Produces: `POST /projects/{id}/jobs/assemble`, `POST /projects/{id}/jobs/check`
- Produces: `GET /projects/{id}/jobs/{job_id}`, `POST /projects/{id}/jobs/{job_id}/cancel`
- Produces: `GET /projects/{id}/document`, `GET /projects/{id}/report`

- [ ] **Step 1: Write failing route tests**

```python
def test_start_assemble_enqueues_job(client, configured_models, project_with_source):
    response = client.post(f"/projects/{project_with_source.id}/jobs/assemble", data={"template_id": "use-case"})
    assert response.status_code == 202
    assert "Сборка поставлена в очередь" in response.text


def test_missing_model_configuration_returns_503(client, project_with_source):
    response = client.post(f"/projects/{project_with_source.id}/jobs/assemble", data={"template_id": "use-case"})
    assert response.status_code == 503
    assert "Локальные модели не настроены" in response.text
```

Also test empty sources, invalid template, 101-page warning, cancel, completed result, report grouping and user-safe failure messages.

- [ ] **Step 2: Run and verify failure**

Run: `cd Проекты/DocGen/app && python -m pytest tests/generation/test_routes.py -v`

Expected: FAIL with 404 because routes do not exist.

- [ ] **Step 3: Implement routes and HTMX polling**

Return 202 for created jobs. `status.html` polls every two seconds with `hx-get` while queued/running, displays progress, status message and cancel button, then swaps to result/report or a retry action. Prevent a second active job for the same project with 409 `Проект уже обрабатывается`.

- [ ] **Step 4: Render result and report**

Render document nodes in order. Render report groups: confirmed problems, low-confidence remarks, and unchecked rules. Every finding links to its document node ID. Show gaps as explicit `Нет данных в источниках` blocks.

- [ ] **Step 5: Run complete tests and lint**

Run: `cd Проекты/DocGen/app && python -m pytest -v && python -m ruff check .`

Expected: PASS; no Ruff errors.

- [ ] **Step 6: Commit**

```bash
git add Проекты/DocGen/app/src/docgen Проекты/DocGen/app/tests/generation
git commit -m "feat: add DocGen assembly and checking UI"
```

### Task 10: Add quality corpus and Stage 2 acceptance test

**Files:**
- Create: `Проекты/DocGen/app/tests/quality/cases/use-case-basic/sources/input.md`
- Create: `Проекты/DocGen/app/tests/quality/cases/use-case-basic/expected.yaml`
- Create: `Проекты/DocGen/app/tests/quality/test_quality_metrics.py`
- Create: `Проекты/DocGen/app/tests/test_stage2_journey.py`
- Modify: `Проекты/DocGen/app/README.md`

**Interfaces:**
- Produces: deterministic offline quality metric runner
- Produces: documented web/worker launch commands

- [ ] **Step 1: Define the first frozen quality case**

`expected.yaml` lists applicable template rule IDs, required node kinds/titles, forbidden claims, expected gaps and maximum allowed processing seconds for the fake-model test. Do not store real corporate data; use synthetic Russian banking content.

- [ ] **Step 2: Write metric tests**

```python
def test_quality_case_scores_applicable_requirements():
    score = evaluate_case(CASE_DIR, deterministic_fake_model())
    assert score.requirement_coverage >= 0.80
    assert score.ungrounded_claims == 0
```

Add an integration test that uploads a source, starts assembly, runs one worker iteration, reads the saved document, starts checking, runs one worker iteration and reads the saved report.

- [ ] **Step 3: Run acceptance tests**

Run: `cd Проекты/DocGen/app && python -m pytest tests/quality tests/test_stage2_journey.py -v`

Expected: PASS without network access.

- [ ] **Step 4: Document runtime configuration**

Add commands for web and worker processes:

```bash
.venv/bin/uvicorn docgen.main:app --port 8000
.venv/bin/python -m docgen.jobs.worker
```

Document all local model and Confluence environment variable names without values. State that actual secrets live only in the workspace root `.env` and must never be printed or committed.

- [ ] **Step 5: Run complete verification and commit**

Run: `cd Проекты/DocGen/app && python -m pytest -v && python -m ruff check .`

Expected: PASS; no Ruff errors.

```bash
git add Проекты/DocGen/app/tests Проекты/DocGen/app/README.md
git commit -m "test: verify DocGen AI pipeline"
```
