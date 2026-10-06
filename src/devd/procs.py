"""Process table, process trees, listeners, and kills."""

from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import time

from .config import PROXY_PID_FILE
from .util import eprint, now


class Proc:
    __slots__ = ("pid", "ppid", "rss", "lstart", "command", "env")

    def __init__(self, pid: int, ppid: int, rss: int, lstart: str, command: str):
        self.pid = pid
        self.ppid = ppid
        self.rss = rss
        self.lstart = lstart
        self.command = command
        self.env: dict[str, str] = {}


def snapshot() -> dict[int, Proc]:
    """All processes with rss (KB), start time, command, and env (own processes only)."""
    out = subprocess.run(
        ["/bin/ps", "-axww", "-o", "pid=,ppid=,rss=,lstart=,command="],
        capture_output=True, text=True, check=False,
    ).stdout
    procs: dict[int, Proc] = {}
    for line in out.splitlines():
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        try:
            pid, ppid, rss = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        procs[pid] = Proc(pid, ppid, rss, " ".join(parts[3:8]), parts[8])
    env_out = subprocess.run(
        ["/bin/ps", "-xEww", "-o", "pid=,command="], capture_output=True, text=True, check=False,
    ).stdout
    for line in env_out.splitlines():
        head, _, rest = line.strip().partition(" ")
        if not head.isdigit():
            continue
        proc = procs.get(int(head))
        if proc is None or ("DEVD_ID=" not in rest and "PORTLESS_URL=" not in rest):
            continue
        for key in ("DEVD_ID", "PORTLESS_URL"):
            m = re.search(rf"(?:^|\s){key}=(\S+)", rest)
            if m:
                proc.env[key] = m.group(1)
    return procs


def descendants(procs: dict[int, Proc], root: int) -> set[int]:
    children: dict[int, list[int]] = {}
    for p in procs.values():
        children.setdefault(p.ppid, []).append(p.pid)
    seen: set[int] = set()
    stack = [root]
    while stack:
        pid = stack.pop()
        for c in children.get(pid, []):
            if c not in seen:
                seen.add(c)
                stack.append(c)
    return seen


def alive(procs: dict[int, Proc], pid: int | None, lstart: str | None) -> bool:
    if not pid or pid not in procs:
        return False
    return lstart is None or procs[pid].lstart == lstart


def family(procs: dict[int, Proc], rec: dict) -> set[int]:
    pids = {p.pid for p in procs.values() if p.env.get("DEVD_ID") == rec["id"]}
    if alive(procs, rec.get("runner_pid"), rec.get("runner_lstart")):
        pids.add(rec["runner_pid"])
        pids |= descendants(procs, rec["runner_pid"])
    return pids


def route_pids(rec: dict, procs: dict[int, Proc], routes: list[dict]) -> set[int]:
    """Live processes registered on this server's portless hostname, plus their trees.

    The route file is the source portless itself checks. A restart can miss the
    process in the tagged family (env scan truncated, runner start time drifted)
    and still need to stop whoever holds the name.
    """
    host = rec.get("hostname")
    if not host:
        return set()
    pids: set[int] = set()
    for route in routes:
        if route.get("hostname") != host:
            continue
        pid = route.get("pid")
        if not isinstance(pid, int) or pid not in procs:
            continue
        pids.add(pid)
        pids |= descendants(procs, pid)
    return pids


def tree_rss(procs: dict[int, Proc], pids: set[int]) -> int:
    return sum(procs[p].rss for p in pids if p in procs)


def kill_pids(pids: set[int], timeout: float = 6.0) -> None:
    me = os.getpid()
    pids = {p for p in pids if p not in (me, os.getppid(), 0, 1)}
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for p in pids:
            try:
                os.kill(p, sig)
            except ProcessLookupError:
                pass
            except PermissionError:
                eprint(f"devd: no permission to signal pid {p}")
        deadline = now() + (timeout if sig == signal.SIGTERM else 2.0)
        while now() < deadline:
            pids = {p for p in pids if pid_exists(p)}
            if not pids:
                return
            time.sleep(0.2)


def pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def listeners() -> dict[int, list[int]]:
    """pid -> listening TCP ports."""
    out = subprocess.run(
        ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpn"], capture_output=True, text=True, check=False,
    ).stdout
    result: dict[int, list[int]] = {}
    pid = None
    for line in out.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            m = re.search(r":(\d+)$", line)
            if m:
                ports = result.setdefault(pid, [])
                port = int(m.group(1))
                if port not in ports:
                    ports.append(port)
    return result


def cwd_of(pids: list[int]) -> dict[int, str]:
    if not pids:
        return {}
    out = subprocess.run(
        ["lsof", "-a", "-p", ",".join(map(str, pids)), "-d", "cwd", "-Fpn"],
        capture_output=True, text=True, check=False,
    ).stdout
    result: dict[int, str] = {}
    pid = None
    for line in out.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            result[pid] = line[1:]
    return result


def proxy_pid() -> int | None:
    try:
        return int(PROXY_PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def port_open(port: int) -> bool:
    for family_, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family_, socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                if s.connect_ex((host, port)) == 0:
                    return True
        except OSError:
            continue
    return False


def own_ancestors(procs: dict[int, Proc]) -> set[int]:
    result: set[int] = set()
    pid = os.getpid()
    while pid in procs and pid not in result and pid > 1:
        result.add(pid)
        pid = procs[pid].ppid
    return result


def cpu_seconds(root: int) -> dict[int, float]:
    """Cumulative CPU seconds per process in the tree under `root`."""
    out = subprocess.run(["/bin/ps", "-axo", "pid=,ppid=,time="], capture_output=True, text=True).stdout
    parents: dict[int, int] = {}
    cpu: dict[int, float] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        secs = 0.0
        for piece in parts[2].replace("-", ":").split(":"):
            secs = secs * 60 + float(piece)
        parents[pid], cpu[pid] = ppid, secs
    children: dict[int, list[int]] = {}
    for pid, ppid in parents.items():
        children.setdefault(ppid, []).append(pid)
    tree, todo = {}, [root]
    while todo:
        pid = todo.pop()
        if pid in cpu and pid not in tree:
            tree[pid] = cpu[pid]
            todo.extend(children.get(pid, []))
    return tree


def connections(port: int | None) -> frozenset[tuple[str, str]]:
    """TCP connections touching `port`, including TIME_WAIT ones (~30 s), so short requests are still seen."""
    if not port:
        return frozenset()
    out = subprocess.run(["netstat", "-anp", "tcp"], capture_output=True, text=True).stdout
    suffix = f".{port}"
    conns = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 6 and parts[0].startswith("tcp") and parts[5] != "LISTEN":
            if parts[3].endswith(suffix) or parts[4].endswith(suffix):
                conns.add((parts[3], parts[4]))
    return frozenset(conns)

