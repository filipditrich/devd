# Configuration

devd has two kinds of settings:

- **Limits** (memory budget, server count, idle timeouts). These change often. Set them with `devd config` and they are stored in devd's state.
- **Project rules** (where your code lives, what to call each app, which commands are dev servers). These live in `~/.config/devd/config.json`, which both devd and the agent hook read.

## Limits: `devd config`

```bash
devd config                                   # show
devd config max_rss=12G max_servers=6         # set (user only)
devd config agent_idle=30m user_idle=off
```

| Key | Default | Meaning |
| --- | --- | --- |
| `max_rss` | `8G` | total RSS of all dev servers before agents get exit 4 |
| `max_servers` | `5` | number of running dev servers before agents get exit 4 |
| `agent_idle` | `15m` | idle time before an agent-started server is stopped; `off` disables |
| `user_idle` | `off` | the same for servers you start |

Durations accept `30s`, `15`, `15m`, `2h`, `off`. Sizes accept `512M`, `8G`.

## Project rules: `~/.config/devd/config.json`

All keys are optional. `devd setup` creates the file from [`config.example.json`](../config.example.json). A missing file means defaults; invalid JSON makes devd exit with an error naming the file.

### `work_dirs`

Default: `["~"]`. Directories that hold your code. Dev servers started outside devd (from a plain terminal, say) are only listed and counted if their working directory is inside one of these. Servers with a portless route are always listed.

### `names`

The app name decides the URL (`https://<name>.localhost`) and the id (`<name>@<checkout>`). devd picks the name in this order:

1. `--name` on `devd up`;
2. the `portless` key in the nearest `package.json` (`"portless": "shop"` or `"portless": { "name": "shop" }`);
3. the first matching `names` rule, tested against `"<cwd> <command>"`, then against the package name;
4. the `package.json` name without its scope;
5. the git checkout folder name.

Each rule has either `match` (a plain substring) or `pattern` (a JavaScript regular expression), plus `name`. The first rule that matches wins, so put specific rules before general ones:

```json
"names": [
  { "match": "/acme-api/apps/admin", "name": "api-admin" },
  { "pattern": "/acme-api(?:/|\\s|$)", "name": "api" },
  { "pattern": "\\bportal\\b.*(?:apps/web|--filter[= ]@portal/web)", "name": "portal-web" }
]
```

Matching against the command as well as the directory handles monorepo roots, where `bun run --filter @portal/web dev` runs from the repo root.

### `dev_scripts`

Default: `[]`. Extra package script names that start a dev server, on top of `dev` (and `start`/`serve` for devd's own checks). For example, `["storybook"]` makes the hook treat `bun run storybook` as a dev server.

### `dev_commands`

Default: `[]`. Regular expressions for commands that start a dev server but don't look like one, such as `"\\bbun\\s+run\\s+src/main\\.ts\\b"` for a Bun API started from its entry file.

### `ignore`

Default: `[]`. Regular expressions, tested against `"<cwd> <command>"`, for commands the hook must never wrap: background workers, MCP servers, scripts that manage their own ports. Built-in exceptions that always pass through: `tsc --watch`, rollup, docker, `next build`, `next start`, `tsx` scripts.

### `wrap_start`

Default: `[]`. `<pm> start` is usually a production start, so the hook leaves it alone. List regexes (on `"<cwd> <command>"`) for checkouts where `start` really is the dev server.

## Environment variables

| Variable | Meaning |
| --- | --- |
| `DEVD_CONFIG` | path to the project rules file (default `~/.config/devd/config.json`) |
| `DEVD_STATE_DIR` | state, lock and logs directory (default `~/.portless/devd`) |
| `DEVD_BIN` | the devd executable the hook writes into rewritten commands |
| `DEVD_IDLE_TICK_S` | idle check interval in seconds (default 20; tests use 4) |
| `DEVD_ACTOR` | `agent` or `user`; set by the hook. Agents must not set it, and the hook denies commands that do. |

devd treats a call as coming from an agent when `DEVD_ACTOR=agent` is set, or when it sees the harness markers `CURSOR_AGENT`, `CLAUDECODE`, `CODEX_THREAD_ID` or `CODEX_SANDBOX`.
