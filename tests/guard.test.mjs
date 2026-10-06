import './setup-env.mjs';

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, test } from 'node:test';
import assert from 'node:assert/strict';

import { DEVD_BIN, guardCommand, handleHook, inferName, isDevServerCommand, splitInvocation } from '../guard/guard.mjs';

const temps = [];
const UP = `export DEVD_ACTOR=agent; ${DEVD_BIN} up`;

function makePkg(script, extra = {}) {
	const dir = mkdtempSync(join(tmpdir(), 'devd-guard-'));
	temps.push(dir);
	writeFileSync(
		join(dir, 'package.json'),
		JSON.stringify({ name: extra.name ?? 'demo', scripts: { dev: script, start: extra.start }, portless: extra.portless }),
	);
	return dir;
}

afterEach(() => {
	for (const dir of temps.splice(0)) {
		rmSync(dir, { recursive: true, force: true });
	}
});

describe('isDevServerCommand', () => {
	test('matches next/vite/pm/nest/email and configured scripts and commands', () => {
		assert.equal(isDevServerCommand('next dev --turbopack'), true);
		assert.equal(isDevServerCommand('bun run dev'), true);
		assert.equal(isDevServerCommand('pnpm dev'), true);
		assert.equal(isDevServerCommand('vite'), true);
		assert.equal(isDevServerCommand('nest start --watch | pino-pretty'), true);
		assert.equal(isDevServerCommand('pnpm email dev --dir src/emails --port 4001'), true);
		assert.equal(isDevServerCommand('bun run dashboard'), true);
		assert.equal(isDevServerCommand('bun run src/main.ts'), true);
		assert.equal(isDevServerCommand('bun run --cwd apps/web dev'), true);
		assert.equal(isDevServerCommand('bun run --filter=@invoicer/web dev'), true);
	});

	test('does not match builds, unrelated scripts, or unconfigured script names', () => {
		assert.equal(isDevServerCommand('next build'), false);
		assert.equal(isDevServerCommand('nest build'), false);
		assert.equal(isDevServerCommand('vite build'), false);
		assert.equal(isDevServerCommand('git status'), false);
		assert.equal(isDevServerCommand('bun run --cwd apps/web dev:app'), false);
		assert.equal(isDevServerCommand('bun run storybook'), false);
	});
});

describe('splitInvocation', () => {
	test('peels cd, nohup, redirects and a trailing background part', () => {
		const s = splitInvocation('cd apps/web && nohup bun run dev > /tmp/x.log 2>&1 & sleep 5; curl -s localhost', '/r');
		assert.equal(s.cwd, '/r/apps/web');
		assert.equal(s.server, 'bun run dev');
		assert.equal(s.after, 'sleep 5; curl -s localhost');
	});
});

