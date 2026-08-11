@echo off
setlocal

cd /d "%~dp0app"

if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
)

".venv\Scripts\python.exe" -m pip install -e ".[dev]"

if "%DOCGEN_WORKER_ID%"=="" (
  set "DOCGEN_WORKER_ID=local-worker"
)

start "DocGen worker" cmd /k ".venv\Scripts\python.exe -m docgen.jobs.worker"
".venv\Scripts\python.exe" -m uvicorn docgen.main:app --port 8000 --reload --reload-dir src
