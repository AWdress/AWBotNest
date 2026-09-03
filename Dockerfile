FROM node:22-slim AS frontend

WORKDIR /app
COPY VERSION ./VERSION
WORKDIR /app/frontend
# 不复制 Windows 生成的 lock：让 npm 按 Linux 平台解析 Rollup/Esbuild 原生可选依赖。
COPY frontend/package.json frontend/sync-version.js ./
RUN npm install
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

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md VERSION ./
COPY awbotnest ./awbotnest
COPY plugins ./plugins
COPY docker-entrypoint.sh ./docker-entrypoint.sh
COPY --from=frontend /app/static ./static

RUN pip install --no-cache-dir --no-deps . \
    && mkdir -p data sessions plugins

# Exercise the Linux startup wrapper and X server before publishing the image.
RUN sh /app/docker-entrypoint.sh python -c "import os, socket; s = socket.socket(socket.AF_UNIX); s.connect('/tmp/.X11-unix/X' + os.environ['DISPLAY'].split(':')[-1].split('.')[0]); s.close(); print('Xvfb startup OK')"

EXPOSE 18001
VOLUME ["/app/data", "/app/sessions", "/app/plugins"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18001/api/health', timeout=3)" || exit 1

ENTRYPOINT ["sh", "/app/docker-entrypoint.sh"]
CMD ["python", "-m", "awbotnest.main"]