describe('guardCommand: raw dev servers', () => {
	test('leaves non-servers and ignored commands alone', () => {
		assert.equal(guardCommand('git status').action, 'allow');
		assert.equal(guardCommand('tsc --watch').action, 'allow');
		assert.equal(guardCommand('rollup -c -w', '/Users/x/ui-kit').action, 'allow');
		assert.equal(guardCommand('pnpm --filter worker-billing dev', '/Users/x/acme-api').action, 'allow');
		assert.equal(guardCommand('next start', '/Users/x/acme-shop').action, 'allow');
		assert.equal(guardCommand('bun run start', '/Users/x/portal/apps/engine').action, 'allow');
	});

	test('a hard-coded port is stripped so portless can assign one', () => {
		const r = guardCommand('next dev -p 4000', '/Users/x/acme-shop');
		assert.equal(r.action, 'rewrite');
		assert.equal(r.command, `${UP} --name shop -- 'next dev'`);
	});

	test('name rules apply in order and pipes stay inside one quoted arg', () => {
		assert.equal(guardCommand('pnpm dev', '/Users/x/acme-api/services/api').command, `${UP} --name api -- 'pnpm dev'`);
		assert.equal(
			guardCommand('nest start --watch | pino-pretty', '/Users/x/acme-api/services/api-admin').command,
			`${UP} --name api-admin -- 'nest start --watch | pino-pretty'`,
		);
	});

	test('vite --port is stripped', () => {
		const r = guardCommand('bun --bun vite dev --host 127.0.0.1 --port 4173', '/Users/x/tools/apps/dashboard');
		assert.equal(r.command, `${UP} --name dashboard -- 'bun --bun vite dev --host 127.0.0.1'`);
	});

	test('email preview gets $PORT inside the quoted command', () => {
		const r = guardCommand('pnpm email dev --dir src/emails --port 4001', '/Users/x/acme-mail');
		assert.equal(r.command, `${UP} --name mail -- 'pnpm email dev --dir src/emails --port "$PORT"'`);
	});

	test('`<pm> start` is wrapped only where wrap_start says so', () => {
		assert.equal(guardCommand('bun run start', '/Users/x/portal/apps/web').command, `${UP} --name portal-web -- 'bun run start'`);
		assert.equal(guardCommand('bun run start', '/Users/x/acme-shop').action, 'allow');
	});

	test('self-wrapping scripts and turbo run raw under devd', () => {
		const turbo = makePkg('turbo dev --filter=@invoicer/web', { name: 'invoicer' });
		assert.equal(guardCommand('bun run dev', turbo).command, `${UP} --raw --name invoicer -- 'bun run dev'`);
		const wrapped = makePkg('portless', { portless: 'invoicer' });
		assert.equal(guardCommand('bun run dev', wrapped).command, `${UP} --raw --name invoicer -- 'bun run dev'`);
	});

	test('PORTLESS=0 stays supervised', () => {
		const r = guardCommand('PORTLESS=0 bun run dev', '/Users/x/acme-shop');
		assert.equal(r.command, `${UP} --raw --name shop -- 'PORTLESS=0 bun run dev'`);
	});

	test('cd prefix becomes --cwd and background tail runs after devd', () => {
		const shop = makePkg('vite dev', { name: 'acme-shop' });
		mkdirSync(join(shop, 'apps'));
		const r = guardCommand('cd apps && bun run dev & sleep 8 && curl -sI https://shop.localhost', shop);
		assert.equal(r.command, `${UP} --name shop --cwd ${join(shop, 'apps')} -- 'bun run dev'; sleep 8 && curl -sI https://shop.localhost`);
	});

	test('unknown checkouts get no --name, so devd falls back to the package or folder name', () => {
		assert.equal(guardCommand('vite', '/Users/x/side-project').command, `${UP} -- vite`);
	});

	test('quoted text is not a command', () => {
		assert.equal(guardCommand('git commit -m "fix bun run dev script"', '/Users/x/acme-shop').action, 'allow');
		assert.equal(guardCommand(`claude -p "run npm run dev and report"`, '/Users/x/acme-shop').action, 'allow');
		assert.equal(guardCommand(`rg 'next dev' src`, '/Users/x/acme-shop').action, 'allow');
		assert.equal(guardCommand(`echo "portless run --name shop -- bun run dev"`).action, 'allow');
	});

	test('sh -c wrappers are unwrapped', () => {
		assert.equal(guardCommand(`bash -lc 'bun run dev'`, '/Users/x/acme-shop').command, `${UP} --name shop -- 'bun run dev'`);
	});

	test('launcher terminals are denied', () => {
		assert.equal(guardCommand("orca terminal create --command 'bun run dev'", '/Users/x/acme-shop').action, 'deny');
		assert.equal(guardCommand("tmux new -d 'pnpm dev'", '/Users/x/acme-shop').action, 'deny');
	});
});

describe('guardCommand: portless', () => {
	test('portless run becomes devd up', () => {
		assert.equal(
			guardCommand('portless run --name shop -- bun run dev', '/Users/x/acme-shop').command,
			`${UP} --name shop -- 'bun run dev'`,
		);
		assert.equal(guardCommand('portless myapp next dev', '/tmp/x').command, `${UP} --name myapp -- 'next dev'`);
		assert.equal(guardCommand('portless', '/Users/x/acme-shop').command, `${UP} --name shop`);
	});

	test('--force, proxy stop and clean are denied; read-only subcommands pass', () => {
		assert.equal(guardCommand('portless run --force --name shop -- bun run dev').action, 'deny');
		assert.equal(guardCommand('portless proxy stop').action, 'deny');
		assert.equal(guardCommand('portless clean').action, 'deny');
		assert.equal(guardCommand('portless list').action, 'allow');
		assert.equal(guardCommand('portless get shop').action, 'allow');
		assert.equal(guardCommand('portless doctor').action, 'allow');
	});

	test('only portless in command position counts', () => {
		assert.equal(guardCommand('npm view portless repository.url version').action, 'allow');
		assert.equal(guardCommand('echo portless proxy stop').action, 'allow');
		assert.equal(guardCommand('rg -n portless README.md && ls').action, 'allow');
		assert.equal(guardCommand('cd /tmp/x && portless myapp next dev', '/tmp').action, 'rewrite');
		assert.equal(guardCommand('FOO=1 portless myapp next dev', '/tmp/x').action, 'rewrite');
		assert.equal(guardCommand('npx portless proxy stop').action, 'deny');
		assert.equal(guardCommand('ls | ~/.bun/bin/portless clean').action, 'deny');
	});
});

