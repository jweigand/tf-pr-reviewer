#!/usr/bin/env bash
# Triage candidate PRs before labeling: size, fit, and which patterns the .tf changes contain.
# Prints only public PR metadata, so the output is safe to share.
#
# Usage: GITHUB_TOKEN=... ./triage.sh [candidates.txt]
# Cost:  one core API call per PR (files endpoint, up to 100 files).

set -euo pipefail
LIST="${1:-candidates.txt}"
NUM_CTX="${NUM_CTX:-8192}"
: "${GITHUB_TOKEN:?Set GITHUB_TOKEN (a fine-grained token with public read-only access is enough)}"
command -v jq >/dev/null || { echo "jq not found: brew install jq"; exit 1; }

count() { grep -Ec "$1" <<< "$2" || true; }

printf '%-58s %4s %6s %-4s %3s  %s\n' "PR" "tf" "tokens" "fits" "ren" "patterns in .tf changes"
grep -vE '^[[:space:]]*(#|$)' "$LIST" | while read -r repo pr hint; do
  files=$(curl -sf --max-time 30 -H "Authorization: Bearer $GITHUB_TOKEN" -H "User-Agent: triage" \
            -H "Accept: application/vnd.github+json" \
            "https://api.github.com/repos/$repo/pulls/$pr/files?per_page=100") \
    || { printf '%-58s fetch failed\n' "$repo#$pr"; continue; }

  tf=$(jq '[.[] | select(.filename | endswith(".tf"))] | length' <<< "$files")
  ren=$(jq '[.[] | select((.filename | endswith(".tf")) and .status == "renamed")] | length' <<< "$files")
  nopatch=$(jq '[.[] | select((.filename | endswith(".tf")) and (.patch == null) and .status != "renamed")] | length' <<< "$files")
  patch=$(jq -r '.[] | select(.filename | endswith(".tf")) | .patch // empty' <<< "$files")
  tokens=$(( $(printf '%s' "$patch" | wc -c | tr -d ' ') / 3 ))
  if [ "$tf" -eq 0 ]; then fits="none"
  elif [ "$nopatch" -gt 0 ]; then fits="big"
  elif [ $((tokens + 3800)) -le "$NUM_CTX" ]; then fits="yes"
  else fits="no"; fi

  flags=""
  add() { [ "$2" -gt 0 ] && flags="$flags $1:$2" || true; }
  add moved     "$(count '^\+[[:space:]]*moved[[:space:]]*\{' "$patch")"
  add rm_block  "$(count '^-[[:space:]]*(resource|module)[[:space:]]+"' "$patch")"
  add add_block "$(count '^\+[[:space:]]*(resource|module)[[:space:]]+"' "$patch")"
  add rm_var    "$(count '^-[[:space:]]*variable[[:space:]]+"' "$patch")"
  add add_var   "$(count '^\+[[:space:]]*variable[[:space:]]+"' "$patch")"
  add rm_output "$(count '^-[[:space:]]*output[[:space:]]+"' "$patch")"
  add default   "$(count '^[-+][[:space:]]*default[[:space:]]*=' "$patch")"
  add lifecycle "$(count '^[-+].*(prevent_destroy|deletion_protection|ignore_changes)' "$patch")"
  add open_cidr "$(count '^\+.*(0\.0\.0\.0/0|::/0)' "$patch")"
  add iam_wild  "$(count '^\+.*("\*"|:\*")' "$patch")"
  add encrypt   "$(count '^[-+].*(encrypt|kms)' "$patch")"
  add public    "$(count '^[-+].*public' "$patch")"

  printf '%-58s %4s %6s %-4s %3s %s   (%s)\n' "$repo#$pr" "$tf" "$tokens" "$fits" "$ren" "${flags:- -}" "$hint"
done
