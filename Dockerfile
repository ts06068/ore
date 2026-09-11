# syntax=docker/dockerfile:1
FROM node:24-alpine@sha256:50c8e8ca1d27439048670df5883f32d57cf81cff6233222c893fd0d9884cbd81 AS console
WORKDIR /build
COPY packages/sdk/package*.json packages/sdk/tsconfig.json ./packages/sdk/
COPY packages/sdk/src ./packages/sdk/src
RUN cd packages/sdk && npm ci --no-audit --no-fund && npm run build
COPY web/package*.json web/tsconfig.json web/vite.config.ts web/index.html ./web/
COPY web/src ./web/src
RUN cd web && npm ci --no-audit --no-fund && npm run build

# Exact Playwright image matches pyproject.toml; includes Chromium and OS dependencies.
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble@sha256:aa81288e738725378becba5b3e06cb0f3a7f012a610e87e8d767a090ea3f740d AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ORE_STATE_DIR=/var/lib/ore \
    ORE_WEB_DIR=/app/web/dist \
    ORE_MAX_WORKERS=0 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts/hatch_build.py ./scripts/hatch_build.py
COPY packages/ore-scholarly ./packages/ore-scholarly
COPY --from=console /build/web/dist ./web/dist
RUN python3 -m pip install --no-cache-dir . ./packages/ore-scholarly \
    && groupadd --gid 10001 ore \
    && useradd --uid 10001 --gid ore --create-home ore \
    && mkdir -p /var/lib/ore \
    && chown ore:ore /var/lib/ore
USER ore
EXPOSE 8765
HEALTHCHECK --interval=20s --timeout=5s --start-period=30s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4)" || exit 1
ENTRYPOINT ["ore"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8765", "--workers", "0"]

# Separate runtime roles: executors hold only private tmpfs browser/download state.
FROM runtime AS executor
ENV ORE_CONTAINER_EXECUTOR=1 \
    ORE_EXECUTOR_STATE_DIR=/tmp/ore-executor
HEALTHCHECK NONE
ENTRYPOINT ["python3", "-m", "ore.executor"]
CMD ["--server", "http://coordinator:8765"]

FROM runtime AS coordinator
ENV ORE_EXECUTION_BACKEND=remote
