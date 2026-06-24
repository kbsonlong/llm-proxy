# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

litellm proxy 代理火山方舟（Volcengine ARK）模型，带累计 Token 上限控制。

核心架构是两个独立组件：

1. **sync_endpoints.py** — 调用火山方舟管控面 ListEndpoints API，查询推理接入点列表并生成为 litellm 兼容的 model_list 配置（config.gen.yaml）。可选运行 HTTP 服务定时同步。
2. **tracker.py** — litellm 的 CustomLogger 回调，在请求前置拦截（async_pre_call_hook）检查配额，在请求成功后（async_log_success_event）累计 token 用量。按模型 + 全局阈值双重检查，超限返回 429。

两者通过 **token_limits.yaml** 共享配额配置：sync_endpoints 负责将新发现的模型加入配额文件（已有用户配置不覆盖），tracker 负责运行时读写该文件。

## 核心文件

| 文件 | 职责 | 归属 |
|------|------|------|
| `config.yaml` | litellm 主配置：注册 tracker 回调 + master_key + 模型列表。模型列表可手动维护，也可由 sync_endpoints 管理 | 手动编辑 |
| `config.gen.yaml` | sync_endpoints 自动生成的模型列表，勿手动编辑 | 自动生成 |
| `sync_endpoints.py` | 管控面同步工具：ListEndpoints → config.gen.yaml + token_limits.yaml | 主线代码 |
| `tracker.py` | 自定义回调：前置配额检查 + 后置用量累计 + 持久化 | 主线代码 |
| `token_limits.yaml` | 按模型配额 + 全局上限 | 手动编辑（sync_endpoints 会追加新模型） |
| `docker-compose.yml` | 三服务编排：postgres + litellm-proxy + sync-endpoints | 部署 |

## 数据流

```
火山方舟管控面 API         火山方舟数据面 API
     │                          ▲
     ▼                          │
sync_endpoints.py               litellm proxy
     │                              │
     ├─→ config.gen.yaml      ┌────┴────┐
     │    (model_list)         │ tracker ├─→ litellm_token_usage.json
     └─→ token_limits.yaml    └─────────┘
              │                     │
              └─────── 共享 ────────┘
```

## 关键设计决策

- **两套密钥体系**：管控面（ListEndpoints）用 AK/SK V4 签名认证；数据面（模型推理）用 API Key。对应环境变量 VOLC_AK/VOLC_SK 和 VOLCENGINE_API_KEY。
- **模型名自动命名**：取火山方舟 `foundation_model.name` 转 kebab-case。可通过 `endpoint_aliases.yaml` 按 Endpoint ID 覆盖。
- **配额不覆盖**：sync_endpoints 同步 token_limits.yaml 时，已有用户修改过配额的模型保留不动，只追加新模型。
- **tracker 模式**：APPEND（重启后延续历史用量） / RESET（每次启动清零）。
- **litellm Settings**：`retry: false` 避免自动重试突破 Token 限流。

## 常用命令

```bash
# 安装依赖
pip install -r requirements.txt

# 同步火山方舟模型列表 → 生成 config.gen.yaml
python sync_endpoints.py

# 同步并合并到 config.yaml（覆盖 config.yaml 内 model_list）
python sync_endpoints.py --merge

# HTTP 服务模式（每 3600s 自动同步，POST /sync 手动触发）
python sync_endpoints.py --serve --interval 3600

# 本地启动 litellm proxy
litellm --config config.yaml --config config.gen.yaml --port 4000

# Docker 部署
python sync_endpoints.py    # 先生产一次 config.gen.yaml
docker compose up -d

# 查看 token 用量
cat litellm_token_usage.json

# 手动触发同步（Docker HTTP 服务模式）
curl -X POST http://localhost:9100/sync

# 测试调用
curl http://localhost:4000/chat/completions \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"model": "doubao-pro-32k", "messages": [{"role": "user", "content": "你好"}]}'
```

## 环境变量

| 变量 | 用途 | 必须 |
|------|------|------|
| `VOLC_AK` | 火山方舟管控面 Access Key | 是 |
| `VOLC_SK` | 火山方舟管控面 Secret Key | 是 |
| `VOLCENGINE_API_KEY` | 火山方舟数据面 API Key | 是 |
| `LITELLM_MASTER_KEY` | litellm 管理密钥 | 是 |
| `VOLC_REGION` | 地域 (默认 cn-beijing) | 否 |
| `TOKEN_TRACK_MODE` | APPEND / RESET | 否 |
| `TOKEN_DB_PATH` | 持久化路径 | 否 |
| `TOKEN_LIMITS_PATH` | 配额文件路径 | 否 |
| `DEFAULT_MODEL_LIMIT` | 新模型默认配额 | 否 |
| `LIST_ENDPOINTS_VERSION` | ListEndpoints API 版本 | 否 |

## 注意事项

- 启动 litellm 时必须同时加载 `config.yaml` 和 `config.gen.yaml`（`--config config.yaml --config config.gen.yaml`），缺少 gen 文件会导致模型列表不可用。
- config.gen.yaml 由 sync_endpoints 自动生成，**请勿手动编辑**。
- sync_endpoints HTTP 服务监听 `0.0.0.0:9100`，无内置认证，生产环境建议配 nginx 反向代理。
- tracker 使用模块级单例 (`token_tracker = TokenTracker()`)，由 litellm 的 `callbacks: [tracker.token_tracker]` 加载。
