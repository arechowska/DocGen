# Fact-Coverage Assembly Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every semantic-template assembly auditable and complete by extracting atomic source facts, assembling from those facts in bounded batches, and refusing to save documents with uncovered facts.

**Architecture:** Replace the single semantic assembly call with a fact-inventory stage followed by section-level assembly. Deterministic validators own source disposition, exact grounding, evidence IDs, and set-based coverage; the model only extracts and rewrites content. Persist the inventory and coverage report at the saved document revision, while keeping `no-template` assembly unchanged.

**Tech Stack:** Python 3.12, Pydantic 2, SQLAlchemy 2, FastAPI, Jinja2/HTMX, httpx, pytest.

**Spec:** `docs/superpowers/specs/2026-08-18-fact-coverage-assembly-design.md`

## Global Constraints

- Apply the pipeline to all built-in semantic templates: `faq`, `use-case`, `technical-spec`, `api-docs`, and `release-notes`.
- Do not change the `no-template` direct-conversion path.
- Never silently truncate input blocks, facts, or model output.
- Do not save a new document revision when fact coverage is incomplete.
- Preserve loading and export of documents saved before `evidence_fact_ids` exists.
- Keep all model-facing errors user-safe and do not include source text, prompts, tokens, or credentials in raised messages.

---

### Task 1: Preserve model completion metadata and reject truncated output

**Files:**
- Modify: `app/src/docgen/ai/client.py`
- Modify: `app/tests/ai/test_client.py`

**Interfaces:**
- Produces: `ModelCompletion[T](value: T, finish_reason: str | None, prompt_tokens: int | None, completion_tokens: int | None)`
- Produces: `TextModel.generate_json_completion(system: str, user: str, schema: type[T]) -> ModelCompletion[T]`
- Preserves: `TextModel.generate_json(...) -> T` as a compatibility wrapper.
- Produces: `ModelOutputLimitError(ModelError)` when `finish_reason == "length"`.

- [ ] **Step 1: Write failing client tests**

Add tests proving that a normal response exposes `finish_reason` and token usage, `generate_json()` still returns only the parsed value, and a response with `finish_reason: "length"` raises `ModelOutputLimitError` before accepting its content.

```python
def test_text_model_exposes_completion_metadata() -> None:
    transport = completion_transport(
        content='{"title":"FAQ","template_id":"faq","nodes":[]}',
        finish_reason="stop",
        usage={"prompt_tokens": 120, "completion_tokens": 40},
    )
    model = OpenAICompatibleTextModel(base_url=LOCAL_URL, model="qwen-test", transport=transport)

    result = model.generate_json_completion("system", "user", WorkingDocument)

    assert result.value.title == "FAQ"
    assert result.finish_reason == "stop"
    assert result.prompt_tokens == 120
    assert result.completion_tokens == 40


def test_text_model_rejects_length_finish_reason() -> None:
    model = OpenAICompatibleTextModel(
        base_url=LOCAL_URL,
        model="qwen-test",
        transport=completion_transport(content="{}", finish_reason="length"),
    )

    with pytest.raises(ModelOutputLimitError, match="лимит"):
        model.generate_json_completion("system", "user", WorkingDocument)
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `cd app && uv run pytest tests/ai/test_client.py -q`

Expected: FAIL because `ModelCompletion`, `generate_json_completion`, and `ModelOutputLimitError` do not exist.

- [ ] **Step 3: Implement metadata parsing without breaking existing callers**

Add a frozen generic dataclass, parse `choices[0].finish_reason` and optional `usage`, reject `length`, and make `generate_json()` delegate to `generate_json_completion(...).value`. Do not print token counts or content in exceptions.

```python
@dataclass(frozen=True)
class ModelCompletion(Generic[T]):
    value: T
    finish_reason: str | None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class ModelOutputLimitError(ModelError):
    pass
