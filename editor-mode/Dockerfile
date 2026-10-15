# Editor Mode for the server installation (deploy/server): dashboard, API,
# worker and migrations from the same image.
FROM ghcr.io/astral-sh/uv:0.13.1-python3.13-trixie-slim
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev && useradd --system --no-create-home editor
ENV PATH="/app/.venv/bin:$PATH"
USER editor
EXPOSE 8090
CMD ["editor", "serve", "--host", "0.0.0.0", "--port", "8090"]
