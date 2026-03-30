#!/usr/bin/env bash
set -euo pipefail

BASE_URL="http://localhost:8000"
DT="0.05"          # 20 Hz control loop
SPEED="0.18"       # constant slower turn speed
TOL_DEG="3.0"      # acceptable final error
TIMEOUT_S=20
# +angular command decreases heading (observed on this rover)
HEADING_SIGN="-1"

post_control() {
  local linear="$1"
  local angular="$2"
  curl -s -X POST "${BASE_URL}/control" \
    -H "Content-Type: application/json" \
    -d "{\"command\":{\"linear\":${linear},\"angular\":${angular}}}" > /dev/null
}

stop_rover() {
  post_control 0 0
  sleep 0.05
  post_control 0 0
}

get_heading() {
  # Extract "orientation" from JSON using awk only
  curl -s "${BASE_URL}/data" | awk -F'[:,}]' '
    {
      for (i=1; i<=NF; i++) {
        key=$i; gsub(/^[ \t"]+|[ \t"]+$/, "", key)
        if (key=="orientation") {
          val=$(i+1); gsub(/^[ \t"]+|[ \t"]+$/, "", val)
          print val+0
          exit
        }
      }
    }'
}

# shortest signed angle target-current in [-180, 180)
shortest_diff() {
  local target="$1"
  local current="$2"
  awk -v t="$target" -v c="$current" 'BEGIN{
    d=t-c
    while (d >= 180) d-=360
    while (d <  -180) d+=360
    print d
  }'
}

sign_of() {
  local x="$1"
  awk -v x="$x" 'BEGIN{
    if (x > 0) print 1;
    else if (x < 0) print -1;
    else print 0;
  }'
}

wrap_360() {
  local a="$1"
  awk -v a="$a" 'BEGIN{
    while (a >= 360) a -= 360;
    while (a < 0) a += 360;
    print a
  }'
}

turn_by() {
  local degrees="$1"

  local start target_delta target t0 now current err abs_err err_sign cmd final net
  start="$(get_heading)"
  target_delta="$(awk -v d="$degrees" -v hs="$HEADING_SIGN" 'BEGIN{ print d*hs }')"
  target="$(wrap_360 "$(awk -v s="$start" -v td="$target_delta" 'BEGIN{ print s+td }')")"
  t0="$(date +%s)"

  while true; do
    current="$(get_heading)"
    err="$(shortest_diff "$target" "$current")"
    abs_err="$(awk -v e="$err" 'BEGIN{ if (e<0) e=-e; print e }')"

    if awk -v a="$abs_err" -v tol="$TOL_DEG" 'BEGIN{ exit !(a<=tol) }'; then
      break
    fi

    err_sign="$(sign_of "$err")"
    cmd="$(awk -v es="$err_sign" -v hs="$HEADING_SIGN" -v a="$SPEED" \
      'BEGIN{ printf "%.3f", es*hs*a }')"
    post_control 0 "$cmd"
    sleep "$DT"

    now="$(date +%s)"
    if (( now - t0 > TIMEOUT_S )); then
      echo "Timeout reached, stopping for safety."
      break
    fi
  done

  stop_rover
  final="$(get_heading)"
  net="$(shortest_diff "$final" "$start")"

  echo "Start=${start}°, Target=${target}°, Final=${final}°, Error=$(shortest_diff "$target" "$final")°, NetDelta=${net}°"
}

# Usage: bash precise_turn.sh <degrees>
# Positive = left, Negative = right
# Examples:
#   bash precise_turn.sh 90    # turn left 90°
#   bash precise_turn.sh -90   # turn right 90°
#   bash precise_turn.sh 180   # turn left 180°

DEGREES="${1:-90}"
echo "Turning ${DEGREES}°..."
turn_by "$DEGREES"