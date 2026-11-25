# The Team Notes app alone: the service, the test page and the built docs site, on port
# 8000 inside. Postgres runs in its own container (docker-compose.yml), so each restarts,
# upgrades and is backed up on its own, and the database could be a managed service.
#
# Compose is the intended way to run it. Without compose, point it at any Postgres
# (or a SQLite file inside the container, lost with it):
#   docker build -t team-notes-app .
#   docker run --rm -p 127.0.0.1:8765:8000 -e DATABASE_URL=postgresql://user:pass@host:5432/db team-notes-app
#
# Notes on the image:
# - Size: docs/RUNNING.md records the measured size.
# - Not fully pinned: the base image tag below floats, so a rebuild picks up its security
#   updates (and could change behaviour). uv and every Python dependency are pinned
#   (uv.lock). To freeze the base too, use its digest:
#   `docker build --build-arg PYTHON_IMAGE=python:3.12-slim-trixie@sha256:<digest> .`
# - No database client libraries: psycopg's binary wheel carries its own libpq.

ARG PYTHON_IMAGE=python:3.12-slim-trixie

# 1. The runtime virtualenv, from the lock file, without the dev and docs groups.
FROM ${PYTHON_IMAGE} AS deps
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# 2. The docs site: the same environment plus the docs group, used only to build site/.
#    mkdocstrings reads app/ and tests/ statically; nothing here is imported or run.
FROM deps AS docs
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --group docs
COPY mkdocs.yml README.md ./
COPY scripts/docs_pages.py scripts/docs_hooks.py scripts/griffe_inherit.py scripts/
COPY docs docs
COPY app app
COPY tests tests
RUN NO_MKDOCS_2_WARNING=1 DISABLE_MKDOCS_2_WARNING=true .venv/bin/mkdocs build

# 3. Runtime: Python from the base image, the venv, the code and the built site. uv itself
#    is not copied: the venv is complete and the app runs from it directly.
FROM ${PYTHON_IMAGE}
RUN useradd --uid 10001 --no-create-home --no-log-init notes
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=deps /app/.venv /app/.venv
COPY --from=docs /app/site /app/site
COPY app app
COPY static static
COPY docker/entrypoint.sh /usr/local/bin/team-notes-entrypoint
USER notes
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]
ENTRYPOINT ["team-notes-entrypoint"]
