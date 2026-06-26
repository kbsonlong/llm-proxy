#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wrapper script to run Headroom compression proxy in front of LiteLLM.
Adapted from angrysky56/hermes-headroom with llm-proxy specific defaults.

Architecture:
  Hermes Agent → Headroom (:8787) → LiteLLM (:4000) → ARK / DeepSeek / ZhipuAI / ...
                                     ^ compression & memory layer

Usage:
  python headroom_wrapper.py [proxy|mcp|perf|...] [args...]
"""

import os
import sys

# ── Defaults for llm-proxy integration ──────────────────────────────────────
# Headroom listens on this port; Hermes points custom provider here.
HEADROOM_PORT = int(os.environ.get("HEADROOM_PORT", "8787"))
# LiteLLM runs here; Headroom proxies requests to it.
LITELLM_PORT = int(os.environ.get("LITELLM_PORT", "4000"))

# ── 1. Apply monkeypatches before importing cli.main ────────────────────────
try:
    custom_ttl = int(os.environ.get("HEADROOM_CCR_TTL", "3600"))

    import headroom.cache.compression_store as cs
    import headroom.ccr.mcp_server as ms
    import headroom.config as hc

    # Patch get_compression_store default TTL
    orig_get_compression_store = cs.get_compression_store

    def patched_get_compression_store(
        max_entries=1000, default_ttl=custom_ttl, backend=None
    ):
        return orig_get_compression_store(
            max_entries=max_entries, default_ttl=default_ttl, backend=backend
        )

    cs.get_compression_store = patched_get_compression_store

    # Patch CompressionEntry field default
    if hasattr(cs, "CompressionEntry"):
        cs.CompressionEntry.__dataclass_fields__["ttl"].default = custom_ttl

    # Patch CCRConfig default
    if hasattr(hc, "CCRConfig"):
        hc.CCRConfig.store_ttl_seconds = custom_ttl

    # Patch MCP Retrieve handler: return clean text, not raw JSON
    async def patched_handle_retrieve(self, arguments: dict) -> list:
        import json
        import re
        from mcp.types import TextContent

        hash_key = arguments.get("hash")
        if not hash_key:
            return [TextContent(type="text", text="Error: hash parameter is required")]

        query = arguments.get("query")
        visited = set()
        current_hash = hash_key
        result = {}

        while current_hash and current_hash not in visited:
            visited.add(current_hash)
            result = await self._retrieve_content(current_hash, query)

            if "error" not in result and not query and "original_content" in result:
                content = result["original_content"]
                if isinstance(content, str):
                    ccr_match = re.fullmatch(
                        r"\s*<<ccr:([a-fA-F0-9]+)[^>]*>>\s*", content
                    )
                    if ccr_match:
                        current_hash = ccr_match.group(1).lower()
                        continue
                return [TextContent(type="text", text=content)]
            break

        if "error" in result:
            return [TextContent(type="text", text=f"Error: {result['error']}")]

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    if hasattr(ms, "HeadroomMCPServer"):
        ms.HeadroomMCPServer._handle_retrieve = patched_handle_retrieve

    # Patch CompressionStore.store to prevent self-referential hash loops
    if hasattr(cs, "CompressionStore") and hasattr(cs.CompressionStore, "store"):
        orig_store = cs.CompressionStore.store

        def patched_store(self, original: str, compressed: str, **kwargs):
            import re
            if isinstance(original, str):
                ccr_match = re.fullmatch(r"\s*<<ccr:([a-fA-F0-9]+)[^>]*>>\s*", original)
                if ccr_match:
                    return ccr_match.group(1).lower()
            return orig_store(self, original, compressed, **kwargs)

        cs.CompressionStore.store = patched_store

except Exception as e:
    print(
        f"[headroom-wrapper] Warning: Failed to apply monkeypatches: {e}",
        file=sys.stderr,
    )

# ── 2. Delegate to the original headroom entry point ───────────────────────
from headroom.cli import main


if __name__ == "__main__":
    # Insert default backend flags when the `proxy` subcommand is used
    # without explicit --backend/--host/--port.
    if len(sys.argv) >= 2 and sys.argv[1] == "proxy":
        idx = 2  # after "proxy"
        known_flags = {"--backend", "--host", "--port"}
        has_any = any(f in sys.argv[idx:] for f in known_flags)
        if not has_any:
            # Insert after "proxy", before any other args
            sys.argv[idx:idx] = [
                "--backend", "openai",
                "--host", "127.0.0.1",
                "--port", str(HEADROOM_PORT),
            ]

    # Clean script name for Click help text
    if sys.argv[0].endswith("-script.pyw"):
        sys.argv[0] = sys.argv[0][:-11]
    elif sys.argv[0].endswith(".exe"):
        sys.argv[0] = sys.argv[0][:-4]

    sys.exit(main())
