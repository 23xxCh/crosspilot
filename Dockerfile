FROM python:3.13-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.11.1 /uv /uvx /usr/local/bin/

# Install locked third-party dependencies first for layer caching.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy the complete runtime package, then install CrossPilot itself.
COPY crosspilot/ ./crosspilot/
COPY scripts/ ./scripts/
COPY web/ ./web/
COPY main_cli.py keys.example.json ./
RUN uv sync --frozen --no-dev

# Create data directory
RUN mkdir -p data/uploads data/cache

EXPOSE 8765

CMD ["uv", "run", "--no-sync", "uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8765"]
