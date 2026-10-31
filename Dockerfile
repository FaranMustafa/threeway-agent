FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy THREEWAY_DB=/app/threeway.db
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY data ./data
COPY evals ./evals
RUN uv sync --locked --no-dev && uv run threeway seed
EXPOSE 8000
CMD ["uv", "run", "--no-dev", "uvicorn", "threeway.api:app", "--host", "0.0.0.0", "--port", "8000"]
