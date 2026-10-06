"""Server lifecycle rules: refresh, exit classification, budget, unmanaged servers."""

from __future__ import annotations

import re
import time

from .config import AGENT_PROC_RE, DEV_CMD_RE, HOME, LOG_DIR, SIGNAL_CODES, STARTUP_GRACE_S, WORK_DIRS, WRAPPER_RE
from .util import fmt_minutes, now
from .state import read_routes
from .procs import Proc, cwd_of, descendants, family, own_ancestors, proxy_pid, tree_rss
from .naming import default_cmd, find_record, git_root, unique_id


def refresh(state: dict, procs: dict[int, Proc]) -> None:
    routes = read_routes()
    for rec in state["servers"].values():
        fam = family(procs, rec)
        rec["_family"] = fam
        rec["_rss"] = tree_rss(procs, fam)
        route = next((r for r in routes if r.get("pid") in fam), None)
        if route:
            rec["hostname"] = route["hostname"]
            rec["port"] = route["port"]
        note_presence(rec, alive=bool(fam), has_route=route is not None)


def note_presence(rec: dict, alive: bool, has_route: bool) -> None:
    """Reconcile the recorded status with whether its process tree is actually alive.

    A record stuck at killed/failed/exited while the tree is still up would make
    the next start skip the kill and then lose the portless name to that process.
    """
    if alive and rec["status"] in ("killed", "failed", "exited"):
        rec["status"] = "running" if has_route or rec.get("port") else "starting"
        rec.pop("ended_at", None)
        rec.pop("exit_code", None)
        return
    if rec["status"] not in ("starting", "running"):
        return
    if alive:
        rec["status"] = "running" if has_route or rec["status"] == "running" else "starting"
    else:
        classify_exit(rec, None)


def classify_exit(rec: dict, code: int | None) -> None:
    uptime = now() - rec.get("started_at", now())
    rec["ended_at"] = now()
    rec["exit_code"] = code
    if code is not None and code in SIGNAL_CODES:
        rec["status"] = "killed"
    elif code is None:
        rec["status"] = "killed"
    elif uptime < STARTUP_GRACE_S:
        rec["status"] = "failed"
    else:
        rec["status"] = "exited"


def agent_may_start(rec: dict) -> bool:
    if rec["status"] in ("new", "failed"):
        return True
    return rec["status"] == "stopped" and rec.get("stopped_by") in ("agent", "idle")


def idle_limit_s(rec: dict, cfg: dict) -> int:
    """Seconds without activity before devd stops the server; 0 means never."""
    if rec.get("keep"):
        return 0
    return int(float(cfg.get(f"{rec.get('started_by', 'user')}_idle_min", 0)) * 60)


def down_reason(rec: dict) -> str:
    when = time.strftime("%H:%M", time.localtime(rec.get("ended_at") or rec.get("stopped_at") or now()))
    status = rec["status"]
    if status == "stopped" and rec.get("stopped_by") == "idle":
        return f"stopped after {fmt_minutes(rec.get('idle_limit_min', 0))} idle at {when}"
    if status == "stopped":
        return f"stopped by {rec.get('stopped_by', 'user')} at {when}"
    if status == "killed":
        return f"killed from outside devd at {when} (Activity Monitor, kill, or OOM)"
    if status == "exited":
        return f"exited on its own at {when} (code {rec.get('exit_code')})"
    if status == "failed":
        return f"failed during startup at {when} (code {rec.get('exit_code')})"
    return status


def unmanaged_units(state: dict, procs: dict[int, Proc], ports: dict[int, list[int]]) -> list[dict]:
    """Dev servers not started by devd: portless routes and raw dev listeners under the configured work dirs."""
    managed: set[int] = set()
    for rec in state["servers"].values():
        managed |= rec.get("_family", set())
    skip = managed | own_ancestors(procs)
    ppid = proxy_pid()
    if ppid:
        skip.add(ppid)

    units: list[dict] = []
    claimed: set[int] = set()
    for route in read_routes():
        rpid = route.get("pid")
        if not rpid or rpid in skip or rpid not in procs:
            continue
        url = f"https://{route['hostname']}"
        pids = {rpid} | descendants(procs, rpid)
        pids |= {p.pid for p in procs.values() if p.env.get("PORTLESS_URL") == url}
        pids -= managed
        claimed |= pids
        units.append({
            "kind": "portless", "hostname": route["hostname"], "port": route["port"], "root_pid": rpid,
            "pids": pids, "rss": tree_rss(procs, pids), "command": procs[rpid].command,
        })

    candidates = [p for p in ports if p not in skip and p not in claimed and p in procs]
    cwds = cwd_of(candidates)
    for pid in candidates:
        cwd = cwds.get(pid, "")
        if not any(cwd == d or cwd.startswith(d.rstrip("/") + "/") for d in WORK_DIRS):
            continue
        top = pid
        chain = [procs[pid].command]
        while True:
            parent = procs.get(procs[top].ppid)
            if parent is None or parent.pid in (0, 1) or not WRAPPER_RE.match(parent.command):
                break
            if AGENT_PROC_RE.search(parent.command):
                break
            if parent.pid in skip:
                break
            top = parent.pid
            chain.append(parent.command)
        if not any(DEV_CMD_RE.search(c) for c in chain):
            continue
        if top in claimed:
            continue
        pids = {top} | descendants(procs, top)
        claimed |= pids
        units.append({
            "kind": "raw", "hostname": None, "port": ports[pid][0], "root_pid": top, "pids": pids,
            "rss": tree_rss(procs, pids), "command": procs[top].command, "cwd": cwd,
        })
    for u in units:
        if "cwd" not in u:
            u["cwd"] = cwd_of([u["root_pid"]]).get(u["root_pid"], "")
    return units


def budget_used(state: dict, units: list[dict]) -> tuple[int, int]:
    running = [r for r in state["servers"].values() if r["status"] in ("starting", "running")]
    rss_kb = sum(r.get("_rss", 0) for r in running) + sum(u["rss"] for u in units)
    return rss_kb, len(running) + len(units)


def mark_stopped(rec: dict, procs: dict[int, Proc], who: str) -> tuple[set[int], int]:
    """Marks the record stopped and returns (pids to kill, rss). Kill after releasing the lock."""
    fam = family(procs, rec)
    rec["status"] = "stopped"
    rec["stopped_by"] = who
    rec["stopped_at"] = now()
    return fam, tree_rss(procs, fam)


def adopt_unit_as_stopped(state: dict, unit: dict, who: str) -> dict | None:
    """Remember a killed unmanaged portless server so agents do not respawn it."""
    if unit["kind"] != "portless":
        return None
    m = re.search(r"\s--\s(.+)$", unit["command"])
    nm = re.search(r"--name\s+(\S+)", unit["command"])
    name = nm.group(1) if nm else unit["hostname"].removesuffix(".localhost").split(".")[-1]
    cwd = unit.get("cwd") or str(HOME)
    root = git_root(cwd)
    rec = find_record(state, name, root)
    if rec is None:
        rid = unique_id(state, name, root)
        rec = {"id": rid, "name": name, "root": root, "created_at": now(), "log": str(LOG_DIR / f"{rid}.log")}
        state["servers"][rid] = rec
    rec.update({
        "cwd": cwd, "cmd": m.group(1) if m else default_cmd(cwd), "hostname": unit["hostname"],
        "status": "stopped", "stopped_by": who, "stopped_at": now(),
    })
    return rec

