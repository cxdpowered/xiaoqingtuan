# 小青团 Agent 镜像（NoneBot + LangGraph Agent Core，架构 §10.4）
# 基础镜像 python:3.13-slim + uv；将来启用 Playwright 时改用 playwright 基础镜像。
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# uv：快速可复现依赖安装。
RUN pip install --no-cache-dir uv

WORKDIR /app

# 先装依赖（利用层缓存）。仅装依赖、不装本项目（源码用 PYTHONPATH/cwd 即可）。
# mcp 客户端始终装入：内置 ccnu_library 功能组依赖它接入外部 MCP 服务。
# bge-small / torch 较重且可选：构建时 --build-arg INSTALL_EMBEDDINGS=true 才装。
COPY pyproject.toml uv.lock ./
ARG INSTALL_EMBEDDINGS=false
RUN if [ "$INSTALL_EMBEDDINGS" = "true" ]; then \
        uv sync --frozen --no-dev --no-install-project --extra mcp --extra embeddings; \
    else \
        uv sync --frozen --no-dev --no-install-project --extra mcp; \
    fi

# 项目源码与 wiki 初始内容。
COPY . .

# 让后续命令默认使用 venv，并把项目根加入 import 路径。
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app \
    XQT_DATA_DIR=/data \
    XQT_WIKI_DIR=/app/wiki \
    HF_HOME=/data/hf_cache

# 派生数据卷（SQLite / LanceDB / HF 模型缓存）。
VOLUME ["/data"]

# 默认启动 NoneBot（QQ/微信接入）。冒烟测试可改跑 CLI：
#   docker compose run --rm xiaoqingtuan python -m src.channels.cli
CMD ["python", "bot.py"]
