#!/bin/bash
# End-to-end lifecycle: start, reuse, outside kill, user stop, agent rules, budget, startup failure, strays.
source "$(dirname "$0")/lib.sh"
APP=$ROOT/app
make_app "$APP" devdtest
cd "$APP" || exit 1

tagged() { /bin/ps -xEww -o pid=,comm=,command= | awk -v t="DEVD_ID=$1 " '$2 !~ /(grep|awk|ps|sed)$/ && index($0 " ", t)'; }
family() { tagged devdtest@app | wc -l | tr -d ' '; }
listener_pid() {
	lsof -nP -iTCP -sTCP:LISTEN -a -c node -Fp 2>/dev/null | sed -n 's/^p//p' | while read -r p; do
		/bin/ps -Eww -o command= -p "$p" | grep -q "DEVD_ID=$1" && echo "$p"
	done | head -1
}

echo "== 1. agent starts a server"
out=$(agent up --name devdtest -- npm run dev); rc=$?; echo "$out" | sed 's/^/     /'
check $rc 0 "agent up exits 0"
check "$(url_ok devdtest.localhost)" 0 "https://devdtest.localhost answers"
check "$([ "$(family)" -ge 3 ] && echo yes)" yes "whole tree is tagged ($(family) processes)"

echo "== 2. second agent up reuses it"
out=$(agent up --name devdtest -- npm run dev); rc=$?
check $rc 0 "exit 0"
echo "$out" | grep -q "already running"; check $? 0 "says already running"

echo "== 3. killed from outside (SIGKILL, like OOM or Activity Monitor)"
kill -9 "$(listener_pid devdtest@app)"; sleep 2
out=$(agent up --name devdtest -- npm run dev); rc=$?; echo "$out" | sed 's/^/     /'
check $rc 3 "agent is refused (left down)"
check "$(family)" 0 "no leftover processes"
agent restart devdtest@app >/dev/null 2>&1; check $? 3 "agent restart is refused too"

echo "== 4. user brings it back"
user up devdtest@app >/dev/null; check $? 0 "user up exits 0"
check "$(url_ok devdtest.localhost)" 0 "answers again"

echo "== 5. user stop keeps it down for agents; tree fully gone"
t0=$(date +%s); user stop devdtest@app >/dev/null; t1=$(date +%s)
check "$(family)" 0 "every tagged process is gone"
check "$([ $((t1 - t0)) -le 3 ] && echo fast)" fast "stop took <= 3s ($((t1 - t0))s)"
agent up --name devdtest -- npm run dev >/dev/null 2>&1; check $? 3 "agent up refused after user stop"

echo "== 6. agent may stop and restart its own server"
user up devdtest@app >/dev/null
pid1=$(listener_pid devdtest@app)
agent restart devdtest@app >/dev/null; check $? 0 "agent restart of a running server"
pid2=$(listener_pid devdtest@app)
check "$([ -n "$pid2" ] && [ "$pid1" != "$pid2" ] && echo new)" new "listener pid changed ($pid1 -> $pid2)"
agent stop devdtest@app >/dev/null; check $? 0 "agent stop"
agent up devdtest@app >/dev/null; check $? 0 "agent may start what it stopped itself"

echo "== 7. budget"
user config max_servers=1 >/dev/null
out=$(agent up --name devdtest2 -- npm run dev); rc=$?; echo "$out" | sed 's/^/     /'
check $rc 4 "second server refused over budget"
agent config max_servers=9 >/dev/null 2>&1; check $? 5 "agent cannot raise the budget"
user config max_servers=5 >/dev/null

echo "== 8. startup failure is retryable by the agent"
out=$(agent up --name devdfail -- node does-not-exist.js 2>&1); check $? 1 "failed start exits 1"
echo "$out" | grep -q "failed during startup"; check $? 0 "reported as failed during startup"
agent up devdfail@app >/dev/null 2>&1; check $? 1 "agent may retry a startup failure (fails again, not refused)"

echo "== 9. servers started outside devd"
nohup "$PORTLESS" run --name devdraw -- node server.js >"$ROOT/devdraw.log" 2>&1 &
sleep 3
"$D" ls --json | python3 -c 'import json,sys; d=json.load(sys.stdin); print("     unmanaged:", [u["hostname"] for u in d["unmanaged"]])'
agent stop devdraw.localhost >/dev/null 2>&1; check $? 5 "agent cannot stop a server it did not start"
user stop devdraw.localhost >/dev/null; check $? 0 "user stops it"
sleep 0.5; check "$(url_ok devdraw.localhost)" 1 "devdraw no longer answers"
agent up --name devdraw -- node server.js >/dev/null 2>&1; check $? 3 "agent refused to respawn it"

echo "== 10. user-only bookkeeping"
agent forget devdraw@app >/dev/null 2>&1; check $? 5 "agent cannot forget"
agent stop --all >/dev/null 2>&1; check $? 5 "agent cannot stop --all"

echo "== cleanup"
user stop --app devdtest >/dev/null
check "$(family)" 0 "nothing left running"
echo "passed $PASS, failed $FAIL"
[ "$FAIL" -eq 0 ]
