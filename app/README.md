# DocGen

DocGen Stage 1 provides a local workspace for creating projects and collecting source files and Confluence URLs.

## Setup and launch

Run the following commands from the workspace root:

```bash
cd Проекты/DocGen/app
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/uvicorn docgen.main:app --reload
```

Open `http://127.0.0.1:8000/projects` to use the workspace.

## Test and lint

```bash
cd Проекты/DocGen/app
.venv/bin/pytest -v
.venv/bin/ruff check .
```

## Local configuration

Configuration uses `DOCGEN_` environment variables. The defaults store the SQLite database at `./var/docgen.db` and uploaded files below `./var/data`.

- `DOCGEN_DATABASE_URL` sets the SQLAlchemy database URL, for example `sqlite:///./var/docgen.db`.
- `DOCGEN_DATA_DIR` sets the directory for uploaded files, for example `./var/data`.
- `DOCGEN_CONFLUENCE_HOSTS` is a JSON array of allowed Confluence host names, for example `'["wiki.example.test", "confluence.internal.example"]'`.

Keep private values only in the repository-root file `Проекты/DocGen/.env`. Never commit that file, credentials, tokens, passwords, or other secrets. `Settings` does not load `.env` automatically: load its `DOCGEN_*` assignments into the process before starting the application, without printing their values:

```bash
cd Проекты/DocGen
set -a
. ./.env
set +a
cd app
.venv/bin/uvicorn docgen.main:app --reload
```

The `.env` file must contain only trusted shell-compatible `DOCGEN_*` assignments, including a JSON value for `DOCGEN_CONFLUENCE_HOSTS` when it is set.

## Reset local data

Stop the server first. The following removes the local default database and all uploaded files irreversibly:

```bash
cd Проекты/DocGen/app
rm -rf var
```

If `DOCGEN_DATABASE_URL` or `DOCGEN_DATA_DIR` points elsewhere, remove or replace only those configured local paths.

## Stage 1 boundary

Stage 1 accepts supported source files and stores allowed Confluence URLs as source records. It does not connect to Confluence or read its content. Stage 2 will retrieve and interpret the stored Confluence content.
