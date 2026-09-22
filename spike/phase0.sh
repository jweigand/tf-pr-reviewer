#!/usr/bin/env bash
# Phase 0, v3: can a local model review a real Terraform PR?
#
# Usage:
#   REPO=owner/name PR=123 ./phase0.sh          (defaults: fixture #1, qwen3.5:9b, thinking off)
#   To run the whole golden set, use ./eval.sh instead.
#
# Needs: Ollama running natively, jq (brew install jq), GITHUB_TOKEN recommended
#
# v3: adds the default_behavior_change impact category (new defaults that change runtime behavior).
#
# v2 changes (see "rubric" and "verify_finding" below):
#   - thinking off by default; if a thinking run returns no usable JSON, retry once with thinking off
#   - impact prompt defines each category and puts security out of scope
#   - schema adds a "dismissed" list for things the model considered and rejected; summary moved last
#   - a finding is verified only if its evidence appears in the file it names, and
#     resource_removal / rename_without_moved must cite a removed resource or module block
#   - forced_replacement and anything under examples/ can't produce red on their own
#
# Layout:
#   phase0/cache/<repo>_<pr>/            PR metadata and diffs, fetched once
#   phase0/runs/<label>/<repo>_<pr>/     per model/think/version, per PR

set -euo pipefail

PROMPT_VERSION="v3"
REPO="${REPO:-padok-team/terraform-azurerm-postgresql-server}"
PR="${PR:-15}"
MODEL="${MODEL:-qwen3.5:9b}"
THINK="${THINK:-false}"      # true | false | none (none sends nothing, for models without thinking support)
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
NUM_CTX="${NUM_CTX:-8192}"
NUM_PREDICT="${NUM_PREDICT:-3000}"   # includes thinking tokens

SLUG="$(printf '%s_%s' "$REPO" "$PR" | tr '/' '_')"
LABEL="${RUN_LABEL:-$(printf '%s-think-%s-%s' "$MODEL" "$THINK" "$PROMPT_VERSION" | tr ':/' '--')}"
CACHE="phase0/cache/$SLUG"
OUT="phase0/runs/$LABEL/$SLUG"
mkdir -p "$CACHE" "$OUT"
rm -f "$OUT/summary.jsonl"

echo "################ $REPO #$PR  [$LABEL]"

# ---------- preflight ----------
command -v jq >/dev/null || { echo "jq not found: brew install jq"; exit 1; }
curl -sf "$OLLAMA_URL/api/tags" >/dev/null || { echo "Ollama not reachable at $OLLAMA_URL"; exit 1; }
if ! curl -s "$OLLAMA_URL/api/tags" | jq -e --arg m "$MODEL" '.models[].name | select(. == $m)' >/dev/null; then
  echo "Model $MODEL not pulled. Run: ollama pull $MODEL"; exit 1
fi

# ---------- fetch PR (cached) ----------
gh_get() {  # $1 = accept header, $2 = output file
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    curl -sf --max-time 30 -H "Authorization: Bearer $GITHUB_TOKEN" -H "User-Agent: phase0-spike" \
      -H "Accept: $1" "https://api.github.com/repos/$REPO/pulls/$PR" -o "$2"
  else
    curl -sf --max-time 30 -H "User-Agent: phase0-spike" \
      -H "Accept: $1" "https://api.github.com/repos/$REPO/pulls/$PR" -o "$2"
  fi
}
[ -s "$CACHE/pr.json" ]   || gh_get "application/vnd.github+json" "$CACHE/pr.json" || { echo "PR fetch failed"; exit 1; }
[ -s "$CACHE/full.diff" ] || gh_get "application/vnd.github.diff" "$CACHE/full.diff" || { echo "Diff fetch failed"; exit 1; }
head -c 10 "$CACHE/full.diff" | grep -q "diff --git" || { echo "Diff looks wrong; check $CACHE/full.diff"; exit 1; }

TITLE=$(jq -r '.title' "$CACHE/pr.json")
BODY=$(jq -r '.body // "(no description)"' "$CACHE/pr.json")
echo "Title: $TITLE"

# ---------- keep only .tf files ----------
# .tfvars are excluded on purpose: a value change means nothing without the .tf code that
# consumes it, which the diff doesn't include. Known blind spot; see golden.txt notes.
# $NF on a "diff --git a/x b/x" line is "b/<path>"
awk '/^diff --git /{keep = ($NF ~ /\.tf$/)} keep' "$CACHE/full.diff" > "$CACHE/tf.diff"
TF_FILES=$(grep -c '^diff --git' "$CACHE/tf.diff" || true)
echo "Full diff: $(grep -c '^diff --git' "$CACHE/full.diff") files | .tf only: $TF_FILES files"
if [ "$TF_FILES" -eq 0 ]; then
  echo "No .tf files changed; skipping (the real pipeline would drop this PR before the model)."
  exit 0
