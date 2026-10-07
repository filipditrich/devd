#!/usr/bin/env node
/**
 * Agent shell guard: every dev server goes through `devd up`.
 *
 * Modes (argv[2]):
 * - preToolUse / beforeShellExecution: Cursor hook protocol
 * - claude / codex: PreToolUse hookSpecificOutput protocol
 *
 * Fails open on empty stdin or parse errors.
 */

import { existsSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { stdin } from 'node:process';
import { fileURLToPath, pathToFileURL } from 'node:url';

const REPO_BIN = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'bin', 'devd');
const LINKED_BIN = join(homedir(), '.local', 'bin', 'devd');

/** The devd executable written into rewritten commands. */
export const DEVD_BIN = process.env.DEVD_BIN || (existsSync(LINKED_BIN) ? LINKED_BIN : REPO_BIN);
const AGENT_PREFIX = 'export DEVD_ACTOR=agent; ';

const PORTLESS_SAFE_SUBCOMMANDS = new Set(['list', 'get', 'doctor', 'prune', 'alias', 'trust', 'hosts', 'help', '--help', '-h', '--version', '-v']);
const LAUNCHER_RE = /^(?:\S*\/)?(?:orca\s+terminal\s+(?:create|send|split)|orca\s+orchestration\s+(?:dispatch|worker-start)|tmux|screen|paseo|pm2|forever|launchctl|osascript)\b/;
const PM = String.raw`\b(?:bun|pnpm|npm|yarn)(?:\s+run)?(?:\s+--cwd\s+\S+)?(?:\s+--filter(?:=|\s+)\S+)?`;
/** Start of a simple command: after a separator, env assignments, and exec-style prefixes (npx, nohup, env -i ...). */
const COMMAND_START = String.raw`(?:^|[;&|(\n]|\b(?:then|do|else)\s)\s*(?:\w+=\S*\s+)*(?:(?:nohup|exec|time|command|sudo|env(?:\s+(?:-\S+|\w+=\S*))*|bunx|npx|pnpx|bun\s+x|pnpm\s+(?:dlx|exec)|yarn\s+dlx)\s+)*(?:\S*\/)?`;
const DEVD_CALL_RE = new RegExp(String.raw`${COMMAND_START}devd(?:\s|$)`);
const PORTLESS_CALL_RE = new RegExp(String.raw`${COMMAND_START}portless(?:\s|$)`);
const PORTLESS_TAIL_RE = new RegExp(String.raw`${COMMAND_START}portless(?:\s+(.*))?$`, 's');

let cachedSettings = null;

/**
 * User settings shared with devd (`~/.config/devd/config.json`, or `$DEVD_CONFIG`).
 *
 * - `names`: ordered `{ match | pattern, name }` rules for app names (first hit wins)
 * - `dev_scripts`: extra package scripts that start a dev server (besides dev / start)
 * - `dev_commands`: extra regexes for commands that start a dev server
 * - `ignore`: regexes (on "cwd command") that are never dev servers
 * - `wrap_start`: regexes (on "cwd command") where `<pm> start` is a dev server
 */
export function settings() {
	if (cachedSettings) return cachedSettings;
	const file = process.env.DEVD_CONFIG || join(homedir(), '.config', 'devd', 'config.json');
	let raw = {};
	try {
		raw = JSON.parse(readFileSync(file, 'utf8'));
	} catch {
		raw = {};
	}
	const regexes = (list) => (Array.isArray(list) ? list : []).flatMap((source) => {
		try {
			return [new RegExp(source)];
		} catch {
			return [];
		}
	});
	cachedSettings = {
		names: (Array.isArray(raw.names) ? raw.names : []).filter((rule) => typeof rule?.name === 'string'),
		devScripts: ['dev', ...(Array.isArray(raw.dev_scripts) ? raw.dev_scripts : [])],
		devCommands: regexes(raw.dev_commands),
		ignore: regexes(raw.ignore),
		wrapStart: regexes(raw.wrap_start),
	};
	return cachedSettings;
}

/** Drops the cached settings (tests). */
export function resetSettings() {
	cachedSettings = null;
}

