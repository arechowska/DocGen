FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --system docgen \
    && useradd --system --gid docgen --create-home --home-dir /home/docgen docgen

# WeasyPrint (used by the PDF exporter) dlopen()s Pango/HarfBuzz at import
# time and does not ship them as Python wheels -- they must be present as
# system libraries or the worker process crashes on startup (see
# docgen.export.pdf). Package list is WeasyPrint's own documented "Debian
# >= 11, installing via pip wheels" set (doc.courtbouillon.org/weasyprint/
# stable/first_steps.html), confirmed to exist under python:3.12-slim's
# current Debian "trixie" base. fonts-dejavu-core is added on top so
# rendered PDFs (Cyrillic-heavy in this app) have an actual fallback
# typeface instead of silently rendering blank glyphs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libharfbuzz-subset0 \
        fonts-dejavu-core \
        libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

COPY app/pyproject.toml ./pyproject.toml
COPY app/src ./src

RUN python -m pip install --upgrade pip \
    && python -m pip install .

RUN mkdir -p /app/var/data \
    && chown -R docgen:docgen /app /home/docgen

USER docgen

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["uvicorn", "docgen.main:app", "--host", "0.0.0.0", "--port", "8000"]
