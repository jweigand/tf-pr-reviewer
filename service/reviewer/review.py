"""Turn one candidate into one review record.

The steps are deliberately flat and in order: enrich, filter, budget, then reason.
Each one can end the review early, and every ending writes a row. A pull request that
vanished from the queue would look like a bug to whoever is using it.

Step 6 replaces the final placeholder with the two model calls, and step 7 adds
verification and the rubric. Nothing above that point has to change.
"""

import logging

from . import diff, model, prompts, rubric, schema, seen, sources, verify

log = logging.getLogger("reviewer.review")

# Headroom for the system prompt, the task text and the PR title and body, none of
# which are in the diff. Same figure the spike used.
PROMPT_OVERHEAD_TOKENS = 800


def empty_record(candidate: dict) -> dict:
    """The review record with everything the candidate already tells us filled in.

    Keys map one to one onto the reviews table, so the Connect sink in step 8 is a
    plain projection with no renaming. Anything not yet known is None, which is what
    the schema expects for a row that was not reviewed.
    """
    return {
        "repo": candidate["repo"],
        "pr_number": candidate["pr_number"],
        "title": candidate.get("title"),
        "author": candidate.get("author"),
        "html_url": candidate.get("html_url"),
        "pr_updated_at": candidate.get("pr_updated_at"),
        "head_sha": None,
        "status": None,
        "reason": None,
        "impact_color": None,
        "security_color": None,
        "impact": None,
        "security": None,
        "model": None,
        "model_seconds": None,
        "retried": False,
        "done_reason": None,
        "prompt_tokens": None,
    }


def _ended(record: dict, status: str, reason: str) -> dict:
    record["status"] = status
    record["reason"] = reason
    return record


def _rule_verdict(summary: str) -> dict:
    """A light's payload in the same shape the model returns one.

    Same keys means the web page and the Postgres sink treat a rule-assigned light and
    a model-assigned light identically, with no special case for either.
    """
    return {
        "findings": [],
        "dismissed": [],
        "confidence": "high",
        "summary": summary,
    }


def _decided_by_rule(record: dict, reason: str, summary: str) -> dict:
    """Both lights green, assigned in code, with no model call.

    model stays NULL, so "which greens came from a rule rather than the model" is a
    query rather than a guess: status = 'reviewed' AND model IS NULL.
    """
    record["status"] = "reviewed"
    record["reason"] = reason
    record["impact_color"] = "green"
    record["security_color"] = "green"
    record["impact"] = _rule_verdict(summary)
    record["security"] = _rule_verdict(summary)
    return record


