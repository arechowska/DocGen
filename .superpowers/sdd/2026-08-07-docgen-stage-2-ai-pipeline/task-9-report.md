# Task 9 report — generation and checking screens

## Result

Implemented the FastAPI/HTMX generation UI and staged it for commit as
`feat: add DocGen assembly and checking UI`.

- Added assemble/check start routes with `202`, validation errors (`422`), dependency
  degradation (`503`), and one-active-job conflict handling (`409`).
- Added project-owned status/cancel routes, two-second HTMX polling, cancellation and retry.
- Added document and report views with safe node-kind rendering, explicit gaps, grouped findings,
  node/document links, and stable HTMX swap targets.
- Added SQLite `BEGIN IMMEDIATE` serialization around the route enqueue check/insert.
- Added project-page setup forms and router wiring.

## RED

Initial command:

```powershell
cd app
.\.venv\Scripts\python.exe -m pytest tests/generation/test_routes.py -v
```

Initial result: `19 failed`; every requested route returned `404`.

Additional focused RED cycles caught and proved:

- an error response replacing the status target with a nested full setup form;
- a cancel/worker-completion race rendering a false cancellation notice;
- a report finding without `node_id` receiving no document link;
- a `409` conflict replacing active polling/cancel controls with a static message;
- completed result/report fragments losing the stable `#generation-status` target.

Each focused test failed for the expected missing behavior before its production fix.

## GREEN

Final focused command:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/generation/test_routes.py -v
```

Result: `21 passed`.

The route suite includes an actual route -> persisted queue -> `JobRunner` -> `CheckWorkflow` ->
saved report -> HTMX report flow with local deterministic dependencies.

## Full tests and Ruff

Final commands:

```powershell
.\.venv\Scripts\python.exe -m pytest -v -k "not test_save_rejects_projects_directory_symlinked_outside_data_dir and not test_delete_project_rejects_symlink_to_another_project"
.\.venv\Scripts\python.exe -m ruff check .
git diff --cached --check
```

Results:

- `212 passed, 2 deselected`;
- Ruff: `All checks passed!`;
- staged diff check: clean.

The two deselected storage tests require Windows symlink privileges and are the approved environment
exclusions from the brief. An unfiltered run otherwise produced `210 passed` at the earlier checkpoint
and failed only those two tests with `WinError 1314`.

The pre-existing normalization regression covers the 101-page long-processing warning.

## Security and ownership review

- Every job status/cancel request first validates the project and then verifies
  `job.project_id == project_id`; cross-project reads and cancellation return `404` without mutation.
- Start routes validate project ownership, sources, template, model configuration and conditional
  Confluence configuration before enqueue.
- Failed jobs render a fixed user-safe message and never render persisted exception details.
- Templates rely on Jinja autoescaping; image nodes never use model-provided URLs/base64 as markup.
- Only saved validated `WorkingDocument` and `CheckReport` content is rendered.
- Cancel completion races return the completed artifact, not a misleading cancellation response.

## Self-review

The mandatory reviewer found three important HTMX edge cases (cancel race, active-job conflict losing
polling, and terminal fragments losing their swap target) plus the optional-node-link gap. All were
fixed with focused regressions. No critical findings remain.

One upstream product concern remains: PRD describes checking an uploaded document as a standalone
scenario, while Task 8's implemented `CheckWorkflow` explicitly requires a current saved
`WorkingDocument`. Task 9 now rejects a check without that artifact with a safe `422`, preventing a
known late worker failure, and proves the supported saved-document flow end to end. Converting a raw
uploaded DOCX/PDF source into the check target requires a Task 8/workflow contract change and is not
silently claimed as complete here.