```

- [ ] **Step 4: Run client tests**

Run: `cd app && uv run pytest tests/ai/test_client.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/src/docgen/ai/client.py app/tests/ai/test_client.py
git commit -m "feat: expose model completion limits"
```

### Task 2: Define and deterministically validate the fact inventory

**Files:**
- Create: `app/src/docgen/workflows/facts.py`
- Create: `app/tests/workflows/test_facts.py`
- Modify: `app/src/docgen/documents/schemas.py`
- Modify: `app/tests/documents/test_schemas.py`

**Interfaces:**
- Produces: `FactCandidate`, `BlockFactResult`, `FactExtractionResponse`, `FactRecord`, `BlockExclusion`, `FactInventory`, `CoverageReport`.
- Produces: `build_fact_inventory(response, blocks, template) -> FactInventory`.
- Produces: `merge_fact_inventories(parts: list[FactInventory], expected_block_ids: set[str], template_id: str) -> FactInventory`.
- Produces: `fact_id(source_block_id: str, quote: str, text: str, section_id: str) -> str` using UUIDv5.
- Extends: `DocumentNode.evidence_fact_ids: list[str] = Field(default_factory=list)`.

- [ ] **Step 1: Write failing schema and inventory tests**

Cover these cases with explicit fixtures: old document JSON loads with empty evidence IDs; IDs are stable across runs; every input block must be returned exactly once; a block cannot contain both facts and an exclusion; quotes must be exact non-empty substrings; section IDs must exist in the selected template; duplicate fact IDs are rejected.

```python
def test_inventory_rejects_unaccounted_source_block() -> None:
    response = FactExtractionResponse(blocks=[])

    with pytest.raises(FactInventoryError, match="block-1"):
        build_fact_inventory(response, [_block("block-1", "Текст")], faq_template())


def test_legacy_document_defaults_evidence_ids() -> None:
    document = WorkingDocument.model_validate(
        {"title": "Legacy", "template_id": "faq", "nodes": [{"kind": "paragraph"}]}
    )
    assert document.nodes[0].evidence_fact_ids == []
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `cd app && uv run pytest tests/workflows/test_facts.py tests/documents/test_schemas.py -q`

Expected: FAIL because the fact schemas and evidence field do not exist.

- [ ] **Step 3: Implement immutable schemas and validation**

Use `Literal` for exclusion reasons:

```python
ExclusionReason = Literal[
    "heading-only",
    "navigation",
    "duplicate",
    "no-semantic-content",
    "out-of-template-scope",
]
```

Store original `source_block_id` and locator on every fact. Generate IDs in application code after validating model output; never trust a model-generated ID. `merge_fact_inventories` must reject a repeated or missing block across batches and return facts in original block order. Return errors listing IDs only, not source text.

- [ ] **Step 4: Run focused tests**

Run: `cd app && uv run pytest tests/workflows/test_facts.py tests/documents/test_schemas.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/src/docgen/workflows/facts.py app/src/docgen/documents/schemas.py app/tests/workflows/test_facts.py app/tests/documents/test_schemas.py
git commit -m "feat: add atomic fact inventory"
```

### Task 3: Extract facts from every source block in bounded batches

**Files:**
- Create: `app/src/docgen/workflows/fact_extraction.py`
- Create: `app/tests/workflows/test_fact_extraction.py`
- Modify: `app/src/docgen/ai/prompts.py`
- Modify: `app/src/docgen/config.py`
- Modify: `app/tests/test_config.py`

**Interfaces:**
- Produces: `FactExtractionService(text_model: TextModel, max_batch_chars: int)`.
- Produces: `FactExtractionService.extract(blocks: list[NormalizedBlock], template: SemanticTemplate, checkpoint: Callable[[], None]) -> FactInventory`.
- Consumes: `build_fact_inventory(...)` from Task 2 and `ModelOutputLimitError` from Task 1.
- Adds setting: `fact_extraction_batch_chars: int = 60_000` with `gt=0`.

- [ ] **Step 1: Write failing extraction tests**

Use a recording fake model to prove that all blocks are present in prompts, multiple batches are created at the configured character budget, cancellation runs before each call, responses merge into one inventory, and a `ModelOutputLimitError` recursively splits a multi-block batch. Prove that a single-block limit failure becomes a user-safe `FactExtractionError` rather than looping.

