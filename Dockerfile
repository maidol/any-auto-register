# Stage 1: 构建前端
FROM node:20-slim AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2: Python 后端 + 运行环境
FROM python:3.12-slim
ARG MIHOMO_VERSION=1.19.31
ARG TARGETARCH

# 系统依赖：Chromium、Xvfb、x11vnc、noVNC
RUN apt-get update && apt-get install -y --no-install-recommends \
    # 浏览器运行依赖
    chromium chromium-driver \
    # 虚拟显示 + VNC
    xvfb x11vnc \
    # noVNC 依赖
    novnc websockify \
    # 其他
    curl ca-certificates fonts-liberation libnss3 libatk-bridge2.0-0 \
    libdrm2 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libxkbcommon0 \
    libasound2 libpango-1.0-0 libcairo2 libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/*

# Install the pinned Mihomo release during image build.
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) asset="mihomo-linux-amd64-v${MIHOMO_VERSION}.gz"; sha256="d5e74bbddbdfff49a1aef7775bf5911da59f0d7196ed509a0ac914b3653dd5f1" ;; \
      arm64) asset="mihomo-linux-arm64-v${MIHOMO_VERSION}.gz"; sha256="9e0f11afbf38426b8bd88fdc594678f8161c57eccb4e1b77acb12b493904f1d4" ;; \
      *) echo "Unsupported Mihomo Docker architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    url="https://github.com/MetaCubeX/mihomo/releases/download/v${MIHOMO_VERSION}/${asset}"; \
    curl --fail --location --silent --show-error "$url" -o /tmp/mihomo.gz; \
    echo "${sha256}  /tmp/mihomo.gz" | sha256sum --check --status; \
    gzip -dc /tmp/mihomo.gz > /usr/local/bin/mihomo; \
    chmod 0755 /usr/local/bin/mihomo; \
    rm /tmp/mihomo.gz; \
    /usr/local/bin/mihomo -v

ENV MIHOMO_BIN=/usr/local/bin/mihomo

WORKDIR /app

# 安装 Python 依赖（包含 Solver 依赖：patchright, quart, rich）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 安装 patchright/playwright 浏览器（Solver 使用）
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps chromium

# 安装 camoufox 浏览器（Solver 的 camoufox 模式使用）
RUN python -m camoufox fetch

# 复制后端代码
ARG APP_VERSION=dev
COPY . .
# 注入版本号
RUN echo "__version__ = \"${APP_VERSION}\"" > core/version.py
# 不需要 .venv 和 frontend 源码
RUN rm -rf .venv frontend

# 复制前端构建产物
COPY --from=frontend-builder /app/static ./static

# 启动脚本
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# APP_PASSWORD: 运行时通过 -e APP_PASSWORD=xxx 设置
# 不设置则无密码保护（适用于本地使用）
ENV APP_PASSWORD=""

EXPOSE 8000 6080 8889

ENTRYPOINT ["/docker-entrypoint.sh"]
