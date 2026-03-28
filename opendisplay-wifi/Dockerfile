FROM python:3.13-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copy project files
COPY pyproject.toml ./
COPY server.py ./
COPY templates/ ./templates/

# Install dependencies using uv
RUN uv sync --no-dev

# Expose ports: web UI + OpenDisplay protocol
EXPOSE 8099 2446

CMD ["uv", "run", "python", "server.py"]
