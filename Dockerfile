FROM python:3.12-slim

# uv's official binary, no pip install needed
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Deps layer cached separately so app/ edits skip reinstalling them
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app

# exec + shell form: real $PORT expansion, proper SIGTERM handling
CMD exec uv run --no-sync uvicorn app.gateway.gateway:app --host 0.0.0.0 --port $PORT
