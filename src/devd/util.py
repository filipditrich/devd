"""Small helpers: actor detection, formatting, parsing."""

from __future__ import annotations

import os
import re
import sys
import time

from .config import AGENT_ENV_MARKERS, EXIT_USER_ONLY, HOME


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def now() -> float:
    return time.time()


def is_agent() -> bool:
    if os.environ.get("DEVD_ACTOR") == "agent":
        return True
    if os.environ.get("DEVD_ACTOR") == "user":
        return False
    return any(os.environ.get(k) for k in AGENT_ENV_MARKERS)


def actor() -> str:
    return "agent" if is_agent() else "user"


def human_bytes(kb: int) -> str:
    if kb <= 0:
        return "-"
    mb = kb / 1024
    return f"{mb / 1024:.1f}G" if mb >= 1024 else f"{mb:.0f}M"


def human_age(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600}h"


def short_path(p: str) -> str:
    home = str(HOME)
    return "~" + p[len(home):] if p.startswith(home) else p


def parse_size_mb(value: str) -> int:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([gGmM]?)[bB]?\s*", value)
    if not m:
        raise ValueError(f"bad size: {value}")
    n = float(m.group(1))
    return int(n * 1024) if m.group(2).lower() == "g" else int(n)


def parse_minutes(value: str) -> float:
    """Parses `15`, `15m`, `1h`, `30s`, `0`, or `off` into minutes (0 disables)."""
    v = value.strip().lower()
    if v in ("off", "never", "0", ""):
        return 0
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(s|m|min|h)?", v)
    if not m:
        raise ValueError(f"bad duration: {value}")
    n = float(m.group(1))
    return {"h": n * 60, "s": n / 60}.get(m.group(2) or "m", n)


def fmt_minutes(minutes: float) -> str:
    if not minutes:
        return "off"
    if minutes < 1:
        return f"{round(minutes * 60)}s"
    if minutes >= 60 and minutes % 60 == 0:
        return f"{int(minutes // 60)}h"
    return f"{minutes:g}m"


def user_only(what: str, command: str) -> int:
    eprint(f"devd: {what} is user-only. Agents must not do this.")
    eprint(f"Tell the user it is down and ask them to run: {command}")
    return EXIT_USER_ONLY

