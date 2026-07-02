#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
llm-proxy Stack CLI Manager.
Manages the docker-compose services (litellm, headroom-proxy, image-gen-bridge, sync-endpoints)
and configures Claude Code settings in ~/.claude/settings.json.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent


BACKUP_PATH = PROJECT_DIR / ".claude_settings.bak"


def load_master_key() -> str:
    env_path = PROJECT_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("LITELLM_MASTER_KEY="):
                return line.partition("=")[2].strip().strip("\"'")
    return "PROXY_MANAGED"  # Default fallback


def backup_and_update_settings():
    settings_path = Path.home() / ".claude" / "settings.json"
    
    # 1. Read existing settings
    settings = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except Exception as e:
            print(f"⚠ Warning: Failed to parse existing Claude settings: {e}")

    # 2. Prepare keys to backup
    target_keys = [
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
    ]

    # Only create a backup if it doesn't already exist.
    if not BACKUP_PATH.exists():
        backup_data = {}
        env_dict = settings.get("env", {})
        for key in target_keys:
            if key in env_dict:
                backup_data[key] = env_dict[key]
            else:
                backup_data[key] = None
        
        try:
            BACKUP_PATH.write_text(json.dumps(backup_data, indent=2))
            print(f"📁 Backed up original Claude environment variables to {BACKUP_PATH}")
        except Exception as e:
            print(f"⚠ Warning: Failed to create Claude settings backup: {e}")

    # 3. Update with new settings
    master_key = load_master_key()
    new_keys = {
        "ANTHROPIC_AUTH_TOKEN": master_key,
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4-5",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME": "glm-5-flash",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-8",
        "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME": "glm-5.2",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-6",
        "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME": "glm-5-flash",
    }

    if "env" not in settings or not isinstance(settings["env"], dict):
        settings["env"] = {}
    settings["env"].update(new_keys)

    # Ensure ~/.claude directory exists
    if not settings_path.parent.exists():
        settings_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        settings_path.write_text(json.dumps(settings, indent=2))
        print(f"✅ Successfully updated Claude settings at {settings_path}")
    except Exception as e:
        print(f"❌ Failed to write Claude settings: {e}")


def restore_settings():
    settings_path = Path.home() / ".claude" / "settings.json"
    
    if not BACKUP_PATH.exists():
        print("ℹ No Claude settings backup found. Skipping restore.")
        return

    try:
        backup_data = json.loads(BACKUP_PATH.read_text())
        
        settings = {}
        if settings_path.exists():
            try:
                settings = json.loads(settings_path.read_text())
            except Exception as e:
                print(f"⚠ Warning: Failed to parse existing Claude settings: {e}")
                return

        env_dict = settings.get("env", {})
        for key, original_val in backup_data.items():
            if original_val is None:
                if key in env_dict:
                    del env_dict[key]
            else:
                env_dict[key] = original_val

        if "env" in settings and not settings["env"]:
            del settings["env"]
            
        settings_path.write_text(json.dumps(settings, indent=2))
        BACKUP_PATH.unlink()
        print(f"⏪ Restored original Claude environment variables from backup.")
    except Exception as e:
        print(f"❌ Failed to restore Claude settings: {e}")


def ensure_host_file(path: Path, default_content: str = ""):
    """Ensure a file exists on the host, cleaning up if Docker created it as a directory."""
    if path.exists():
        if path.is_dir():
            print(f"🧹 Removing invalid directory '{path.name}' (mis-created by Docker bind-mount)...")
            import shutil
            shutil.rmtree(path)
            path.write_text(default_content)
    else:
        print(f"📝 Creating empty file '{path.name}' to prevent directory bind-mount...")
        path.write_text(default_content)


def cmd_up(build=False):
    print("🚀 Starting docker-compose stack...")
    
    # 1. Check critical source files
    for critical_file in ["config.yaml", "tracker.py"]:
        p = PROJECT_DIR / critical_file
        if p.exists() and p.is_dir():
            print(f"❌ Error: '{critical_file}' is a directory! Please remove it manually (run 'rm -rf {critical_file}') and restore from git.")
            sys.exit(1)

    # 2. Ensure bind-mounted files exist as files, not directories
    ensure_host_file(PROJECT_DIR / "config.gen.yaml", "model_list: []\n")
    ensure_host_file(PROJECT_DIR / "token_limits.yaml", "models: {}\nfallbacks: {}\n")
    ensure_host_file(PROJECT_DIR / "litellm_token_usage.json", "{}\n")

    # Build up-d command
    cmd = ["docker", "compose", "up", "-d"]
    if build:
        cmd.append("--build")

    # Run docker compose
    try:
        subprocess.run(cmd, check=True, cwd=PROJECT_DIR)
        print("✅ Docker-compose stack started successfully.")
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to start docker-compose stack: {e}")
        sys.exit(1)

    # Update Claude settings
    backup_and_update_settings()



def cmd_down():
    print("🛑 Stopping docker-compose stack...")
    try:
        subprocess.run(["docker", "compose", "down"], check=True, cwd=PROJECT_DIR)
        print("✅ Docker-compose stack stopped successfully.")
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to stop docker-compose stack: {e}")
        sys.exit(1)

    # Restore Claude settings
    restore_settings()


def cmd_sync():
    print("🔄 Triggering endpoint synchronization...")
    import urllib.request
    try:
        req = urllib.request.Request("http://127.0.0.1:9100/sync", method="POST")
        with urllib.request.urlopen(req, timeout=10) as response:
            res_data = json.loads(response.read().decode())
            if res_data.get("ok"):
                print(f"✅ Sync complete. Synchronized {res_data.get('endpoints')} endpoints.")
            else:
                print(f"❌ Sync failed: {res_data}")
    except Exception as e:
        print(f"❌ Failed to connect to sync service: {e}")
        print("   Make sure the stack is running ('python start_proxy.py up').")


def cmd_status():
    print("📊 Stack Status:")
    try:
        subprocess.run(["docker", "compose", "ps"], check=True, cwd=PROJECT_DIR)
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to get docker-compose status: {e}")


def main():
    parser = argparse.ArgumentParser(description="llm-proxy CLI stack manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    up_parser = subparsers.add_parser("up", help="Start the stack and configure Claude settings")
    up_parser.add_argument("--build", action="store_true", help="Rebuild the Docker images")
    
    subparsers.add_parser("down", help="Stop the stack")
    subparsers.add_parser("sync", help="Trigger endpoint synchronization")
    subparsers.add_parser("status", help="Show the status of the stack services")

    args = parser.parse_args()

    if args.command == "up":
        cmd_up(build=args.build)
    elif args.command == "down":
        cmd_down()
    elif args.command == "sync":
        cmd_sync()
    elif args.command == "status":
        cmd_status()


if __name__ == "__main__":
    main()
