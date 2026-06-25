#!/usr/bin/env bash
# 启动 litellm proxy，自动加载 .env 环境变量
set -a  # 自动 export 所有变量
source /Users/kbsonlong/llm-proxy/.env
set +a

exec /Users/kbsonlong/.hermes/hermes-agent/venv/bin/litellm \
    --config /Users/kbsonlong/llm-proxy/config.yaml \
    --port 4000
