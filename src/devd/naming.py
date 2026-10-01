"""Checkout roots, app names, record ids, and lookups."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import HOME, HOOK_SCRIPT
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


def fallback_name(cwd: str) -> str:
    """package.json name without scope, else the checkout folder name."""
    d = Path(cwd)
    for _ in range(8):
        pkg = d / "package.json"
        if pkg.exists():
            try:
                raw = json.loads(pkg.read_text()).get("name")
            except (json.JSONDecodeError, OSError):
                raw = None
            if isinstance(raw, str) and raw.strip():
                return re.sub(r"[^a-z0-9-]+", "-", raw.split("/")[-1].lower()).strip("-")
            break
        if d.parent == d:
            break
        d = d.parent
    return re.sub(r"[^a-z0-9-]+", "-", Path(git_root(cwd)).name.lower()).strip("-")


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