```python
def test_extraction_splits_output_limited_batch_and_keeps_all_blocks() -> None:
    model = SplittingFactModel(limit_when_block_count_above=1)
    service = FactExtractionService(model, max_batch_chars=100_000)

    inventory = service.extract(two_blocks(), faq_template(), lambda: None)

    assert {fact.source_block_id for fact in inventory.facts} == {"b1", "b2"}
    assert model.batch_sizes == [2, 1, 1]
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `cd app && uv run pytest tests/workflows/test_fact_extraction.py tests/test_config.py -q`

Expected: FAIL because the service and setting do not exist.

- [ ] **Step 3: Implement batching and the extraction prompt**

Build batches from serialized public block payloads without splitting a `NormalizedBlock`. The prompt must require one disposition per input block, atomic facts, exact quotes, one valid template section per fact, and a controlled exclusion reason. Validate each batch before merging; reject unknown or repeated block IDs across batches.

```python
def iter_block_batches(blocks: list[NormalizedBlock], max_chars: int) -> Iterator[list[NormalizedBlock]]:
    batch: list[NormalizedBlock] = []
    size = 0
    for block in blocks:
        block_size = len(json.dumps(public_block(block), ensure_ascii=False))
        if batch and size + block_size > max_chars:
            yield batch
            batch, size = [], 0
        batch.append(block)
        size += block_size
    if batch:
        yield batch
```

- [ ] **Step 4: Run focused tests**

Run: `cd app && uv run pytest tests/workflows/test_fact_extraction.py tests/test_config.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/src/docgen/workflows/fact_extraction.py app/src/docgen/ai/prompts.py app/src/docgen/config.py app/tests/workflows/test_fact_extraction.py app/tests/test_config.py
git commit -m "feat: extract source facts in bounded batches"
```

### Task 4: Assemble template sections from facts and enforce coverage

**Files:**
- Create: `app/src/docgen/workflows/fact_assembly.py`
- Create: `app/tests/workflows/test_fact_assembly.py`
- Replace: `app/src/docgen/ai/grounding.py`
- Replace: `app/tests/ai/test_grounding.py`
- Modify: `app/src/docgen/config.py`
- Modify: `app/tests/test_config.py`

**Interfaces:**
- Produces: `SectionDraft(section_id: str, nodes: list[DocumentNode])`.
- Produces: `CoverageValidator.validate(document, inventory, template) -> CoverageReport`.
- Produces: `FactAssemblyService.assemble(title, inventory, template, checkpoint) -> tuple[WorkingDocument, CoverageReport]`.
- Adds settings: `fact_assembly_batch_chars: int = 60_000` and `fact_coverage_retries: int = 2`, both `gt=0`.
- Consumes: `DocumentNode.evidence_fact_ids`, `FactInventory`, and completion-limit handling from Tasks 1–3.

- [ ] **Step 1: Write failing grounding and coverage tests**

Prove that unknown block references, missing provenance, non-verbatim quotes, wrong locators, unknown fact IDs, cross-section fact IDs, and missing facts are rejected. Prove that nested child nodes with exact provenance and matching evidence IDs pass.

```python
def test_coverage_reports_fact_missing_from_document() -> None:
    report = CoverageValidator().validate(empty_section_document(), inventory_with("fact-1"), faq_template())
    assert report.missing_fact_ids == ["fact-1"]
    assert report.complete is False
