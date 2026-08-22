# DocGen chat as a semantic document editor

## Goal and boundaries

Chat is an editor of the `WorkingDocument` semantic tree, not a FAQ generator and
not a free-form model prompt. Every request is routed before source extraction or a
model call. A route either produces validated document operations, asks one concrete
clarifying question, or fails with a stable reason and next action.

The shared pipeline is template-independent:

`request -> intent router -> semantic command -> executor -> operation validation -> revision-checked atomic apply`

FAQ is a template adapter. Its question/answer invariant is not added to generic
document nodes or generic operation validation. Export, DOCX/PDF rendering and source
storage are outside this change.

## Intent and semantic-command contracts

The router returns an `IntentDecision` with `kind`, `confidence`, and a typed payload.
It uses command grammar, document shape and template capabilities; it does not read
sources and does not call a model.

Routes:

| Intent | Semantic command | Executor |
| --- | --- | --- |
| authored edit | insert or replace user-supplied content | deterministic |
| grounded edit | update/insert content constrained by evidence | retrieval + model |
| template action | adapter-owned action such as `faq.add_entry` | retrieval + adapter contract + model |
| structure | add/delete/move/merge/split semantic blocks | deterministic when arguments are complete; otherwise clarification |
| format | update presentation data on selected nodes | deterministic |
| unknown/ambiguous | no operation | clarification/error response |

The semantic command vocabulary is independent from phrases and templates. It covers
node selection plus `insert`, `update_text`, `update_data`, `delete`, `move`, `merge`
and `split`. Executors compile these commands to existing `DocumentOperation` values.
No command may directly persist a document.

Author-supplied text is marked with `manual-edit` and requires no evidence. An explicit
replacement/correction is an authored decision even when it contradicts a source.
Generic replacement resolution must work in node text and textual list/table data; it
must not be a special case tied to product names. If the target cannot be resolved
uniquely, the service asks for the old text or target block.

Structural and formatting commands never load sources. Safe, complete commands are
compiled in code. For example, sectioning a flat document creates neutral semantic
section headings and moves existing nodes without rewriting their text. A request such
as “раздели по темам” without themes may ask which themes to use; it must never become
a grounded/no-op error.

## Retrieval and grounded execution

Only `grounded edit` and evidence-requiring template actions enter retrieval. The
source provider returns a `SourceSnapshot` rather than a bare list:

- configured source count;
- normalized blocks;
- safe extraction warnings (no source body);
- snapshot identity suitable for later caching/invalidation.

Retrieval ranks blocks using content terms from the semantic command, not the entire
imperative phrase. It returns at most 12 blocks and never falls back to arbitrary first
blocks when the best score is zero. The exact selected IDs remain the only evidence IDs
the model may cite. The model receives the current document plus this bounded context;
ordinary logs contain only route, counts, retry count, duration and error code.

Grounded model output is schema-specific but compiles into the same operation pipeline.
Every factual operation must cite one or more selected block IDs. Existing lexical and
numeric grounding validation remains a second guard; provenance is copied from cited
blocks only after the whole plan validates.

No source state is distinguished as follows:

- no configured source: `sources_missing`;
- configured sources but extraction produced no usable blocks and warnings: `source_unavailable`;
- usable blocks but retrieval score is zero: `relevant_fragment_missing`.

## Template adapters and FAQ

Adapters expose supported actions for a `template_id`, route aliases to a typed action,
validate model output and compile it to generic semantic commands. An unsupported
template action is an actionable validation error, not a generic model call.

The FAQ adapter owns this model-output contract:

```json
{
  "question": "...",
  "answer": "...",
  "placement": {"parent_id": null, "index": 3},
  "evidence_block_ids": ["source:block"]
}
```

`question`, `answer` and `evidence_block_ids` are non-empty. Placement must resolve
inside the current document. The adapter compiles the pair into the FAQ node shape used
by that template and attaches provenance through the shared operation compiler.
Technical fields are never serialized into visible document text. A question-only,
answer-only, unknown-evidence or invalid-placement response rejects the complete edit.

For non-FAQ templates, “добавь вопрос” is either mapped by that template's adapter or
answered with a precise unsupported/clarification result. It is not treated as a
universal document structure.

## Validation, atomicity and model failures

Validation order is fixed:

1. message and expected revision;
2. intent and command arguments;
3. source snapshot and retrieval when required;
4. strict model schema, with at most one schema-repair retry;
5. node/placement and operation validation;
6. evidence membership and grounding;
7. compile provenance;
8. apply all operations atomically with the existing revision check.

Any failure before step 8 leaves the document and revision unchanged. A concurrent
write at step 8 returns `revision_conflict`. Invalid model operations never receive a
best-effort partial apply.

## API and UI error contract

Domain failures use `ChatError(code, message, action, http_status)`. The HTML response
renders both `message` and `action` and exposes `data-error-code` for future API/UI
clients.

| Code | Message | Next action |
| --- | --- | --- |
| `sources_missing` | К проекту не добавлены источники | Добавь источник и повтори запрос |
| `source_unavailable` | Источник не удалось прочитать | Проверь доступ к источнику и повтори запрос |
| `relevant_fragment_missing` | В источниках не найден фрагмент по теме запроса | Уточни формулировку или добавь подходящий источник |
| `model_invalid_json` | Модель вернула ответ в неверном формате | Повтори запрос; при повторении проверь модель |
| `evidence_missing` | Ответ модели не содержит подтверждения | Уточни запрос или проверь содержание источника |
| `grounding_failed` | Предложенная правка не подтверждается выбранным фрагментом | Уточни факт или источник |
| `revision_conflict` | Документ уже изменён | Обнови документ и повтори правку |
| `clarification_required` | Команда неоднозначна | Выполни указанный в ответе уточняющий шаг |

Model transport/configuration errors remain service-unavailable responses but use safe
messages. Source bodies, serialized prompts and raw model responses are never logged.

## Test strategy and delivery slices

1. Router/command tests: authored insert and generic replacement, grounded update,
   FAQ adapter action, structure, formatting, ambiguous request.
2. Retrieval/error tests: missing sources, unavailable sources, no thematic match,
   bounded relevant context and evidence membership.
3. Contract/atomicity tests: complete FAQ pair, rejected incomplete pair, invalid JSON,
   invalid node operation, missing/irrelevant evidence and revision conflict all preserve
   the stored revision.
4. Route tests: each stable code renders a useful Russian message and action.
5. End-to-end journeys: grounded FAQ pair, source-free sectioning, manual correction,
   missing fact, invalid model operation and concurrent revision.

Implementation should introduce small chat modules (`intents`, `retrieval`, `errors`,
`executors`, `adapters`) and keep `ChatService` as orchestration. This avoids replacing
one monolith with another and permits new templates to add actions without changing the
shared router/grounding/application pipeline.
