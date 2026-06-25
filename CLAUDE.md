# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

litellm proxy 代理火山方舟（Volcengine ARK）模型，带累计 Token 上限控制。v2.0 新增多模态支持：图片生成、语音识别直接在 LiteLLM config 中配置，无需额外代理。

核心组件：

1. **sync_endpoints.py** — 调用火山方舟管控面 ListEndpoints API，查询推理接入点并生成为 litellm 兼容的 model_list 配置（config.gen.yaml）。可选 HTTP 服务定时同步。
2. **tracker.py** — litellm 的自定义 CustomLogger 回调，前置检查配额 + 后置累计 token（含图片生成和 ASR 请求的兼容处理）。
3. **config.yaml** — 主配置，除了 LLM 模型列表外，还包含图片生成和 ASR 模型条目（使用 `openai/` 前缀 + `api_base` 路由到 ARK 数据面）。

## 核心文件

| 文件 | 职责 | 归属 |
|------|------|------|
| `config.yaml` | litellm 主配置：tracker 回调 + 模型列表（含 LLM + 图片生成 + ASR） | 手动编辑 |
| `config.gen.yaml` | sync_endpoints 自动生成的 LLM 模型列表，勿手动编辑 | 自动生成 |
| `sync_endpoints.py` | 管控面同步工具：ListEndpoints → config.gen.yaml + token_limits.yaml | 主线代码 |
| `tracker.py` | 自定义回调：前置配额检查 + 后置用量累计 + 持久化 | 主线代码 |
| `token_limits.yaml` | 按模型配额 + 全局上限（含图片生成模型） | 手动编辑 |

## 模型类型

| 类型 | 配置方式 | 路由 |
|------|----------|------|
| LLM 聊天 | `volcengine/ep-xxxxxxxx` | ARK `/api/v3/chat/completions` |
| 图片生成 | `openai/doubao-seedream-xxx` + `api_base: https://ark.cn-beijing.volces.com/api/v3` | ARK `/api/v3/images/generations` |
| 语音识别 | `openai/<ark-endpoint-id>` + `api_base: https://ark.cn-beijing.volces.com/api/v3` | ARK `/api/v3/audio/transcriptions` |

图片生成和 ASR 共享 `VOLCENGINE_API_KEY`（与 LLM 相同），复用 `openai/` provider + custom `api_base` 路由到 ARK 数据面。

## 数据流

```
火山方舟管控面 API         火山方舟数据面 API
     │                          ▲
     ▼                          │
sync_endpoints.py               litellm proxy
     │                              │
     ├─→ config.gen.yaml      ┌────┴────┐
     │    (LLM model_list)     │ tracker ├─→ litellm_token_usage.json
     └─→ token_limits.yaml    └─────────┘
              │                     │
              └─────── 共享 ────────┘

                              multimodal requests
                                    │
                              litellm proxy (:4000)
                              ┌─────┼─────────┐
                              │     │         │
                         chat     img      audio
                       completions gen  transcriptions
                              │     │         │
                              ▼     ▼         ▼
                         ARK 数据面 API (/api/v3/)
```

## 关键设计决策

- **两套密钥体系**：管控面（ListEndpoints）用 AK/SK V4 签名认证；数据面（模型推理）用 API Key。
- **图片生成/ASR 不用额外代理**：通过 LiteLLM 的 `openai/` provider + `api_base` 直接路由到 ARK，复用同一 API Key。
- **tracker 兼容多模态**：图片生成/ASR 请求的 token 跟踪自动跳过（无 prompt/completion tokens），配额检查依然生效。
- **其他决策**：同上版（模型名自动 kebab-case、配额不覆盖、retry: false）。

## 常用命令

```bash
# 安装依赖
pip install -r requirements.txt

# 同步火山方舟模型列表 → 生成 config.gen.yaml
python sync_endpoints.py

# 同步并合并到 config.yaml（覆盖 config.yaml 内 model_list）
python sync_endpoints.py --merge

# 本地启动 litellm proxy（含图片生成 + ASR 支持）
litellm --config config.yaml --config config.gen.yaml --port 4000

# Docker 部署
python sync_endpoints.py
docker compose up -d

# ── 测试 LLM ────────────────────────────────────────────
curl http://localhost:4000/chat/completions \
  -H "Authorization: Bearer ${LITE...Y}" \
  -H "Content-Type: application/json" \
  -d '{"model": "doubao-seed-2-1-pro", "messages": [{"role": "user", "content": "你好"}]}'

# ── 测试图片生成 ─────────────────────────────────────────
curl http://localhost:4000/v1/images/generations \
  -H "Authorization: Bearer ${LITE...Y}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "doubao-seedream-5-0",
    "prompt": "一只可爱的小猫，卡通风格",
    "size": "1:1",
    "n": 1
  }'

# ── 测试语音识别（需先部署 ARK 语音推理接入点） ──────────
# curl http://localhost:4000/v1/audio/transcriptions \
#   -H "Authorization: Bearer ${LITE...Y}" \
#   -F "file=@test.wav" \
#   -F "model=<your-asr-model-name>"

# 其他：token 用量查看、手动触发 sync 同上版
```

## 环境变量

| 变量 | 用途 | 必须 |
|------|------|------|
| `VOLC_AK` | 火山方舟管控面 Access Key | 是 |
| `VOLC_SK` | 火山方舟管控面 Secret Key | 是 |
| `VOLCENGINE_API_KEY` | 火山方舟数据面 API Key（LLM + 图片 + ASR 共用） | 是 |
| `LITELLM_MASTER_KEY` | litellm 管理密钥 | 是 |
| `VOLC_REGION` | 地域 (默认 cn-beijing) | 否 |
| `TOKEN_TRACK_MODE` | APPEND / RESET | 否 |
| `TOKEN_DB_PATH` | 持久化路径 | 否 |
| `TOKEN_LIMITS_PATH` | 配额文件路径 | 否 |
| `DEFAULT_MODEL_LIMIT` | 新模型默认配额 | 否 |
| `LIST_ENDPOINTS_VERSION` | ListEndpoints API 版本 | 否 |

## 注意事项

- 启动时必须同时加载 `config.yaml` 和 `config.gen.yaml`，缺少 gen 文件 LLM 模型列表不可用。
- 图片生成和 ASR 模型配置在 `config.yaml` 中，**不受** `config.gen.yaml` 影响。
- sync_endpoints 合并时不会覆盖图片生成和 ASR 模型条目（它们在 `config.yaml` 中）。
- tracker 对非 LLM 请求（图片生成、ASR）的 token 统计自动跳过，但仍执行配额检查。
