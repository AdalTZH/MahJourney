FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev
COPY mahjourney ./mahjourney
COPY migrations ./migrations
ENV PATH="/app/.venv/bin:$PATH"
CMD ["uvicorn", "mahjourney.main:app", "--host", "0.0.0.0", "--port", "8000"]

