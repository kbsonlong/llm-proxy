#!/usr/bin/env python3
"""
Mini bridge: OpenAI-compatible images/generations → ARK doubao-seedream.

Accepts OpenAI-format requests (model=gpt-image-2, size=1024x1024),
auto-scales size to meet ARK minimum (≥1920×1920),
forwards to llm-proxy (LiteLLM), returns OpenAI-format response.

Usage:
  python3 image_gen_bridge.py [--port 4001]

Environment:
  VOLCENGINE_API_KEY or reads from ../llm-proxy/.env
  LITELLM_MASTER_KEY or reads from ../llm-proxy/.env
"""

import json
import os
import re
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import HTTPError

PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:4000")
MIN_PIXELS = 1920 * 1920  # 3,686,400


def _load_env_var(name: str) -> str:
    """Get env var, falling back to llm-proxy's .env."""
    val = os.environ.get(name, "")
    if val:
        return val
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _fix_size(size_str: str) -> str:
    """Ensure size meets ARK's minimum pixel requirement."""
    m = re.match(r"^(\d+)[xX](\d+)$", size_str.strip())
    if not m:
        return "1920x1920"
    w, h = int(m.group(1)), int(m.group(2))
    if w * h < MIN_PIXELS:
        scale = (MIN_PIXELS / (w * h)) ** 0.5
        w2 = int(w * scale)
        h2 = int(h * scale)
        return f"{w2}x{h2}"
    return size_str


class BridgeHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path not in ("/v1/images/generations", "/images/generations"):
            self._json(404, {"error": "Not found"})
            return

        auth_key = _load_env_var("LITELLM_MASTER_KEY")
        if not auth_key:
            self._json(500, {"error": "LITELLM_MASTER_KEY not found"})
            return

        body = self._read_body()
        if body is None:
            return

        # Translate request
        body["model"] = "gpt-image-2"  # proxy routes this to doubao-seedream
        if "size" in body:
            body["size"] = _fix_size(body["size"])

        # Forward to llm-proxy
        data = json.dumps(body).encode()
        headers = {
            "Authorization": f"Bearer {auth_key}",
            "Content-Type": "application/json",
        }
        req = Request(
            f"{PROXY_URL}/v1/images/generations",
            data=data,
            headers=headers,
            method="POST",
        )

        try:
            resp = urlopen(req, timeout=120)
            result = json.loads(resp.read().decode())
        except HTTPError as e:
            err_body = e.read().decode()[:500]
            try:
                err_json = json.loads(err_body)
            except Exception:
                err_json = {"error": err_body}
            self._json(e.code, err_json)
            return
        except Exception as e:
            self._json(500, {"error": str(e)})
            return

        self._json(200, result)

    def do_GET(self):
        if self.path in ("/health", "/v1/health"):
            self._json(200, {"status": "ok"})
            return
        if self.path in ("/v1/models", "/models"):
            # Return the models the OpenAI plugin expects
            self._json(200, {
                "data": [
                    {"id": "gpt-image-2", "object": "model"},
                    {"id": "gpt-image-2-low", "object": "model"},
                    {"id": "gpt-image-2-medium", "object": "model"},
                    {"id": "gpt-image-2-high", "object": "model"},
                ]
            })
            return
        self._json(404, {"error": "Not found"})

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw)
        except Exception as e:
            self._json(400, {"error": f"Invalid JSON: {e}"})
            return None

    def _json(self, code: int, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[bridge] {args[0]} {args[1]} {args[2]}\n")


def main():
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 4001
    if len(sys.argv) > 1 and sys.argv[1] == "--port":
        port = int(sys.argv[2])

    server = HTTPServer(("0.0.0.0", port), BridgeHandler)
    print(f"[bridge] Image gen bridge listening on :{port}")
    print(f"[bridge] Proxy target: {PROXY_URL}")
    print(f"[bridge] Auto-scales sizes to ≥{MIN_PIXELS}px ({1920}×{1920})")
    print(f"[bridge] Set OPENAI_BASE_URL=http://localhost:{port}/v1")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[bridge] Shutting down")
        server.server_close()


if __name__ == "__main__":
    main()