```

- [ ] **Step 2: Write failing section-assembly tests**

Cover one top-level node per required section, fact batching, child-node evidence, deterministic merge order, empty sections as gaps, repair calls containing only missing fact IDs, output-limit batch splitting, and failure after `fact_coverage_retries` without returning a document.

- [ ] **Step 3: Run focused tests and verify failure**

Run: `cd app && uv run pytest tests/ai/test_grounding.py tests/workflows/test_fact_assembly.py tests/test_config.py -q`

Expected: FAIL because strict grounding, coverage validation, and section assembly do not exist.

- [ ] **Step 4: Implement strict grounding and set-based coverage**

Walk all document nodes. For each provenance entry, resolve the original block, verify locator ownership and exact quote. For every `evidence_fact_id`, resolve the fact and verify that its `section_id` matches the containing top-level section. Compute missing and unknown IDs with ordinary set operations.

- [ ] **Step 5: Implement section batching, merge, and bounded repair**

For a section with facts, create one heading top-level node using the catalog title and append model-generated elements as children. Use the current project name as `WorkingDocument.title`, preserving the existing `"Документ"` fallback. Require every generated child to carry evidence IDs and exact provenance. After all sections are merged, ask the model to add content only for `missing_fact_ids`; revalidate after each retry. Raise `IncompleteCoverageError("Модель не включила все факты из источников")` when retries are exhausted.

- [ ] **Step 6: Run focused tests**

Run: `cd app && uv run pytest tests/ai/test_grounding.py tests/workflows/test_fact_assembly.py tests/test_config.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/src/docgen/workflows/fact_assembly.py app/src/docgen/ai/grounding.py app/src/docgen/config.py app/tests/workflows/test_fact_assembly.py app/tests/ai/test_grounding.py app/tests/test_config.py
git commit -m "feat: enforce semantic fact coverage"
```

### Task 5: Integrate the fact pipeline into semantic assembly

**Files:**
- Modify: `app/src/docgen/workflows/assemble.py`
- Modify: `app/src/docgen/jobs/runner.py`
- Modify: `app/tests/workflows/test_assemble.py`
- Modify: `app/tests/jobs/test_runner.py`

**Interfaces:**
- Consumes: `FactExtractionService` and `FactAssemblyService` from Tasks 3–4.
- Changes: `AssembleWorkflow` receives both services as dependencies.
- Preserves: `_document_without_template(...)` and its model-free behavior.

- [ ] **Step 1: Replace permissive workflow tests with failing coverage tests**

Delete tests that explicitly allow invented content or missing provenance. Add tests proving semantic assembly runs normalize → image enrichment → fact extraction → fact assembly → save; all five template IDs use the pipeline; incomplete coverage raises `WorkflowError` and leaves the previous document unchanged; cancellation checkpoints occur between every batch; `no-template` never invokes either service.

```python
@pytest.mark.parametrize("template_id", ["faq", "use-case", "technical-spec", "api-docs", "release-notes"])
def test_every_semantic_template_uses_fact_pipeline(template_id: str) -> None:
    workflow, extraction, assembly, documents = semantic_workflow()
    workflow.run(_job(template_id), ProgressSpy([]))
    assert extraction.template_ids == [template_id]
    assert assembly.template_ids == [template_id]
    assert documents.get_document("p1") is not None
