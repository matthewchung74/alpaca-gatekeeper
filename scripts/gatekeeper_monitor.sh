#!/bin/zsh
# Gatekeeper MONITOR — the "wake up and check for bugs" layer for the cloud agent. Fired by launchd
# every ~15 min during the session. Two layers: (1) the deterministic health-check (schedulers armed,
# jobs succeeding, sweeps not stale, journal errors, risk limits, expiry flatten, broker-vs-journal
# reconciliation — raises an OS alert and exits non-zero on anything critical), then (2) a local
# Claude instance that reads the state and looks for subtler bugs, writes a short report, and
# ESCALATES anything concerning (it does NOT place orders — the agent trades in the cloud and this
# layer only ever issues GETs).
#
# INSTALL LOCATION MATTERS. This runs from ~/.local/share/gatekeeper, NOT from the repo on ~/Desktop:
# Desktop is a TCC-protected directory and a launchd agent cannot read files there ("can't open input
# file", exit 127). The repo copy under scripts/ is the editable source; reinstall after changing it:
#     cp ~/Desktop/alpaca/scripts/gatekeeper_health.py        ~/.local/share/gatekeeper/
#     cp ~/Desktop/alpaca/scripts/gatekeeper_monitor_prompt.md ~/.local/share/gatekeeper/
#     cp ~/Desktop/alpaca/scripts/gatekeeper_monitor.sh        ~/.local/share/gatekeeper/
#
# Unlike the trader, this is laptop-bound: a sleeping Mac means no monitoring, not a stalled agent.
DOW=$(TZ=America/New_York date +%u); HM=$(TZ=America/New_York date +%H%M)
[ "$DOW" -gt 5 ] && exit 0           # weekday only
[ "$HM" -lt 935 ] && exit 0          # before 09:35 ET — first sweep (09:00) has run, nothing to judge yet
[ "$HM" -ge 1615 ] && exit 0         # after 16:15 ET — the last sweep (16:50) only closes; session done
DAY=$(TZ=America/New_York date +%F)
CACHE="$HOME/.cache/gatekeeper"
REPORT="$CACHE/gatekeeper_monitor_${DAY}.log"
INSTALL="$HOME/.local/share/gatekeeper"
PY=/opt/homebrew/bin/python3
CLAUDE=/Users/mattc/.local/bin/claude
mkdir -p "$CACHE"

echo "=== monitor $(TZ=America/New_York date '+%H:%M ET') ===" >>"$REPORT"

# (1) deterministic health-check. Exit 2 = critical (it has already raised the OS alert itself).
cd "$INSTALL" && $PY gatekeeper_health.py >>"$REPORT" 2>&1
HEALTH=$?
echo "health exit=$HEALTH" >>"$REPORT"

# (2) Claude assessment — judgement on subtler bugs. Read-only; escalate the rest.
# gcloud/curl are scoped to read-only verbs; the alpaca CLI is deliberately absent.
if [ -x "$CLAUDE" ]; then
  "$CLAUDE" -p "$(cat "$INSTALL/gatekeeper_monitor_prompt.md")

Today's report so far (the deterministic layer has already run):
$(tail -40 "$REPORT")" \
    --allowedTools "Read" "Bash(curl:*)" "Bash(tail:*)" "Bash(grep:*)" "Bash(cat:*)" \
                   "Bash(/Users/mattc/google-cloud-sdk/bin/gcloud * list:*)" \
                   "Bash(/Users/mattc/google-cloud-sdk/bin/gcloud * describe:*)" \
                   "Bash(/usr/bin/osascript:*)" \
    >>"$REPORT" 2>&1 || {
      # Layer 2 going quiet looks identical to "nothing is wrong", so say it out loud.
      echo "claude monitor invocation FAILED" >>"$REPORT"
      /usr/bin/osascript -e 'display notification "Claude monitor layer failed - only the deterministic checks are running" with title "Gatekeeper"'
    }
else
  echo "claude binary not executable at $CLAUDE" >>"$REPORT"
  /usr/bin/osascript -e 'display notification "claude binary missing - only the deterministic checks are running" with title "Gatekeeper"'
fi