fi

# chars/3 is deliberately pessimistic: chars/4 under-counted fixture #1 by about 25%
CHARS=$(wc -c < "$CACHE/tf.diff" | tr -d ' ')
EST_TOKENS=$((CHARS / 3))
echo "Rough prompt size: ~$EST_TOKENS tokens (num_ctx=$NUM_CTX, num_predict=$NUM_PREDICT)"
if [ $((EST_TOKENS + NUM_PREDICT + 800)) -gt "$NUM_CTX" ]; then
  echo "SKIPPING: prompt may not fit and Ollama would silently truncate it. Raise NUM_CTX or pick a smaller PR."
  exit 0
fi

# ---------- prompts ----------
SYSTEM='You are reviewing a Terraform pull request. You only see the diff, not the whole repository.
Report only real problems the diff itself shows, at most 3. Each finding must name the file and quote one line copied exactly from that file in the diff as evidence.
Anything you considered but decided is not a problem goes in dismissed, with a one-line reason. An empty findings list is a normal, good answer.
Keep every explanation to one sentence. Set confidence to low if you would need files outside the diff to be sure.'

IMPACT_TASK='TASK: assess IMPACT: could this change break or disrupt existing infrastructure, the code that depends on it, or how it behaves for existing users?
Categories:
- resource_removal: a resource or module block is deleted.
- rename_without_moved: a resource or module block is removed and a near-identical one is added under a different name, with no moved block.
- forced_replacement: an attribute change you believe makes Terraform destroy and recreate the resource. Name the attribute. Only a plan can confirm this.
- module_interface_change: a module input variable or output is removed, renamed, or retyped, or a module directory is renamed or moved, so existing callers break.
- output_change: an output of a root configuration is removed or retyped.
- lifecycle_change: lifecycle settings such as prevent_destroy or ignore_changes are removed or weakened.
- default_behavior_change: a default value or built-in setting changes, so existing users who never set it get different behavior after upgrading, even though their code still plans cleanly. Example: a variable default flips from true to false.
- other: a real impact that fits none of the above.
Out of scope: security exposure (open CIDRs, IAM, encryption, secrets) is assessed separately. Do not report it here.
Not impact: moving a block within a file; changing how a value is referenced when the value stays the same; changes under examples/ only affect the examples.'
IMPACT_CATS='["resource_removal","rename_without_moved","forced_replacement","module_interface_change","output_change","lifecycle_change","default_behavior_change","other"]'

SECURITY_TASK='TASK: assess SECURITY. Look for: network exposure (ingress from 0.0.0.0/0 or ::/0, public access enabled, public IPs);
IAM or role grants broader than needed (wildcards, admin roles); encryption disabled or weakened; secrets or passwords written as literal values;
logging or auditing disabled. Judge severity by context: a literal password under examples/ is lower severity than in production code;
0.0.0.0/0 on port 443 of a load balancer is lower than on a database port. A change that improves security is not a finding; put it in dismissed.'
SECURITY_CATS='["network_exposure","iam_privilege","encryption","secret_exposure","logging_or_audit","other"]'

# Field order matters: constrained decoding writes fields in schema order.
# Findings come first and the summary last, so the summary can't anchor the findings.
make_schema() {
  jq -n --argjson cats "$1" '{
    type: "object", additionalProperties: false,
    required: ["findings", "dismissed", "confidence", "summary"],
    properties: {
      findings: {type: "array", items: {
        type: "object", additionalProperties: false,
        required: ["file", "evidence", "explanation", "category", "severity"],
        properties: {
          file: {type: "string"},
          evidence: {type: "string"},
          explanation: {type: "string"},
          category: {type: "string", enum: $cats},
          severity: {type: "string", enum: ["low", "medium", "high"]}
        }}},
      dismissed: {type: "array", items: {
        type: "object", additionalProperties: false,
        required: ["item", "reason"],
        properties: {item: {type: "string"}, reason: {type: "string"}}}},
      confidence: {type: "string", enum: ["high", "medium", "low"]},
      summary: {type: "string"}
    }}'
}

# Diff first, task last: all lights share an identical prefix.
build_user_msg() {
  printf 'Pull request title: %s\nPull request description: %s\n\nDiff (.tf files only):\n%s\n\n%s' \
    "$TITLE" "$BODY" "$(cat "$CACHE/tf.diff")" "$1"
}

