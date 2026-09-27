FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev
COPY mahjourney ./mahjourney
COPY migrations ./migrations
COPY contracts ./contracts
RUN useradd --create-home --uid 10001 appuser
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER appuser
CMD ["uvicorn", "mahjourney.main:app", "--host", "0.0.0.0", "--port", "8000"]

