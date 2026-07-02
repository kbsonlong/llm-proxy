#!/usr/bin/env python3
"""
sync_endpoints —— 同步火山方舟推理接入点到 litellm proxy 配置

基于 volcengine-python-sdk（管控面），未来可扩展：
  - ARKApi.create_endpoint   创建推理接入点
  - ARKApi.delete_endpoint   删除推理接入点
  - ARKApi.get_endpoint      查询接入点详情
  - ARKApi.stop_endpoint     停用接入点

用法：
  1. 设置环境变量
     export VOLC_AK="AKLxxxxxxxxxx"
     export VOLC_SK="xxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

  2. 运行
     python sync_endpoints.py                          # 生成 config.gen.yaml
     python sync_endpoints.py --output config.yaml      # 直接覆写
     python sync_endpoints.py --merge config.yaml       # 合并到已有配置文件
     python sync_endpoints.py --serve                   # 作为 HTTP 服务周期性同步

  3. 可选：自定义模型别名（传 --alias 指定映射文件）
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("缺少依赖，请运行:  pip install pyyaml", file=sys.stderr)
    sys.exit(1)

# ── 火山方舟管控面 SDK ─────────────────────────────────────────


def _build_ark_api() -> tuple[Any, str]:
    """用 VOLC_AK / VOLC_SK 初始化 ARKApi 客户端。

    返回 (ark_api, region)，region 用于后续构造 litellm base_url。
    """
    from volcenginesdkcore import ApiClient, Configuration
    from volcenginesdkark import ARKApi

    ak = os.environ.get("VOLC_AK")
    sk = os.environ.get("VOLC_SK")
    if not ak or not sk:
        print("错误: 请设置 VOLC_AK 和 VOLC_SK 环境变量", file=sys.stderr)
        sys.exit(1)

    region = os.environ.get("VOLC_REGION", "cn-beijing")

    config = Configuration()
    config.ak = ak
    config.sk = sk
    config.region = region
    config.scheme = "https"

    client = ApiClient(config)
    api = ARKApi(client)
    return api, region


# ── API 调用 ──────────────────────────────────────────────────


def list_endpoints() -> list[dict[str, Any]]:
    """调用 ARKApi.list_endpoints 获取所有 Running 状态的接入点。"""
    from volcenginesdkark import ListEndpointsRequest

    api, _region = _build_ark_api()

    req = ListEndpointsRequest()
    # 不设分页参数时 SDK 会用默认值取回所有

    resp = api.list_endpoints(req)
    # 响应结果结构: { "Items": [...], "Total": N, "PageNumber": ..., "PageSize": ... }
    items = resp.items or []
    return [item.to_dict() for item in items]


def to_snake(name: str) -> str:
    """camelCase → snake_case（ItemForListEndpointsOutput.to_dict() 返回 camelCase 键）。"""
    s = re.sub(r"([A-Z])", r"_\1", name).lower()
    return s.lstrip("_")


# ── 字段解析 ──────────────────────────────────────────────────


def extract_model_name(item: dict) -> str:
    """从 SDK 返回项中提取人类可读的模型名。

    to_dict() 输出结构：
      { "id": "...", "name": "...", "model_reference": {
          "foundation_model": { "name": "doubao-pro-32k", ... }
        }, "status": "Running", ... }
    """
    ref = item.get("model_reference") or {}
    fn = ref.get("foundation_model") or {}
    name = fn.get("name") or item.get("name", "")
    return str(name) or item.get("id", "unknown-model")


def extract_endpoint_id(item: dict) -> str:
    return str(item.get("id", "unknown-endpoint"))


def extract_status(item: dict) -> str:
    return str(item.get("status", "Unknown"))


def make_safe_alias(name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "-", name.lower().strip())
    safe = re.sub(r"-+", "-", safe)
    return safe.strip("-")


# ── token_limits.yaml 管理 ──────────────────────────────────

DEFAULT_MODEL_LIMIT = int(os.environ.get("DEFAULT_MODEL_LIMIT", "500000"))
"""每个新同步的模型默认配额（500,000 token）。用户修改过的不覆盖。"""


def resolve_model_name(
    eid: str, item: dict, aliases: dict[str, str]
) -> str:
    """确定最终的 model_name（别名优先）。"""
    return aliases.get(eid) or make_safe_alias(extract_model_name(item))


def sync_token_limits(
    items: list[dict],
    aliases: dict[str, str],
    limits_path: Path = Path("token_limits.yaml"),
) -> dict[str, int]:
    """同步新模型到 token_limits.yaml，已有用户修改过的配额不覆盖。

    返回值: { model_name: max_tokens } 完整字典，供 generate_config 等使用。
    """
    # 读取当前存在的配置
    limits_cfg: dict = {}
    models_cfg: dict[str, int] = {}
    if limits_path.exists():
        try:
            limits_cfg = yaml.safe_load(limits_path.read_text()) or {}
            models_cfg = {
                k: int(v) for k, v in (limits_cfg.get("models") or {}).items()
            }
        except Exception:
            pass

    # 收集本次同步的所有 Running 模型名
    active_models: set[str] = set()
    for item in items:
        if extract_status(item).upper() != "RUNNING":
            continue
        eid = extract_endpoint_id(item)
        model_name = resolve_model_name(eid, item, aliases)
        active_models.add(model_name)

    # 已存在配置中的模型（用户可能改过配额的）保留不动
    # 只补充新出现的模型
    changed = False
    for m in sorted(active_models):
        if m not in models_cfg:
            models_cfg[m] = DEFAULT_MODEL_LIMIT
            changed = True
            print(f"  [配额] 新模型 {m:40s} → 默认 {DEFAULT_MODEL_LIMIT:,} token")
        else:
            print(f"  [配额] {m:40s} → 已有 {models_cfg[m]:,} token（保留用户配置）")

    if changed:
        limits_cfg["models"] = dict(sorted(models_cfg.items()))
        # 保留原有的 global 字段
        if "global" not in limits_cfg:
            limits_cfg["global"] = None
        limits_path.write_text(
            yaml.dump(limits_cfg, default_flow_style=False, allow_unicode=True, sort_keys=False)
        )
        print(f"\n已更新 {limits_path.resolve()}")

    return models_cfg


# ── 别名映射 ──────────────────────────────────────────────────


def load_aliases(alias_path: Path) -> dict[str, str]:
    if not alias_path.exists():
        return {}
    with open(alias_path) as f:
        return yaml.safe_load(f) or {}


def load_existing_config(config_path: Path) -> list[dict]:
    if not config_path.exists():
        return []
    with open(config_path) as f:
        cfg = yaml.safe_load(f) or {}
    return cfg.get("model_list", [])


def merge_model_lists(
    new_list: list[dict], existing_list: list[dict]
) -> list[dict]:
    existing = {m["model_name"]: m for m in existing_list}
    for m in new_list:
        existing[m["model_name"]] = m
    return list(existing.values())


# ── 生成 litellm config ──────────────────────────────────────


def generate_config(
    items: list[dict],
    aliases: dict[str, str],
    merge_with: list[dict] | None = None,
) -> dict:
    """生成 litellm 可用的 model_list 配置字典。"""
    model_list: list[dict] = []

    for item in items:
        eid = extract_endpoint_id(item)
        status = extract_status(item)

        # 只包含 Running 状态的接入点
        if status.upper() != "RUNNING":
            ep_name = extract_model_name(item)
            print(f"  跳过 [{status}] {ep_name} ({eid})")
            continue

        model_name = aliases.get(eid) or make_safe_alias(
            extract_model_name(item)
        )

        model_list.append({
            "model_name": model_name,
            "litellm_params": {
                "model": f"volcengine/{eid}",
                "api_key": "os.environ/VOLCENGINE_API_KEY",
            },
        })

        print(f"  ✓ {model_name:40s} → volcengine/{eid}")

    if merge_with:
        model_list = merge_model_lists(model_list, merge_with)

    return {"model_list": model_list}


def write_config(
    config: dict,
    output: Path,
    header: str = "# 由 sync_endpoints 自动生成 — 请勿手动编辑\n",
) -> None:
    """写入 litellm 配置。"""
    result = {
        "model_list": config.get("model_list", [])
    }
    content = header + yaml.dump(
        result,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    output.write_text(content)
    print(f"\n已写入 {output.resolve()}")


# ── CLI ──────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="同步火山方舟推理接入点到 litellm proxy 配置"
    )
    parser.add_argument(
        "--output",
        "-o",
        default="config.gen.yaml",
        help="输出路径（默认 config.gen.yaml）",
    )
    parser.add_argument(
        "--alias",
        default="endpoint_aliases.yaml",
        help="别名映射文件（默认 endpoint_aliases.yaml，不存在则无别名）",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="以 HTTP 服务模式启动（POST /sync 触发同步）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9100,
        help="HTTP 服务端口（默认 9100）",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=0,
        help="定时同步间隔（秒，0 表示仅手动触发）",
    )
    args = parser.parse_args()

    if args.serve:
        return run_server(args)

    # 单次运行
    print("正在获取火山方舟推理接入点列表 ...\n")
    items = list_endpoints()
    print(f"共获取 {len(items)} 个接入点\n")

    aliases = load_aliases(Path(args.alias))
    if aliases:
        print(f"已加载 {len(aliases)} 个别名映射")

    # 默认同步 token_limits.yaml（新增模型给默认配额，已有不覆盖）
    print(f"\n同步 token_limits.yaml（新模型默认 {DEFAULT_MODEL_LIMIT:,} token）...")
    sync_token_limits(items, aliases, Path("token_limits.yaml"))

    config = generate_config(items, aliases, merge_with=None)
    write_config(config, Path(args.output))


# ── HTTP 服务模式（可选） ─────────────────────────────────────


def run_server(args):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class SyncHandler(BaseHTTPRequestHandler):
        def _do_sync(self):
            items = list_endpoints()
            aliases = load_aliases(Path(args.alias))
            sync_token_limits(items, aliases, Path("token_limits.yaml"))
            config = generate_config(items, aliases, merge_with=None)
            write_config(config, Path(args.output))
            return items

        def do_POST(self):
            if self.path != "/sync":
                self.send_response(404)
                self.end_headers()
                return
            try:
                items = self._do_sync()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps({"ok": True, "endpoints": len(items)}).encode()
                )
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())

        def log_message(self, fmt, *args):
            print(f"[sync-server] {fmt % args}", file=sys.stderr)

        def do_GET(self):
            if self.path == "/health":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
                return
            self.send_response(404)
            self.end_headers()

    server = HTTPServer(("0.0.0.0", args.port), SyncHandler)
    print(f"sync-endpoints 服务运行在 http://0.0.0.0:{args.port}")
    print(f"  POST /sync   — 触发同步")
    print(f"  GET  /health — 健康检查")

    if args.interval > 0:
        import threading

        def periodic_sync():
            while True:
                time.sleep(args.interval)
                print("\n[定时同步] 开始 ...")
                try:
                    items = list_endpoints()
                    sync_token_limits(
                        items,
                        load_aliases(Path(args.alias)),
                        Path("token_limits.yaml"),
                    )
                    config = generate_config(
                        items,
                        load_aliases(Path(args.alias)),
                        merge_with=None,
                    )
                    write_config(config, Path(args.output))
                    print(f"[定时同步] 完成，共 {len(items)} 个接入点")
                except Exception as e:
                    print(f"[定时同步] 失败: {e}", file=sys.stderr)

        t = threading.Thread(target=periodic_sync, daemon=True)
        t.start()
        print(f"  定时同步已启动，间隔 {args.interval}s")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")


if __name__ == "__main__":
    main()
