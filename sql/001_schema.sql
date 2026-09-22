-- One row per pull request, keyed on (repo, pr_number).
--
-- The key is the PR, not the commit: the review queue shows current state, so a
-- new head commit updates the row rather than adding one. head_sha records which
-- commit the row describes, and the service skips a SHA it has already reviewed.
--
-- Connect upserts into this table, so the service does not write to Postgres.
-- Everything here has to be fillable from one JSON message on pr.reviews.

CREATE TABLE IF NOT EXISTS reviews (
    repo            TEXT        NOT NULL,
    pr_number       INTEGER     NOT NULL,

    -- what was reviewed
    head_sha        TEXT,
    title           TEXT,
    author          TEXT,
    html_url        TEXT,
    pr_updated_at   TIMESTAMPTZ,

    -- Outcome. status is always set; the colors are NULL when status is not 'reviewed'.
    --   reviewed -> both lights carry a color
    --   skipped  -> filtered out before the model, reason says why:
    --               no_fixture           replay only, no recorded diff for this PR
    --               no_tf_files_changed  changed nothing ending in .tf
    --               too_large            would not fit in num_ctx
    --   error    -> the pipeline broke, reason carries the failure
    --               (fetch_failed, model_unusable, model_timeout)
    -- A skipped PR still gets a row. One that silently vanishes looks like a bug.
    --
    -- Some rows are 'reviewed' with no model call: a diff whose .tf changes are all
    -- comments is green by rule, carrying reason 'comments_only'. model IS NULL is
    -- what distinguishes those from a model's answer.
    status          TEXT        NOT NULL,
    reason          TEXT,
    impact_color    TEXT,
    security_color  TEXT,

    -- The worst of the two lights, stored rather than recomputed in every query.
    -- Ranked numerically because sorting the colour names alphabetically gives green,
    -- red, yellow, which is wrong. The page defaults to newest first and offers this
    -- as one of its sortable columns.
    worst_rank      SMALLINT GENERATED ALWAYS AS (
        CASE
            WHEN impact_color = 'red'    OR security_color = 'red'    THEN 0
            WHEN impact_color = 'yellow' OR security_color = 'yellow' THEN 1
            WHEN impact_color = 'green' AND security_color = 'green'  THEN 2
            ELSE 3
        END
    ) STORED,

    -- full findings, evidence, verification statuses and dismissals, one blob per light
    impact          JSONB,
    security        JSONB,

    -- How the answer was produced. Kept because these are the first questions asked
    -- when a review looks wrong, and reconstructing them later is impossible.
    model           TEXT,
    model_seconds   NUMERIC(8,2),
    retried         BOOLEAN     NOT NULL DEFAULT FALSE,
    done_reason     TEXT,
    prompt_tokens   INTEGER,

    reviewed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (repo, pr_number),

    CONSTRAINT reviews_status_known
        CHECK (status IN ('reviewed', 'skipped', 'error')),
    CONSTRAINT reviews_impact_color_known
        CHECK (impact_color   IS NULL OR impact_color   IN ('green', 'yellow', 'red')),
    CONSTRAINT reviews_security_color_known
        CHECK (security_color IS NULL OR security_color IN ('green', 'yellow', 'red'))
);

-- Serves the ordering the API returns: worst first, newest first within a rank.
CREATE INDEX IF NOT EXISTS reviews_queue_idx ON reviews (worst_rank, reviewed_at DESC);
