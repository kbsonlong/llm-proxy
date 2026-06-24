"""
累计 Token 追踪器 —— litellm proxy 自定义回调

支持：
  - 按模型（model_name）独立配额限制
  - 全局上限兜底
  - 持久化到本地 JSON 文件，proxy 重启不丢失
  - RESET / APPEND 模式

配额配置文件（token_limits.yaml）示例：
  models:
    doubao-pro-32k:  500000      # 这个模型最多用 50 万 token
    doubao-lite-128k: 2000000    # 这个模型最多用 200 万 token
  global: 10000000               # 所有模型合计上限（不设则无）

用法：
  litellm_settings:
    callbacks: [tracker.token_tracker]

环境变量：
  TOKEN_TRACK_MODE  = RESET | APPEND（默认 APPEND）
  TOKEN_DB_PATH     = 持久化文件路径（默认 ~/.litellm_token_usage.json）
  TOKEN_LIMITS_PATH = 配额配置路径（默认 ./token_limits.yaml）
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger


def _load_yaml(path: Path) -> dict:
    """加载 YAML，失败返回空 dict。"""
    try:
        import yaml
    except ImportError:
        return {}
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


class TokenTracker(CustomLogger):
    """按模型 + 全局的累计 Token 追踪器。"""

    def __init__(self):
        super().__init__()

        self.mode = os.environ.get("TOKEN_TRACK_MODE", "APPEND").upper()
        self.db_path = Path(
            os.environ.get("TOKEN_DB_PATH", Path.home() / ".litellm_token_usage.json")
        )

        # ── 加载配额配置 ────────────────────────────────────────
        limits_path = Path(
            os.environ.get("TOKEN_LIMITS_PATH", "token_limits.yaml")
        )
        limits_cfg = _load_yaml(limits_path)

        # 按模型的配额: { model_name: max_tokens }
        self.model_limits: dict[str, int] = {
            k: int(v) for k, v in (limits_cfg.get("models") or {}).items()
        }

        # 全局配额（兜底）
        raw_global = limits_cfg.get("global")
        self.global_limit: int | None = int(raw_global) if raw_global is not None else None

        # ── 加载已用数据 ────────────────────────────────────────
        self.usage: dict = self._load()

        # 全局已用量
        self.global_used: int = self.usage.get("global_used", 0)

        # 按模型已用量: { model_name: used_tokens }
        self.model_used: dict[str, int] = self.usage.get("model_used") or {}

        # 如果 RESET 模式，在持久化中记下配额总量以供跨进程参考
        # 但实际值仍从 usage dict 读取

        print(
            f"[token_tracker] limits_file={limits_path} "
            f"global_limit={self.global_limit or '(none)'} "
            f"model_limits={self.model_limits or '(none)'} "
            f"db={self.db_path} mode={self.mode}"
        )

    # ── 持久化 ──────────────────────────────────────────────────

    def _load(self) -> dict:
        if self.db_path.exists():
            try:
                return json.loads(self.db_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {"global_used": 0, "model_used": {}}

    def _save(self) -> None:
        self.usage["global_used"] = self.global_used
        self.usage["model_used"] = self.model_used
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.write_text(json.dumps(self.usage, indent=2, ensure_ascii=False))

    # ── 配额检查 ────────────────────────────────────────────────

    def _check_limits(self, model: str | None) -> None:
        """检查全局 + 指定模型是否超限，超限抛 429。"""
        errors = {}

        # 全局检查
        if self.global_limit is not None and self.global_used >= self.global_limit:
            errors["global"] = {
                "used": self.global_used,
                "limit": self.global_limit,
            }

        # 按模型检查
        if model and model in self.model_limits:
            used = self.model_used.get(model, 0)
            limit = self.model_limits[model]
            if used >= limit:
                errors[model] = {"used": used, "limit": limit}

        if errors:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "token_limit_exceeded",
                    "limits": errors,
                },
            )

    # ── hook 实现 ───────────────────────────────────────────────

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        """请求前置检查。"""
        model = (data or {}).get("model")
        self._check_limits(model)
        return None

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        """请求成功后累计 token。"""
        usage = getattr(response_obj, "usage", None)
        if not usage:
            return

        # streaming 场景
        if kwargs.get("stream"):
            streaming_resp = kwargs.get("complete_streaming_response")
            if streaming_resp and hasattr(streaming_resp, "usage"):
                usage = streaming_resp.usage

        prompt = max(getattr(usage, "prompt_tokens", 0) or 0, 0)
        completion = max(getattr(usage, "completion_tokens", 0) or 0, 0)
        total = prompt + completion
        if total == 0:
            return

        # 优先用客户端请求时的原始 model_name，而非路由后的 endpoint ID
        model = (
            kwargs.get("proxy_server_request", {})
            .get("body", {})
            .get("model")
        ) or kwargs.get("litellm_model_name") or kwargs.get("model")
        print(f"[token_tracker] DEBUG model resolution: proxy_server_request.body.model={kwargs.get('proxy_server_request',{}).get('body',{}).get('model')!r}, litellm_model_name={kwargs.get('litellm_model_name')!r}, kwargs.model={kwargs.get('model')!r}, resolved={model!r}")

        # 累计全局
        self.global_used += total

        # 累计按模型
        if model:
            prev = self.model_used.get(model, 0)
            self.model_used[model] = prev + total

        self._save()

        # 超过 90% 时打印警告
        if model and model in self.model_limits:
            used = self.model_used[model]
            limit = self.model_limits[model]
            ratio = used / limit
            if ratio >= 0.90:
                print(
                    f"[token_tracker] WARNING: {model} "
                    f"used {used}/{limit} ({ratio:.1%})"
                )

        if self.global_limit:
            ratio = self.global_used / self.global_limit
            if ratio >= 0.90:
                print(
                    f"[token_tracker] WARNING: global "
                    f"used {self.global_used}/{self.global_limit} ({ratio:.1%})"
                )

    # ── 同步 fallback ──────────────────────────────────────────

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        import asyncio

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                self.async_log_success_event(kwargs, response_obj, start_time, end_time)
            )
        except RuntimeError:
            pass


# 模块级单例
token_tracker = TokenTracker()
