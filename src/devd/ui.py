"""
devd ui: terminal control panel for devd.

A thin client over the devd CLI: reads `devd ls --json` in the background and runs
`devd up / stop / restart / keep / forget` for actions, so every devd rule still applies.
"""

from __future__ import annotations

import curses
import json
import locale
import os
import re
import subprocess
import textwrap
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import BIN, HOME

DEVD = str(BIN)
REFRESH_S = 2.0
LOG_MAX_LINES = 5000
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Z0-9]")
ERR_RE = re.compile(r"\b(error|err!|failed|exception|fatal|cannot|unhandled|eaddrinuse)\b", re.I)
WARN_RE = re.compile(r"\b(warn|warning|deprecated)\b", re.I)

LIVE = ("running", "starting")
STATE_ICON = {
    "running": "●", "starting": "◐", "stopped": "○", "killed": "✕", "exited": "◌", "failed": "✕", "unmanaged": "◆",
}


# ---------- devd client ----------


def devd_env() -> dict:
    env = dict(os.environ)
    env["DEVD_ACTOR"] = "user"
    return env


def run_devd(*args: str, timeout: float = 90) -> tuple[int, str]:
    try:
        r = subprocess.run([DEVD, *args], capture_output=True, text=True, env=devd_env(), timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"devd {' '.join(args)} timed out"
    return r.returncode, (r.stdout + r.stderr).strip()


@dataclass
class Row:
    kind: str
    key: str
    data: dict = field(default_factory=dict)

    @property
    def state(self) -> str:
        return "unmanaged" if self.kind == "unmanaged" else self.data.get("status", "?")

    @property
    def live(self) -> bool:
        return self.kind == "unmanaged" or self.state in LIVE

    @property
    def url(self) -> str | None:
        if self.kind == "unmanaged":
            return f"https://{self.data['hostname']}" if self.data.get("hostname") else None
        return self.data.get("url")

    @property
    def target(self) -> str:
        """What to pass to `devd stop`."""
        if self.kind == "unmanaged":
            host = self.data.get("hostname")
            return host.removesuffix(".localhost") if host else str(self.data["root_pid"])
        return self.data["id"]

    @property
    def label(self) -> str:
        if self.kind == "unmanaged":
            host = self.data.get("hostname")
            return host.removesuffix(".localhost") if host else f"pid {self.data['root_pid']}"
        return self.data["id"]


def repo_label(path: str) -> str:
    """Git repo folder for a checkout path.

    `.worktrees/<effort>/<repo>/...` is `<repo>`. A plain checkout's git root
    is already that folder, so the last path component is the repo.
    """
    if not path:
        return ""
    parts = Path(path).parts
    if ".worktrees" in parts[:-2]:
        return parts[parts.index(".worktrees") + 2]
    return Path(path).name


def _repo_of(data: dict, known: list[tuple[str, str]]) -> str:
    root = data.get("root") or ""
    if root:
        return repo_label(root) or "other"
    cwd = data.get("cwd") or ""
    best, name = "", ""
    for known_root, repo in known:
        if known_root and (cwd == known_root or cwd.startswith(known_root.rstrip("/") + "/")) and len(known_root) > len(best):
            best, name = known_root, repo
    return name or repo_label(cwd) or "other"


def _recency(row: Row) -> float:
    data = row.data
    return float(data.get("ended_at") or data.get("stopped_at") or data.get("started_at") or 0)


def group_rows(servers: list[dict], unmanaged: list[dict], filt: str) -> list[Row]:
    """One section per git repo. A repo with a live server comes first; the rest are alphabetical."""
    known = [(s.get("root") or "", repo_label(s.get("root") or "")) for s in servers if s.get("root")]
    labeled: list[tuple[str, Row]] = []
    for server in servers:
        labeled.append((_repo_of(server, known), Row("server", server["id"], server)))
    for unit in unmanaged:
        key = f"u:{unit.get('hostname') or unit['root_pid']}"
        labeled.append((_repo_of(unit, known), Row("unmanaged", key, unit)))
    if filt:
        needle = filt.lower()

        def keep(item: tuple[str, Row]) -> bool:
            repo, row = item
            hay = f"{repo} {row.label} {row.data.get('cwd', '')} {row.data.get('cmd', '')} {row.data.get('command', '')} {row.state}"
            return needle in hay.lower()

        labeled = [item for item in labeled if keep(item)]

    groups: dict[str, list[Row]] = {}
    for repo, row in labeled:
        groups.setdefault(repo, []).append(row)

    def member_key(row: Row) -> tuple:
        name = (row.data.get("name") or row.label).lower()
        return (not row.live, name, -_recency(row), row.label.lower())

    out: list[Row] = []
    for repo in sorted(groups, key=lambda name: (not any(r.live for r in groups[name]), name == "other", name.lower())):
        members = groups[repo]
        members.sort(key=member_key)
        out.append(Row("header", f"h:{repo}", {"title": repo, "live": any(r.live for r in members)}))
        out.extend(members)
    return out


class Model:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.data: dict | None = None
        self.error: str | None = None
        self.loaded_at = 0.0
        self.busy: dict[str, str] = {}
        self.message: tuple[str, str, float] | None = None
        self.wake = threading.Event()
        self.stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while not self.stop.is_set():
            code, out = run_devd("ls", "--json", timeout=20)
            with self.lock:
                if code == 0:
                    try:
                        self.data = json.loads(out)
                        self.error = None
                        self.loaded_at = time.time()
                    except json.JSONDecodeError as exc:
                        self.error = f"bad json from devd ls: {exc}"
                else:
                    self.error = out.splitlines()[-1] if out else f"devd ls exited {code}"
            self.wake.wait(REFRESH_S)
            self.wake.clear()

    def refresh_now(self) -> None:
        self.wake.set()

    def say(self, text: str, level: str = "info") -> None:
        with self.lock:
            self.message = (text, level, time.time())

    def act(self, key: str, label: str, steps: list[list[str]]) -> None:
        """Runs devd commands in sequence in the background; `{id}` in a step is replaced by the id the previous step printed."""
        with self.lock:
            if key in self.busy:
                return
            self.busy[key] = label

        def work() -> None:
            last_id = None
            code, out = 0, ""
            for step in steps:
                args = [a.replace("{id}", last_id or "") for a in step]
                code, out = run_devd(*args)
                m = re.search(r"^stopped (\S+@\S+)", out, re.M)
                if m:
                    last_id = m.group(1)
                if code != 0:
                    break
            first = next((ln for ln in out.splitlines() if ln.strip() and not ln.startswith("logs:")), "")
            level = "ok" if code == 0 else "warn" if code in (3, 4) else "error"
            hint = "  (press U to start over budget)" if code == 4 else ""
            self.say(f"{label}: {first or 'done'}{hint}", level)
            with self.lock:
                self.busy.pop(key, None)
            self.refresh_now()

        threading.Thread(target=work, daemon=True).start()

    def rows(self, filt: str) -> list[Row]:
        with self.lock:
            data = self.data
        if not data:
            return []
        return group_rows(data.get("servers", []), data.get("unmanaged", []), filt)


# ---------- log tail ----------


def clean_line(line: str) -> str:
    line = ANSI_RE.sub("", line)
    if "\r" in line:
        line = line.rstrip("\r").split("\r")[-1]
    return line.replace("\t", "    ")


class LogTail:
    def __init__(self, path: str | None) -> None:
        self.path = path
        self.offset = 0
        self.lines: list[str] = []
        self.partial = ""
        self.inode = None

    def poll(self) -> None:
        if not self.path:
            return
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return
        if self.inode != st.st_ino or st.st_size < self.offset:
            self.inode, self.offset, self.lines, self.partial = st.st_ino, 0, [], ""
            if st.st_size > 2_000_000:
                self.offset = st.st_size - 2_000_000
        if st.st_size == self.offset:
            return
        with open(self.path, "rb") as fh:
            fh.seek(self.offset)
            chunk = fh.read()
        self.offset += len(chunk)
        text = self.partial + chunk.decode("utf-8", errors="replace")
        parts = text.split("\n")
        self.partial = parts.pop()
        self.lines.extend(clean_line(p) for p in parts)
        if len(self.lines) > LOG_MAX_LINES:
            del self.lines[: len(self.lines) - LOG_MAX_LINES]

    def all_lines(self) -> list[str]:
        return self.lines + ([clean_line(self.partial)] if self.partial else [])


# ---------- formatting ----------


def human_kb(kb: int) -> str:
    if not kb:
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
    return "~" + p[len(home):] if p and p.startswith(home) else (p or "")


# ---------- UI ----------


C_OK, C_WARN, C_ERR, C_DIM, C_ACCENT, C_UNMANAGED, C_BAR, C_SEL, C_DEVD = range(1, 10)


class App:
    def __init__(self, scr) -> None:
        self.scr = scr
        self.model = Model()
        self.mode = "list"
        self.sel_key: str | None = None
        self.scroll = 0
        self.filter = ""
        self.input: tuple[str, str, object] | None = None
        self.confirm: tuple[str, object] | None = None
        self.log_row: Row | None = None
        self.log: LogTail | None = None
        self.log_offset = 0
        self.log_follow = True
        self.log_wrap = True
        self.log_search = ""
        self.preview: LogTail | None = None
        self.preview_key: str | None = None
        self.setup()

    def setup(self) -> None:
        curses.curs_set(0)
        self.scr.keypad(True)
        self.scr.timeout(250)
        curses.use_default_colors()
        pairs = {
            C_OK: curses.COLOR_GREEN, C_WARN: curses.COLOR_YELLOW, C_ERR: curses.COLOR_RED, C_DIM: 8 if curses.COLORS > 8 else curses.COLOR_WHITE,
            C_ACCENT: curses.COLOR_CYAN, C_UNMANAGED: curses.COLOR_MAGENTA, C_DEVD: curses.COLOR_BLUE,
        }
        for pair, color in pairs.items():
            curses.init_pair(pair, color, -1)
        curses.init_pair(C_BAR, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(C_SEL, curses.COLOR_BLACK, curses.COLOR_WHITE)

    # ----- drawing helpers -----

    def put(self, y: int, x: int, text: str, attr: int = 0, width: int | None = None) -> None:
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        limit = (width if width is not None else w - x)
        limit = min(limit, w - x - (1 if y == h - 1 else 0))
        if limit <= 0:
            return
        try:
            self.scr.addnstr(y, x, text, limit, attr)
        except curses.error:
            pass

    def fill(self, y: int, attr: int) -> None:
        _, w = self.scr.getmaxyx()
        self.put(y, 0, " " * w, attr)

    def state_attr(self, state: str) -> int:
        if state == "running":
            return curses.color_pair(C_OK)
        if state == "starting":
            return curses.color_pair(C_WARN)
        if state in ("killed", "failed"):
            return curses.color_pair(C_ERR)
        if state == "unmanaged":
            return curses.color_pair(C_UNMANAGED)
        return curses.color_pair(C_DIM)

    def line_attr(self, line: str) -> int:
        if line.startswith("[devd]"):
            return curses.color_pair(C_ACCENT) | curses.A_BOLD
        if ERR_RE.search(line):
            return curses.color_pair(C_ERR)
        if WARN_RE.search(line):
            return curses.color_pair(C_WARN)
        return 0

    # ----- list view -----

    def selectable(self, rows: list[Row]) -> list[Row]:
        return [r for r in rows if r.kind != "header"]

    def selected(self, rows: list[Row]) -> Row | None:
        sel = self.selectable(rows)
        if not sel:
            return None
        for r in sel:
            if r.key == self.sel_key:
                return r
        self.sel_key = sel[0].key
        return sel[0]

    def move(self, rows: list[Row], delta: int) -> None:
        sel = self.selectable(rows)
        if not sel:
            return
        keys = [r.key for r in sel]
        idx = keys.index(self.sel_key) if self.sel_key in keys else 0
        self.sel_key = keys[max(0, min(len(keys) - 1, idx + delta))]

    def draw_header(self, title_extra: str = "") -> None:
        h, w = self.scr.getmaxyx()
        with self.model.lock:
            data = self.model.data
            err = self.model.error
            loaded = self.model.loaded_at
        bar = curses.color_pair(C_BAR)
        self.fill(0, bar)
        left = " devd "
        if data:
            b = data.get("budget", {})
            used_mb = b.get("rss_kb", 0) / 1024
            max_mb = b.get("max_rss_mb", 1) or 1
            servers = sum(1 for s in data["servers"] if s["status"] in LIVE) + len(data.get("unmanaged", []))
            frac = min(1.0, used_mb / max_mb)
            cells = 14
            meter = "█" * round(frac * cells) + "░" * (cells - round(frac * cells))
            left += f"│ {servers} up · {b.get('servers', 0)}/{b.get('max_servers', '?')} servers │ RAM {meter} {human_kb(b.get('rss_kb', 0))} / {max_mb / 1024:.1f}G "
        left += title_extra
        self.put(0, 0, left, bar | curses.A_BOLD)
        stale = time.time() - loaded > REFRESH_S * 4 if loaded else True
        clock = time.strftime("%H:%M:%S") + (" (stale)" if stale and data else "") + " "
        self.put(0, max(0, w - len(clock)), clock, bar)
        if err:
            self.put(1, 0, f" ! {err}", curses.color_pair(C_ERR))

    def draw_footer(self, hints: str) -> None:
        h, w = self.scr.getmaxyx()
        if self.input:
            prompt, buf, _ = self.input
            self.fill(h - 1, curses.A_REVERSE)
            self.put(h - 1, 0, f" {prompt}{buf}█", curses.A_REVERSE)
            return
        if self.confirm:
            self.fill(h - 1, curses.color_pair(C_WARN) | curses.A_REVERSE)
            self.put(h - 1, 0, f" {self.confirm[0]}  [y/N]", curses.color_pair(C_WARN) | curses.A_REVERSE | curses.A_BOLD)
            return
        with self.model.lock:
            msg = self.model.message
            busy = dict(self.model.busy)
        if busy:
            spin = "◐◓◑◒"[int(time.time() * 4) % 4]
            self.put(h - 2, 0, f" {spin} " + " · ".join(busy.values()), curses.color_pair(C_WARN))
        elif msg and time.time() - msg[2] < 12:
            color = {"ok": C_OK, "warn": C_WARN, "error": C_ERR}.get(msg[1], C_ACCENT)
            self.put(h - 2, 0, f" {msg[0]}", curses.color_pair(color))
        self.fill(h - 1, curses.A_REVERSE)
        self.put(h - 1, 0, " " + hints, curses.A_REVERSE)

    def draw_list(self) -> None:
        h, w = self.scr.getmaxyx()
        rows = self.model.rows(self.filter)
        cur = self.selected(rows)
        now = time.time()
        with self.model.lock:
            busy = dict(self.model.busy)
            loaded = self.model.data is not None

        self.draw_header(f"│ filter: {self.filter} " if self.filter else "")
        id_w = max(18, min(40, max((len(r.label) for r in rows if r.kind != "header"), default=18) + 2))
        head = f"   {'SERVER':{id_w}} {'STATE':11} {'RSS':>6} {'UP':>7} {'IDLE':>11}  URL / REASON"
        self.put(2, 0, head, curses.color_pair(C_DIM) | curses.A_BOLD)

        preview_h = max(7, h // 3) if h >= 22 else 0
        list_top, list_bottom = 3, h - 3 - preview_h
        visible = max(1, list_bottom - list_top)
        idx = next((i for i, r in enumerate(rows) if cur and r.key == cur.key), 0)
        if idx < self.scroll:
            self.scroll = idx
        elif idx >= self.scroll + visible:
            self.scroll = idx - visible + 1
        self.scroll = max(0, min(self.scroll, max(0, len(rows) - visible)))

        if not rows:
            msg = "loading…" if not loaded else ("nothing matches the filter" if self.filter else "no dev servers yet. Agents start them with `devd up`.")
            self.put(list_top + 1, 3, msg, curses.color_pair(C_DIM))

        for i, r in enumerate(rows[self.scroll: self.scroll + visible]):
            y = list_top + i
            if r.kind == "header":
                attr = curses.color_pair(C_ACCENT) | curses.A_BOLD if r.data.get("live") else curses.color_pair(C_DIM)
                self.put(y, 1, f"── {r.data['title']} " + "─" * w, attr)
                continue
            is_sel = cur is not None and r.key == cur.key
            state = r.state
            d = r.data
            if r.key in busy or (r.kind == "server" and d.get("id") in busy):
                state_txt = "working…"
            else:
                state_txt = f"{STATE_ICON.get(state, '?')} {state}"
            rss = human_kb(d.get("rss_kb") or d.get("rss") or 0) if r.live else "-"
            if r.kind == "server" and state in LIVE:
                up = human_age(now - d.get("started_at", now))
            elif r.kind == "server":
                ended = d.get("ended_at") or d.get("stopped_at")
                up = f"-{human_age(now - ended)}" if ended else "-"
            else:
                up = "-"
            if r.live:
                tail = r.url or f":{d.get('port')}"
                if not d.get("listening", True):
                    tail += "  (not listening)"
            else:
                tail = d.get("reason") or ""
            idle_txt, idle_attr = self.idle_cell(r)
            base = curses.color_pair(C_SEL) if is_sel else 0
            marker = "▶ " if is_sel else "  "
            self.put(y, 0, " " * w, base)
            self.put(y, 0, f" {marker}{r.label:{id_w}} ", base | (curses.A_BOLD if r.live else 0))
            x = 3 + id_w + 1
            self.put(y, x, f"{state_txt:11}", base if is_sel else self.state_attr(state))
            self.put(y, x + 12, f"{rss:>6} {up:>7} ", base)
            self.put(y, x + 27, f"{idle_txt:>11}  ", base if is_sel else idle_attr)
            tail_attr = base if is_sel else (curses.color_pair(C_DIM) if not r.live else curses.color_pair(C_ACCENT))
            self.put(y, x + 40, tail, tail_attr)

        if preview_h and cur:
            self.draw_preview(cur, h - 3 - preview_h, preview_h)

        hints = "↑↓ move  ⏎ logs  r start/restart  s stop  p pin  o open  c copy  f folder  d forget  / filter  S stop all  ? help  q quit"
        self.draw_footer(hints)

    def idle_cell(self, r: Row) -> tuple[str, int]:
        d = r.data
        if r.kind != "server" or d.get("status") not in LIVE:
            return "-", curses.color_pair(C_DIM)
        if d.get("keep"):
            return "kept", curses.color_pair(C_ACCENT)
        idle, limit = d.get("idle_s", 0), d.get("idle_limit_s", 0)
        if not limit:
            return human_age(idle), curses.color_pair(C_DIM)
        attr = curses.color_pair(C_WARN) if idle >= limit * 2 / 3 else curses.color_pair(C_DIM)
        return f"{human_age(idle)}/{human_age(limit)}", attr

    def draw_preview(self, row: Row, top: int, height: int) -> None:
        _, w = self.scr.getmaxyx()
        d = row.data
        title = f"── {row.label} "
        self.put(top, 0, title + "─" * w, curses.color_pair(C_DIM))
        cwd = short_path(d.get("cwd", ""))
        if row.kind == "server":
            info = f"{cwd}  ·  {d.get('cmd', '')}  ·  started by {d.get('started_by', '?')}"
            if d.get("raw"):
                info += "  ·  raw"
            if d.get("status") in LIVE and d.get("keep"):
                info += "  ·  kept (no idle stop)"
            elif d.get("status") in LIVE and d.get("idle_limit_s"):
                left = max(0, d["idle_limit_s"] - d.get("idle_s", 0))
                info += f"  ·  idle stop in {human_age(left)} (p to pin)"
        else:
            info = f"{cwd}  ·  {d.get('command', '')}  ·  pid {d.get('root_pid')}"
        self.put(top + 1, 1, info, curses.color_pair(C_DIM))
        if row.kind != "server":
            self.put(top + 3, 1, "Started outside devd, so there is no devd log. Press s to stop it, or r to restart it under devd.", curses.color_pair(C_DIM))
            return
        if self.preview_key != row.key:
            self.preview = LogTail(d.get("log"))
            self.preview_key = row.key
        self.preview.poll()
        lines = self.preview.all_lines()
        room = height - 2
        for i, line in enumerate(lines[-room:] if room > 0 else []):
            self.put(top + 2 + i, 1, line, self.line_attr(line))
        if not lines:
            self.put(top + 2, 1, "(log is empty)", curses.color_pair(C_DIM))

    # ----- log view -----

    def open_log(self, row: Row) -> None:
        if row.kind != "server" or not row.data.get("log"):
            self.model.say("No devd log: it was started outside devd.", "warn")
            return
        self.mode = "log"
        self.log_row = row
        self.log = LogTail(row.data["log"])
        self.log_offset = 0
        self.log_follow = True
        self.log_search = ""

    def current_log_row(self) -> Row | None:
        if not self.log_row:
            return None
        for r in self.model.rows(""):
            if r.key == self.log_row.key:
                self.log_row = r
        return self.log_row

    def draw_log(self) -> None:
        h, w = self.scr.getmaxyx()
        row = self.current_log_row()
        assert self.log is not None and row is not None
        self.log.poll()
        lines = self.log.all_lines()
        self.draw_header()
        state = row.state
        sub = f" {STATE_ICON.get(state, '?')} {row.label}  {state}  {row.url or ''}  ·  {short_path(row.data.get('cwd', ''))}"
        self.put(1, 0, " " * w)
        self.put(1, 0, sub, self.state_attr(state) | curses.A_BOLD)
        flags = f"{'FOLLOW' if self.log_follow else 'paused'} · {'wrap' if self.log_wrap else 'nowrap'} · {len(lines)} lines "
        self.put(1, max(0, w - len(flags)), flags, curses.color_pair(C_OK if self.log_follow else C_WARN))

        top, bottom = 2, h - 2
        room = bottom - top
        if self.log_follow:
            self.log_offset = 0
        self.log_offset = max(0, min(self.log_offset, max(0, len(lines) - 1)))
        end = len(lines) - self.log_offset
        rendered: list[tuple[str, int]] = []
        i = end - 1
        while i >= 0 and len(rendered) < room:
            line = lines[i]
            attr = self.line_attr(line)
            if self.log_search and self.log_search.lower() in line.lower():
                attr |= curses.A_REVERSE
            chunks = textwrap.wrap(line, max(10, w - 2), replace_whitespace=False, drop_whitespace=False) if self.log_wrap and line else [line]
            for chunk in reversed(chunks or [""]):
                rendered.append((chunk, attr))
            i -= 1
        rendered = list(reversed(rendered[:room]))
        for j, (text, attr) in enumerate(rendered):
            self.put(top + j, 1, text, attr)
        if not lines:
            self.put(top + 1, 1, "(log is empty)", curses.color_pair(C_DIM))

        hints = "q back  f follow  ↑↓/PgUp/PgDn scroll  g/G top/bottom  / search  n/N next/prev  w wrap  r restart  s stop  o open"
        self.draw_footer(hints)

    def search_log(self, direction: int) -> None:
        if not self.log or not self.log_search:
            return
        lines = self.log.all_lines()
        needle = self.log_search.lower()
        cur = len(lines) - 1 - self.log_offset
        rng = range(cur - 1, -1, -1) if direction < 0 else range(cur + 1, len(lines))
        for i in rng:
            if needle in lines[i].lower():
                self.log_follow = False
                self.log_offset = len(lines) - 1 - i
                return
        self.model.say(f"no more matches for “{self.log_search}”", "warn")

    # ----- actions -----

    def act_start(self, row: Row, over_budget: bool = False) -> None:
        d = row.data
        if row.kind == "unmanaged":
            self.confirm = (
                f"Restart {row.label} under devd? (stops it, then starts it supervised)",
                lambda: self.model.act(row.key, f"restarting {row.label} under devd", [["stop", row.target], ["up", "{id}"]]),
            )
            return
        if d["status"] in LIVE:
            self.model.act(d["id"], f"restarting {d['id']}", [["restart", d["id"]]])
        else:
            args = ["up", d["id"]] + (["--over-budget"] if over_budget else [])
            self.model.act(d["id"], f"starting {d['id']}", [args])

    def act_stop(self, row: Row) -> None:
        if not row.live:
            self.model.say(f"{row.label} is already down.", "warn")
            return
        self.model.act(row.key if row.kind == "unmanaged" else row.data["id"], f"stopping {row.label}", [["stop", row.target]])

    def act_open(self, row: Row) -> None:
        if not row.url or not row.live:
            self.model.say("No live URL for this server.", "warn")
            return
        subprocess.Popen(["open", row.url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.model.say(f"opened {row.url}", "ok")

    def act_copy(self, row: Row) -> None:
        if not row.url:
            self.model.say("No URL to copy.", "warn")
            return
        subprocess.run(["pbcopy"], input=row.url, text=True, check=False)
        self.model.say(f"copied {row.url}", "ok")

    def act_folder(self, row: Row) -> None:
        cwd = row.data.get("cwd")
        if not cwd or not os.path.isdir(cwd):
            self.model.say("Checkout folder is gone.", "warn")
            return
        subprocess.Popen(["open", cwd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.model.say(f"opened {short_path(cwd)}", "ok")

    def act_keep(self, row: Row) -> None:
        if row.kind != "server":
            self.model.say("Only servers started by devd have an idle timer.", "warn")
            return
        rid = row.data["id"]
        if row.data.get("keep"):
            self.model.act(rid, f"unpinning {rid}", [["keep", rid, "--off"]])
        else:
            self.model.act(rid, f"keeping {rid}", [["keep", rid]])

    def act_forget(self, row: Row) -> None:
        if row.kind != "server" or row.live:
            self.model.say("Only down servers can be forgotten. Stop it first.", "warn")
            return
        rid = row.data["id"]
        self.confirm = (f"Forget {rid}? Its record and down-state are deleted.", lambda: self.model.act(rid, f"forgetting {rid}", [["forget", rid]]))

    # ----- input -----

    def handle_text_input(self, ch: int) -> None:
        assert self.input is not None
        prompt, buf, done = self.input
        if ch in (27,):
            self.input = None
            if prompt.startswith("filter"):
                self.filter = ""
            return
        if ch in (10, 13, curses.KEY_ENTER):
            self.input = None
            done(buf)  # type: ignore[operator]
            return
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            buf = buf[:-1]
        elif 32 <= ch < 0x110000:
            try:
                buf += chr(ch)
            except ValueError:
                pass
        self.input = (prompt, buf, done)
        if prompt.startswith("filter"):
            self.filter = buf

    def handle_key(self, ch: int) -> bool:
        if self.input:
            self.handle_text_input(ch)
            return True
        if self.confirm:
            text, fn = self.confirm
            self.confirm = None
            if ch in (ord("y"), ord("Y")):
                fn()  # type: ignore[operator]
            else:
                self.model.say("cancelled", "info")
            return True
        if self.mode == "help":
            self.mode = "list"
            return True
        if self.mode == "log":
            return self.handle_log_key(ch)
        return self.handle_list_key(ch)

    def handle_list_key(self, ch: int) -> bool:
        rows = self.model.rows(self.filter)
        cur = self.selected(rows)
        if ch in (ord("q"), 3):
            return False
        if ch in (curses.KEY_DOWN, ord("j")):
            self.move(rows, 1)
        elif ch in (curses.KEY_UP, ord("k")):
            self.move(rows, -1)
        elif ch in (curses.KEY_NPAGE,):
            self.move(rows, 10)
        elif ch in (curses.KEY_PPAGE,):
            self.move(rows, -10)
        elif ch in (curses.KEY_HOME, ord("g")):
            self.move(rows, -10_000)
        elif ch in (curses.KEY_END, ord("G")):
            self.move(rows, 10_000)
        elif ch == ord("?"):
            self.mode = "help"
        elif ch == ord("/"):
            self.input = ("filter: ", self.filter, lambda s: setattr(self, "filter", s))
        elif ch == 27:
            self.filter = ""
        elif ch == ord("R"):
            self.model.refresh_now()
            self.model.say("refreshing…")
        elif ch == ord("S"):
            self.confirm = ("Stop ALL dev servers, including ones started outside devd?", lambda: self.model.act("all", "stopping everything", [["stop", "--all"]]))
        elif ch == ord("X"):
            self.confirm = ("Stop every server started outside devd?", lambda: self.model.act("strays", "stopping strays", [["stop", "--strays"]]))
        elif cur is None:
            return True
        elif ch in (10, 13, curses.KEY_ENTER, ord("l")):
            self.open_log(cur)
        elif ch == ord("r"):
            self.act_start(cur)
        elif ch == ord("U"):
            if cur.kind == "server" and not cur.live:
                self.act_start(cur, over_budget=True)
        elif ch == ord("s"):
            self.act_stop(cur)
        elif ch == ord("o"):
            self.act_open(cur)
        elif ch == ord("c"):
            self.act_copy(cur)
        elif ch == ord("f"):
            self.act_folder(cur)
        elif ch in (ord("d"), curses.KEY_DC):
            self.act_forget(cur)
        elif ch == ord("p"):
            self.act_keep(cur)
        return True

    def handle_log_key(self, ch: int) -> bool:
        h, _ = self.scr.getmaxyx()
        page = max(1, h - 5)
        row = self.current_log_row()
        if ch in (ord("q"), 27, ord("h"), curses.KEY_LEFT):
            self.mode = "list"
            self.log = None
        elif ch in (curses.KEY_UP, ord("k")):
            self.log_follow = False
            self.log_offset += 1
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.log_offset = max(0, self.log_offset - 1)
            self.log_follow = self.log_offset == 0
        elif ch in (curses.KEY_PPAGE, ord("b")):
            self.log_follow = False
            self.log_offset += page
        elif ch in (curses.KEY_NPAGE, ord(" ")):
            self.log_offset = max(0, self.log_offset - page)
            self.log_follow = self.log_offset == 0
        elif ch == ord("g"):
            self.log_follow = False
            self.log_offset = 10**9
        elif ch == ord("G"):
            self.log_follow = True
        elif ch == ord("f"):
            self.log_follow = not self.log_follow
        elif ch == ord("w"):
            self.log_wrap = not self.log_wrap
        elif ch == ord("/"):
            def done(s: str) -> None:
                self.log_search = s
                self.search_log(-1)
            self.input = ("search: ", "", done)
        elif ch == ord("n"):
            self.search_log(-1)
        elif ch == ord("N"):
            self.search_log(1)
        elif row and ch == ord("r"):
            self.act_start(row)
            self.log_follow = True
        elif row and ch == ord("s"):
            self.act_stop(row)
        elif row and ch == ord("o"):
            self.act_open(row)
        elif row and ch == ord("c"):
            self.act_copy(row)
        return True

    def draw_help(self) -> None:
        self.draw_header("│ help ")
        lines = [
            "devd control panel. It reads `devd ls --json` every 2 s and runs devd commands as you.",
            "",
            "List",
            "  grouped by git repo; a repo with something running is listed first",
            "  ↑ ↓ / j k     move               ⏎ / l     open the log",
            "  r             start a down server, or restart a running one",
            "                (on a server started outside devd: stop it and restart it supervised)",
            "  U             start a down server even if the budget is full",
            "  s             stop (agents will not restart it until you start it again)",
            "  p             pin / unpin: a pinned server is never stopped for being idle",
            "                (agent-started servers stop after 15m idle: no CPU, log output, or new connections)",
            "  o / c / f     open URL in browser / copy URL / open the checkout folder",
            "  d             forget a down server (deletes its record)",
            "  /             filter by name, path, command, or state     Esc clears",
            "  S / X         stop everything / stop servers started outside devd",
            "  R             refresh now                                  q quit",
            "",
            "Log view",
            "  f follow on/off   ↑ ↓ PgUp PgDn scroll   g / G top / bottom   w wrap",
            "  / search   n / N older / newer match   r restart   s stop   o open   q back",
            "",
            "States",
            "  ● running   ◐ starting   ○ stopped (by you, an agent, or idle)   ✕ killed from outside or failed at startup",
            "  ◌ exited on its own   ◆ started outside devd",
            "",
            "Press any key to go back.",
        ]
        for i, line in enumerate(lines):
            attr = curses.A_BOLD if line in ("List", "Log view", "States") else 0
            self.put(2 + i, 2, line, attr)

    # ----- loop -----

    def run(self) -> None:
        drawn_layout = (self.mode, self.filter)
        try:
            while True:
                if (self.mode, self.filter) != drawn_layout:
                    # full repaint: curses' line-diffing leaves stale rows behind when the layout changes
                    self.scr.clear()
                    drawn_layout = (self.mode, self.filter)
                self.scr.erase()
                if self.mode == "log" and self.log is not None:
                    self.draw_log()
                elif self.mode == "help":
                    self.draw_help()
                else:
                    self.draw_list()
                self.scr.refresh()
                ch = self.scr.getch()
                if ch == -1:
                    continue
                if ch == curses.KEY_RESIZE:
                    curses.update_lines_cols()
                    self.scr.clear()
                    continue
                if not self.handle_key(ch):
                    break
        finally:
            self.model.stop.set()
            self.model.wake.set()


def main() -> None:
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    try:
        curses.wrapper(lambda scr: App(scr).run())
    except KeyboardInterrupt:
        pass
