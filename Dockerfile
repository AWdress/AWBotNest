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
    tini xvfb xauth ca-certificates fonts-noto-cjk fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md VERSION ./
COPY awbotnest ./awbotnest
COPY plugins ./plugins
COPY --from=frontend /app/static ./static

RUN pip install --no-cache-dir --no-deps . \
    && mkdir -p data sessions plugins

# Keep xvfb-run away from PID 1: its X-server readiness handshake uses signals.
# Bound the check so a broken handshake fails the build instead of hanging it.
RUN ["timeout", "--kill-after=5s", "30s", "/usr/bin/tini", "-s", "-g", "--", "xvfb-run", "-a", "-e", "/dev/stderr", "-s", "-screen 0 1920x1080x24 -nolisten tcp", "python", "-c", "import os, socket; s = socket.socket(socket.AF_UNIX); s.settimeout(5); s.connect('/tmp/.X11-unix/X' + os.environ['DISPLAY'].split(':')[-1].split('.')[0]); s.close(); print('Xvfb startup OK')"]

EXPOSE 18001
VOLUME ["/app/data", "/app/sessions", "/app/plugins"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18001/api/health', timeout=3)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD ["xvfb-run", "-a", "-e", "/dev/stderr", "-s", "-screen 0 1920x1080x24 -nolisten tcp", "python", "-m", "awbotnest.main"]
