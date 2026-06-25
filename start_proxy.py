#!/usr/bin/env python3
"""启动 litellm proxy，自动加载 .env。"""
import os, subprocess
from pathlib import Path

env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip("\"'")
        if key:
            os.environ.setdefault(key, val)

litellm_bin = str(Path.home() / ".hermes/hermes-agent/venv/bin/litellm")
cmd = [
    litellm_bin,
    "--config", str(Path(__file__).parent / "config.yaml"),
    "--config", str(Path(__file__).parent / "config.gen.yaml"),
    "--port", "4000",
]
os.environ.setdefault("LITELLM_DATABASE_URL", f"sqlite:///{Path(__file__).parent / 'litellm.db'}")
print(f"[start_proxy] Starting: {' '.join(cmd)}", flush=True)
subprocess.run(cmd)
