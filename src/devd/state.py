"""The state file (servers and budget config) and portless routes."""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from pathlib import Path

from .config import DEFAULT_CONFIG, DEVD_DIR, LOCK_FILE, ROUTES_FILE, STATE_FILE


@contextmanager
def locked_state():
    DEVD_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_state()
        try:
            yield state
        finally:
            save_state(state)
            fcntl.flock(lock, fcntl.LOCK_UN)


def load_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("servers", {})
    state.setdefault("config", {})
    for k, v in DEFAULT_CONFIG.items():
        state["config"].setdefault(k, v)
    return state


def save_state(state: dict) -> None:
    DEVD_DIR.mkdir(parents=True, exist_ok=True)
    clean = {
        **state,
        "servers": {rid: {k: v for k, v in rec.items() if not k.startswith("_")} for rid, rec in state["servers"].items()},
    }
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=2, sort_keys=True))
    tmp.replace(STATE_FILE)


def read_routes() -> list[dict]:
    try:
        data = json.loads(ROUTES_FILE.read_text())
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def strip_private(state: dict) -> None:
    for rec in state["servers"].values():
        for k in [k for k in rec if k.startswith("_")]:
            del rec[k]


def tail(path: str, n: int) -> str:
    try:
        lines = Path(path).read_text(errors="replace").splitlines()
    except FileNotFoundError:
        return ""
    return "\n".join(lines[-n:])


def url_of(rec: dict) -> str | None:
    return f"https://{rec['hostname']}" if rec.get("hostname") else None

