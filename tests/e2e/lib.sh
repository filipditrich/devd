#!/bin/bash
# Shared setup for the end-to-end scripts: isolated state, config, and a throwaway node app.
# Needs a running portless proxy; real servers keep running and only count as unmanaged.
unset CURSOR_AGENT CLAUDECODE CODEX_THREAD_ID CODEX_SANDBOX
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
D=$REPO/bin/devd
ROOT=$(cd "$(mktemp -d "${TMPDIR:-/tmp}/devd-e2e.XXXXXX")" && pwd -P)
export DEVD_STATE_DIR=$ROOT/state
export DEVD_CONFIG=$ROOT/config.json
export DEVD_BIN=$D
printf '{ "work_dirs": ["%s"] }\n' "$ROOT" > "$DEVD_CONFIG"
PORTLESS=$(command -v portless || echo "$HOME/.bun/bin/portless")
PASS=0
FAIL=0

agent() { DEVD_ACTOR=agent "$D" "$@"; }
user() { DEVD_ACTOR=user "$D" "$@"; }
check() {
	if [ "$1" = "$2" ]; then PASS=$((PASS + 1)); echo "  ok   $3"
	else FAIL=$((FAIL + 1)); echo "  FAIL $3 (got $1, want $2)"; fi
}
url_ok() { curl -s --max-time 3 "https://$1" | grep -q '^ok'; echo $?; }
field() {
	"$D" ls --json | python3 -c "import json,sys; s=next((s for s in json.load(sys.stdin)['servers'] if s['id']=='$1'),{}); print(s.get('$2'))"
}

# make_app <dir> <package name> [extra server js]
make_app() {
	mkdir -p "$1"
	cat > "$1/server.js" <<EOF
const http = require('http');
$3
http.createServer((q, s) => s.end(\`ok \${process.pid}\n\`)).listen(Number(process.env.PORT || 3999), '127.0.0.1', () => console.log('listening'));
EOF
	printf '{ "name": "%s", "private": true, "scripts": { "dev": "node server.js" } }\n' "$2" > "$1/package.json"
	touch "$1/package-lock.json"
	git -C "$1" init -q
}

finish() {
	local id
	for id in $("$D" ls --json | python3 -c "import json,sys; print(' '.join(s['id'] for s in json.load(sys.stdin)['servers'] if s['status'] in ('starting','running')))"); do
		DEVD_ACTOR=user "$D" stop "$id" >/dev/null 2>&1
	done
	cd / && rm -rf "$ROOT"
}
trap finish EXIT

if ! "$PORTLESS" list >/dev/null 2>&1; then
	echo "portless proxy is not running (portless proxy start)" >&2
	exit 1
fi