# ---------- verification ----------
# Paths in the diff that match the file a finding names: exact, or as a path suffix,
# so "main.tf" matches "modules/x/main.tf". A bare name can match several files.
paths_for() {  # $1 = claimed file
  local f="${1#./}"; f="${f#a/}"; f="${f#b/}"
  awk -v f="$f" '
    function hit(p) { return p == f || (length(p) > length(f) && substr(p, length(p) - length(f)) == "/" f) }
    /^diff --git /{ a = $3; b = $4; sub(/^a\//, "", a); sub(/^b\//, "", b); if (hit(a) || hit(b)) print b }' "$CACHE/tf.diff"
}

section_for() {  # $1 = exact new-side path
  awk -v p="$1" '/^diff --git /{ b = $4; sub(/^b\//, "", b); keep = (b == p) } keep' "$CACHE/tf.diff"
}

normalize() { printf '%s' "$1" | sed -E 's/^[+ -]?[[:space:]]*//; s/[[:space:]]+$//'; }

# Every non-blank evidence line must appear in the section. Blank lines are skipped:
# a removed blank line is a lone "-" and normalizes to nothing.
evidence_in() {  # $1 = section, $2 = evidence
  local line norm checked=0
  while IFS= read -r line; do
    norm=$(normalize "$line")
    [ -z "$norm" ] && continue
    checked=$((checked + 1))
    printf '%s\n' "$1" | grep -Fq -- "$norm" || return 1
  done <<< "$2"
  [ "$checked" -gt 0 ]
}

# True if some evidence line is a resource/module block header that the section removes.
cites_removed_block() {  # $1 = section, $2 = evidence
  local line norm
  while IFS= read -r line; do
    norm=$(normalize "$line")
    printf '%s' "$norm" | grep -Eq '^(resource|module)[[:space:]]+"' || continue
    printf '%s\n' "$1" | grep '^-' | grep -Fq -- "$norm" && return 0
  done <<< "$2"
  return 1
}

# Prints "<status> <resolved path>". Status: ok | file_not_in_diff | evidence_not_found | category_mismatch
verify_finding() {  # $1 = file, $2 = evidence, $3 = category
  local p section resolved="" paths
  paths=$(paths_for "$1")
  [ -z "$paths" ] && { echo "file_not_in_diff -"; return; }
  while IFS= read -r p; do
    section=$(section_for "$p")
    if evidence_in "$section" "$2"; then resolved="$p"; break; fi
  done <<< "$paths"
  [ -z "$resolved" ] && { echo "evidence_not_found -"; return; }
  case "$3" in
    resource_removal|rename_without_moved)
      cites_removed_block "$section" "$2" || { echo "category_mismatch $resolved"; return; } ;;
  esac
  echo "ok $resolved"
}

# ---------- model call ----------
call_model() {  # $1 = light name, $2 = categories, $3 = task, $4 = think setting, $5 = attempt tag
  jq -n --arg model "$MODEL" --arg sys "$SYSTEM" --arg user "$(build_user_msg "$3")" \
        --argjson schema "$(make_schema "$2")" \
        --argjson ctx "$NUM_CTX" --argjson np "$NUM_PREDICT" --arg think "$4" '{
      model: $model, stream: false, keep_alive: "30m", format: $schema,
      options: {num_ctx: $ctx, num_predict: $np, temperature: 0, presence_penalty: 0},
      messages: [{role: "system", content: $sys}, {role: "user", content: $user}]
    } + (if $think == "true" then {think: true} elif $think == "false" then {think: false} else {} end)' \
    > "$OUT/$1$5.request.json"
  curl -s --max-time 900 "$OLLAMA_URL/api/chat" -d @"$OUT/$1$5.request.json" > "$OUT/$1$5.response.json"
  jq -r '"done_reason: \(.done_reason)   (\"length\" means output was cut off)",
         "prompt eval: \(.prompt_eval_count) tokens in \(.prompt_eval_duration / 1e9 | . * 10 | round / 10)s",
         "generation:  \(.eval_count) tokens in \(.eval_duration / 1e9 | . * 10 | round / 10)s",
         "total:       \(.total_duration / 1e9 | . * 10 | round / 10)s",
         "thinking:    \(.message.thinking // "" | length) chars"' "$OUT/$1$5.response.json"
}

valid_content() {  # $1 = response file
  jq -r '.message.content // ""' "$1" | jq -e '.findings and .dismissed and .confidence' >/dev/null 2>&1
}

