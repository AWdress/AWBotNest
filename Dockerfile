# =============================================================================
# AWBotNest 平台镜像（多阶段构建）
#   stage 1: Node 构建 Vue 前端 → 产物在 webui/static
#   stage 2: Python 运行时，装依赖、拷代码与前端产物，直接 python main.py
# =============================================================================

# ---------- Stage 1: 前端构建 ----------
FROM node:22-slim AS frontend
# 镜像内镜像仓库布局，使 vite 的 outDir=../static 落到 /webui/static
WORKDIR /webui/frontend

# 先拷依赖清单，利用层缓存。
# 故意只拷 package.json、不拷 package-lock.json：lock 文件在 Windows 生成，
# 其可选依赖树缺 Linux 平台的原生包（rollup/esbuild 的 *-linux-* 二进制），
# 一旦带入，npm 在 Linux 上会漏装这些原生包，导致 vite 构建报
# "Cannot find module @rollup/rollup-linux-x64-gnu"。
# 不带 lock 让 npm install 按 Linux 平台重新解析，跨平台构建才稳。
COPY webui/frontend/package.json ./
RUN npm install

# 拷前端源码并构建（vite.config outDir=../static → /webui/static）
COPY webui/frontend/ ./
RUN npm run build


# ---------- Stage 2: Python 运行时 ----------
FROM python:3.13-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

# 系统依赖：ddddocr(onnxruntime/opencv 需 libgl/libglib)、wkhtmltopdf(imgkit)、CJK 字体、mysql 客户端
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget ca-certificates curl \
    libgl1 libglib2.0-0 libgomp1 \
    fontconfig libfontconfig1 libfreetype6 \
    libx11-6 libxext6 libxrender1 \
    fonts-wqy-zenhei fonts-wqy-microhei fonts-noto-cjk fonts-noto-color-emoji \
    default-mysql-client \
    && wget -q https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6.1-3/wkhtmltox_0.12.6.1-3.bookworm_amd64.deb \
    && (dpkg -i wkhtmltox_0.12.6.1-3.bookworm_amd64.deb || apt-get install -f -y) \
    && rm -f wkhtmltox_0.12.6.1-3.bookworm_amd64.deb \
    && rm -rf /var/lib/apt/lists/*

# 先装 Python 依赖（利用层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷项目代码
COPY . .

# 从前端构建阶段拷入已编译的静态产物
COPY --from=frontend /webui/static ./webui/static

# 运行时目录
RUN mkdir -p logs sessions db_file/SQLite db_file/dbflag data/kv

# 以 root 运行：bind-mount 进来的宿主目录(logs/sessions/db_file/data/plugins)
# 属主通常是 root，降权用户无写权限会崩溃，故直接以 root 运行容器。

# Web 控制台端口（与 config.WEB_UI_PORT 对齐，默认 18001）
EXPOSE 18001

CMD ["python", "main.py"]
