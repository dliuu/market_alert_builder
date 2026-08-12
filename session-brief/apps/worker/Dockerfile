# syntax=docker/dockerfile:1
# apps/worker image for Fly.io (M10). The worker is the long-running scheduler;
# Python owns the schema, so migrations run as a release step (see fly.toml).
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencies first, from the lockfile, so this layer caches across code changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Application code (package = false, so no build step — it's importable as-is).
COPY worker ./worker
COPY contracts ./contracts
COPY alembic ./alembic
COPY alembic.ini ./

# The unattended entrypoint: the self-rescheduling daily loop + dead-man's switch.
CMD ["uv", "run", "--no-dev", "--frozen", "-m", "worker.cli", "schedule"]
