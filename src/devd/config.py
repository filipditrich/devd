"""Paths, tunables, exit codes, and user settings."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

HOME = Path.home()
REPO_ROOT = Path(__file__).resolve().parents[2]
BIN = REPO_ROOT / "bin" / "devd"
HOOK_SCRIPT = REPO_ROOT / "guard" / "guard.mjs"

PORTLESS_DIR = HOME / ".portless"
DEVD_DIR = Path(os.environ.get("DEVD_STATE_DIR") or PORTLESS_DIR / "devd")
STATE_FILE = DEVD_DIR / "state.json"
LOCK_FILE = DEVD_DIR / "state.lock"
LOG_DIR = DEVD_DIR / "logs"
ROUTES_FILE = PORTLESS_DIR / "routes.json"
PROXY_PID_FILE = PORTLESS_DIR / "proxy.pid"

CONFIG_FILE = Path(os.environ.get("DEVD_CONFIG") or HOME / ".config" / "devd" / "config.json")

DEFAULT_CONFIG = {"max_rss_mb": 8192, "max_servers": 5, "agent_idle_min": 15, "user_idle_min": 0}
IDLE_TICK_S = float(os.environ.get("DEVD_IDLE_TICK_S") or 20)
IDLE_CPU_S = 0.3
IDLE_SAVE_S = 60
LOG_MAX_BYTES = 5 * 1024 * 1024
STARTUP_GRACE_S = 60
SIGNAL_CODES = {-1, -2, -9, -15, 129, 130, 137, 143}
AGENT_ENV_MARKERS = ("CURSOR_AGENT", "CLAUDECODE", "CODEX_THREAD_ID", "CODEX_SANDBOX")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_LEFT_DOWN = 3
EXIT_BUDGET = 4
EXIT_USER_ONLY = 5


def load_settings() -> dict:
    """User settings from ~/.config/devd/config.json (shared with the guard). Missing file means defaults."""
    try:
        data = json.loads(CONFIG_FILE.read_text())
    except FileNotFoundError:
        data = {}
    except json.JSONDecodeError as exc:
        raise SystemExit(f"devd: {CONFIG_FILE} is not valid JSON: {exc}") from exc
    return data if isinstance(data, dict) else {}


SETTINGS = load_settings()
WORK_DIRS = [str(Path(os.path.expanduser(d)).resolve()) for d in SETTINGS.get("work_dirs") or ["~"]]
DEV_SCRIPTS = ["dev", "start", "serve", *SETTINGS.get("dev_scripts", [])]


def find_portless() -> str:
    found = shutil.which("portless")
    if found:
        return found
    for candidate in (HOME / ".bun/bin/portless", Path("/opt/homebrew/bin/portless"), Path("/usr/local/bin/portless")):
        if candidate.exists():
            return str(candidate)
    return "portless"


PORTLESS_BIN = find_portless()

SHELL_META = re.compile(r"[|&;<>()$`\\\"'*?{}\n]")
DEV_CMD_RE = re.compile(
    r"\b(?:vite|next\s+dev|nest\s+start|nuxt\s+dev|astro\s+dev|email\s+dev|webpack\s+serve|"
    rf"(?:bun|pnpm|npm|yarn)(?:\s+run)?\s+(?:{'|'.join(map(re.escape, DEV_SCRIPTS))})\b|bun\s+(?:--hot|--watch)|tsx\s+watch)"
)
WRAPPER_RE = re.compile(r"^(?:\S*/)?(?:node|bun|pnpm|npm|yarn|sh|bash|turbo)\b")
AGENT_PROC_RE = re.compile(r"claude|codex|cursor|orca|paseo|opencode|gemini|\.vscode|\.app/|shell-snapshots|-l\b", re.I)
