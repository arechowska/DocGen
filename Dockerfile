FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --system docgen \
    && useradd --system --gid docgen --create-home --home-dir /home/docgen docgen

COPY app/pyproject.toml ./pyproject.toml
COPY app/src ./src

RUN python -m pip install --upgrade pip \
    && python -m pip install .

RUN mkdir -p /app/var/data \
    && chown -R docgen:docgen /app /home/docgen

USER docgen

EXPOSE 8000

CMD ["uvicorn", "docgen.main:app", "--host", "0.0.0.0", "--port", "8000"]
