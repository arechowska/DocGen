# Fact-Coverage Assembly Design

## Problem

Semantic assembly currently sends every normalized source block to the text model once and accepts any structurally valid `WorkingDocument`. The prompt asks for complete coverage, but the application does not prove that the model used every relevant source fact. A model can therefore omit most of a second source while still returning all required template sections.

## Goal

For every semantic template, build documents from an auditable inventory of atomic facts and save a result only when every relevant fact is represented in the document. The `no-template` path must remain unchanged and preserve its single source directly.

## Architecture

Semantic assembly becomes a two-stage pipeline:

1. **Fact extraction:** normalized blocks are processed in bounded batches. Every block receives an explicit disposition: one or more atomic facts, or an exclusion with a controlled reason. Every fact carries a deterministic ID, the source block ID, locator, exact quote, and target template section.
2. **Section assembly:** facts are grouped by template section and assembled in bounded batches. Generated content nodes carry the IDs of the facts they represent. A deterministic coverage validator compares inventory IDs with document evidence IDs. Missing facts are repaired in bounded retries; unresolved omissions fail the job instead of saving an incomplete document.

The application, not the model, owns batching, IDs, set comparison, exact-quote validation, retry limits, and the final success decision.

## Data model

- `FactRecord`: deterministic `id`, `source_block_id`, `locator`, exact `quote`, normalized `text`, and `section_id`.
- `BlockExclusion`: `source_block_id`, controlled reason (`heading-only`, `navigation`, `duplicate`, `no-semantic-content`, `out-of-template-scope`) and explanation.
- `FactInventory`: template ID, all facts, all exclusions, and all processed block IDs.
- `DocumentNode.evidence_fact_ids`: IDs of facts represented by that node; defaults to an empty list for compatibility with existing saved documents.
- `CoverageReport`: counts plus missing, unknown, duplicate, and excluded IDs. A report is complete only when missing and unknown are empty.

For generated semantic documents, each required template section remains one top-level node. The actual generated elements are child nodes, so FAQ questions, use-case steps, requirements, API operations, and release-note items can each carry their own evidence IDs and provenance. Existing exporters already render nested children.

## Validation rules

- Every input block must appear exactly once in fact extraction output, either with facts or with an exclusion.
- A block cannot be both extracted and excluded.
- Every fact quote must be a non-empty exact substring of its source block.
- Every fact section must exist in the selected semantic template.
- Every generated evidence ID must exist in the inventory and belong to the node's top-level section.
- Every non-excluded fact must be used at least once.
- Reuse of a fact is allowed only when the template explicitly needs the same fact in multiple sections; otherwise it is reported as a duplicate.
- A `finish_reason` indicating output truncation is never accepted as a successful model response.

## Batching and recovery

Batch by serialized character budget rather than by page count. Never silently truncate a block or fact. If a model response reaches its output limit, split the batch and retry both halves. If a single atomic unit still cannot complete, fail with a user-safe error. Check cancellation between all model calls.

Section assembly retries only missing facts, not the whole document. The retry count is bounded by configuration. If coverage remains incomplete, preserve the previous document and mark the assembly job failed.

## Persistence and UI

Persist the fact inventory and coverage report beside the assembled document, bound to the document revision. Any editor mutation invalidates the persisted coverage report because the document no longer matches the audited revision. The result panel shows a compact status such as `142/142 фактов включено; 9 блоков исключено` and never exposes internal IDs.

## Compatibility

- Existing documents load because `evidence_fact_ids` is optional with an empty default.
- Existing editor and export formats remain valid because content stays in `DocumentNode` trees.
- Existing chat edits invalidate coverage rather than pretending the old audit still applies.
- `no-template` assembly does not run fact extraction and is unaffected.

## Testing

Unit tests cover model finish reasons, deterministic IDs, block disposition, quote validation, batching, section merge, and coverage repair. Workflow tests prove that omitted facts prevent saving, all five built-in semantic templates use the same pipeline, large inputs split into multiple calls, and `no-template` behavior remains unchanged. Repository and route tests cover schema migration, revision binding, invalidation after edits, and the compact UI summary.
