"""Where a pull request's head SHA and diff come from: recorded fixtures, or GitHub.

This is the enrichment step. The candidate on the topic carries only metadata and a
pointer; the content a model can actually reason about has to be fetched.

Two implementations behind one function, chosen by INGEST_MODE, so replay needs no
GitHub token and the default `docker compose up` works from a fresh clone.
"""

import json
import pathlib
from dataclasses import dataclass

import httpx

USER_AGENT = "tf-pr-reviewer"
API_VERSION = "2022-11-28"


class NotRecorded(Exception):
    """Replay mode was asked for a pull request with no fixture on disk."""


@dataclass(frozen=True)
class PullRequest:
    head_sha: str
    diff: str


def slug(repo: str, pr_number: int) -> str:
    """fixtures/prs directory name. Must match scripts/record-fixtures.sh."""
    return f"{repo}_{pr_number}".replace("/", "_")


def fetch(candidate: dict, cfg) -> PullRequest:
    if cfg.ingest_mode == "replay":
        return _from_fixtures(candidate, cfg)
    return _from_github(candidate, cfg)


def _from_fixtures(candidate: dict, cfg) -> PullRequest:
    directory = pathlib.Path(cfg.fixtures_dir) / "prs" / slug(
        candidate["repo"], candidate["pr_number"]
    )
    detail = directory / "pr.json"
    diff = directory / "full.diff"
    if not detail.is_file() or not diff.is_file():
        raise NotRecorded(f"no fixture at {directory}")
    return PullRequest(
        head_sha=json.loads(detail.read_text())["head"]["sha"],
        # surrogateescape: diffs are bytes from arbitrary repositories and a stray
        # non-UTF-8 sequence should not take down a review.
        diff=diff.read_text(errors="surrogateescape"),
    )


def _from_github(candidate: dict, cfg) -> PullRequest:
    url = candidate["pr_api_url"]
    headers = {
        # GitHub rejects requests with an empty User-Agent.
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": API_VERSION,
    }
    if cfg.github_token:
        # Omitted entirely when there is no token: sending "Bearer " with an empty
        # value is a 401, not anonymous access.
        headers["Authorization"] = f"Bearer {cfg.github_token}"

    # follow_redirects is not httpx's default, and diff URLs redirect to another host.
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        detail = client.get(
            url, headers={**headers, "Accept": "application/vnd.github+json"}
        )
        detail.raise_for_status()
        # Same URL, different Accept. GitHub answers 406 for very large diffs, which
        # raise_for_status turns into an error the caller records as fetch_failed.
        diff = client.get(
            url, headers={**headers, "Accept": "application/vnd.github.diff"}
        )
        diff.raise_for_status()

    body = diff.text
    if not body.startswith("diff --git"):
        # A redirect body or an error page rather than a diff. Better to fail here than
        # to hand the model something that is not a diff at all.
        raise ValueError(f"response for {url} does not look like a diff")

    return PullRequest(head_sha=detail.json()["head"]["sha"], diff=body)