```

- [ ] **Step 2: Run workflow tests and verify failure**

Run: `cd app && uv run pytest tests/workflows/test_assemble.py tests/jobs/test_runner.py -q`

Expected: FAIL because `AssembleWorkflow` still calls the model directly.

- [ ] **Step 3: Wire services in production and remove the old one-shot prompt path**

Keep `public_blocks()` available to fact extraction or move it with imports updated. Remove `_assemble_prompt`, `_format_example`, and direct `WorkingDocument` model generation after all references disappear. Map fact/coverage/model-limit errors to stable user-safe workflow messages.

- [ ] **Step 4: Run workflow and journey regression tests**

Run: `cd app && uv run pytest tests/workflows/test_assemble.py tests/jobs/test_runner.py tests/test_stage1_journey.py tests/test_stage2_journey.py tests/test_stage3_journey.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/src/docgen/workflows/assemble.py app/src/docgen/jobs/runner.py app/tests/workflows/test_assemble.py app/tests/jobs/test_runner.py
git commit -m "feat: assemble semantic documents from fact inventory"
```

### Task 6: Persist revision-bound coverage and show a compact result

**Files:**
- Modify: `app/src/docgen/documents/models.py`
- Modify: `app/src/docgen/documents/repository.py`
- Modify: `app/src/docgen/db.py`
- Modify: `app/src/docgen/generation/routes.py`
- Modify: `app/src/docgen/templates/generation/assemble_complete.html`
- Modify: `app/tests/documents/test_repository.py`
- Modify: `app/tests/generation/test_routes.py`
- Modify: `app/tests/test_stage3_journey.py`

**Interfaces:**
- Adds columns: `fact_inventory_json TEXT`, `coverage_json TEXT`, `coverage_revision INTEGER` to `project_artifacts`.
- Produces: `DocumentRepository.save_assembled_document(project_id, document, inventory, coverage) -> int`.
- Produces: `DocumentRepository.get_current_coverage(project_id) -> CoverageReport | None`.
- Invalidation rule: all editor/document replacement methods clear `coverage_json` and `coverage_revision`; a fresh semantic assembly stores both at the new document revision.

- [ ] **Step 1: Write failing migration and repository tests**

Create a legacy SQLite schema without the three columns, run `initialize_database`, and assert migration adds them without changing existing documents. Test atomic save, revision binding, and invalidation through `replace_document` and `save_workspace`.

- [ ] **Step 2: Write a failing result-panel test**

Assert successful assembly renders `Включено 142 из 142 фактов` and `Исключено блоков: 9`, without rendering fact IDs, quotes, or source text. Assert edited documents do not show stale coverage.

- [ ] **Step 3: Run focused tests and verify failure**

Run: `cd app && uv run pytest tests/documents/test_repository.py tests/generation/test_routes.py -q`

Expected: FAIL because persistence and coverage rendering do not exist.

- [ ] **Step 4: Implement migration, repository methods, and invalidation**

Add nullable columns through `_migrate_project_artifacts`. Serialize with Pydantic JSON. Bind coverage to the revision created by `save_assembled_document`; return `None` unless `coverage_revision == document_revision`.

- [ ] **Step 5: Render only aggregate coverage in the result panel**

Pass `coverage` from `_assemble_complete_response` and add compact neutral text below the document metadata. Do not add a new screen or expose exclusions unless a later requirement asks for an audit UI.

- [ ] **Step 6: Run repository, route, and journey tests**

Run: `cd app && uv run pytest tests/documents/test_repository.py tests/generation/test_routes.py tests/test_stage3_journey.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/src/docgen/documents/models.py app/src/docgen/documents/repository.py app/src/docgen/db.py app/src/docgen/generation/routes.py app/src/docgen/templates/generation/assemble_complete.html app/tests/documents/test_repository.py app/tests/generation/test_routes.py app/tests/test_stage3_journey.py
git commit -m "feat: persist semantic coverage audit"
```

### Task 7: Verify all templates, exports, editor compatibility, and full suite

**Files:**
- Modify: `app/tests/workflows/test_fact_assembly.py`
- Modify: `app/tests/export/test_docx.py`
- Modify: `app/tests/export/test_html.py`
- Modify: `app/tests/export/test_markdown.py`
- Modify: `app/tests/export/test_pdf.py`
- Modify: `app/tests/editor/test_complex_nodes.py`
- Modify: `README.md`

**Interfaces:**
- Verifies all interfaces delivered by Tasks 1–6; introduces no new production interface unless a failing compatibility test exposes a required fix.

- [ ] **Step 1: Add parameterized template-shape tests**

For each built-in semantic template, create an inventory with at least two sections and assert one top-level node per catalog section, per-element evidence IDs on child nodes, complete coverage, and stable catalog ordering.

- [ ] **Step 2: Add nested evidence-node compatibility tests**

Create one representative fact-backed document and assert DOCX, HTML, Markdown, and PDF exports contain every child element. Assert the editor renders the children and preserves `evidence_fact_ids` when unrelated text is edited.

- [ ] **Step 3: Run compatibility tests and fix only demonstrated regressions**

Run: `cd app && uv run pytest tests/workflows/test_fact_assembly.py tests/export/test_docx.py tests/export/test_html.py tests/export/test_markdown.py tests/export/test_pdf.py tests/editor/test_complex_nodes.py -q`

Expected: PASS after any evidence-preservation fixes required by the tests.

- [ ] **Step 4: Document semantic completeness behavior**

Update README assembly behavior with: two-stage fact extraction and assembly, all relevant facts must be covered or explicitly excluded, large inputs are batched, incomplete coverage fails without replacing the previous document, and editor changes invalidate the coverage badge.

- [ ] **Step 5: Run formatting and the full test suite**

Run: `cd app && uv run ruff check .`

Expected: PASS.

Run: `cd app && uv run pytest -q`

Expected: PASS.

- [ ] **Step 6: Inspect the final diff for accidental user-file changes**

Run: `git status --short && git diff --check`

Expected: only files listed in this plan are modified; pre-existing user changes in `Планы/ROADMAP.md` and `TODO.md` remain untouched; `git diff --check` exits 0.

- [ ] **Step 7: Commit**

```bash
git add app/tests/workflows/test_fact_assembly.py app/tests/export/test_docx.py app/tests/export/test_html.py app/tests/export/test_markdown.py app/tests/export/test_pdf.py app/tests/editor/test_complex_nodes.py README.md
git commit -m "test: verify fact-complete assembly end to end"
```