function scriptAlternation() {
	return settings().devScripts.map((s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|');
}

export function isDevServerCommand(command) {
	if (/\bnext\s+build\b/.test(command)) return false;
	if (/\bnest\s+build\b/.test(command)) return false;
	if (/\bvite\s+(build|optimize|test)\b/.test(command)) return false;
	if (/\bnext\s+dev\b/.test(command)) return true;
	if (/\bnuxt\s+dev\b/.test(command)) return true;
	if (/\bastro\s+dev\b/.test(command)) return true;
	if (/\bnest\s+start\b/.test(command)) return true;
	if (/\bemail\s+dev\b/.test(command)) return true;
	if (/(^|[\s;|&(])vite(\s|$)/.test(command)) return true;
	if (/(^|[\s;|&(])vite\s+(?:dev|serve|preview)\b/.test(command)) return true;
	if (/\bturbo\s+(?:run\s+)?dev\b/.test(command)) return true;
	if (/\b(?:bunx|npx|pnpm\s+dlx|bun\s+x)\s+next\s+dev\b/.test(command)) return true;
	if (new RegExp(`${PM}\\s+(?:${scriptAlternation()})(?:\\s|$)`).test(command)) return true;
	if (isPmStart(command)) return true;
	return settings().devCommands.some((re) => re.test(command));
}

export function isNonProxyCommand(command, cwd = '') {
	const hay = `${cwd} ${command}`;
	if (/\btsc\s+--watch\b/.test(command)) return true;
	if (/\bdocker\b/.test(command)) return true;
	if (/\btsx\b/.test(command)) return true;
	if (/\brollup\b/.test(command)) return true;
	if (/\bnext\s+start\b/.test(command)) return true;
	return settings().ignore.some((re) => re.test(hay));
}

function findPackageJson(cwd) {
	if (!cwd) return null;
	let dir = cwd;
	for (let i = 0; i < 8; i += 1) {
		const file = join(dir, 'package.json');
		if (existsSync(file)) {
			try {
				return { dir, pkg: JSON.parse(readFileSync(file, 'utf8')) };
			} catch {
				return null;
			}
		}
		const parent = dirname(dir);
		if (parent === dir) break;
		dir = parent;
	}
	return null;
}

function ruleMatches(rule, text) {
	if (typeof rule.match === 'string' && rule.match !== '' && text.includes(rule.match)) return true;
	if (typeof rule.pattern === 'string') {
		try {
			return new RegExp(rule.pattern).test(text);
		} catch {
			return false;
		}
	}
	return false;
}

/** `@nfctron/api` → `nfctron-api`. A name that already starts with its scope is not doubled. */
export function packageLabel(raw) {
	const text = String(raw).trim();
	const slug = (value) => value.toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '');
	const scoped = text.match(/^@([^/]+)\/(.+)$/);
	if (!scoped) return slug(text);
	const scope = slug(scoped[1]);
	const name = slug(scoped[2]);
	if (name === scope || name.startsWith(`${scope}-`)) return name;
	return slug(`${scope}-${name}`);
}

/**
 * App name for a checkout: package.json `portless` key, then the `names` rules
 * (on "cwd command", then on the package name), then the package name with its
 * scope kept. Null lets devd fall back to the repo folder.
 */
export function inferName(cwd, command = '') {
	const hay = `${cwd} ${command}`;
	const found = findPackageJson(cwd);
	const portlessKey = found?.pkg?.portless;
	if (typeof portlessKey === 'string' && portlessKey !== '') return portlessKey;
	if (portlessKey && typeof portlessKey === 'object' && typeof portlessKey.name === 'string') {
		return portlessKey.name;
	}
	const { names } = settings();
	const byPath = names.find((rule) => ruleMatches(rule, hay));
	if (byPath) return byPath.name;
	const pkgName = found?.pkg?.name;
	if (typeof pkgName === 'string' && pkgName.trim()) {
		const byPkg = names.find((rule) => ruleMatches(rule, pkgName));
		if (byPkg) return byPkg.name;
		return packageLabel(pkgName);
	}
	return null;
}

function stripHardcodedPort(command) {
	return command.replace(/\s+(?:-p|--port)\s+\d+/g, '');
}

function resolveScriptWithPort(dir, scriptName, depth = 0) {
	if (depth > 3 || !dir) return null;
	const found = findPackageJson(dir);
	const script = found?.pkg?.scripts?.[scriptName];
	if (typeof script !== 'string') return null;
	if (/(?:-p|--port)\s+\d+/.test(script)) return script;
	const nested = script.match(new RegExp(`\\b(?:bun|pnpm|npm|yarn)\\s+run\\s+--cwd\\s+(\\S+)\\s+(${scriptAlternation()})\\b`));
	if (!nested) return null;
	const nestedDir = nested[1].startsWith('/') ? nested[1] : join(found.dir, nested[1]);
	return resolveScriptWithPort(nestedDir, nested[2], depth + 1);
}

/** a package script that hard-codes `-p 4000` is expanded so the port can be stripped */
function expandToPortedScript(command, cwd) {
	const match = command.match(
		new RegExp(`\\b(?:bun|pnpm|npm|yarn)(?:\\s+run)?(?:\\s+--cwd\\s+(\\S+))?(?:\\s+--filter(?:=|\\s+)\\S+)?\\s+(${scriptAlternation()})(?:\\s|$)`),
	);
	if (!match) return command;
	const cwdArg = match[1];
	const scriptName = match[2];
	const dir = cwdArg ? (cwdArg.startsWith('/') ? cwdArg : join(cwd || '', cwdArg)) : cwd;
	const resolved = resolveScriptWithPort(dir, scriptName);
	return resolved ?? command;
}

function withAssignedPort(command) {
	if (/\bemail\s+dev\b/.test(command) && !/--port\s+"\$PORT"/.test(command)) {
		return `${command} --port "$PORT"`;
	}
	return command;
}

function devScriptOf(cwd) {
	const found = findPackageJson(cwd);
	return typeof found?.pkg?.scripts?.dev === 'string' ? found.pkg.scripts.dev : '';
}

/** the package script already runs portless itself (or fans out through turbo) */
function selfWrapping(command, cwd) {
	const script = devScriptOf(cwd);
	return /^\s*portless\b/.test(script) || /\bturbo\b/.test(script) || /\bturbo\s+(?:run\s+)?dev\b/.test(command);
}

function isPmStart(command) {
	return new RegExp(`${PM}\\s+start(?:\\s|$)`).test(command);
}

/** `<pm> start` is usually a production server; only some checkouts use it for development */
function shouldWrapStart(command, cwd) {
	const hay = `${cwd} ${command}`;
	return settings().wrapStart.some((re) => re.test(hay));
}

/** quoted text is data (commit messages, prompts), not a command */
export function unquoted(command) {
	return command.replace(/'[^']*'/g, ' _q_ ').replace(/"(?:[^"\\]|\\.)*"/g, ' _q_ ');
}

/** `sh -c '<cmd>'` style wrappers: the quoted part is the real command */
function shellWrapped(command) {
	const m = command.trim().match(/^(?:\S*\/)?(?:sh|bash|zsh)\s+-l?c\s+(?:'([^']*)'|"((?:[^"\\]|\\.)*)")\s*$/);
	return m ? (m[1] ?? m[2]) : null;
}

export function shellQuote(value) {
	if (/^[\w@%+=:,./-]+$/.test(value)) return value;
	return `'${value.replace(/'/g, `'\\''`)}'`;
}

/**
 * Splits `cd dir && ... && <server> [&] [; rest]` into a cwd, the server part, and what runs after it.
 */
export function splitInvocation(command, cwd) {
	let rest = command.trim();
	let dir = cwd || '';
	for (;;) {
		const m = rest.match(/^cd\s+("[^"]+"|'[^']+'|\S+)\s*(?:&&|;)\s*/);
		if (!m) break;
		const target = m[1].replace(/^["']|["']$/g, '').replace(/^~(?=\/|$)/, homedir());
		dir = isAbsolute(target) ? target : resolve(dir || '.', target);
		rest = rest.slice(m[0].length);
	}
	rest = rest.replace(/\s+\d?>&\d/g, '').replace(/\s+(?:\d?>>?|&>)\s*[^\s&;|]+/g, '');
	let after = '';
	const bg = rest.match(/^(.*?[^&])&(?!&)\s*(.*)$/s);
	if (bg) {
		rest = bg[1];
		after = bg[2].replace(/^[;\s]+/, '');
	}
	rest = rest.replace(/^nohup\s+/, '').trim();
	return { cwd: dir, server: rest, after };
}

function devdUp({ name, cwd, inner, raw, originalCwd }) {
	const parts = [DEVD_BIN, 'up'];
	if (raw) parts.push('--raw');
	if (name) parts.push('--name', name);
	if (cwd && cwd !== originalCwd) parts.push('--cwd', shellQuote(cwd));
	if (inner) parts.push('--', shellQuote(inner));
	return parts.join(' ');
}

function portlessInvocation(command) {
	const m = command.match(PORTLESS_TAIL_RE);
	if (!m) return null;
	const tail = (m[1] ?? '').trim();
	const first = tail.split(/\s+/)[0] ?? '';
	return { tail, first };
}

/**
 * Classifies one agent shell command.
 * Returns { action: 'allow' } | { action: 'rewrite', command, reason } | { action: 'deny', reason }.
 */
export function guardCommand(input, cwd = '') {
	if (!input || input.trim() === '') return { action: 'allow' };
	const marked = input.startsWith(AGENT_PREFIX);
	const command = marked ? input.slice(AGENT_PREFIX.length) : input;

	const wrapped = shellWrapped(command);
	if (wrapped !== null) return guardCommand(wrapped, cwd);

	if (/\bDEVD_ACTOR=/.test(command)) {
		return { action: 'deny', reason: 'Do not set DEVD_ACTOR. Run devd without it.' };
	}

	const bare = unquoted(command);

	if (DEVD_CALL_RE.test(bare)) {
		if (/\benv\s+(?:-\S*[iu]|--ignore-environment|--unset)|\bunset\s+(?:CURSOR_AGENT|CLAUDECODE|CODEX_)/.test(bare)) {
			return { action: 'deny', reason: 'Do not strip the agent environment around devd. Run devd directly.' };
		}
		if (marked) {
			const again = guardCommand(command, cwd);
			return again.action === 'deny' ? again : { action: 'allow' };
		}
		if (/\bdevd\s+forget\b/.test(command)) return userOnly('devd forget');
		if (/\bdevd\s+keep\b/.test(command)) return userOnly('devd keep');
		if (/\bdevd\s+(?:ui|setup)\b/.test(command)) return userOnly(`devd ${command.match(/\bdevd\s+(ui|setup)\b/)[1]}`);
		if (/\bdevd\s+config\s+\S+=/.test(command)) return userOnly('devd config');
		if (/\bdevd\s+stop\b[^;&|]*--(?:all|strays)\b/.test(command)) return userOnly('devd stop --all / --strays');
		if (/\bdevd\s+up\b[^;&|]*--over-budget\b/.test(command)) return userOnly('devd up --over-budget');
		if (/\bdevd\s+up\b[^;&|]*--force\b/.test(command)) return userOnly('devd up --force');
		return { action: 'rewrite', command: `${AGENT_PREFIX}${command}`, reason: 'devd runs as agent' };
	}

	const portless = PORTLESS_CALL_RE.test(bare) ? portlessInvocation(command) : null;
	if (portless) {
		if (/(?:^|\s)--force(?:\s|$)/.test(portless.tail)) {
			return { action: 'deny', reason: 'portless --force takes over a running server. Only the user may do that. Use `devd ls` and reuse the live URL.' };
		}
		if (/^proxy\s+stop\b/.test(portless.tail) || portless.first === 'clean' || /^service\s+uninstall\b/.test(portless.tail)) {
			return userOnly(`portless ${portless.tail}`);
		}
		if (portless.first === '' || !PORTLESS_SAFE_SUBCOMMANDS.has(portless.first) && portless.first !== 'proxy' && portless.first !== 'service') {
			return rewritePortless(command, portless.tail, cwd);
		}
		return { action: 'allow' };
	}

	if (LAUNCHER_RE.test(command.trim())) {
		const plain = command.replace(/['"\\]|\\n/g, ' ');
		if (isDevServerCommand(plain) && !isNonProxyCommand(plain, cwd)) {
			return {
				action: 'deny',
				reason: 'Do not start dev servers in orca/tmux/screen/paseo terminals. Run it with `devd up` so it stays supervised and killable.',
			};
		}
		return { action: 'allow' };
	}

	if (!isDevServerCommand(bare)) return { action: 'allow' };
	const split = splitInvocation(command, cwd);
	const server = split.server;
	if (!isDevServerCommand(unquoted(server)) || isNonProxyCommand(server, split.cwd)) return { action: 'allow' };
	if (isPmStart(server) && !shouldWrapStart(server, split.cwd)) return { action: 'allow' };

	const bypass = /(?:^|\s)PORTLESS=0(?:\s|$)/.test(server);
	const raw = bypass || selfWrapping(server, split.cwd);
	const inner = raw ? server : withAssignedPort(stripHardcodedPort(expandToPortedScript(server, split.cwd)).trim());
	const name = inferName(split.cwd, inner);
	const up = devdUp({ name, cwd: split.cwd, inner, raw, originalCwd: cwd });
	const rewritten = split.after ? `${up}; ${split.after}` : up;
	return { action: 'rewrite', command: `${AGENT_PREFIX}${rewritten}`, reason: `dev servers run under devd: ${up}` };
}

function rewritePortless(command, tail, cwd) {
	const split = splitInvocation(command, cwd);
	let args = tail;
	let name = null;
	if (/^run(?:\s|$)/.test(args)) {
		args = args.replace(/^run\s*/, '');
	} else if (args !== '' && !args.startsWith('-')) {
		const [first, ...restArgs] = args.split(/\s+/);
		name = first;
		args = restArgs.join(' ');
	}
	const nameFlag = args.match(/(?:^|\s)--name(?:=|\s+)(\S+)/);
	if (nameFlag) {
		name = nameFlag[1];
		args = args.replace(nameFlag[0], ' ');
	}
	args = args.replace(/(?:^|\s)--(?:tailscale|funnel|ngrok)\b/g, ' ').trim();
	const inner = args.replace(/^--\s*/, '').replace(/\s+(?:\d?>>?|&>)\s*\S+/g, '').replace(/\s+2>&1/g, '').replace(/\s*&\s*$/, '').trim();
	const resolvedName = name ?? inferName(split.cwd, inner);
	const up = devdUp({ name: resolvedName, cwd: split.cwd, inner: inner || null, raw: false, originalCwd: cwd });
	return { action: 'rewrite', command: `${AGENT_PREFIX}${up}`, reason: `portless servers run under devd: ${up}` };
}

function userOnly(what) {
	return {
		action: 'deny',
		reason: `${what} is user-only. Do not work around this. Tell the user what you need and let them run it.`,
	};
}

function readStdin() {
	return new Promise((resolveInput) => {
		const chunks = [];
		stdin.setEncoding('utf8');
		stdin.on('data', (chunk) => chunks.push(chunk));
		stdin.on('end', () => resolveInput(chunks.join('')));
		stdin.on('error', () => resolveInput(''));
	});
}

/**
 * Builds the hook response for one harness event.
 */
export function handleHook(raw, mode) {
	let input = {};
	try {
		input = raw.trim() === '' ? {} : JSON.parse(raw);
	} catch {
		return mode === 'claude' || mode === 'codex' ? null : { permission: 'allow' };
	}

	const event = mode || input.hook_event_name || '';
	const toolInput = input.tool_input ?? {};
	const command = input.command ?? toolInput.command ?? '';
	const cwd = toolInput.cwd || toolInput.working_directory || input.cwd || '';
	const result = guardCommand(command, cwd);

	if (event === 'claude' || event === 'codex') {
		if (result.action === 'allow') return null;
		if (result.action === 'deny') {
			return {
				hookSpecificOutput: {
					hookEventName: 'PreToolUse',
					permissionDecision: 'deny',
					permissionDecisionReason: result.reason,
				},
			};
		}
		return {
			hookSpecificOutput: {
				hookEventName: 'PreToolUse',
				permissionDecision: 'allow',
				permissionDecisionReason: result.reason,
				updatedInput: { ...toolInput, command: result.command },
			},
		};
	}

	if (event === 'beforeShellExecution') {
		if (result.action === 'deny') {
			return { permission: 'deny', agent_message: result.reason, user_message: 'Blocked a dev-server command. See the agent message.' };
		}
		if (result.action === 'rewrite' && !isDevdOnlyRewrite(command, result.command)) {
			return {
				permission: 'deny',
				agent_message: `Dev servers must run under devd. Re-run exactly: ${result.command}`,
				user_message: 'Blocked an unsupervised dev server. The agent was given the devd command.',
			};
		}
		return { permission: 'allow' };
	}

	if (result.action === 'deny') {
		return { permission: 'deny', agent_message: result.reason, user_message: 'Blocked a dev-server command. See the agent message.' };
	}
	if (result.action === 'rewrite') {
		const updated_input = { ...toolInput, command: result.command };
		return { permission: 'allow', updated_input, agent_message: result.reason };
	}
	return { permission: 'allow' };
}

/** the only change was adding the agent marker to a command that already calls devd */
function isDevdOnlyRewrite(original, rewritten) {
	return rewritten === `${AGENT_PREFIX}${original}`;
}

const isMain = process.argv[1] !== undefined && import.meta.url === pathToFileURL(process.argv[1]).href;

if (isMain) {
	const mode = process.argv[2] ?? '';
	const raw = await readStdin();
	const payload = handleHook(raw, mode);
	if (payload !== null) process.stdout.write(`${JSON.stringify(payload)}\n`);
	process.exit(0);
}
