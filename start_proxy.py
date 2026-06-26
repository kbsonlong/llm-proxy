#!/usr/bin/env python3
"""
Combined launcher: Headroom compression proxy + LiteLLM proxy.

Architecture:
  Hermes Agent → Headroom (:8787) → LiteLLM (:4000) → ARK / DeepSeek / ZhipuAI
                   ^ compression, memory, caching layer

Manages both processes as a single unit:
  - Starts LiteLLM first (upstream target)
  - Starts Headroom pointing to LiteLLM
  - Handles graceful shutdown of both on SIGINT/SIGTERM
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).parent

# ── Config ──────────────────────────────────────────────────────────────────
HEADROOM_PORT = int(os.environ.get("HEADROOM_PORT", "8787"))
LITELLM_PORT = int(os.environ.get("LITELLM_PORT", "4000"))
LITELLM_TIMEOUT = int(os.environ.get("LITELLM_TIMEOUT", "30"))  # seconds to wait

HEADROOM_BIN = str(PROJECT_DIR / "headroom_wrapper.py")
LITELLM_BIN = str(Path.home() / ".hermes/hermes-agent/venv/bin/litellm")
HERMES_PYTHON = str(Path.home() / ".hermes/hermes-agent/venv/bin/python")


def load_env():
    """Load .env from project root into os.environ (not overriding existing)."""
    env_path = PROJECT_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip("\"'")
        if key:
            os.environ.setdefault(key, val)


def wait_for_ready(url: str, timeout: int = 15, label: str = "service"):
    """Poll an HTTP endpoint until it responds (any status code)."""
    import urllib.request

    deadline = time.time() + timeout
    print(url)
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except (OSError, urllib.error.URLError):
            time.sleep(0.5)
    print(f"  ⚠ {label} not ready after {timeout}s", flush=True)
    return False


def main():
    load_env()

    # ── 1. Start LiteLLM (background) ──────────────────────────────────────
    config_files = [
        str(PROJECT_DIR / "config.yaml"),
        str(PROJECT_DIR / "config.gen.yaml"),
    ]
    litellm_cmd = [
        LITELLM_BIN,
        "--config", config_files[0],
        "--port", str(LITELLM_PORT),
        "--use_prisma_db_push",
        "--drop_params",
    ]
    # Add config.gen.yaml only if it exists
    if Path(config_files[1]).exists():
        litellm_cmd.insert(litellm_cmd.index("--port"), "--config")
        litellm_cmd.insert(litellm_cmd.index("--port"), config_files[1])

    os.environ.setdefault(
        "LITELLM_DATABASE_URL",
        f"sqlite:///{PROJECT_DIR / 'litellm.db'}",
    )

    print(f"\n{'='*60}", flush=True)
    print(f"  🚀 Starting llm-proxy stack", flush=True)
    print(f"{'='*60}", flush=True)
    print(f"  LiteLLM :{LITELLM_PORT}     ({' '.join(litellm_cmd)})", flush=True)
    print(f"  Headroom :{HEADROOM_PORT}   → LiteLLM :{LITELLM_PORT}", flush=True)
    print(f"  Hermes   → Headroom :{HEADROOM_PORT}  (via custom provider)", flush=True)
    print(f"{'='*60}\n", flush=True)

    litellm_proc = subprocess.Popen(
        litellm_cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    # Wait for LiteLLM to be ready
    litellm_url = f"http://127.0.0.1:{LITELLM_PORT}/health/readiness"
    litellm_ready = wait_for_ready(litellm_url, timeout=LITELLM_TIMEOUT, label="LiteLLM")
    if not litellm_ready:
        # Check if it started at all
        time.sleep(2)
        litellm_ready = wait_for_ready(litellm_url, timeout=10, label="LiteLLM (retry)")

    if not litellm_ready:
        print("  ❌ LiteLLM failed to start. Check logs above.", flush=True)
        print(f"     Run manually: {' '.join(litellm_cmd)}", flush=True)
        litellm_proc.kill()
        sys.exit(1)

    print(f"  ✅ LiteLLM ready on :{LITELLM_PORT}\n", flush=True)

    # ── 2. Start Headroom (foreground, handles both processes) ──────────────
    # Headroom now proxies to LiteLLM as the OpenAI-compatible backend.
    # Extra args from env (e.g. --memory --memory-storage=user)
    extra_args = os.environ.get("HEADROOM_EXTRA_ARGS", "").strip().split()
    headroom_cmd = [
        HERMES_PYTHON, HEADROOM_BIN,
        "proxy",
        "--backend", "openai",
        "--host", "0.0.0.0",
        "--port", str(HEADROOM_PORT),
    ]
    # Add extra args AFTER explicit flags so they can be overridden
    for arg in extra_args:
        if arg:
            headroom_cmd.append(arg)

    print(headroom_cmd)
    headroom_proc = subprocess.Popen(
        headroom_cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    # Wait for Headroom to be ready
    headroom_url = f"http://127.0.0.1:{HEADROOM_PORT}/stats"
    headroom_ready = wait_for_ready(headroom_url, timeout=15, label="Headroom")
    if headroom_ready:
        print(f"  ✅ Headroom ready on :{HEADROOM_PORT}\n", flush=True)
    else:
        print(f"  ⚠ Headroom may not be ready yet. Check logs.\n", flush=True)

    # ── 3. Forward both processes' stdout to our stdout ─────────────────────
    import threading

    def forward_output(proc: subprocess.Popen, prefix: str):
        try:
            for line in iter(proc.stdout.readline, ""):
                print(f"{prefix} {line.rstrip()}", flush=True)
        except (ValueError, OSError):
            pass

    threading.Thread(
        target=forward_output, args=(litellm_proc, "[litellm]"), daemon=True
    ).start()
    threading.Thread(
        target=forward_output, args=(headroom_proc, "[headroom]"), daemon=True
    ).start()

    # ── 4. Handle shutdown ──────────────────────────────────────────────────
    shutdown_requested = False

    def shutdown(signum, frame):
        nonlocal shutdown_requested
        if shutdown_requested:
            return  # already shutting down
        shutdown_requested = True
        print(f"\n  ⏳ Shutting down...", flush=True)
        # Headroom first (it's the entry point), then LiteLLM
        headroom_proc.terminate()
        try:
            headroom_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            headroom_proc.kill()
        litellm_proc.terminate()
        try:
            litellm_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            litellm_proc.kill()
        print(f"  ✅ Both proxies stopped.\n", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Wait for either process to exit (monitor mode)
    while True:
        time.sleep(1)
        if litellm_proc.poll() is not None:
            print(f"  ❌ LiteLLM exited (code {litellm_proc.returncode}). Shutting down Headroom...", flush=True)
            headroom_proc.terminate()
            break
        if headroom_proc.poll() is not None:
            print(f"  ❌ Headroom exited (code {headroom_proc.returncode}). Shutting down LiteLLM...", flush=True)
            litellm_proc.terminate()
            break

    for p in [headroom_proc, litellm_proc]:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
    sys.exit(1)


if __name__ == "__main__":
    main()
