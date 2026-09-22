"""The .tf filter, checked against every recorded pull request.

Each fixture carries a tf.expected.diff produced by the awk in spike/phase0.sh. These
tests assert the Python port reproduces all of them byte for byte, which is what turns
"I rewrote the awk in Python" into something a reviewer can trust.

Naming the pull request each case came from is deliberate: when one fails you want to
know which real change broke it, not just that case 17 broke.
"""

import pathlib

import pytest

from reviewer import diff

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "prs"
CASES = sorted(p.name for p in FIXTURES.iterdir() if p.is_dir())


def read(name: str, filename: str) -> str:
    return (FIXTURES / name / filename).read_text(errors="surrogateescape")


def test_fixture_corpus_is_present():
    """A silently empty corpus would make every test below pass vacuously."""
    assert len(CASES) >= 28


@pytest.mark.parametrize("pr", CASES)
def test_keep_tf_files_matches_the_recorded_awk_output(pr):
    assert diff.keep_tf_files(read(pr, "full.diff")) == read(pr, "tf.expected.diff")


@pytest.mark.parametrize("pr", CASES)
def test_every_kept_section_is_a_tf_file(pr):
    """The filter's actual contract, stated independently of the recorded output."""
    for path in diff.split_sections(read(pr, "tf.expected.diff")):
        assert path.endswith(".tf"), f"{pr} kept a non-.tf path: {path}"


@pytest.mark.parametrize("pr", CASES)
def test_sections_partition_the_filtered_diff(pr):
    """Splitting by file must not lose or invent lines."""
    tf = read(pr, "tf.expected.diff")
    sections = diff.split_sections(tf)
    assert sum(len(s) for s in sections.values()) == len(tf)
    assert len(sections) == diff.count_files(tf)


def test_tfvars_are_excluded():
    """The deliberate blind spot. A value change means nothing without the .tf code
    that consumes it, and the diff does not include that code. This test exists so the
    exclusion stays a decision someone made rather than something that quietly drifts."""
    sample = (
        "diff --git a/main.tf b/main.tf\n"
        "+resource \"aws_s3_bucket\" \"b\" {}\n"
        "diff --git a/prod.tfvars b/prod.tfvars\n"
        "+networking_enabled = false\n"
    )
    kept = diff.keep_tf_files(sample)
    assert "aws_s3_bucket" in kept
    assert "networking_enabled" not in kept


def test_a_renamed_file_is_judged_on_its_new_path():
    """awk reads the last field of the header, which is the b/ side."""
    sample = (
        "diff --git a/old.txt b/new.tf\n"
        "+keep me\n"
        "diff --git a/old.tf b/new.txt\n"
        "+drop me\n"
    )
    kept = diff.keep_tf_files(sample)
    assert "keep me" in kept
    assert "drop me" not in kept


def test_estimate_is_pessimistic():
    """chars/4 under-counted fixture #1 by about 25%, so the estimate uses chars/3."""
    text = "x" * 1200
    assert diff.estimate_tokens(text) == 400
    assert diff.estimate_tokens(text) > len(text) // 4


# --------------------------------------------------------------- comments-only

# The two real pull requests in the corpus whose .tf changes are comments and blank
# lines only. surajkoditala#14 is titled "dummy change to validate ... end to end" and
# tbadmus#9 rewrites a commented-out setup instruction.
COSMETIC_PRS = [
    "surajkoditala_humaid-risk-governance_14",
    "tbadmus_tech-challenge_9",
]


@pytest.mark.parametrize("pr", COSMETIC_PRS)
def test_real_comment_only_prs_are_detected(pr):
    assert diff.is_cosmetic_only(read(pr, "tf.expected.diff")) is True


@pytest.mark.parametrize("pr", [c for c in CASES if c not in COSMETIC_PRS])
def test_no_other_pr_is_called_cosmetic(pr):
    """The expensive direction. A false positive here silently skips a real change,
    so every other pull request in the corpus must come back False."""
    tf = read(pr, "tf.expected.diff")
    if not tf.strip():
        pytest.skip("no .tf changes")
    assert diff.is_cosmetic_only(tf) is False


def test_a_plain_comment_edit_is_cosmetic():
    assert diff.is_cosmetic_only(
        "diff --git a/main.tf b/main.tf\n-# just a note\n+# another note\n"
    ) is True


def test_an_inline_comment_on_a_code_line_is_not_cosmetic():
    assert diff.is_cosmetic_only(
        'diff --git a/main.tf b/main.tf\n+  acl = "public-read" # loosened\n'
    ) is False


def test_indentation_only_change_is_not_cosmetic():
    """Both sides are code lines. Conservative on purpose: this costs a wasted review
    rather than a missed change."""
    assert diff.is_cosmetic_only(
        "diff --git a/main.tf b/main.tf\n-  foo = 1\n+    foo = 1\n"
    ) is False


def test_a_diff_with_no_changed_lines_is_not_cosmetic():
    """A pure rename has no +/- content lines. In Terraform a moved file can be a
    module restructure, which is exactly what must not be skipped."""
    assert diff.is_cosmetic_only(
        "diff --git a/old.tf b/new.tf\nsimilarity index 100%\n"
        "rename from old.tf\nrename to new.tf\n"
    ) is False


def test_file_headers_are_not_mistaken_for_changed_lines():
    """+++ and --- share the added and removed markers but are metadata."""
    assert diff.changed_lines(
        "diff --git a/main.tf b/main.tf\n--- a/main.tf\n+++ b/main.tf\n"
        "@@ -1 +1 @@\n-# old\n+# new\n"
    ) == ["# old", "# new"]
