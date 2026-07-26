FROM python:3.13-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Install dependencies
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy source
COPY scripts/ ./scripts/
COPY web/ ./web/
COPY main_cli.py keys.example.json ./

# Create data directory
RUN mkdir -p data/uploads data/cache

EXPOSE 8765

CMD ["uv", "run", "uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8765"]
