#!/bin/bash
# End-to-end idle auto-stop with a 40 s limit and a 4 s tick. Takes about 4 minutes.
source "$(dirname "$0")/lib.sh"
export DEVD_IDLE_TICK_S=4
APP=$ROOT/idle
RID=devdidle@idle
make_app "$APP" devdidle
cd "$APP" || exit 1
user config agent_idle=40s >/dev/null

echo "== 1. requests keep an agent server alive past the limit"
agent up --name devdidle -- npm run dev >/dev/null; check $? 0 "agent starts it"
for _ in 1 2 3 4 5 6; do curl -s https://devdidle.localhost >/dev/null; t0=$SECONDS; sleep 10; done
check "$(field $RID status)" running "still running after 60 s of requests every 10 s"

echo "== 2. no requests: stopped as idle, even while devd ls polls"
while [ "$(field $RID status)" = running ] && [ $((SECONDS - t0)) -lt 75 ]; do sleep 3; done
took=$((SECONDS - t0))
check "$(field $RID status)" stopped "stopped ${took}s after the last request"
check "$(field $RID stopped_by)" idle "stopped_by is idle"
echo "     reason: $(field $RID reason)"
[ $took -ge 36 ] && [ $took -le 60 ]; check $? 0 "stopped near the 40 s limit"
user logs $RID -n 3 | grep -q "no activity for 40s"; check $? 0 "log notes the idle stop"

echo "== 3. agent may restart an idle-stopped server"
agent up $RID >/dev/null; check $? 0 "agent up after idle stop"
check "$(field $RID status)" running "running again"

echo "== 4. pinned server is not stopped"
user keep $RID >/dev/null; check $? 0 "user pins it"
agent keep $RID >/dev/null 2>&1; check $? 5 "agent cannot pin"
sleep 52
check "$(field $RID status)" running "pinned server survives 52 s idle"
user keep $RID --off >/dev/null

echo "== 5. user-started server has no idle limit by default"
user restart $RID >/dev/null
check "$(field $RID idle_limit_s)" 0 "no limit for user-started"
sleep 50
check "$(field $RID status)" running "user server survives 50 s idle"

echo "passed $PASS, failed $FAIL"
[ "$FAIL" -eq 0 ]
