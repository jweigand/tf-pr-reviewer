#!/usr/bin/env bash
# Record the fixtures that replay mode and the tests run from.
#
#   ./scripts/record-fixtures.sh search              a Search API page -> fixtures/search/
#   ./scripts/record-fixtures.sh pr owner/name 123   one PR           -> fixtures/prs/
#
# Reads GITHUB_TOKEN from .env if present. Public repos only, so the token needs
# no scopes; without one you get 60 requests per hour instead of 5000.

set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] && { set -a; . ./.env; set +a; }

UA="tf-pr-reviewer"
API="https://api.github.com"

# GitHub rejects an empty User-Agent. The token header is omitted entirely when
# GITHUB_TOKEN is unset, because sending "Bearer " with no value is a 401.
gh() {  # $1 = accept, $2 = url, $3 = output file
  local auth=()
  [ -n "${GITHUB_TOKEN:-}" ] && auth=(-H "Authorization: Bearer $GITHUB_TOKEN")
  curl -sS --fail-with-body --max-time 60 \
    "${auth[@]}" -H "User-Agent: $UA" -H "Accept: $1" \
    -H "X-GitHub-Api-Version: 2022-11-28" "$2" -o "$3"
}

# Fetch and pretty-print. Committed fixtures are indented on purpose: GitHub returns
# JSON on a single line, which makes a re-record show up as one changed line and hides
# what actually moved.
gh_json() {  # $1 = url, $2 = output file
  gh "application/vnd.github+json" "$1" "$2"
  jq "." "$2" > "$2.tmp" && mv "$2.tmp" "$2"
}

record_search() {
  mkdir -p fixtures/search
  local n out
  n=$(ls fixtures/search/page-*.json 2>/dev/null | wc -l | tr -d ' ')
  out=$(printf 'fixtures/search/page-%02d.json' $((n + 1)))

  # advanced_search=true is required; GitHub is retiring the legacy issue search.
  gh_json \
     "$API/search/issues?q=is:pr+is:open+language:HCL&sort=updated&order=desc&per_page=100&advanced_search=true" \
     "$out"

  jq -e '.items | length > 0' "$out" >/dev/null || { echo "no items in $out"; exit 1; }

  # Print the filter composition. A page with no bots or drafts is a bad replay
  # fixture: it would let a broken filter pass unnoticed.
  jq -r --arg out "$out" '
    def isbot: (.user.type == "Bot") or (.user.login | test("\\[bot\\]$"));
    "recorded \($out): \(.items | length) items",
    "  bots:      \([.items[] | select(isbot)] | length)",
    "  drafts:    \([.items[] | select(.draft == true)] | length)",
    "  survivors: \([.items[] | select((isbot | not) and (.draft != true))] | length)"
  ' "$out"
}

record_pr() {  # $1 = owner/name, $2 = number
  local repo="$1" pr="$2" slug dir
  slug="$(printf '%s_%s' "$repo" "$pr" | tr '/' '_')"
  dir="fixtures/prs/$slug"
  mkdir -p "$dir"

  gh_json "$API/repos/$repo/pulls/$pr" "$dir/pr.json"
  gh "application/vnd.github.diff" "$API/repos/$repo/pulls/$pr" "$dir/full.diff"

  # A diff that does not start with "diff --git" is an error page or a redirect
  # body, not a diff. GitHub also returns 406 for very large diffs.
  head -c 10 "$dir/full.diff" | grep -q "diff --git" \
    || { echo "not a diff, check $dir/full.diff"; exit 1; }

  # The expected output of the .tf filter, used as the test oracle in tests/.
  awk '/^diff --git /{keep = ($NF ~ /\.tf$/)} keep' "$dir/full.diff" > "$dir/tf.expected.diff"

  printf 'recorded %s: %s files, %s of them .tf\n' "$dir" \
    "$(grep -c '^diff --git' "$dir/full.diff" || true)" \
    "$(grep -c '^diff --git' "$dir/tf.expected.diff" || true)"
}

case "${1:-}" in
  search) record_search ;;
  pr)     [ $# -eq 3 ] || { echo "usage: $0 pr owner/name 123"; exit 1; }
          record_pr "$2" "$3" ;;
  *)      sed -n '2,8p' "$0"; exit 1 ;;
esac
