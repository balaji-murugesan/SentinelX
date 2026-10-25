# Engine image. The one-shot `migrate` service reuses this image with a different command.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:0.11.33 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first, so this layer is cached until pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY src ./src

# Run as an unprivileged user.
RUN useradd --system --uid 10001 --no-create-home sentinelx
USER sentinelx

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src

CMD ["python", "-m", "engine.main"]
