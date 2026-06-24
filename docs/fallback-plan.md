# Token 超限自动 Fallback 方案

## 背景

当前当模型 Token 配额耗尽时，tracker 在 `async_pre_call_hook` 中直接 raise HTTPException(429)，客户端收到错误。需要在配额耗尽时自动将请求 fallback 到其他模型，并考虑输入 token 大小做前置判断。

## 核心逻辑

在 `tracker.py` 的 `async_pre_call_hook` 中：

1. 当前模型超限 → 查 `fallbacks[model]` 获取候选链
2. 用 `litellm.acount_tokens()` 估算本次请求输入 token
3. 遍历候选链，找到 **配额充足且能装下本次请求** 的模型
4. 修改 `data["model"]` → litellm Router 透明转发

## Token 统计归属

修改 `data["model"]` 后，litellm Router 会按新 model_name 路由。`async_log_success_event` 中 `model_group` 从 `kwargs.litellm_params.metadata.model_group` 获取，该值由 Router 路由后设置，**等于实际调用的模型名**。因此 token 正确计入 fallback 模型，不会记到原始模型头上。

## fallback 判定流程

```
请求 model = A
    ↓
async_pre_call_hook:
① A 已用 ≥ 限额 → 超限
② litellm.acount_tokens(data["messages"]) → 估算输入 token ≈ N
③ 查 fallbacks[A] = [B, C]
④ 遍历候选链:
   → B: 已用量 + N ≤ 限额 → 选中 ✅
   → B: 已用量 + N > 限额 → 跳过，继续
   → C: 已用量 + N ≤ 限额 → 选中 ✅
   → 全部跳过 → 返回 429
⑤ data["model"] = 选中的模型
```

## 边界情况

| 场景 | 行为 |
|------|------|
| 全局超限 | 无条件 429（兜底，不做 fallback） |
| 模型超限 + 无 fallback 配置 | 429（保持现有行为） |
| 模型超限 + fallback 配额充足 | `data["model"]` → fallback，透明转发 |
| 模型超限 + fallback 剩余额度装不下本次请求 | 继续寻找下一个候选 |
| fallback 链全部超限 | 429，detail 包含完整超限链路 |
| acount_tokens 失败（网络超时等） | 降级为只检查历史用量（等于是 剩余 > 0 即可，不做输入预估） |
| 并发请求 | 每个请求有独立 data dict，无竞态；pre-check 和 post-accumulate 间的竞态是 tracker 已存在的问题，fallback 未引入新竞态 |

## 变更清单

### `tracker.py`

- **`__init__`**：新增 `self.fallbacks: dict[str, list[str]]` 从 `token_limits.yaml` 加载
- **`_async_pick_fallback(model, input_tokens)`**：新方法，遍历 fallback 链，检查 `已用量 + 输入量 ≤ 限额`
- **`async_pre_call_hook`**：重构配额检查逻辑
  - 全局超限 → 直接 429
  - 模型超限 → 尝试 fallback（含输入 token 预估）
- **`_check_limits`**：改为内部调用，拆分为纯历史检查

### `token_limits.yaml`

新增 `fallbacks` 区段：

```yaml
models:
  doubao-seed-1-6: 2000
  doubao-seed-2-1-pro: 500000
  doubao-smart-router: 500000
  glm-4-7: 500000
global: 9999999

fallbacks:
  doubao-seed-1-6: [doubao-smart-router, glm-4-7]
  doubao-seed-2-1-pro: [doubao-smart-router]
```

### 不做变更

- `config.yaml` — 不涉及
- `config.gen.yaml` — 自动生成，不涉及
- `sync_endpoints.py` — YAML 整体读写自动保留 fallbacks 字段
- `docker-compose.yml` — 不涉及
- `requirements.txt` — `litellm` 已包含 token 计数能力

## 验证方法

1. **基本 fallback**：设置 `doubao-seed-1-6` 限额为 0，请求 `doubao-seed-1-6`，观察返回正常（实际走了 fallback 模型）
2. **输入 token 前置检查**：设 `doubao-smart-router` 剩余配额 100，发送长 prompt（估算 >100），确认跳过 doubao-smart-router 继续寻找
3. **统计正确性**：请求后检查 `litellm_token_usage.json`，确认 token 计入 fallback 模型
4. **全部超限**：候选链所有模型全部超额，确认返回 429
5. **全局超限**：global 达到上限，确认直接 429 不走 fallback
6. **无 fallback 配置**：模型无 fallback 配置时超限，确认返回 429（保持现有行为）
