# devd

**A supervisor for local dev servers, built for working with coding agents.**

[![CI](https://github.com/filipditrich/devd/actions/workflows/ci.yml/badge.svg)](https://github.com/filipditrich/devd/actions/workflows/ci.yml)
![macOS](https://img.shields.io/badge/platform-macOS-lightgrey)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![no dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)
[![MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Coding agents (Cursor, Claude Code, Codex, and anything running them, such as Orca) start dev servers all day. They put them in background shells nobody can see, fight over port 3000, start a second copy of something that is already running, quietly restart the server you just killed because it was eating 6 GB, and leave a dozen `next dev` processes behind when the session ends.

devd gives every local dev server one owner. Agents and you both start servers with `devd up`. devd runs them detached behind [portless](https://github.com/vercel-labs/portless), so each gets a stable `https://<name>.localhost` URL instead of a port. Every server is logged and listed, killable in one go, and stopped automatically once an agent stops using it. A shell hook in each agent harness turns raw `bun dev` / `next dev` / `vite` into `devd up`, and blocks the things only you should decide.

```text
 devd │ 2 up · 2/5 servers │ RAM ██░░░░░░░░░░░░ 1.1G / 8.0G                                        14:02:11

   SERVER                      STATE          RSS      UP        IDLE  URL / REASON
 ▶ shop@fix-cart               ● running     812M     21m    2m/15m    https://fix-cart.shop.localhost
   api@acme-api                ● running     301M    1h04m        3s  https://api.localhost

 ── down ──────────────────────────────────────────────────────────────────────────────────────────────
   admin@acme-admin            ■ stopped        -    -12m          -  stopped after 15m idle at 13:50
   mail@acme-mail              ✕ killed         -     -2h          -  killed from outside devd at 11:58 (Activity Monitor, kill, or OOM)

── shop@fix-cart ──────────────────────────────────────────────────────────────────────────────────────
 ~/code/.worktrees/fix-cart/acme-shop  ·  bun run dev  ·  started by agent  ·  idle stop in 13m (p to pin)
  ✓ Compiled /checkout in 412ms
  GET /checkout 200 in 38ms

 ↑↓ move  ⏎ logs  r start/restart  s stop  p pin  o open  c copy  f folder  d forget  / filter  ? help  q quit
```

## What you get

- **One way to start servers.** `devd up` runs the server detached in its own session and waits until the port answers. It prints the URL, or reuses the server if it is already up.
- **Stable URLs instead of ports.** portless gives `https://shop.localhost`; git worktrees get `https://<branch>.shop.localhost`. Nothing fights over 3000.
- **You stay in charge.** If you stop a server, or kill it in Activity Monitor, or the OOM killer takes it, agents are told to leave it down and ask you. They can still restart their own servers and retry ones that crashed during startup.
- **No leftovers.** Agent-started servers stop themselves after 15 idle minutes (no requests, CPU, or log output), and you get a notification. Agents are told to stop servers when they are done.
- **A memory and server budget.** An agent cannot start a sixth server, or go past 8 GB of dev-server RAM, without asking you.
- **Clean kills.** devd tags the whole process tree (`DEVD_ID`), so a stop really stops the server, including the workers that `next dev` and `turbo` fork.
- **A terminal control panel.** `devd` opens the panel shown above. It shows live state, RSS, uptime and idle time, tails logs with follow and search, and starts, stops, restarts or pins servers with one key.
- **Works across harnesses.** The same guard is wired into Cursor, Claude Code and Codex. Orca runs those agents, so it is covered too.
- **No dependencies.** Python standard library plus a small Node script for the hook. Nothing to `pip install`.

## Requirements

- macOS. devd reads process, socket and port data from `ps`, `lsof` and `netstat` output.
- Python 3.10+ (the system or Homebrew `python3` is fine).
- Node.js 20+ (for the agent hook).
- [portless](https://github.com/vercel-labs/portless) with its proxy running: `npm i -g portless && portless proxy start`. Use `portless service install` to start it at login.

## Install

```bash
git clone https://github.com/filipditrich/devd ~/code/devd
~/code/devd/bin/devd setup           # add --dry-run to preview, --harness cursor,claude to limit
devd doctor                          # check everything is wired
```

`devd setup` links `~/.local/bin/devd` and the hook launcher into this checkout. It wires the hook into every harness it finds, installs the agent instructions, and creates `~/.config/devd/config.json`. Running it again is safe. Every JSON or Markdown file it edits is backed up once as `<file>.devd-backup`. Restart running agent sessions afterwards. Codex additionally asks you to trust the new hook once in `/hooks`.

[docs/setup.md](docs/setup.md) lists every file setup touches and how to wire things by hand.

## Quick start

```bash
cd ~/code/acme-shop
devd up                       # runs the package's dev script behind portless, prints https://acme-shop.localhost
devd up --name shop -- bun run dev --turbo     # explicit name and command
devd ls                       # what is running, what is down and why, budget
devd logs shop -f             # follow the log
devd restart shop
devd stop shop                # agents will not restart it until you do
devd                          # control panel
```

Ids look like `name@checkout`: `shop@acme-shop`, or `shop@fix-cart` for a checkout under `.worktrees/fix-cart/`. Commands accept an id, a name (if unique), a hostname, or a path.

## How agents use it

Agents get short instructions: a Cursor rule, a skill, and a block in `CLAUDE.md` / `AGENTS.md`. They say:

- run `devd ls` first and reuse;
- start with `devd up`;
- stop your server when you are done;
- respect exit codes 3 (left down) and 4 (budget).

The hook enforces the important parts:

| The agent runs | What happens |
| --- | --- |
| `bun run dev`, `pnpm dev`, `next dev`, `vite`, `nest start --watch` | rewritten to `devd up --name <app> -- '<command>'` |
| `cd apps/web && PORT=3000 bun dev &` | rewritten to `devd up --cwd apps/web -- '…'`; the port is dropped so portless picks one |
| `portless run …`, `portless <name> <cmd>` | rewritten to `devd up` |
| `devd …` | marked as an agent call (`DEVD_ACTOR=agent`) |
| `devd stop --all`, `devd forget`, `devd keep`, `devd config k=v`, `devd up --over-budget`, `devd setup`, `devd ui` | denied: user only |
| `portless --force`, `portless proxy stop`, `portless clean` | denied: user only |
| a dev server inside `tmux`, `screen`, `pm2`, or an Orca/Paseo terminal | denied |
| `tsc --watch`, `docker compose up`, `next build`, your `ignore` patterns | left alone |

The hook is a guard rail, not a sandbox. It sees the shell commands the harness shows it, and it fails open: if Node is missing, commands go through unchanged.

## Lifecycle rules

| Status | Meaning | Agent may `devd up` it? |
| --- | --- | --- |
| `running`, `starting` | up | reuses it |
| `failed` | died during the first 60 s | yes, after fixing the error |
| `stopped` by agent | an agent ran `devd stop` | yes |
| `stopped` by idle | idle auto-stop | yes |
| `stopped` by user | you ran `devd stop` (or `s` in the panel) | no: exit 3, the agent asks you |
| `killed` | killed outside devd (Activity Monitor, `kill`, OOM) | no: exit 3 |
| `exited` | exited on its own after startup | no: exit 3 |

## Idle auto-stop

Every 20 s, each server's runner checks whether anything is using it:

- CPU time spent by its process tree (at least 0.3 s);
- new log output;
- a new TCP connection on its port, including short requests already in TIME_WAIT.

If none of these happen for `agent_idle` (15 minutes by default), an agent-started server is stopped as `stopped by idle`. You get a macOS notification, and anyone can bring it back with `devd up <id>`. `devd ls` and the panel never count as activity.

Servers you start have no idle limit unless you set `user_idle`. Pin any server with `devd keep <id>` (or `p` in the panel).

```bash
devd config agent_idle=30m user_idle=2h     # off disables
```

## Budget

```bash
devd config                       # max_rss=8.0G max_servers=5 agent_idle=15m user_idle=off
devd config max_rss=12G max_servers=6
```

The budget counts devd servers and dev servers started outside devd. An agent that would go over it gets exit 4. You can override it once with `devd up --over-budget`.

## Configuration

Budget and idle limits live in devd's state and are set with `devd config`. Everything about your projects lives in `~/.config/devd/config.json`, which both devd and the hook read:

```json
{
  "work_dirs": ["~/code"],
  "names": [
    { "match": "/acme-shop", "name": "shop" },
    { "pattern": "/acme-api/services/api(?:/|$)", "name": "api" }
  ],
  "dev_scripts": ["storybook"],
  "dev_commands": ["\\bbun\\s+run\\s+src/main\\.ts\\b"],
  "ignore": ["\\bworker-[a-z]+\\b"],
  "wrap_start": []
}
```

Every key is optional. Without a config, devd names an app from the `portless` key in `package.json`, then the package name, then the folder name. See [docs/configuration.md](docs/configuration.md).

## Commands

| Command | |
| --- | --- |
| `devd up [target] [--name N] [--cwd DIR] [--raw] [--wait S] [-- cmd…]` | start, or print the URL if already up |
| `devd ls [--json] [--all]` | servers, unmanaged servers, down records, budget |
| `devd logs <target> [-n N] [-f]` | show or follow the log |
| `devd restart <target>` | restart in place |
| `devd stop <target> \| --app NAME \| --strays \| --all` | stop and keep down |
| `devd keep <target> [--off]` | exempt from idle auto-stop |
| `devd config [key=value …]` | show or set budget and idle limits |
| `devd forget <target>` / `devd prune` | drop a down record / drop records whose checkout is gone |
| `devd` / `devd ui` | control panel (`?` shows every key) |
| `devd setup [--harness …] [--dry-run] [--uninstall]` | install or remove the integration |
| `devd doctor` | check the install |

Exit codes: `0` ok, `1` error (including a startup failure), `3` left down by the user or killed from outside, `4` over budget, `5` user-only action.

## Uninstall

```bash
devd stop --all
devd setup --uninstall      # removes links, hooks and instruction blocks; keeps config and state
rm -rf ~/.config/devd ~/.portless/devd    # optional
```

## Development

```bash
node --test tests/guard.test.mjs                 # hook rules
python3 -m unittest discover -s tests            # core logic and setup (throwaway HOME)
bash tests/e2e/lifecycle.sh                      # real servers through portless (isolated state)
bash tests/e2e/idle.sh                           # idle auto-stop, about 4 minutes
pip install pyte && python3 tests/e2e/ui.py      # drives the control panel in a pseudo-terminal
```

The end-to-end tests need the portless proxy. They use their own state dir and config, so your servers and settings are left alone. [docs/how-it-works.md](docs/how-it-works.md) explains the internals.

## License

[MIT](LICENSE)
