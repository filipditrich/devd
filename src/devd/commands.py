"""The devd subcommands."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys

from . import ui
from .config import EXIT_BUDGET, EXIT_ERROR, EXIT_LEFT_DOWN, EXIT_OK, EXIT_USER_ONLY, LOG_DIR
from .util import (
    actor,
    eprint,
    fmt_minutes,
    human_age,
    human_bytes,
    is_agent,
    now,
    parse_minutes,
    parse_size_mb,
    short_path,
    user_only,
)
from .state import load_state, locked_state, read_routes, strip_private, tail, url_of
from .procs import kill_pids, listeners, route_pids, snapshot
from .naming import agent_name_error, claim_checkout, default_cmd, find_record, git_root, infer_name, lookup, unique_id
from .model import (
    adopt_unit_as_stopped,
    agent_may_start,
    budget_used,
    down_reason,
    idle_limit_s,
    mark_stopped,
    refresh,
    unmanaged_units,
)
from .runner import spawn, wait_ready


def resolve_up_target(state: dict, args: argparse.Namespace) -> tuple[str, str, str, str]:
    """Returns (name, cwd, root, cmd)."""
    cwd = os.path.abspath(args.cwd or os.getcwd())
    cmd_parts = args.cmd or []
    cmd = cmd_parts[0] if len(cmd_parts) == 1 else shlex.join(cmd_parts)
    target = args.target
    if target and not args.name:
        rec = lookup(state, target, quiet=True)
        if rec:
            return rec["name"], rec["cwd"], rec["root"], cmd or rec["cmd"]
        if "@" in target:
            raise SystemExit(f"devd: no server with id {target}. See `devd ls --all`.")
    requested = args.name or target
    if not cmd:
        existing = find_record(state, requested, git_root(cwd)) if requested else None
        cmd = existing["cmd"] if existing else default_cmd(cwd)
    inferred = infer_name(cwd, cmd)
    if is_agent() and requested:
        mismatch = agent_name_error(requested, inferred)
        if mismatch:
            raise SystemExit(mismatch)
        if inferred:
            requested = inferred
    name = requested or inferred
    if not name:
        raise SystemExit("devd: could not infer the app name. Pass --name <name>.")
    return name, cwd, git_root(cwd), cmd


def begin_replace(rec: dict, procs: dict, who: str) -> set[int]:
    """Mark the record stopped and return every pid that still owns it.

    Stop is recorded before the kill so the outgoing runner does not classify
    its own exit over the start that replaces it.
    """
    pids, _ = mark_stopped(rec, procs, who)
    pids |= route_pids(rec, procs, read_routes())
    return pids


def finish_replace(rec: dict, who: str) -> None:
    rec.update({"status": "starting", "started_at": now(), "started_by": who, "port": None, "last_active": now()})
    for k in ("stopped_at", "stopped_by", "ended_at", "exit_code", "idle_limit_min"):
        rec.pop(k, None)


def cmd_up(args: argparse.Namespace) -> int:
    who = actor()
    if who == "agent" and args.over_budget:
        return user_only("`--over-budget`", "devd up --over-budget ...")
    if who == "agent" and args.force:
        return user_only("`--force`", "devd up --force ...")

    procs = snapshot()
    ports = listeners()
    to_kill: set[int] = set()
    reclaim = False
    with locked_state() as state:
        refresh(state, procs)
        name, cwd, root, cmd = resolve_up_target(state, args)
        rec = find_record(state, name, root) or claim_checkout(state, name, root, cwd)
        if rec and rec["status"] in ("starting", "running") and not args.force:
            rec["last_active"] = now()
            strip_private(state)
            url = url_of(rec) or "(route not registered yet)"
            print(f"{rec['id']} already running: {url}")
            print(f"logs: devd logs {rec['id']}")
            return EXIT_OK
        if rec and who == "agent" and not agent_may_start(rec):
            print(f"{rec['id']} left down: {down_reason(rec)}.")
            print(f"Do not start it again. Tell the user; they can run: devd up {rec['id']}")
            return EXIT_LEFT_DOWN

        units = unmanaged_units(state, procs, ports)
        dup = next((u for u in units if u.get("hostname") and u["hostname"].split(".")[-2] == name
                    and u.get("cwd", "").startswith(root)), None)
        if dup and not args.force:
            strip_private(state)
            print(f"{name} is already running outside devd: https://{dup['hostname']} (pid {dup['root_pid']}).")
            print("Reuse it. The user can stop it with: devd stop " + dup["hostname"].removesuffix(".localhost"))
            return EXIT_OK

        replacing = bool(rec and rec["status"] in ("starting", "running"))
        rss_kb, count = budget_used(state, units)
        cfg = state["config"]
        if not replacing and not args.over_budget and (rss_kb / 1024 >= cfg["max_rss_mb"] or count >= cfg["max_servers"]):
            strip_private(state)
            print(
                f"devd: budget full ({human_bytes(rss_kb)} of {cfg['max_rss_mb'] / 1024:.1f}G, "
                f"{count} of {cfg['max_servers']} servers). Not starting {name}."
            )
            print("Reuse a running server, or ask the user to stop one (see `devd ls`).")
            return EXIT_BUDGET

        if rec is None:
            rid = unique_id(state, name, root)
            rec = {"id": rid, "name": name, "root": root, "created_at": now()}
            state["servers"][rid] = rec
        # a previous instance of this name may still be registered. take it back,
        # including when devd lost track of the process and thinks the server is down.
        reclaim = bool(args.force or rec.get("hostname")) and not args.raw
        rec.update({
            "cwd": cwd, "cmd": cmd, "raw": args.raw, "log": str(LOG_DIR / f"{rec['id']}.log"),
            "hostname": rec.get("hostname"), "force": reclaim,
        })
        rid = rec["id"]
        if reclaim:
            to_kill = begin_replace(rec, procs, who)
        else:
            finish_replace(rec, who)
            spawn(rec)
        strip_private(state)

    if to_kill:
        kill_pids(to_kill)
    if reclaim:
        with locked_state() as state:
            rec = state["servers"][rid]
            finish_replace(rec, who)
            spawn(rec)
    rec = wait_ready(rid, args.wait)
    return report_start(rec)


def report_start(rec: dict) -> int:
    url = url_of(rec)
    if rec["status"] not in ("starting", "running"):
        print(f"{rec['id']} did not start: {down_reason(rec)}.")
        print("--- last log lines ---")
        print(tail(rec["log"], 30))
        return EXIT_ERROR
    if rec.get("_ready"):
        print(f"{rec['id']} up: {url}")
    else:
        print(f"{rec['id']} starting: {url or '(route not registered yet)'}. Check `devd logs {rec['id']}`.")
    print(f"logs: devd logs {rec['id']}   stop: devd stop {rec['id']}")
    limit = idle_limit_s(rec, load_state()["config"])
    if rec.get("started_by") == "agent":
        idle = f" Unused, it stops by itself after {fmt_minutes(limit / 60)} idle." if limit else ""
        print(f"Stop it when you are done, unless the user needs it to check your work.{idle}")
    return EXIT_OK


def cmd_stop(args: argparse.Namespace) -> int:
    who = actor()
    if who == "agent" and (args.all or args.strays):
        return user_only("`devd stop --all/--strays`", "devd stop --all")
    procs = snapshot()
    ports = listeners()
    freed = 0
    stopped: list[str] = []
    with locked_state() as state:
        refresh(state, procs)
        units = unmanaged_units(state, procs, ports)
        targets: list[dict] = []
        unit_targets: list[dict] = []
        if args.all:
            targets = [r for r in state["servers"].values() if r["status"] in ("starting", "running")]
            unit_targets = units
        elif args.strays:
            unit_targets = units
        elif args.app:
            targets = [r for r in state["servers"].values() if r["name"] == args.app and r["status"] in ("starting", "running")]
            unit_targets = [u for u in units if u.get("hostname") and u["hostname"].removesuffix(".localhost").split(".")[-1] == args.app]
        else:
            if not args.target:
                eprint("devd: stop needs a target (id, hostname, name, or path), --app, --strays, or --all")
                return EXIT_ERROR
            rec = lookup(state, args.target)
            if rec:
                targets = [rec]
            else:
                t = args.target.removeprefix("https://").removesuffix("/").removesuffix(".localhost")
                unit_targets = [u for u in units if (u.get("hostname") or "").removesuffix(".localhost") == t
                                or str(u["root_pid"]) == t or str(u["port"]) == t]
                if not unit_targets:
                    eprint(f"devd: nothing matches {args.target}. See `devd ls`.")
                    strip_private(state)
                    return EXIT_ERROR
        if who == "agent" and unit_targets and not targets:
            return user_only("stopping servers devd did not start", f"devd stop {args.target or '--app ' + args.app}")
        to_kill: set[int] = set()
        for rec in targets:
            pids, rss = mark_stopped(rec, procs, who)
            to_kill |= pids
            freed += rss
            stopped.append(rec["id"])
        if who == "user":
            for unit in unit_targets:
                to_kill |= unit["pids"]
                freed += unit["rss"]
                adopted = adopt_unit_as_stopped(state, unit, who)
                stopped.append(adopted["id"] if adopted else f"pid {unit['root_pid']} ({short_path(unit.get('cwd', ''))})")
    if to_kill:
        kill_pids(to_kill)
    if not stopped:
        print("nothing to stop")
        return EXIT_OK
    for s in stopped:
        print(f"stopped {s}")
    print(f"freed about {human_bytes(freed)}")
    if who == "user":
        print("Agents will not restart these. Bring one back with: devd up <id>")
    return EXIT_OK


def cmd_restart(args: argparse.Namespace) -> int:
    who = actor()
    procs = snapshot()
    with locked_state() as state:
        refresh(state, procs)
        rec = lookup(state, args.target)
        if rec is None:
            strip_private(state)
            eprint(f"devd: nothing matches {args.target}. See `devd ls`.")
            return EXIT_ERROR
        if rec["status"] not in ("starting", "running") and who == "agent" and not agent_may_start(rec):
            strip_private(state)
            print(f"{rec['id']} left down: {down_reason(rec)}.")
            print(f"Do not start it again. Tell the user; they can run: devd up {rec['id']}")
            return EXIT_LEFT_DOWN
        fam = begin_replace(rec, procs, who)
        rid = rec["id"]
        rec["force"] = not rec.get("raw")
    if fam:
        kill_pids(fam)
    with locked_state() as state:
        rec = state["servers"][rid]
        finish_replace(rec, who)
        spawn(rec)
    return report_start(wait_ready(rid, args.wait))


def cmd_ls(args: argparse.Namespace) -> int:
    procs = snapshot()
    ports = listeners()
    # from lsof, not by connecting: a probe would count as activity for the idle timer
    listening = {p for plist in ports.values() for p in plist}
    with locked_state() as state:
        refresh(state, procs)
        units = unmanaged_units(state, procs, ports)
        recs = sorted(state["servers"].values(), key=lambda r: (r["status"] not in ("starting", "running"), r["id"]))
        cfg = dict(state["config"])
        rss_kb, count = budget_used(state, units)
        view = [dict(r) for r in recs]
        strip_private(state)

    if args.json:
        def live_fields(r: dict) -> dict:
            if r["status"] in ("starting", "running"):
                return {
                    "listening": r.get("port") in listening, "reason": None,
                    "idle_s": int(now() - r.get("last_active", r["started_at"])), "idle_limit_s": idle_limit_s(r, cfg),
                }
            return {"listening": False, "reason": down_reason(r)}

        out = {
            "now": now(),
            "servers": [
                {k: v for k, v in r.items() if not k.startswith("_")}
                | {"rss_kb": r.get("_rss", 0), "url": url_of(r)}
                | live_fields(r)
                for r in view
            ],
            "unmanaged": [
                {k: v for k, v in u.items() if k != "pids"} | {"listening": u["port"] in listening} for u in units
            ],
            "budget": {"rss_kb": rss_kb, "servers": count, **cfg},
        }
        print(json.dumps(out, indent=2, default=str))
        return EXIT_OK

    live = [r for r in view if r["status"] in ("starting", "running")]
    down = [r for r in view if r not in live]
    if not live and not units:
        print("no dev servers running")
    if live:
        print(f"{'ID':32} {'STATE':9} {'RSS':>6} {'UP':>6}  URL")
        for r in live:
            health = "" if not r.get("port") or r["port"] in listening else "  (not listening)"
            print(f"{r['id']:32} {r['status']:9} {human_bytes(r.get('_rss', 0)):>6} {human_age(now() - r['started_at']):>6}  {url_of(r) or '-'}{health}")
            limit = idle_limit_s(r, cfg)
            idle = human_age(now() - r.get("last_active", r["started_at"]))
            idle_txt = "kept" if r.get("keep") else f"idle {idle} of {fmt_minutes(limit / 60)}" if limit else f"idle {idle}"
            print(f"{'':32} {short_path(r['cwd'])}  ·  {r['cmd']}  ·  by {r.get('started_by', '?')}  ·  {idle_txt}")
    if units:
        print("\nnot started by devd (stop with `devd stop <host|pid>` or `devd stop --strays`):")
        for u in units:
            host = u["hostname"].removesuffix(".localhost") if u.get("hostname") else f"pid {u['root_pid']}"
            health = "" if u["port"] in listening else "  (not listening)"
            print(f"  {host:30} {human_bytes(u['rss']):>6}  :{u['port']}  {short_path(u.get('cwd', ''))}{health}")
            print(f"  {'':30} {u['command'][:110]}")
    if down and (args.all or len(down) <= 8):
        print("\ndown (agents only restart ones that failed at startup, an agent stopped, or idled out):")
        for r in down[: None if args.all else 8]:
            print(f"  {r['id']:30} {down_reason(r)}")
    print(f"\nbudget: {human_bytes(rss_kb)} of {cfg['max_rss_mb'] / 1024:.1f}G · {count} of {cfg['max_servers']} servers")
    return EXIT_OK


def cmd_logs(args: argparse.Namespace) -> int:
    state = load_state()
    rec = lookup(state, args.target)
    if rec is None:
        eprint(f"devd: nothing matches {args.target}. See `devd ls`.")
        return EXIT_ERROR
    if args.follow:
        os.execvp("tail", ["tail", "-n", str(args.lines), "-f", rec["log"]])
    print(tail(rec["log"], args.lines))
    return EXIT_OK


def cmd_forget(args: argparse.Namespace) -> int:
    if is_agent():
        return user_only("`devd forget`", f"devd forget {args.target}")
    procs = snapshot()
    with locked_state() as state:
        refresh(state, procs)
        rec = lookup(state, args.target)
        if rec is None:
            strip_private(state)
            eprint(f"devd: nothing matches {args.target}")
            return EXIT_ERROR
        if rec["status"] in ("starting", "running"):
            strip_private(state)
            eprint(f"devd: {rec['id']} is running. Stop it first.")
            return EXIT_ERROR
        del state["servers"][rec["id"]]
        strip_private(state)
    print(f"forgot {rec['id']}")
    return EXIT_OK


def cmd_prune(_args: argparse.Namespace) -> int:
    procs = snapshot()
    removed = []
    with locked_state() as state:
        refresh(state, procs)
        for rid, rec in list(state["servers"].items()):
            if rec["status"] not in ("starting", "running") and not os.path.isdir(rec["root"]):
                removed.append(rid)
                del state["servers"][rid]
        strip_private(state)
    print("pruned " + (", ".join(removed) if removed else "nothing"))
    return EXIT_OK


def cmd_config(args: argparse.Namespace) -> int:
    with locked_state() as state:
        if args.pairs:
            if is_agent():
                return user_only("`devd config`", "devd config " + " ".join(args.pairs))
            for pair in args.pairs:
                key, _, value = pair.partition("=")
                if key == "max_rss":
                    state["config"]["max_rss_mb"] = parse_size_mb(value)
                elif key == "max_servers":
                    state["config"]["max_servers"] = int(value)
                elif key in ("agent_idle", "user_idle"):
                    state["config"][f"{key}_min"] = parse_minutes(value)
                else:
                    eprint(f"devd: unknown key {key} (use max_rss=8G, max_servers=5, agent_idle=15m, user_idle=off)")
                    return EXIT_ERROR
        cfg = state["config"]
    print(
        f"max_rss={cfg['max_rss_mb'] / 1024:.1f}G max_servers={cfg['max_servers']} "
        f"agent_idle={fmt_minutes(cfg['agent_idle_min'])} user_idle={fmt_minutes(cfg['user_idle_min'])}"
    )
    return EXIT_OK


def cmd_keep(args: argparse.Namespace) -> int:
    if is_agent():
        return user_only("`devd keep`", f"devd keep {args.target}")
    with locked_state() as state:
        rec = lookup(state, args.target)
        if rec is None:
            eprint(f"devd: nothing matches {args.target}. See `devd ls`.")
            return EXIT_ERROR
        rec["keep"] = not args.off
        rec["last_active"] = now()
        rid = rec["id"]
    print(f"{rid}: idle auto-stop {'off (kept running)' if not args.off else 'back on'}")
    return EXIT_OK


def cmd_ui(_args: argparse.Namespace) -> int:
    if is_agent():
        eprint("devd: `devd ui` is an interactive panel for the user. Agents use `devd ls`.")
        return EXIT_USER_ONLY
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        eprint("devd: ui needs an interactive terminal. Use `devd ls` instead.")
        return EXIT_ERROR
    ui.main()
    return EXIT_OK

