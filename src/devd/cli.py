"""Argument parsing and dispatch."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .commands import cmd_config, cmd_forget, cmd_keep, cmd_logs, cmd_ls, cmd_prune, cmd_restart, cmd_stop, cmd_ui, cmd_up
from .install import HARNESSES, run_doctor, run_setup
from .runner import cmd_run


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="devd", description="Supervisor for local dev servers behind portless.")
    parser.add_argument("--version", action="version", version=f"devd {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    p = sub.add_parser("up", help="start a dev server (or print the URL if it is already up)")
    p.add_argument("target", nargs="?", help="app name or existing id")
    p.add_argument("--name", help="portless app name (inferred from the checkout if omitted)")
    p.add_argument("--cwd", help="directory to run in (default: current)")
    p.add_argument("--over-budget", action="store_true", help="user only: ignore the memory/server budget")
    p.add_argument("--wait", type=float, default=45, help="seconds to wait for the port (default 45)")
    p.add_argument("--raw", action="store_true", help="do not wrap in portless (the script already does, or PORTLESS=0)")
    p.add_argument("--force", action="store_true", help="user only: take the portless name back if another process still has it")
    p.set_defaults(func=cmd_up)
    p.epilog = "Pass the command after --, e.g. devd up --name hub -- bun run dev"

    p = sub.add_parser("ls", help="list servers, unmanaged servers, and budget")
    p.add_argument("--json", action="store_true")
    p.add_argument("--all", action="store_true", help="show every down record")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("stop", help="stop a server and keep it down")
    p.add_argument("target", nargs="?")
    p.add_argument("--app", help="stop every server of this app name")
    p.add_argument("--strays", action="store_true", help="user only: stop servers devd did not start")
    p.add_argument("--all", action="store_true", help="user only: stop everything (the portless proxy stays up)")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("restart", help="restart a running server in place")
    p.add_argument("target")
    p.add_argument("--wait", type=float, default=45)
    p.add_argument("--force", action="store_true", help="take the portless name back (restart already does this)")
    p.set_defaults(func=cmd_restart)

    p = sub.add_parser("logs", help="show a server's log")
    p.add_argument("target")
    p.add_argument("-n", "--lines", type=int, default=80)
    p.add_argument("-f", "--follow", action="store_true")
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("forget", help="user only: delete a down record")
    p.add_argument("target")
    p.set_defaults(func=cmd_forget)

    p = sub.add_parser("prune", help="drop down records whose checkout no longer exists")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("config", help="show or set budget: devd config max_rss=8G max_servers=5")
    p.add_argument("pairs", nargs="*")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("keep", help="user only: exempt a server from the idle auto-stop (--off to undo)")
    p.add_argument("target")
    p.add_argument("--off", action="store_true")
    p.set_defaults(func=cmd_keep)

    p = sub.add_parser("ui", help="interactive control panel (default when run with no arguments in a terminal)")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("setup", help="user only: link devd and wire the agent hooks and instructions")
    p.add_argument("--harness", help=f"comma-separated subset of {','.join(HARNESSES)} (default: all that are installed)")
    p.add_argument("--uninstall", action="store_true", help="remove everything setup added (config and state stay)")
    p.add_argument("--dry-run", action="store_true", help="show what would change")
    p.set_defaults(func=run_setup)

    p = sub.add_parser("doctor", help="check the install, portless, and hook wiring")
    p.set_defaults(func=run_doctor)

    p = sub.add_parser("_run")
    p.add_argument("id")
    p.set_defaults(func=cmd_run)

    if not argv and sys.stdin.isatty() and sys.stdout.isatty():
        argv = ["ui"]
    cmd: list[str] = []
    if "--" in argv:
        idx = argv.index("--")
        argv, cmd = argv[:idx], argv[idx + 1:]
    args = parser.parse_args(argv)
    args.cmd = cmd
    return args.func(args)

