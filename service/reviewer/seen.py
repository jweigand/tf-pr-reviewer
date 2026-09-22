"""Has this exact commit already been reviewed?

Search results carry no commit SHA, so Connect cannot tell a genuinely new revision
from a pull request whose `updated_at` moved for some other reason. A comment, a label,
a CI status or a base-branch change all bump it while the code stays identical.

Measured on live traffic before this existed: 76 model calls covering 34 distinct
(pull request, head SHA) pairs. 42 re-reviews, about 22 minutes of inference, for
diffs that had not changed. One pull request was reviewed 7 times at the same SHA.

This is the correctness boundary. The Connect dedupe is an optimisation that reduces
how often the question gets asked; this is what actually answers it.
"""

import logging

import psycopg

log = logging.getLogger("reviewer.seen")

# An error row is not a verdict, it is a failure to reach one, so the same commit is
# worth another attempt. Anything else (reviewed, or skipped for no .tf files, comments
# only, too large) is a settled answer that will not change for the same diff.
QUERY = """
    SELECT 1 FROM reviews
    WHERE repo = %s AND pr_number = %s AND head_sha = %s AND status <> 'error'
    LIMIT 1
"""


def already_reviewed(cfg, repo: str, pr_number: int, head_sha: str) -> bool:
    """True if this commit already has a settled verdict.

    A short-lived connection per check rather than a pool. The service reviews one pull
    request at a time and a local connect costs a few milliseconds against a model call
    of forty seconds, so a pool would be machinery with nothing to gain.

    If Postgres cannot be reached this returns False, so the pull request gets reviewed.
    That spends inference that may be wasted, but the upsert makes a repeated review
    harmless, whereas guessing True would silently drop a real one.
    """
    try:
        with psycopg.connect(cfg.postgres_dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(QUERY, (repo, pr_number, head_sha))
                return cur.fetchone() is not None
    except Exception as exc:
        log.warning("could not check for an existing review, assuming new: %s", exc)
        return False
