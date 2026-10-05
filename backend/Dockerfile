# Backend image: Python 3.12, dependencies from uv.lock, the source as-is.
# Speech models are not baked in: the Hugging Face, fastembed and Piper caches
# are mounted from the host (see docker-compose.yml), so the image stays small
# and the same files serve the host and the container.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY backend ./backend
COPY scripts ./scripts
COPY datasets/knowledge/manifest.yaml ./datasets/knowledge/manifest.yaml

EXPOSE 8010
CMD ["uv", "run", "--no-sync", "python", "-m", "backend.main"]
