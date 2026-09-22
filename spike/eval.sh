#!/usr/bin/env bash
# Run phase0.sh over every PR in golden.txt, then compare colors to your labels.
#
# Usage:  ./eval.sh                       (qwen3.5:9b, thinking off)
#         THINK=true ./eval.sh
#         GOLDEN=other-list.txt ./eval.sh
#
# PR data is cached after the first run, so reruns only cost model time.
# Misses are scored by distance: green vs yellow is off by 1, green vs red is off by 2.

set -euo pipefail

GOLDEN="${GOLDEN:-golden.txt}"
MODEL="${MODEL:-qwen3.5:9b}"
THINK="${THINK:-false}"
PROMPT_VERSION="${PROMPT_VERSION:-v3}"   # keep in step with phase0.sh
export MODEL THINK
export RUN_LABEL="${RUN_LABEL:-$(printf '%s-think-%s-%s' "$MODEL" "$THINK" "$PROMPT_VERSION" | tr ':/' '--')}"

entries() { grep -vE '^[[:space:]]*(#|$)' "$GOLDEN"; }

START=$(date +%s)
entries | while read -r repo pr rest; do
  REPO="$repo" PR="$pr" bash ./phase0.sh < /dev/null || echo "!! $repo #$pr failed"
  echo
done
ELAPSED=$(( $(date +%s) - START ))

rank() { case "$1" in green) echo 0;; yellow) echo 1;; red) echo 2;; *) echo x;; esac; }

compare() {  # $1 = expected, $2 = got
  local e g d
  case "$2" in skipped) echo "-"; return;; error) echo "error"; return;; esac
  [ "$1" = "?" ] && { echo "unlabeled"; return; }
  e=$(rank "$1"); g=$(rank "$2")
  { [ "$e" = x ] || [ "$g" = x ]; } && { echo "bad-label"; return; }
  d=$((e - g)); [ "$d" -lt 0 ] && d=$((-d))
  case "$d" in 0) echo "ok";; 1) echo "off1";; *) echo "OFF2";; esac
}

REPORT=$(entries | while read -r repo pr imp sec rest; do
  exp_i="${imp#impact=}"; exp_s="${sec#security=}"
  slug="$(printf '%s_%s' "$repo" "$pr" | tr '/' '_')"
  f="phase0/runs/$RUN_LABEL/$slug/summary.jsonl"
  if [ -s "$f" ]; then
    got_i=$(jq -r 'select(.light == "impact") | .color' "$f")
    got_s=$(jq -r 'select(.light == "security") | .color' "$f")
    secs=$(jq -s 'map(.seconds) | add' "$f")
  else
    got_i="skipped"; got_s="skipped"; secs="-"
  fi
  printf '%-58s %-7s %-8s %-10s %-7s %-8s %-10s %s\n' "$repo#$pr" \
    "$exp_i" "$got_i" "$(compare "$exp_i" "$got_i")" \
    "$exp_s" "$got_s" "$(compare "$exp_s" "$got_s")" "$secs"
done)

count() { printf '%s\n' "$REPORT" | grep -o " $1 " | wc -l | tr -d ' '; }

echo "================ results: $RUN_LABEL ================"
printf '%-58s %-7s %-8s %-10s %-7s %-8s %-10s %s\n' "PR" "imp.exp" "imp.got" "" "sec.exp" "sec.got" "" "secs"
printf '%s\n' "$REPORT"
echo
echo "ok: $(count ok)   off by 1: $(count off1)   off by 2: $(count OFF2)   errors: $(count error)   wall time: ${ELAPSED}s"
echo "Per-light details: phase0/runs/$RUN_LABEL/*/summary.jsonl"
