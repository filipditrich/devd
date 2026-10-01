# Setup

## 1. Prerequisites

```bash
python3 --version        # 3.10+
node --version           # 20+
npm i -g portless        # or: bun add -g portless
portless proxy start     # HTTPS on 443; asks for sudo once to trust its CA
portless service install # optional: start the proxy at login
```

## 2. Install

```bash
git clone https://github.com/filipditrich/devd ~/code/devd
~/code/devd/bin/devd setup --dry-run    # preview
~/code/devd/bin/devd setup
devd doctor
```

Make sure `~/.local/bin` is on your `PATH`. `devd doctor` tells you if it isn't. Keep the clone where it is: the links point into it, and `git pull` updates everything in place.

## What `devd setup` changes

Setup only touches harnesses whose folder exists (`~/.cursor`, `~/.claude`, `~/.codex`). Limit it with `--harness cursor,claude`. Before setup first edits a JSON or Markdown file, it copies the file to `<file>.devd-backup`. If a link target already exists as a regular file or folder, setup renames it to `<name>.devd-backup`.

| Target | Change |
| --- | --- |
| `~/.local/bin/devd` | symlink to `bin/devd` |
| `~/.local/share/devd/guard` | symlink to `guard/guard` (the hook launcher). The hook commands use this stable path, so moving the clone and re-running setup doesn't change them. |
| `~/.config/devd/config.json` | created from `config.example.json` if missing |
| `~/.cursor/hooks.json` | `preToolUse` (matcher `Shell`) and `beforeShellExecution` entries, first in their lists |
| `~/.cursor/rules/devd.mdc` | symlink to `agents/cursor/devd.mdc` (always-applied rule) |
| `~/.cursor/skills/devd` | symlink to `agents/skill` |
| `~/.claude/settings.json` | a `PreToolUse` hook with matcher `Bash`, first in the list |
| `~/.claude/CLAUDE.md` | the contents of `agents/instructions.md`, between `<!-- devd:begin … -->` and `<!-- devd:end -->` |
| `~/.claude/skills/devd` | symlink to `agents/skill` |
| `~/.codex/hooks.json` | a `PreToolUse` hook with matcher `Bash`, first in the list |
| `~/.codex/AGENTS.md` | the same managed block as `CLAUDE.md` |
| `~/.agents/skills/devd` | symlink to `agents/skill` (the shared skills folder Codex reads) |

Other hooks and settings are left as they are. Re-running setup updates devd's own entries in place, and replaces older devd hook entries that point to a different path.

After setup:

- **Cursor** picks up `hooks.json` changes on its own. Restart agent chats so they load the rule.
- **Claude Code**: start a new session.
- **Codex**: start a new session. Open `/hooks` and trust the devd `PreToolUse` hook; Codex ignores untrusted hooks.
- **Orca** runs these agents, so it is covered. The hook also denies starting dev servers inside `orca terminal …`, tmux, screen and pm2.

## Wiring a hook by hand

Every harness runs `~/.local/share/devd/guard <mode>`, which reads the hook JSON on stdin.

Cursor (`~/.cursor/hooks.json`):

```json
{
  "version": 1,
  "hooks": {
    "preToolUse": [{ "command": "/Users/you/.local/share/devd/guard preToolUse", "matcher": "Shell", "timeout": 5 }],
    "beforeShellExecution": [{ "command": "/Users/you/.local/share/devd/guard beforeShellExecution", "timeout": 5 }]
  }
}
```

`preToolUse` rewrites the command (`updated_input`). `beforeShellExecution` is a second check that denies any raw dev server the rewrite missed, and tells the agent the exact `devd up` command to run instead.

Claude Code (`~/.claude/settings.json`) and Codex (`~/.codex/hooks.json`):

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash", "hooks": [{ "type": "command", "command": "/Users/you/.local/share/devd/guard claude", "timeout": 10 }] }
    ]
  }
}
```

For Codex, use `guard codex`. Both return `hookSpecificOutput` with `permissionDecision` and, for rewrites, `updatedInput`.

For other agents, put the text of `agents/instructions.md` wherever that agent reads its standing instructions. If it supports a pre-shell hook, point the hook at the guard. `handleHook` in `guard/guard.mjs` documents the input and output shapes.

## Troubleshooting

**`devd up` fails with a portless error in the log, or the URL does not load.** Check the proxy with `devd doctor` and `portless doctor`; start it with `portless proxy start`.

**An agent ran `next dev` directly anyway.** Check that `devd doctor` shows the hook for that harness. Then start a new agent session, since hooks and rules load when a session starts. In Codex, check `/hooks`.

**The wrong app name, or two checkouts fighting over one URL.** Add a `names` rule, or a `portless` key in `package.json`. Worktrees under `.worktrees/<effort>/` get their own `<branch>.<name>.localhost` URL automatically.

**A command was wrapped that should not be** (a worker, an MCP server). Add a regex to `ignore` in the config.

**A server keeps getting stopped as idle while you use it.** That only happens to agent-started servers. Pin it with `devd keep <id>` or `p` in the panel, raise `agent_idle`, or restart it yourself with `devd restart <id>`; it then counts as yours.

**Exit 3 for something you want back.** Run `devd up <id>` yourself. Agents are not allowed to.

**Logs.** `~/.portless/devd/logs/<id>.log`. A log over 5 MB starts fresh on the next start. The state file is `~/.portless/devd/state.json`.

## Updating and uninstalling

```bash
git -C ~/code/devd pull && devd setup       # setup is idempotent; it refreshes the instruction blocks
devd setup --uninstall                      # removes links, hooks and blocks; keeps config and state
```
