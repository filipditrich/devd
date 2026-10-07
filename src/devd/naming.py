"""Checkout roots, app names, record ids, and lookups."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import HOME, HOOK_SCRIPT, LOG_DIR
from .util import eprint


def git_root(cwd: str) -> str:
    r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else cwd


def infer_name(cwd: str, cmd: str) -> str | None:
    if not HOOK_SCRIPT.exists():
        return fallback_name(cwd)
    code = (
        f"import {{ inferName }} from {json.dumps(HOOK_SCRIPT.as_uri())};"
        f"process.stdout.write(inferName({json.dumps(cwd)}, {json.dumps(cmd)}) ?? '')"
    )
    node = shutil.which("node") or next(iter(sorted(HOME.glob(".nvm/versions/node/*/bin/node"), reverse=True)), None)
    if node is None:
        return fallback_name(cwd)
    r = subprocess.run([str(node), "--input-type=module", "-e", code], capture_output=True, text=True, check=False)
    name = r.stdout.strip()
    return name or fallback_name(cwd)


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")


def package_label(raw: str) -> str:
    """Hostname label for a package name.

    The scope stays, so `@nfctron/api` is `nfctron-api`. A name that already
    starts with its scope is not doubled (`@nfctron/nfctron-hub`).
    """
    text = raw.strip()
    if text.startswith("@") and "/" in text:
        scope, name = text[1:].split("/", 1)
        scope_slug, name_slug = slug(scope), slug(name)
        if name_slug == scope_slug or name_slug.startswith(f"{scope_slug}-"):
            return name_slug
        return slug(f"{scope_slug}-{name_slug}")
    return slug(text)


def package_label_at(directory: Path) -> str | None:
    pkg = directory / "package.json"
    if not pkg.is_file():
        return None
    try:
        raw = json.loads(pkg.read_text()).get("name")
    except (json.JSONDecodeError, OSError):
        return None
    if isinstance(raw, str) and raw.strip():
        return package_label(raw)
    return None


def fallback_name(cwd: str) -> str:
    """Nearest named package.json inside the repo, else the repo folder."""
    root = Path(git_root(cwd))
    d = Path(cwd)
    for _ in range(8):
        label = package_label_at(d)
        if label:
            return label
        if d == root or d.parent == d:
            break
        d = d.parent
    return slug(root.name)


def agent_name_error(given: str | None, inferred: str | None) -> str | None:
    """Agents may not invent a name. Users still can."""
    if not given or not inferred or given == inferred:
        return None
    return (
        f"devd: --name {given} does not match this checkout ({inferred}). "
        "Omit --name. devd names the server from the repo."
    )


def default_cmd(cwd: str) -> str:
    d = Path(cwd)
    for _ in range(8):
        if (d / "bun.lock").exists() or (d / "bun.lockb").exists():
            return "bun run dev"
        if (d / "pnpm-lock.yaml").exists():
            return "pnpm dev"
        if (d / "yarn.lock").exists():
            return "yarn dev"
        if (d / "package-lock.json").exists():
            return "npm run dev"
        if d.parent == d:
            break
        d = d.parent
    return "npm run dev"


def record_id(name: str, root: str) -> str:
    """`name@label`: label is the effort folder for `.worktrees/<effort>/<repo>` checkouts, else the repo folder."""
    parts = Path(root).parts
    label = Path(root).name
    if ".worktrees" in parts[:-2]:
        label = parts[parts.index(".worktrees") + 1]
    return f"{re.sub(r'[^A-Za-z0-9._-]+', '-', name)}@{re.sub(r'[^A-Za-z0-9._-]+', '-', label)}"


def fresh_id(state: dict, name: str, root: str, current_id: str | None = None) -> str:
    """An id for `name` at `root` that does not clobber a different record."""
    base = record_id(name, root)
    rid = base
    n = 2
    while rid in state["servers"] and rid != current_id:
        rid = f"{base}-{n}"
        n += 1
    return rid


def retarget_hostname(hostname: str | None, old_name: str, new_name: str) -> str | None:
    if not hostname or old_name == new_name:
        return hostname
    if hostname == f"{old_name}.localhost":
        return f"{new_name}.localhost"
    suffix = f".{old_name}.localhost"
    if hostname.endswith(suffix):
        return hostname[: -len(suffix)] + f".{new_name}.localhost"
    return hostname


def rename_record(state: dict, rec: dict, name: str) -> dict:
    """Point a stopped record at the canonical app name. Leaves a live server alone."""
    if rec.get("status") in ("starting", "running") or rec.get("name") == name:
        return rec
    old_id = rec["id"]
    old_name = rec["name"]
    new_id = fresh_id(state, name, rec["root"], old_id)
    state["servers"].pop(old_id, None)
    old_log = rec.get("log")
    new_log = str(LOG_DIR / f"{new_id}.log")
    rec["id"] = new_id
    rec["name"] = name
    rec["log"] = new_log
    if new_id == record_id(name, rec["root"]):
        rec["hostname"] = retarget_hostname(rec.get("hostname"), old_name, name)
    state["servers"][new_id] = rec
    if old_log and old_log != new_log and os.path.isfile(old_log) and not os.path.exists(new_log):
        os.replace(old_log, new_log)
    return rec


def claim_checkout(state: dict, name: str, root: str, cwd: str) -> dict | None:
    """The server already recorded for this directory, renamed when it is down."""
    same = [r for r in state["servers"].values() if r.get("root") == root and r.get("cwd") == cwd]
    if not same:
        return None
    live = [r for r in same if r.get("status") in ("starting", "running")]
    rec = live[0] if live else same[0]
    if rec.get("status") in ("starting", "running"):
        return rec
    return rename_record(state, rec, name)


def unique_id(state: dict, name: str, root: str) -> str:
    base = record_id(name, root)
    rid = base
    n = 2
    while rid in state["servers"] and state["servers"][rid]["root"] != root:
        rid = f"{base}-{n}"
        n += 1
    return rid


def find_record(state: dict, name: str, root: str) -> dict | None:
    for rec in state["servers"].values():
        if rec["name"] == name and rec["root"] == root:
            return rec
    return None


def lookup(state: dict, target: str, quiet: bool = False) -> dict | None:
    servers = state["servers"]
    if target in servers:
        return servers[target]
    t = target.removeprefix("https://").removesuffix("/").removesuffix(".localhost")
    for rec in servers.values():
        if rec.get("hostname") and rec["hostname"].removesuffix(".localhost") == t:
            return rec
    by_name = [r for r in servers.values() if r["name"] == target]
    if len(by_name) == 1:
        return by_name[0]
    path = os.path.abspath(os.path.expanduser(target))
    if os.path.isdir(path):
        matches = [r for r in servers.values() if path.startswith(r["root"])]
        if len(matches) == 1:
            return matches[0]
    if not quiet and len(by_name) > 1:
        eprint(f"devd: {target} is ambiguous: " + ", ".join(r["id"] for r in by_name))
    return None

