"""`devd setup` and `devd doctor`: wire devd into the shell, agent harnesses, and agent instructions."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import BIN, CONFIG_FILE, DEVD_DIR, EXIT_ERROR, EXIT_OK, HOME, PORTLESS_BIN, PROXY_PID_FILE, REPO_ROOT
from .procs import pid_exists
from .util import is_agent, user_only

GUARD = REPO_ROOT / "guard" / "guard"
LINKED_BIN = HOME / ".local" / "bin" / "devd"
LINKED_GUARD = HOME / ".local" / "share" / "devd" / "guard"
AGENTS_DIR = REPO_ROOT / "agents"
MARK_BEGIN = "<!-- devd:begin (managed by `devd setup`; edit agents/instructions.md in the devd repo) -->"
MARK_END = "<!-- devd:end -->"
HARNESSES = ("cursor", "claude", "codex")


@dataclass
class Step:
    """One change `devd setup` makes (or would make)."""

    what: str
    status: str

    def show(self) -> None:
        icon = {"ok": "·", "changed": "✓", "skipped": "-", "warn": "!"}.get(self.status, "?")
        print(f"  {icon} {self.what}")


class Setup:
    def __init__(self, dry_run: bool) -> None:
        self.dry_run = dry_run
        self.steps: list[Step] = []

    def note(self, what: str, status: str) -> None:
        step = Step(what, status)
        step.show()
        self.steps.append(step)

    # ----- files -----

    def link(self, link: Path, target: Path, label: str) -> None:
        if link.is_symlink() and link.resolve() == target.resolve():
            self.note(f"{label}: {short(link)} -> {short(target)}", "ok")
            return
        if self.dry_run:
            self.note(f"{label}: would link {short(link)} -> {short(target)}", "changed")
            return
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            backup_existing(link)
        link.symlink_to(target)
        self.note(f"{label}: linked {short(link)} -> {short(target)}", "changed")

    def unlink(self, link: Path, target: Path, label: str) -> None:
        if link.is_symlink() and link.resolve() == target.resolve():
            if not self.dry_run:
                link.unlink()
            self.note(f"{label}: removed {short(link)}", "changed")
        else:
            self.note(f"{label}: {short(link)} is not managed by devd, left alone", "ok")

    def edit_json(self, path: Path, label: str, change) -> None:
        """Applies `change(data) -> bool` to a JSON file; writes (with a one-time backup) only if it changed."""
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
        except json.JSONDecodeError as exc:
            self.note(f"{label}: {short(path)} is not valid JSON ({exc}), skipped", "warn")
            return
        if not change(data):
            self.note(f"{label}: {short(path)} already set", "ok")
            return
        if self.dry_run:
            self.note(f"{label}: would update {short(path)}", "changed")
            return
        if path.exists():
            backup_once(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
        self.note(f"{label}: updated {short(path)}", "changed")

    def edit_block(self, path: Path, label: str, body: str | None) -> None:
        """Puts `body` between the devd markers in a Markdown file (appending if absent); None removes the block."""
        text = path.read_text() if path.exists() else ""
        updated = replace_block(text, body)
        if updated == text:
            self.note(f"{label}: {short(path)} already set", "ok")
            return
        if self.dry_run:
            self.note(f"{label}: would update {short(path)}", "changed")
            return
        if path.exists():
            backup_once(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(updated)
        self.note(f"{label}: updated {short(path)}", "changed")


def short(p: Path) -> str:
    s = str(p)
    return "~" + s[len(str(HOME)):] if s.startswith(str(HOME)) else s


def backup_once(path: Path) -> None:
    backup = path.with_name(path.name + ".devd-backup")
    if not backup.exists():
        shutil.copy2(path, backup)


def backup_existing(path: Path) -> None:
    backup = path.with_name(path.name + ".devd-backup")
    if backup.exists() or backup.is_symlink():
        if backup.is_dir() and not backup.is_symlink():
            shutil.rmtree(backup)
        else:
            backup.unlink()
    path.rename(backup)


def replace_block(text: str, body: str | None) -> str:
    start, end = text.find(MARK_BEGIN), text.find(MARK_END)
    if start != -1 and end != -1:
        before, after = text[:start].rstrip("\n"), text[end + len(MARK_END):].lstrip("\n")
    else:
        before, after = text.rstrip("\n"), ""
    if body is None:
        joined = "\n\n".join(p for p in (before, after.rstrip("\n")) if p)
        return joined + "\n" if joined else ""
    block = f"{MARK_BEGIN}\n{body.strip()}\n{MARK_END}"
    parts = [p for p in (before, block, after.rstrip("\n")) if p]
    return "\n\n".join(parts) + "\n"


def guard_command(mode: str) -> str:
    return f"{LINKED_GUARD} {mode}"


def is_devd_hook(command: object) -> bool:
    return isinstance(command, str) and "devd/guard " in command


# ----- harness wiring -----


def cursor_hooks(install: bool):
    wanted = {
        "preToolUse": {"command": guard_command("preToolUse"), "matcher": "Shell", "timeout": 5},
        "beforeShellExecution": {"command": guard_command("beforeShellExecution"), "timeout": 5},
    }

    def change(data: dict) -> bool:
        before = json.dumps(data, sort_keys=True)
        data.setdefault("version", 1)
        hooks = data.setdefault("hooks", {})
        for event, entry in wanted.items():
            entries = [e for e in hooks.get(event, []) if not is_devd_hook(e.get("command"))]
            if install:
                entries.insert(0, entry)
            if entries:
                hooks[event] = entries
            else:
                hooks.pop(event, None)
        return json.dumps(data, sort_keys=True) != before

    return change


def pretooluse_hooks(mode: str, install: bool):
    """Claude Code settings.json and Codex hooks.json share the PreToolUse shape."""
    entry = {"matcher": "Bash", "hooks": [{"type": "command", "command": guard_command(mode), "timeout": 10}]}

    def change(data: dict) -> bool:
        before = json.dumps(data, sort_keys=True)
        hooks = data.setdefault("hooks", {})
        groups = [
            g for g in hooks.get("PreToolUse", [])
            if not any(is_devd_hook(h.get("command")) for h in g.get("hooks", []))
        ]
        if install:
            groups.insert(0, entry)
        if groups:
            hooks["PreToolUse"] = groups
        else:
            hooks.pop("PreToolUse", None)
        if not hooks:
            data.pop("hooks", None)
        return json.dumps(data, sort_keys=True) != before

    return change


def run_setup(args: argparse.Namespace) -> int:
    if is_agent() and not args.dry_run:
        return user_only("devd setup", "devd setup")
    install = not args.uninstall
    chosen = [h.strip() for h in args.harness.split(",")] if args.harness else list(HARNESSES)
    unknown = [h for h in chosen if h not in HARNESSES]
    if unknown:
        print(f"devd: unknown harness {', '.join(unknown)} (use {', '.join(HARNESSES)})", file=sys.stderr)
        return EXIT_ERROR
    s = Setup(args.dry_run)
    instructions = (AGENTS_DIR / "instructions.md").read_text()

    print("devd " + ("setup" if install else "uninstall") + (" (dry run)" if args.dry_run else ""))
    print("\ncore")
    if install:
        s.link(LINKED_BIN, BIN, "cli")
        s.link(LINKED_GUARD, GUARD, "hook")
        if CONFIG_FILE.exists():
            s.note(f"config: {short(CONFIG_FILE)} exists", "ok")
        elif args.dry_run:
            s.note(f"config: would create {short(CONFIG_FILE)}", "changed")
        else:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_ROOT / "config.example.json", CONFIG_FILE)
            s.note(f"config: created {short(CONFIG_FILE)} from config.example.json", "changed")
    else:
        s.unlink(LINKED_BIN, BIN, "cli")
        s.unlink(LINKED_GUARD, GUARD, "hook")

    for harness in chosen:
        print(f"\n{harness}")
        home = HOME / f".{harness}"
        if not home.is_dir():
            s.note(f"{short(home)} not found, skipped", "skipped")
            continue
        if harness == "cursor":
            s.edit_json(home / "hooks.json", "hooks", cursor_hooks(install))
            pairs = [(home / "rules" / "devd.mdc", AGENTS_DIR / "cursor" / "devd.mdc", "rule"),
                     (home / "skills" / "devd", AGENTS_DIR / "skill", "skill")]
        elif harness == "claude":
            s.edit_json(home / "settings.json", "hooks", pretooluse_hooks("claude", install))
            s.edit_block(home / "CLAUDE.md", "instructions", instructions if install else None)
            pairs = [(home / "skills" / "devd", AGENTS_DIR / "skill", "skill")]
        else:
            s.edit_json(home / "hooks.json", "hooks", pretooluse_hooks("codex", install))
            s.edit_block(home / "AGENTS.md", "instructions", instructions if install else None)
            # codex reads skills from the shared ~/.agents/skills folder
            pairs = [(HOME / ".agents" / "skills" / "devd", AGENTS_DIR / "skill", "skill")]
        for link, target, label in pairs:
            (s.link if install else s.unlink)(link, target, label)

    changed = any(step.status == "changed" for step in s.steps)
    print()
    if args.dry_run:
        print("Dry run: nothing was changed.")
    elif not install:
        print("Done. Config and state are kept: ~/.config/devd, ~/.portless/devd.")
    elif changed and not args.dry_run:
        print("Done. Restart running agent sessions so they load the hooks and instructions.")
        if "codex" in chosen and (HOME / ".codex").is_dir():
            print("Codex: open `/hooks` once and trust the devd PreToolUse hook.")
    elif not changed:
        print("Everything was already set up.")
    if install and str(LINKED_BIN.parent) not in os.environ.get("PATH", "").split(":"):
        print(f"Add {short(LINKED_BIN.parent)} to your PATH to run `devd` directly.")
    return EXIT_OK


# ----- doctor -----


def run_doctor(_args: argparse.Namespace) -> int:
    problems = 0

    def check(ok: bool, label: str, fix: str = "") -> None:
        nonlocal problems
        print(f"  {'✓' if ok else '✗'} {label}" + ("" if ok or not fix else f"\n      fix: {fix}"))
        if not ok:
            problems += 1

    print("devd doctor\n")
    check(sys.platform == "darwin", f"macOS ({sys.platform})", "devd relies on macOS ps/lsof/netstat output")
    check(sys.version_info >= (3, 10), f"python {sys.version.split()[0]}", "install python 3.10+")
    node = shutil.which("node") or next((p for p in ("/opt/homebrew/bin/node", "/usr/local/bin/node") if os.path.exists(p)), None)
    check(node is not None, f"node {'at ' + node if node else 'not found'}", "install Node.js 20+ (the agent hook needs it)")
    portless_ok = PORTLESS_BIN != "portless" or shutil.which("portless") is not None
    check(portless_ok, f"portless at {PORTLESS_BIN}", "npm install -g portless (or bun add -g portless)")
    proxy = None
    try:
        proxy = int(PROXY_PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        pass
    check(proxy is not None and pid_exists(proxy), "portless proxy running", "portless proxy start (or `portless service install` for login start)")
    check(LINKED_BIN.is_symlink() and LINKED_BIN.resolve() == BIN.resolve(), f"{short(LINKED_BIN)} -> this checkout", "devd setup")
    check(str(LINKED_BIN.parent) in os.environ.get("PATH", "").split(":"), f"{short(LINKED_BIN.parent)} on PATH", "add it to PATH in your shell rc")
    check(LINKED_GUARD.is_symlink() and LINKED_GUARD.resolve() == GUARD.resolve(), f"{short(LINKED_GUARD)} -> this checkout", "devd setup")
    try:
        json.loads(CONFIG_FILE.read_text())
        check(True, f"config {short(CONFIG_FILE)}")
    except FileNotFoundError:
        check(True, f"config {short(CONFIG_FILE)} (not created, defaults apply)")
    except json.JSONDecodeError as exc:
        check(False, f"config {short(CONFIG_FILE)}: {exc}", "fix the JSON")
    try:
        DEVD_DIR.mkdir(parents=True, exist_ok=True)
        check(os.access(DEVD_DIR, os.W_OK), f"state dir {short(DEVD_DIR)} writable", f"chmod u+w {DEVD_DIR}")
    except OSError as exc:
        check(False, f"state dir {short(DEVD_DIR)}: {exc}")

    print()
    for harness, path in (("cursor", HOME / ".cursor/hooks.json"), ("claude", HOME / ".claude/settings.json"), ("codex", HOME / ".codex/hooks.json")):
        if not path.parent.is_dir():
            print(f"  - {harness}: not installed")
            continue
        wired = path.exists() and "devd/guard " in path.read_text()
        check(wired, f"{harness} hook in {short(path)}", f"devd setup --harness {harness}")
    if node:
        probe = subprocess.run(
            [str(LINKED_GUARD if LINKED_GUARD.exists() else GUARD), "claude"],
            input=json.dumps({"tool_input": {"command": "portless proxy stop"}}), capture_output=True, text=True, check=False,
        )
        check('"deny"' in probe.stdout, "hook denies `portless proxy stop`", "run the guard by hand to see the error")

    print(f"\n{'all good' if not problems else f'{problems} problem(s)'}")
    return EXIT_OK if not problems else EXIT_ERROR
