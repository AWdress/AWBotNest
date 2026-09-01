FROM node:22-slim AS frontend

WORKDIR /frontend
COPY frontend/package.json ./
COPY frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb xauth ca-certificates fonts-noto-cjk fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY awbotnest ./awbotnest
COPY plugins ./plugins
COPY --from=frontend /static ./static

RUN pip install --no-cache-dir . \
    && playwright install --with-deps chromium \
    && mkdir -p data sessions

EXPOSE 18001
VOLUME ["/app/data", "/app/sessions", "/app/plugins"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18001/api/health', timeout=3)" || exit 1

CMD ["xvfb-run", "-a", "-s", "-screen 0 1920x1080x24 -nolisten tcp", "python", "-m", "awbotnest.main"]
