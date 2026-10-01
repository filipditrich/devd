---
name: devd
description: Start, reuse, stop, and debug local dev servers through devd (supervised, behind portless named .localhost URLs). Use when starting next/vite/nest/bun/pnpm dev, debugging EADDRINUSE or port fights, a server that will not start or keeps stopping, or opening a local app in the browser.
---

# Dev servers: devd + portless

`devd` supervises every local dev server. It starts `portless run --name <name> -- <cmd>` detached in its own session, tags the whole process tree with `DEVD_ID`, logs to `~/.portless/devd/logs/<id>.log`, and keeps state in `~/.portless/devd/state.json`. Portless (HTTPS on 443) gives the URL. A shell hook (`~/.local/share/devd/guard`) rewrites raw dev commands into `devd up` for Cursor, Claude Code, and Codex.

## Workflow

1. `devd ls`. Reuse a live URL. Do not spawn a second server in the same checkout.
2. `devd up [--name <name>] [-- <cmd>]`. It returns once the port answers and prints the URL. No `&`, `nohup`, tmux, or other terminals.
3. Browser: `https://<name>.localhost`. Checkouts under `.worktrees/<effort>/` get `https://<branch>.<name>.localhost` and the id `<name>@<effort>`.
4. `devd logs <id> [-f]` for output, `devd restart <id>` after config changes.
5. Done with it (tests passed, screenshot taken, change verified)? `devd stop <id>` before you finish. Leave it running only when you hand it to the user to check, and say so with the id and URL.
6. Exit 3: the user stopped it, or it was killed (Activity Monitor, OOM) or died on its own. Do not start it again. Tell the user; they run `devd up <id>`. A failure during the first 60 s of startup is not exit 3, so fix the error and retry.
7. Exit 4: the budget is full (`devd config` shows it). Reuse, or ask the user to stop something.
8. Never `portless --force`, `portless proxy stop`, `devd stop --all`, `devd forget`, `devd keep`, or `devd config key=value`. Those are for the user.
9. Health: `portless doctor`, `devd doctor`. `devd ls` marks routes that are registered but `(not listening)`.

## Idle auto-stop

The runner checks every 20 s whether the server is in use: CPU spent by its process tree (at least 0.3 s), log output, or a new TCP connection on its port (including TIME_WAIT, so short requests count). An agent-started server with none of those for `agent_idle` (15 min by default) is stopped with `stopped_by: idle`, and the user gets a macOS notification. Agents may `devd up` it again; it is not a user stop.

- Do not keep a server alive with polling, curl loops, or restarts.
- `devd ls` and `devd ui` do not count as activity.
- User-started servers have no limit unless the user sets `user_idle`. The user can pin one with `devd keep <id>`.

## Names

The app name decides the URL. devd takes it from, in order: `--name`, the `portless` key in package.json, the `names` rules in `~/.config/devd/config.json`, the package.json name (without scope), the checkout folder name. Scripts that already run portless or Turbo go through `devd up --raw` (the hook adds `--raw`).

## Do not wrap

- Watchers that are not HTTP apps (`tsc --watch`, rollup)
- Docker, databases, MCP servers: leave their ports. Optional `portless alias <name> <port>`.
- Anything matching `ignore` in the user's config

## Escape

`PORTLESS=0 bun run dev` runs the script on its default port. It still goes through `devd up --raw`, so it stays supervised.
