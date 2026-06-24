# llm-proxy

通过 litellm 代理火山方舟（Volcengine ARK）模型，带累计 Token 上限控制。

## 项目结构

```
llm-proxy/
├── tracker.py              # Token 追踪器（litellm custom callback）
├── token_limits.yaml       # 按模型 Token 配额配置
├── sync_endpoints.py       # 火山方舟 Endpoint 同步工具
├── config.yaml             # litellm proxy 主配置
├── config.gen.yaml         # 自动生成的模型列表（勿手动编辑）
├── .env                    # 环境变量（密钥）
├── docker-compose.yml      # 一键启动（litellm-proxy + sync-endpoints）
├── Dockerfile.sync         # sync-endpoints 容器构建
├── requirements.txt        # Python 依赖
└── README.md
```

## 快速开始

### 1. 准备

```bash
# Python 依赖
pip install -r requirements.txt

# 复制环境变量模板（然后修改）
cp env.example .env
```

### 2. 配置 .env

```ini
# 管控面 AK/SK（用于 ListEndpoints API）
# 从火山引擎控制台 → "我的凭证" → "API 密钥管理" 获取
# 注意: 这是 AK/SK 对，不是单 API Key
VOLC_AK=AKLxxxxxxxxxxxx
VOLC_SK=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 数据面 API Key（用于实际调用模型推理）
# 从火山方舟控制台 → API Key 管理 获取
VOLCENGINE_API_KEY=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# litellm 管理密钥
LITELLM_MASTER_KEY=sk-your-master-key

# Token 上限（累计）
TOKEN_LIMIT=10000000

# 模式: APPEND（续接历史）| RESET（每次重置）
TOKEN_TRACK_MODE=APPEND
```

### 3. 获取模型列表并启动

```bash
# 单次同步（生成 config.gen.yaml）
python sync_endpoints.py

# 启动 litellm proxy（需叠加 config.gen.yaml 以加载模型列表）
litellm --config config.yaml --config config.gen.yaml --port 4000
```

### 4. 测试调用

```bash
curl http://localhost:4000/chat/completions \
  -H "Authorization: Bearer sk-your-master-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "doubao-pro-32k",        # ← 用 sync_endpoints 生成的 model_name
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

## Docker 部署

```bash
# 1. 准备 .env（见上方）

# 2. 首次手动同步（容器外的 config.gen.yaml 无写入权限问题）
python sync_endpoints.py

# 3. 启动全部服务
docker compose up -d

# 4. 手动触发同步（可选）
curl -X POST http://localhost:9100/sync

# 5. 查看 Token 使用情况
cat litellm_token_usage.json
```

## sync-endpoints 详细用法

```bash
# 单次同步生成 config.gen.yaml
python sync_endpoints.py

# 合并到已有 config.yaml
python sync_endpoints.py --merge

# 指定输出路径
python sync_endpoints.py -o /etc/litellm/config.yaml --merge

# HTTP 服务模式（可定时同步）
python sync_endpoints.py --serve --interval 3600
```

### 自动命名

模型名自动取火山方舟 `foundation_model.name`，转为 `kebab-case`。想看对应的 Endpoint ID，查看 `config.gen.yaml`。

## Token 配额

在 `token_limits.yaml` 中按模型设置累计 Token 上限：

```yaml
models:
  doubao-pro-32k:  500000      # 这个模型最多用 50 万 token
  doubao-lite-128k: 2000000    # 这个模型最多用 200 万 token
global: 10000000               # 全局上限（所有模型合计，可选）
```

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `TOKEN_TRACK_MODE` | `APPEND` | `APPEND` 接续历史；`RESET` 每次启动清零 |
| `TOKEN_DB_PATH` | `~/.litellm_token_usage.json` | 持久化文件路径 |
| `TOKEN_LIMITS_PATH` | `./token_limits.yaml` | 配额配置文件路径 |

超过上限后返回 429：
```json
{
  "error": "token_limit_exceeded",
  "used": 10000000,
  "limit": 10000000
}
```

## 说明

- **获取模型（ListEndpoints）** 和 **调用模型** 是两套不同的 API，因此需要两套不同的密钥
- 管控面 API（ListEndpoints）使用 AK/SK V4 签名认证
- 数据面 API（Chat Completion）使用 API Key 认证
- ListEndpoints 的 API 版本默认为 `2024-01-01`，可通过 `LIST_ENDPOINTS_VERSION` 环境变量自定义
- sync-endpoints HTTP 服务监听 `0.0.0.0:9100`，如需安全加固建议配合 nginx 反向代理
- 启动 litellm proxy 时必须同时加载 `config.gen.yaml`（模型列表居中文件）：`litellm --config config.yaml --config config.gen.yaml`

## litellm UI 管理面板

本项目默认关闭 UI 以降低资源消耗。如需使用 litellm 自带的管理面板（虚拟密钥、调用日志、用量图表），需启用 PostgreSQL。

### 恢复 UI（挂载 PostgreSQL）

```yaml
# docker-compose.yml 在 services: 下新增
services:
  postgres:
    image: agnohq/pgvector:18
    container_name: litellm-postgres
    environment:
      POSTGRES_USER: litellm
      POSTGRES_PASSWORD: litellm
      POSTGRES_DB: litellm
    volumes:
      - postgres_data:/var/lib/postgresql
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U litellm"]
      interval: 5s
      timeout: 5s
      retries: 5
    restart: unless-stopped
```

同时在 `litellm-proxy` 的 `environment` 中添加数据库连接：

```yaml
    environment:
      DATABASE_URL: "postgresql://litellm:litellm@postgres:5432/litellm"
    depends_on:
      postgres:
        condition: service_healthy
```

添加持久化卷声明：

```yaml
volumes:
  postgres_data:
```

### UI 访问

启动后访问 http://localhost:4000/gui，使用 `LITELLM_MASTER_KEY` 登录即可看到管理面板。