describe('guardCommand: devd itself', () => {
	test('marks agent calls and blocks user-only actions', () => {
		assert.equal(guardCommand('devd ls').command, 'export DEVD_ACTOR=agent; devd ls');
		assert.equal(guardCommand('devd stop shop@x').action, 'rewrite');
		assert.equal(guardCommand('devd stop --all').action, 'deny');
		assert.equal(guardCommand('devd stop --strays').action, 'deny');
		assert.equal(guardCommand('devd forget shop@x').action, 'deny');
		assert.equal(guardCommand('devd keep shop@x').action, 'deny');
		assert.equal(guardCommand('devd ui').action, 'deny');
		assert.equal(guardCommand('devd setup --uninstall').action, 'deny');
		assert.equal(guardCommand('devd config agent_idle=2h').action, 'deny');
		assert.equal(guardCommand('devd config max_rss=20G').action, 'deny');
		assert.equal(guardCommand('devd config').action, 'rewrite');
		assert.equal(guardCommand('devd up --over-budget shop').action, 'deny');
		assert.equal(guardCommand('devd up --force shop').action, 'deny');
		assert.equal(guardCommand('devd restart shop --force').action, 'rewrite');
		assert.equal(guardCommand('DEVD_ACTOR=user devd up shop').action, 'deny');
		assert.equal(guardCommand('env -i HOME=/x PATH=/bin devd up shop').action, 'deny');
		assert.equal(guardCommand('unset CURSOR_AGENT; devd up shop').action, 'deny');
	});

	test('only devd in command position counts', () => {
		assert.equal(guardCommand('rg -n devd forget docs/').action, 'allow');
		assert.equal(guardCommand('git log --oneline -- devd').action, 'allow');
		assert.equal(guardCommand('cd x && devd forget shop@x').action, 'deny');
		assert.equal(guardCommand('nohup ~/.local/bin/devd ls').action, 'rewrite');
	});

	test('already-marked commands pass, so the Cursor second pass allows the rewrite', () => {
		assert.equal(guardCommand(`${UP} --name shop -- 'bun run dev'`).action, 'allow');
		assert.equal(guardCommand('export DEVD_ACTOR=agent; devd stop --all').action, 'deny');
	});
});

describe('inferName', () => {
	test('reads the package.json portless key first', () => {
		assert.equal(inferName(makePkg('next dev', { portless: 'invoicer' })), 'invoicer');
	});

	test('more specific rules listed first win', () => {
		assert.equal(inferName('/Users/x/acme-api/services/api-admin'), 'api-admin');
		assert.equal(inferName('/Users/x/acme-api/services/api'), 'api');
		assert.equal(inferName('/Users/x/acme-api', 'pnpm --filter api-admin dev'), 'api-admin');
		assert.equal(inferName('/Users/x/acme-api', 'pnpm --filter @acme/api dev'), 'api');
	});

	test('falls back to the package name, then null', () => {
		assert.equal(inferName(makePkg('vite', { name: '@acme/acme-shop' })), 'shop');
		assert.equal(inferName('/Users/x/side-project'), null);
	});
});

describe('handleHook', () => {
	test('cursor preToolUse rewrites and keeps other tool fields', () => {
		const payload = handleHook(
			JSON.stringify({ tool_input: { command: 'next dev', working_directory: '/Users/x/acme-shop', block_until_ms: 0 } }),
			'preToolUse',
		);
		assert.equal(payload.permission, 'allow');
		assert.equal(payload.updated_input.command, `${UP} --name shop -- 'next dev'`);
		assert.equal(payload.updated_input.block_until_ms, 0);
	});

	test('cursor beforeShellExecution denies raw servers but allows the devd rewrite', () => {
		const raw = handleHook(JSON.stringify({ command: 'next dev', cwd: '/Users/x/acme-shop' }), 'beforeShellExecution');
		assert.equal(raw.permission, 'deny');
		assert.match(raw.agent_message, /devd up --name shop/);
		const rewritten = handleHook(JSON.stringify({ command: `${UP} --name shop -- 'next dev'`, cwd: '/x' }), 'beforeShellExecution');
		assert.equal(rewritten.permission, 'allow');
		const plain = handleHook(JSON.stringify({ command: 'devd ls', cwd: '/x' }), 'beforeShellExecution');
		assert.equal(plain.permission, 'allow');
	});

	test('claude/codex get hookSpecificOutput with updatedInput', () => {
		const payload = handleHook(
			JSON.stringify({ tool_name: 'Bash', cwd: '/Users/x/acme-shop', tool_input: { command: 'bun run dev', run_in_background: true } }),
			'claude',
		);
		assert.equal(payload.hookSpecificOutput.permissionDecision, 'allow');
		assert.equal(payload.hookSpecificOutput.updatedInput.command, `${UP} --name shop -- 'bun run dev'`);
		assert.equal(payload.hookSpecificOutput.updatedInput.run_in_background, true);
		const deny = handleHook(JSON.stringify({ tool_input: { command: 'portless proxy stop' } }), 'codex');
		assert.equal(deny.hookSpecificOutput.permissionDecision, 'deny');
		assert.equal(handleHook(JSON.stringify({ tool_input: { command: 'ls' } }), 'codex'), null);
	});

	test('empty or broken stdin fails open', () => {
		assert.equal(handleHook('', 'preToolUse').permission, 'allow');
		assert.equal(handleHook('{nope', 'claude'), null);
	});
});
