"""Dropping a pull request that has already been reviewed at this exact commit.

The bug this prevents, measured on live traffic: 76 model calls covering 34 distinct
(pull request, head SHA) pairs. The same commit of padok-team#15 was reviewed 7 times
because its updated_at kept moving while its code did not.

is_duplicate is injected, so these run with no database.
"""

import pathlib
import re

import pytest

from reviewer import review, seen

ROOT = pathlib.Path(__file__).resolve().parent.parent

CANDIDATE = {
    "repo": "padok-team/terraform-azurerm-postgresql-server",
    "pr_number": 15,
    "title": "feat(psql_server): add private endpoint",
    "author": "uromys",
    "html_url": "https://github.com/x/y/pull/15",
    "pr_updated_at": "2026-09-13T15:53:33Z",
}


COMMENTS_ONLY = {
    "repo": "surajkoditala/humaid-risk-governance",
    "pr_number": 14,
    "title": "test(iac): dummy comment to validate pipeline end to end",
}


class Config:
    """Minimal stand-in. Only the fields the early steps of review() touch."""

    ingest_mode = "replay"
    fixtures_dir = str(ROOT / "fixtures")
    github_token = ""
    postgres_dsn = "postgres://unused"
    num_ctx = 8192
    num_predict = 3000


def test_a_duplicate_produces_no_record_at_all():
    """None, not a skip record. A skip record would upsert over the real review and
    wipe its colours, which is worse than the wasted model call it was meant to save."""
    assert review.review(CANDIDATE, Config(), is_duplicate=lambda *a: True) is None


def test_a_new_commit_is_not_dropped():
    """A pull request not yet seen at this SHA must still be reviewed.

    Uses the comments-only fixture so the assertion is about the dedupe decision and
    the suite never needs a model."""
    record = review.review(COMMENTS_ONLY, Config(), is_duplicate=lambda *a: False)
    assert record is not None
    assert record["head_sha"] == "a1858e7228fc14e1239484ad4b37fe32c5b9e760"
    assert record["reason"] == "comments_only"


def test_the_check_receives_the_fetched_head_sha_not_the_candidate():
    """The candidate carries no SHA, so the check has to run after enrichment or it
    would be comparing nothing."""
    seen_args = []
    review.review(
        CANDIDATE, Config(), is_duplicate=lambda cfg, r, n, sha: seen_args.append((r, n, sha)) or True
    )
    assert seen_args == [
        ("padok-team/terraform-azurerm-postgresql-server", 15,
         "fa56fb994b4fecde9c5a7eee144a438f57abcccf")
    ]


def test_a_missing_fixture_is_recorded_rather_than_dropped():
    """Only a duplicate returns None. Every other early exit still writes a row."""
    record = review.review(
        {"repo": "no/such", "pr_number": 1}, Config(), is_duplicate=lambda *a: False
    )
    assert record is not None and record["reason"] == "no_fixture"


def test_an_error_row_does_not_suppress_a_retry():
    """A fetch failure or a model timeout is not a verdict. The same commit has to be
    allowed another attempt, or one transient failure buries a pull request forever."""
    assert "status <> 'error'" in seen.QUERY


def test_the_query_keys_on_all_three_identifying_columns():
    for column in ("repo", "pr_number", "head_sha"):
        assert re.search(rf"\b{column}\s*=\s*%s", seen.QUERY), column