run_light() {  # $1 = name, $2 = categories JSON, $3 = task text
  local name="$1" resp content n i f ev cat result status path statuses="[]" paths="[]" retried=false seconds
  echo; echo "================ $name ================"
  call_model "$name" "$2" "$3" "$THINK" ""
  resp="$OUT/$name.response.json"
  seconds=$(jq '.total_duration / 1e9' "$resp")

  # Fallback: thinking can exhaust num_predict and return no JSON. Retry once without it.
  if ! valid_content "$resp" && [ "$THINK" != "false" ]; then
    echo "No usable JSON (done_reason: $(jq -r .done_reason "$resp")). Retrying once with thinking off."
    call_model "$name" "$2" "$3" "false" ".retry"
    resp="$OUT/$name.retry.response.json"
    seconds=$(jq -n --argjson a "$seconds" --argjson b "$(jq '.total_duration / 1e9' "$resp")" '$a + $b')
    retried=true
  fi

  if ! valid_content "$resp"; then
    echo "Model output is NOT usable JSON. Raw output:"; jq -r '.message.content' "$resp"
    jq -nc --arg repo "$REPO" --argjson pr "$PR" --arg light "$name" --arg label "$LABEL" \
      --argjson s "$seconds" --argjson retried "$retried" --arg dr "$(jq -r .done_reason "$resp")" \
      '{repo: $repo, pr: $pr, light: $light, label: $label, color: "error",
        seconds: ($s | round), retried: $retried, done_reason: $dr}' >> "$OUT/summary.jsonl"
    return
  fi

  content=$(jq -r '.message.content' "$resp")
  printf '%s' "$content" | jq . | tee "$OUT/$name.findings.json"

  echo "--- verification ---"
  n=$(printf '%s' "$content" | jq '.findings | length')
  i=0
  while [ "$i" -lt "$n" ]; do
    f=$(printf '%s' "$content"   | jq -r ".findings[$i].file")
    ev=$(printf '%s' "$content"  | jq -r ".findings[$i].evidence")
    cat=$(printf '%s' "$content" | jq -r ".findings[$i].category")
    result=$(verify_finding "$f" "$ev" "$cat"); status="${result%% *}"; path="${result#* }"
    printf '  %-19s %s | %s | %s\n' "$status" "$cat" "$path" "$(printf '%s' "$ev" | head -1)"
    statuses=$(printf '%s' "$statuses" | jq -c --arg s "$status" '. + [$s]')
    paths=$(printf '%s' "$paths" | jq -c --arg p "$path" '. + [$p]')
    i=$((i + 1))
  done

  # ---------- rubric (PROPOSED: review and own these rules) ----------
  #   A finding counts only if verified (evidence in the named file, category consistent).
#   The examples/ cap uses the path where the evidence was actually found, not the name the model gave.
  #   forced_replacement and anything under examples/ are capped at medium: a diff can't
  #     prove a replacement, and examples don't deploy.
  #   red:    any verified finding whose capped severity is high
  #   yellow: any other finding (verified or not), or low confidence
  #   green:  no findings and confidence not low
  printf '%s' "$content" | jq -c --argjson st "$statuses" --argjson ps "$paths" --arg repo "$REPO" --argjson pr "$PR" \
      --arg light "$name" --arg label "$LABEL" --argjson s "$seconds" --argjson retried "$retried" \
      --arg dr "$(jq -r .done_reason "$resp")" '
    def rank: {"low": 0, "medium": 1, "high": 2}[ascii_downcase] // 0;
    [.findings as $f | range(0; $f | length) | $f[.] + {status: $st[.], path: (if $ps[.] == "-" then $f[.].file else $ps[.] end)}
      | .capped = (if .category == "forced_replacement" or (.path | test("(^|/)examples/"))
                   then ([.severity | rank, 1] | min) else (.severity | rank) end)] as $all
    | ($all | map(select(.status == "ok"))) as $ok
    | {repo: $repo, pr: $pr, light: $light, label: $label,
       color: (if ($ok | map(.capped) | index(2)) then "red"
               elif ($all | length) > 0 or (.confidence | ascii_downcase) == "low" then "yellow"
               else "green" end),
       findings: ($all | length), verified: ($ok | length),
       statuses: ($all | map(.status)), categories: ($ok | map(.category) | unique),
       dismissed: (.dismissed | length), confidence: .confidence,
       seconds: ($s | round), retried: $retried, done_reason: $dr}' \
    | tee -a "$OUT/summary.jsonl" | jq -r '"=> \(.light): \(.color | ascii_upcase)   (\(.verified)/\(.findings) findings verified, \(.dismissed) dismissed)"'
}

run_light impact   "$IMPACT_CATS"   "$IMPACT_TASK"
run_light security "$SECURITY_CATS" "$SECURITY_TASK"
echo; echo "Saved to $OUT/"
