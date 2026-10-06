"""The per-server runner process: spawn, wait, idle auto-stop."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import (
    EXIT_ERROR,
    EXIT_OK,
    IDLE_CPU_S,
    IDLE_SAVE_S,
    IDLE_TICK_S,
    LOG_DIR,
    LOG_MAX_BYTES,
    PORTLESS_BIN,
    BIN,
    SHELL_META,
)
from .util import fmt_minutes, now
from .state import load_state, locked_state, strip_private, url_of
from .procs import connections, cpu_seconds, kill_pids, port_open, snapshot
from .model import classify_exit, idle_limit_s, mark_stopped, refresh


def build_argv(name: str, cmd: str, raw: bool = False, force: bool = False) -> list[str]:
    inner = shlex.split(cmd) if not SHELL_META.search(cmd) else ["/bin/sh", "-c", cmd]
    if raw:
        return inner
    argv = [PORTLESS_BIN, "run", "--name", name]
    if force:
        argv.append("--force")
    return [*argv, "--", *inner]


def cmd_run(args: argparse.Namespace) -> int:
    """Internal: session leader for one server. Waits for it and records how it ended."""
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    with locked_state() as state:
        rec = state["servers"].get(args.id)
        if rec is None:
            return EXIT_ERROR
        argv = build_argv(rec["name"], rec["cmd"], rec.get("raw", False), rec.get("force", False))
        cwd = rec["cwd"]
    child = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL)

    def forward(signum, _frame):
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    code = watch(args.id, child)
    print(f"\n[devd] {time.strftime('%Y-%m-%d %H:%M:%S')} process exited with code {code}", flush=True)
    with locked_state() as state:
        rec = state["servers"].get(args.id)
        if rec and rec.get("runner_pid") == os.getpid() and rec["status"] in ("starting", "running"):
            classify_exit(rec, code)
    return EXIT_OK


class Activity:
    """Detects use of a server: CPU spent by its tree, log output, or new connections."""

    def __init__(self, root: int, log: str) -> None:
        self.root = root
        self.log = log
        self.cpu: dict[int, float] = {}
        self.log_size = self.size()
        self.conns: frozenset[tuple[str, str]] = frozenset()

    def size(self) -> int:
        try:
            return os.path.getsize(self.log)
        except OSError:
            return 0

    def poll(self, port: int | None) -> bool:
        cpu = cpu_seconds(self.root)
        spent = sum(max(0.0, v - self.cpu.get(pid, 0.0)) for pid, v in cpu.items())
        self.cpu = cpu
        size = self.size()
        grew = size != self.log_size
        self.log_size = size
        conns = connections(port)
        fresh = bool(conns - self.conns)
        self.conns = conns
        return spent >= IDLE_CPU_S or grew or fresh


def watch(rid: str, child: subprocess.Popen) -> int:
    """Waits for the server; stops it once it has been idle longer than its limit."""
    activity: Activity | None = None
    last_active = now()
    saved = 0.0
    while True:
        try:
            return child.wait(timeout=IDLE_TICK_S)
        except subprocess.TimeoutExpired:
            pass
        state = load_state()
        rec = state["servers"].get(rid)
        if not rec or rec.get("runner_pid") != os.getpid() or rec["status"] not in ("starting", "running"):
            continue
        if activity is None:
            activity = Activity(child.pid, rec["log"])
        if activity.poll(rec.get("port")):
            last_active = now()
        last_active = max(last_active, rec.get("last_active", 0))
        limit = idle_limit_s(rec, state["config"])
        if limit and now() - last_active >= limit:
            idle_stop(rid, limit)
            continue
        if last_active - saved >= IDLE_SAVE_S or not saved:
            with locked_state() as st:
                r = st["servers"].get(rid)
                if r and r.get("runner_pid") == os.getpid():
                    r["last_active"] = max(last_active, r.get("last_active", 0))
            saved = last_active


def idle_stop(rid: str, limit: int) -> None:
    procs = snapshot()
    with locked_state() as state:
        rec = state["servers"].get(rid)
        if not rec or rec.get("runner_pid") != os.getpid() or rec["status"] not in ("starting", "running"):
            return
        pids, _ = mark_stopped(rec, procs, "idle")
        rec["idle_limit_min"] = limit / 60
        url = url_of(rec) or rid
        strip_private(state)
    pids.discard(os.getpid())
    print(f"\n[devd] {time.strftime('%Y-%m-%d %H:%M:%S')} no activity for {fmt_minutes(limit / 60)}, stopping", flush=True)
    kill_pids(pids)
    notify(f"Stopped {rid} after {fmt_minutes(limit / 60)} idle", f"{url} · restart: devd up {rid}")


def notify(title: str, body: str) -> None:
    script = f"display notification {json.dumps(body)} with title \"devd\" subtitle {json.dumps(title)}"
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10, check=False)


def spawn(rec: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = Path(rec["log"])
    if log_path.exists() and log_path.stat().st_size > LOG_MAX_BYTES:
        log_path.unlink()
    env = dict(os.environ)
    env["DEVD_ID"] = rec["id"]
    env.pop("DEVD_ACTOR", None)
    for key in ("PORT", "PORTLESS_URL"):
        env.pop(key, None)
    with open(log_path, "ab") as log:
        header = f"\n[devd] {time.strftime('%Y-%m-%d %H:%M:%S')} start by {rec['started_by']}: {rec['cmd']}  (cwd {rec['cwd']})\n"
        log.write(header.encode())
        log.flush()
        proc = subprocess.Popen(
            [sys.executable, str(BIN), "_run", rec["id"]],
            cwd=rec["cwd"], env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True,
        )
    time.sleep(0.15)
    procs = snapshot()
    rec["runner_pid"] = proc.pid
    rec["runner_lstart"] = procs[proc.pid].lstart if proc.pid in procs else None


def wait_ready(rid: str, timeout: float) -> dict:
    deadline = now() + timeout
    rec: dict = {}
    while True:
        procs = snapshot()
        with locked_state() as state:
            refresh(state, procs)
            rec = dict(state["servers"][rid])
            strip_private(state)
        if rec["status"] not in ("starting", "running"):
            return rec
        if rec.get("port") and port_open(rec["port"]):
            rec["_ready"] = True
            return rec
        if now() >= deadline:
            return rec
        time.sleep(0.5)

