# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
ARG NODE_IMAGE=node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.12@sha256:73d2665b478d8fa2de1cf105c6841f8e9cb6b09e568fc7700440c09f8fcd7ac4
ARG POSTGRES_IMAGE=postgres:17-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826

FROM ${NODE_IMAGE} AS frontend
WORKDIR /frontend
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY scripts/vendor-assets.mjs ./scripts/vendor-assets.mjs
RUN npm run build

FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS dependencies
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project

FROM dependencies AS scan-dependencies
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --group scanner --no-install-project
COPY scripts/vendor-spdx.py /tmp/vendor-spdx.py
RUN python /tmp/vendor-spdx.py /opt/spdx/templates.json

FROM ${PYTHON_IMAGE} AS app-base
RUN apt-get -o Acquire::Retries=3 update && apt-get -o Acquire::Retries=3 install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin app
WORKDIR /app
ENV PATH=/app/.venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    WORK_ROOT=/app/var/work PYTHONPATH=/app
COPY --from=dependencies /app/.venv /app/.venv
COPY . /app
COPY --from=frontend /frontend/static/vendor /app/static/vendor
RUN chmod +x /app/scripts/entrypoint.sh /app/deploy/backup/*.sh \
    && mkdir -p /app/var/work /app/staticfiles \
    && chown -R app:app /app/var /home/app \
    && DJANGO_DEBUG=1 DATABASE_SQLITE=1 LOCAL_MEMORY_CACHE=1 python manage.py collectstatic --noinput
USER app
ENTRYPOINT ["/app/scripts/entrypoint.sh"]

FROM app-base AS web
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--forwarded-allow-ips", "", "--workers", "4", "--threads", "2", "--timeout", "60", "--graceful-timeout", "30", "--access-logfile", "-", "--access-logformat", "%(h)s %(m)s %(s)s %(L)s", "--error-logfile", "-"]

FROM app-base AS scanner
ENV SCANCODE_CACHE=/home/app/.cache/scancode/runtime \
    SCANCODE_LICENSE_INDEX_CACHE=/home/app/.cache/scancode/license-index \
    SCANCODE_PACKAGE_INDEX_CACHE=/home/app/.cache/scancode/package-index
USER root
RUN apt-get -o Acquire::Retries=3 update && apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
    libmagic1 libgomp1 libxml2 libzstd1 libbz2-1.0 liblzma5 zlib1g openjdk-17-jre-headless \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /home/app/.cache/scancode \
    && chown -R app:app /home/app
COPY --from=scan-dependencies /app/.venv /app/.venv
COPY --from=scan-dependencies /opt/spdx /opt/spdx
USER app
CMD ["celery", "-A", "config", "worker", "--loglevel=INFO", "--queues=scan", "--concurrency=2", "--max-tasks-per-child=10"]

FROM scanner AS scanner-test
# Validation-only target, never published as the production scan worker.
USER root
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --group scanner --no-install-project
USER app
CMD ["python", "-m", "pytest", "-p", "no:cacheprovider", "-m", "scancode", "tests/test_licensecheck.py", "tests/test_ingestion.py"]

FROM ${POSTGRES_IMAGE} AS postgres
RUN apt-get -o Acquire::Retries=3 update && apt-get -o Acquire::Retries=3 install -y --no-install-recommends pgbackrest python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /etc/pgbackrest /var/log/pgbackrest /var/spool/pgbackrest /var/lib/restore /var/lib/namelist-backup /app/deploy/backup \
    && chown -R postgres:postgres /etc/pgbackrest /var/log/pgbackrest /var/spool/pgbackrest /var/lib/restore /var/lib/namelist-backup
COPY deploy/backup /app/deploy/backup
COPY deploy/postgres/init-app-role.sh /docker-entrypoint-initdb.d/10-app-role.sh
COPY deploy/postgres/init-backup-stanza.sh /docker-entrypoint-initdb.d/20-backup-stanza.sh
COPY deploy/postgres/entrypoint.sh /usr/local/bin/namelist-postgres-entrypoint
RUN chmod +x /app/deploy/backup/*.sh /docker-entrypoint-initdb.d/*.sh /usr/local/bin/namelist-postgres-entrypoint
ENTRYPOINT ["namelist-postgres-entrypoint"]
CMD ["postgres", "-c", "wal_level=replica", "-c", "archive_mode=on", "-c", "archive_timeout=300"]

FROM postgres AS maintenance
# Both bases use Debian bookworm; keep the exact Python 3.12 interpreter and application dependencies.
COPY --from=app-base /usr/local /usr/local
COPY --from=app-base /app /app
ENV PATH=/app/.venv/bin:/usr/lib/postgresql/17/bin:$PATH PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 WORK_ROOT=/var/lib/restore/work
RUN mkdir -p /var/lib/restore/work && chown -R postgres:postgres /var/lib/restore
WORKDIR /app
USER postgres
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["/app/deploy/backup/maintenance.sh"]
