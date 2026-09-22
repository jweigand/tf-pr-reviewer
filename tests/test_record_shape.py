"""The record shape has to agree in three places, so it gets a test.

  service/reviewer/review.py   builds the record
  connect/sink-postgres.yaml   projects it into an INSERT
  sql/001_schema.sql           declares the columns

Nothing at runtime checks that those agree. A field added to the record but not to the
sink is silently dropped; a column added to the schema but not to the record is silently
NULL. Both are the kind of bug you find weeks later in the data rather than in a log, so
they are checked statically here instead.
"""

import pathlib
import re

from reviewer import review

ROOT = pathlib.Path(__file__).resolve().parent.parent
SINK = (ROOT / "connect" / "sink-postgres.yaml").read_text()
SCHEMA = (ROOT / "sql" / "001_schema.sql").read_text()

# Columns the service never supplies: generated, or defaulted by Postgres.
NOT_FROM_THE_SERVICE = {"worst_rank", "reviewed_at"}


def record_keys() -> set[str]:
    return set(review.empty_record({"repo": "a/b", "pr_number": 1}))


def schema_columns() -> set[str]:
    body = SCHEMA[SCHEMA.index("CREATE TABLE") : SCHEMA.index("PRIMARY KEY")]
    found = set()
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("--"):
            continue
        m = re.match(r"^([a-z_]+)\s+(TEXT|INTEGER|SMALLINT|BOOLEAN|JSONB|NUMERIC|TIMESTAMPTZ)", line)
        if m:
            found.add(m.group(1))
    return found - NOT_FROM_THE_SERVICE


def sink_insert_columns() -> list[str]:
    block = re.search(r"INSERT INTO reviews \(\s*(.*?)\s*\) VALUES", SINK, re.S).group(1)
    return [c.strip() for c in block.replace("\n", " ").split(",") if c.strip()]


def sink_args() -> list[str]:
    """Field names in args_mapping order.

    Consecutive repeats are collapsed: the two jsonb columns are written as
    `if this.impact == null { null } else { this.impact.string() }`, so the name
    legitimately appears twice for one argument.
    """
    block = SINK[SINK.index("root = [") : SINK.index("logger:")]
    names = re.findall(r"this\.([a-z_]+)", block)
    return [n for i, n in enumerate(names) if i == 0 or names[i - 1] != n]


def test_the_record_matches_the_table():
    assert record_keys() == schema_columns()


def test_the_sink_inserts_every_column_the_service_sets():
    assert set(sink_insert_columns()) == record_keys()


def test_the_sink_reads_every_field_it_inserts():
    """A mismatch here means the args are shifted relative to the placeholders, which
    would write values into the wrong columns rather than failing."""
    assert set(sink_args()) == set(sink_insert_columns())


def test_column_and_argument_order_line_up():
    """$1..$18 are positional. Same names in the same order, or the INSERT silently
    puts the title in the author column."""
    assert sink_args() == sink_insert_columns()


def test_placeholder_count_matches_the_column_count():
    values = re.search(r"\) VALUES \(\s*(.*?)\s*\)\s*\n\s*ON CONFLICT", SINK, re.S).group(1)
    placeholders = re.findall(r"\$\d+", values)
    assert len(placeholders) == len(sink_insert_columns())
    assert [int(p[1:]) for p in placeholders] == list(range(1, len(placeholders) + 1))


def test_upsert_refreshes_every_mutable_column():
    """A new head commit must replace the old verdict, not leave half of it behind."""
    block = SINK[SINK.index("DO UPDATE SET") : SINK.index("args_mapping")]
    updated = set(re.findall(r"^\s*([a-z_]+)\s*=\s*EXCLUDED", block, re.M))
    # Everything except the primary key, which is what the conflict is on.
    assert updated == record_keys() - {"repo", "pr_number"}
    assert re.search(r"reviewed_at\s*=\s*now\(\)", block)
