# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.10.0 AS uv
FROM node:22-bookworm-slim AS mcp

WORKDIR /opt/atri-mcp
COPY deploy/mcp/package.json deploy/mcp/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund \
    --fetch-timeout=30000 --fetch-retries=1 \
    --fetch-retry-mintimeout=2000 --fetch-retry-maxtimeout=5000 --loglevel=http \
    && test -f node_modules/@digidai/mcp-website2markdown/dist/index.js \
    && test -f node_modules/@xzxzzx/bilibili-mcp/dist/index.js

FROM python:3.12-slim-bookworm AS python-build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable \
    && /app/.venv/bin/python -c "import atri_bot, aiohttp, mcp; print('Python dependencies ready')"

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PATH="/app/.venv/bin:${PATH}"
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=mcp /usr/local/bin/node /usr/local/bin/node
COPY --from=mcp /opt/atri-mcp /opt/atri-mcp
COPY --from=python-build /app/.venv /app/.venv
WORKDIR /app
COPY resources/daily_routines ./resources/daily_routines
COPY scripts/bilibili-dns.mjs ./scripts/bilibili-dns.mjs
COPY personal_info.txt ./personal_info.txt
COPY deploy/config.toml.template ./config.toml.template
RUN mkdir -p /app/data
EXPOSE 28080
STOPSIGNAL SIGTERM
ENTRYPOINT ["/app/.venv/bin/atri"]
CMD ["--config", "/app/config.toml", "serve"]
