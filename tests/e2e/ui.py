"""Drives `devd ui` in a pseudo-terminal against throwaway servers and checks the rendered screens. Needs `pip install pyte`."""

import fcntl
import json
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

import pyte

REPO = Path(__file__).resolve().parents[2]
DEVD = str(REPO / "bin" / "devd")
ROOT = Path(tempfile.mkdtemp(prefix="devd-ui.")).resolve()
APP = ROOT / "uiapp"
RID = "devdui@uiapp"
COLS, ROWS = 150, 38

env = {k: v for k, v in os.environ.items() if k not in ("CURSOR_AGENT", "CLAUDECODE", "CODEX_THREAD_ID", "CODEX_SANDBOX")}
env.update(TERM="xterm-256color", DEVD_ACTOR="user", DEVD_STATE_DIR=str(ROOT / "state"), DEVD_CONFIG=str(ROOT / "config.json"), DEVD_BIN=DEVD)
fails: list[str] = []


def devd(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([DEVD, *args], capture_output=True, text=True, env=env, cwd=APP, check=False)


def status() -> dict | None:
    data = json.loads(devd("ls", "--json").stdout)
    return next((s for s in data["servers"] if s["id"] == RID), None)


def check(cond: object, what: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        fails.append(what)


(ROOT / "config.json").write_text(json.dumps({"work_dirs": [str(ROOT)]}))
APP.mkdir(parents=True)
(APP / "server.js").write_text(
    "const http=require('http');let n=0;setInterval(()=>console.log('tick',++n),1000);"
    "http.createServer((q,s)=>s.end('ok\\n')).listen(Number(process.env.PORT||3998),'127.0.0.1',()=>console.log('listening'));\n"
)
(APP / "package.json").write_text('{"name":"devdui","private":true,"scripts":{"dev":"node server.js"}}\n')
(APP / "package-lock.json").touch()
subprocess.run(["git", "init", "-q"], cwd=APP, check=True)
print(devd("up", "--name", "devdui", "--", "npm", "run", "dev").stdout.strip())
devd("up", "--name", "devdother", "--", "node", "does-not-exist.js")

screen = pyte.Screen(COLS, ROWS)
stream = pyte.ByteStream(screen)
pid, fd = pty.fork()
if pid == 0:
    fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
    os.execve(DEVD, [DEVD, "ui"], env)


def pump(seconds: float) -> str:
    end = time.time() + seconds
    while time.time() < end:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if ready:
            try:
                stream.feed(os.read(fd, 65536))
            except OSError:
                break
    return "\n".join(line.rstrip() for line in screen.display).rstrip()


def send(keys: str, wait: float = 1.0) -> str:
    os.write(fd, keys.encode())
    return pump(wait)


def last_tick(text: str) -> int:
    return ([0] + [int(n) for n in re.findall(r"tick (\d+)", text)])[-1]


try:
    text = pump(3.5)
    check(RID in text and "running" in text, "list shows the test server running")
    check("devdother@uiapp" in text and "failed during startup" in text, "list shows the failed server in the down section")

    text = send("/devdui\n", 1.5)
    check("filter: devdui" in text and "devdother" not in text, "filter narrows the list")
    check("tick" in text, "preview pane tails the log")

    t1 = last_tick(send("l", 2.0))
    text = pump(2.5)
    t2 = last_tick(text)
    check("FOLLOW" in text and t2 > t1 > 0, f"log view follows new lines ({t1} -> {t2})")
    check("paused" in send("f", 0.5), "f pauses follow")
    check("listening" in send("/listening\n", 0.8), "search finds a line")

    send("q", 0.5)
    text = send("s", 8)
    s = status()
    check(s and s["status"] == "stopped" and s.get("stopped_by") == "user", "s stops it as the user")
    check("stopped by user" in text, "row shows the down reason")

    send("r", 9)
    s = status()
    check(s and s["status"] == "running" and s["listening"], "r starts it again")

    send("r", 10)
    s2 = status()
    check(s2 and s2["status"] == "running" and s2["runner_pid"] != s["runner_pid"], "r on a running server restarts it")

    check("Only down servers" in send("d", 0.6), "forget refuses a running server")
    check("Log view" in send("?", 0.8), "help screen")
    send("x", 0.5)
    send("\x1b", 0.5)
    send("q", 1.0)
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        done = pid
    check(done == pid, "q quits")
finally:
    devd("stop", RID)
    shutil.rmtree(ROOT, ignore_errors=True)

print(f"\n{len(fails)} failures")
sys.exit(1 if fails else 0)
