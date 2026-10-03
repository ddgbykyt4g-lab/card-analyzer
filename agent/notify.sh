#!/usr/bin/env bash
# Phone alert through ntfy.sh. Usage: agent/notify.sh "Title" "message"   (message may also come from stdin)
# Sends to the private topic in $NTFY_TOPIC and never prints it. Retries because ntfy's free quota is
# per sending address and the cloud address is shared. Always exits 0: a failed alert must not fail the run.
set -u
title="${1:-Card analyzer}"
body="${2:-$(cat)}"
if [ -z "${NTFY_TOPIC:-}" ]; then echo "NTFY_TOPIC not set; alert skipped"; exit 0; fi
body="${body:0:3500}"
for attempt in 1 2 3 4; do
  code=$(curl -sS -m 30 -o /dev/null -w "%{http_code}" -H "Title: $title" --data-raw "$body" "https://ntfy.sh/$NTFY_TOPIC" 2>/dev/null)
  if [ "$code" = "200" ]; then echo "alert sent"; exit 0; fi
  echo "alert attempt $attempt failed (HTTP $code)"
  sleep $((30 * attempt))
done
echo "alert failed after retries; the report is on GitHub"
exit 0
