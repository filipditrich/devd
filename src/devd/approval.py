"""One-shot native approvals, serialized independently of the server state lock."""

from __future__ import annotations

import fcntl
import hashlib
import json
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass

from .config import DEVD_DIR

DIALOG_TIMEOUT = 120
QUEUE_TIMEOUT = 300
DENIAL_COOLDOWN = 600


@dataclass(frozen=True)
class StartApproval:
    key: str
    message: str
    exit_code: int


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@contextmanager
def approval_lock(name: str):
    """Bounded cross-process lock; never holds devd's state lock while waiting."""
    directory = DEVD_DIR / "approvals"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{name}.lock").open("a+") as handle:
        deadline = time.monotonic() + QUEUE_TIMEOUT
        acquired = False
        try:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.2)
            yield acquired
        finally:
            if acquired:
                fcntl.flock(handle, fcntl.LOCK_UN)


def native_dialog(message: str) -> str:
    """Arguments carry untrusted command text as data, never AppleScript source."""
    if sys.platform != "darwin":
        return "unavailable"
    script = f'''
on run argv
    set answer to display dialog (item 1 of argv) with title "devd — agent request" buttons {{"Reject", "Allow once"}} default button "Reject" with icon caution giving up after {DIALOG_TIMEOUT}
    if gave up of answer then return "expired"
    return button returned of answer
end run
'''
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script, message],
            capture_output=True, text=True, timeout=DIALOG_TIMEOUT + 5, check=False,
        )
    except subprocess.TimeoutExpired:
        return "expired"
    except OSError:
        return "unavailable"
    if result.returncode:
        return "unavailable"
    return {"Allow once": "allowed", "Reject": "rejected", "expired": "expired"}.get(result.stdout.strip(), "unavailable")


def request_start(request: StartApproval) -> str:
    print("devd: waiting for your approval in the native devd dialog (queued if another request is open). Keep this command alive; Allow once resumes it automatically. Rejection or expiry leaves the server down.", flush=True)
    with approval_lock("dialog") as acquired:
        if not acquired:
            return "queue timed out"
        path = DEVD_DIR / "approvals" / "decisions.json"
        try:
            decisions = json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            decisions = {}
        decisions = {k: v for k, v in decisions.items() if v["until"] > time.time()}
        if request.key in decisions:
            return f"{decisions[request.key]['result']} recently; no repeat dialog for 10 minutes"
        result = native_dialog(request.message)
        # Only refusals are cached. Allow is consumed by this caller, never shared.
        if result != "allowed":
            decisions[request.key] = {"result": result, "until": time.time() + DENIAL_COOLDOWN}
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(decisions))
        temp.replace(path)
        return result