def review(candidate: dict, cfg, is_duplicate=seen.already_reviewed) -> dict | None:
    """Review one candidate, or return None if there is nothing to do.

    None means this exact commit already has a verdict, and is the one path that
    produces no record at all. See step 1.5 below for why that matters.

    is_duplicate is a parameter so tests can drive the duplicate path without a
    database. Production always uses the default.
    """
    record = empty_record(candidate)
    name = f"{record['repo']}#{record['pr_number']}"

    # 1. Enrich. The topic carries a pointer; the content has to be fetched.
    try:
        pull = sources.fetch(candidate, cfg)
    except sources.NotRecorded:
        # Replay only. The recorded search page and the recorded diffs are separate
        # captures, so a candidate can exist with no diff beside it.
        return _ended(record, "skipped", "no_fixture")
    except Exception as exc:
        log.warning("%s fetch failed: %s", name, exc)
        return _ended(record, "error", "fetch_failed")

    record["head_sha"] = pull.head_sha

    # 1.5. Seen this exact commit before? The head SHA is the only reliable signal that
    #      the code changed, and it does not exist until the fetch above.
    #
    #      This path produces nothing, which is the single exception to "every message
    #      writes a row", and it is deliberate: the sink upserts on (repo, pr_number),
    #      so writing a skip record here would overwrite the real review and wipe its
    #      colours. A duplicate has to be dropped, not recorded.
    if is_duplicate(cfg, record["repo"], record["pr_number"], pull.head_sha):
        log.info("%s already reviewed at %s, dropping", name, pull.head_sha[:8])
        return None

    # 2. Keep only Terraform. language:HCL is a property of the repository, not of the
    #    changed files, so roughly half of what reaches here touches no .tf at all.
    tf_diff = diff.keep_tf_files(pull.diff)
    if not tf_diff:
        return _ended(record, "skipped", "no_tf_files_changed")

    # 3. Comments only? Rewording a comment cannot break infrastructure, so this is a
    #    green decided in code rather than a review that never happened. It follows the
    #    same principle as the rubric: the colour is assigned by code, and the model is
    #    only ever asked for the judgment calls a rule cannot make.
    #
    #    Checked before the size test because it holds however large the diff is, and
    #    "only comments changed" is a better answer than "it was too big".
    if diff.is_cosmetic_only(tf_diff):
        return _decided_by_rule(
            record,
            "comments_only",
            "Only comments and blank lines changed in the .tf files.",
        )

    # 4. Will it fit? Ollama silently truncates rather than failing, and a confident
    #    review of half a diff is worse than no review.
    estimated = diff.estimate_tokens(tf_diff)
    if estimated + cfg.num_predict + PROMPT_OVERHEAD_TOKENS > cfg.num_ctx:
        log.info("%s too large: ~%s tokens", name, estimated)
        return _ended(record, "skipped", "too_large")

    log.info(
        "%s reviewing: %s .tf files, ~%s estimated tokens",
        name,
        diff.count_files(tf_diff),
        estimated,
    )

    # Parsed once and passed down, rather than re-scanned for every finding the way the
    # spike did. Verification and milestone 2's candidate extraction both want it.
    sections = diff.split_sections(tf_diff)
    old_paths = diff.old_side_paths(tf_diff)

    # 5. Reason. One call per light, impact then security, sharing a prompt prefix.
    record["model"] = cfg.active_model
    total_seconds = 0.0
    for light, task, categories in prompts.LIGHTS:
        user = prompts.user_message(
            record["title"], candidate.get("body"), tf_diff, task
        )
        try:
            answer = model.ask(cfg, prompts.SYSTEM, user, schema.response_schema(categories))
        except Exception as exc:
            log.warning("%s %s call failed: %s", name, light, exc)
            record["model_seconds"] = round(total_seconds, 2)
            return _ended(record, "error", "model_timeout")

        total_seconds += answer.seconds
        record["retried"] = record["retried"] or answer.retried
        record["done_reason"] = answer.done_reason
        # The first light's prompt tokens are the real cost; the second reuses the
        # cached prefix and reports a much smaller number.
        if record["prompt_tokens"] is None:
            record["prompt_tokens"] = answer.prompt_tokens

        if not answer.ok:
            log.warning(
                "%s %s returned no usable JSON (done_reason=%s)",
                name, light, answer.done_reason,
            )
            record["model_seconds"] = round(total_seconds, 2)
            return _ended(record, "error", "model_unusable")

        # 6. Check the model's evidence against the diff, then let the rubric decide
        #    the colour. Verification results are stored beside each finding so the
        #    page can show why a finding did or did not count.
        results = [
            verify.verify(f, sections, old_paths) for f in answer.content["findings"]
        ]
        judged = rubric.judge(answer.content["findings"], results)
        for finding, j in zip(answer.content["findings"], judged):
            finding["verification"] = j.status
            finding["verified_path"] = j.path
            finding["capped_severity"] = j.capped

        colour = rubric.colour(judged, answer.content["confidence"])
        record[light] = answer.content
        record[f"{light}_color"] = colour

        log.info(
            "%s %s: %s -> %s findings (%s verified), %s dismissed, confidence %s, %.1fs",
            name, light, colour.upper(),
            len(judged),
            sum(1 for j in judged if j.verified),
            len(answer.content["dismissed"]),
            answer.content["confidence"],
            answer.seconds,
        )

    record["model_seconds"] = round(total_seconds, 2)
    record["status"] = "reviewed"
    return record
