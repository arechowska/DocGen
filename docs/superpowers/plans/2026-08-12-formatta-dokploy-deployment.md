# Formatta Dokploy Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Dockploy-specific Docker Compose configuration and deployment instructions without changing local Docker Compose behavior.

**Architecture:** `docker-compose.yml` remains the local entry point with host port 8000. A separate `docker-compose.dokploy.yml` defines web and worker for Dockploy, with an internal-only web port, shared named volume, healthcheck, and worker readiness dependency.

**Tech Stack:** Docker Compose, Docker healthcheck, FastAPI `/health`, Pytest, PyYAML.

## Global Constraints

- Do not change `docker-compose.yml` or `Dockerfile`.
- Do not store secrets in Git; Dockploy provides `.env` at runtime.
- Use one `web` and one `worker` service with SQLite-backed MVP storage.
- Do not publish the app port with `ports`; use `expose: 8000` only.

---

### Task 1: Dockploy compose contract

**Files:**
- Create: `docker-compose.dokploy.yml`
- Modify: `app/tests/test_docker_packaging.py`

**Interfaces:**
- Consumes: existing Dockerfile and root `.env` contract.
- Produces: a Compose file selectable in Dockploy.

- [ ] **Step 1: Write the failing test**

```python
def test_dokploy_compose_keeps_web_internal_and_worker_ready() -> None:
    compose = yaml.safe_load((REPOSITORY_ROOT / "docker-compose.dokploy.yml").read_text())
    assert compose["services"]["web"]["expose"] == ["8000"]
    assert "ports" not in compose["services"]["web"]
    assert compose["services"]["web"]["healthcheck"]["test"] == [
        "CMD", "python", "-c",
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')",
    ]
    assert compose["services"]["worker"]["depends_on"]["web"]["condition"] == "service_healthy"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd app && .venv/bin/python -m pytest tests/test_docker_packaging.py -k dokploy -v`

Expected: FAIL because the Dockploy compose file does not exist.

- [ ] **Step 3: Add minimal Dockploy compose**

```yaml
services:
  web:
    build: { context: . }
    env_file: [.env]
    expose: ["8000"]
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')
  worker:
    build: { context: . }
    command: ["python", "-m", "docgen.jobs.worker"]
    depends_on:
      web: { condition: service_healthy }
volumes:
  formatta-var:
```

- [ ] **Step 4: Run focused Docker configuration tests**

Run: `cd app && .venv/bin/python -m pytest tests/test_docker_packaging.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add docker-compose.dokploy.yml app/tests/test_docker_packaging.py
git commit -m "ops: add Dockploy compose deployment"
```

### Task 2: Dockploy operator instructions

**Files:**
- Modify: `app/README.md`
- Modify: `app/tests/test_docker_packaging.py`

**Interfaces:**
- Consumes: `docker-compose.dokploy.yml` and current configuration documentation.
- Produces: deploy checklist with secrets kept in Dockploy.

- [ ] **Step 1: Write the failing documentation contract test**

```python
def test_readme_documents_dokploy_deployment() -> None:
    readme = (REPOSITORY_ROOT / "app" / "README.md").read_text(encoding="utf-8")
    assert "docker-compose.dokploy.yml" in readme
    assert "service `web`" in readme
    assert "container port `8000`" in readme
    assert "http://<domain>/health" in readme
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd app && .venv/bin/python -m pytest tests/test_docker_packaging.py -k dokploy -v`

Expected: FAIL because README has no Dockploy instructions.

- [ ] **Step 3: Document exact Dockploy setup**

Add a Russian “Dockploy” section: GitHub branch, Compose path, UI-only
environment secrets, `web` domain mapping to container port 8000, named-volume
backup, one web/worker instance, internal-only access and health/log checks.

- [ ] **Step 4: Run full verification**

Run: `cd app && .venv/bin/python -m pytest -q && .venv/bin/python -m ruff check . && docker compose -f ../docker-compose.dokploy.yml config`

Expected: tests and Ruff pass; Docker Compose validates the deployment file.

- [ ] **Step 5: Commit**

```bash
git add app/README.md app/tests/test_docker_packaging.py
git commit -m "docs: document Formatta Dockploy deployment"
```
