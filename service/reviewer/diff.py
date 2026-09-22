"""Parsing a unified diff: keep the .tf files, and split it up by file.

This is a port of the awk in spike/phase0.sh. The 28 recorded pull requests in
fixtures/prs/ each carry a tf.expected.diff produced by that awk, and tests/test_diff.py
asserts this module reproduces all 28 byte for byte. That is what makes the port
checkable rather than a rewrite that looks about right.
"""


FILE_HEADER = "diff --git "


def _new_side_path(header: str) -> str:
    """The b/ path from a 'diff --git a/x b/x' line, with the b/ stripped.

    awk reads this as $NF, the last whitespace-separated field, so a path containing
    a space would confuse both implementations in exactly the same way. Faithful to
    the original on purpose: the fixtures would catch a divergence, not a shared quirk.
    """
    last = header.split()[-1]
    return last[2:] if last.startswith("b/") else last


def keep_tf_files(diff_text: str) -> str:
    """Drop every file section whose new-side path does not end in .tf.

    .tfvars are excluded deliberately. A changed value means nothing without the .tf
    code that consumes it, and the diff does not include that code. That is a known
    blind spot accepted on purpose, not an oversight: judging a one-line flip of
    networking_enabled needs the configuration that reads the variable.
    """
    out = []
    keep = False
    for line in diff_text.splitlines(keepends=True):
        if line.startswith(FILE_HEADER):
            keep = _new_side_path(line).endswith(".tf")
        if keep:
            out.append(line)
    return "".join(out)


def split_sections(diff_text: str) -> dict[str, str]:
    """Map each new-side path to that file's slice of the diff.

    Built once per review and passed down, rather than re-scanning the whole diff for
    every finding the way the spike did. Verification in step 7 needs exactly this, and
    so will the candidate extractor in milestone 2.

    A diff can touch the same path twice; the later section wins, which matches how the
    spike's awk behaved when it matched on the b/ path.
    """
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in diff_text.splitlines(keepends=True):
        if line.startswith(FILE_HEADER):
            current = sections.setdefault(_new_side_path(line), [])
            current.clear()
        if current is not None:
            current.append(line)
    return {path: "".join(lines) for path, lines in sections.items()}


def count_files(diff_text: str) -> int:
    return sum(1 for line in diff_text.splitlines() if line.startswith(FILE_HEADER))


def estimate_tokens(text: str) -> int:
    """Characters over 3, deliberately pessimistic.

    Characters over 4 is the usual rule of thumb and it under-counted fixture #1 by
    about 25%. Ollama truncates a prompt that exceeds num_ctx without reporting it, so
    an over-estimate costs a skipped pull request and an under-estimate costs a silently
    truncated diff and a confident review of half a change.
    """
    return len(text) // 3

# Only these two start a line comment in HCL. Block comments (/* ... */) are not
# detected on purpose: tracking the open and close across a diff hunk is state this
# does not need, and an undetected block comment is classified as code, which costs a
# wasted model call rather than a missed change.
LINE_COMMENT_PREFIXES = ("#", "//")


def changed_lines(diff_text: str) -> list[str]:
    """The added and removed content lines, with the +/- stripped.

    Excludes the +++ and --- file headers, which are metadata that happens to share
    the added and removed markers.
    """
    out = []
    for line in diff_text.splitlines():
        if line.startswith(("+++", "---")) or not line or line[0] not in "+-":
            continue
        out.append(line[1:])
    return out


def is_cosmetic_only(diff_text: str) -> bool:
    """True if every changed line is blank or a line comment, and there is at least one.

    A pull request that only rewords comments cannot break infrastructure, so spending
    50 seconds of inference on it buys nothing. This is the shape of filtering the brief
    asks for: a rule doing what a rule can do, so the model is left with the judgment
    calls it is actually needed for.

    Scope is deliberately narrow: this tool reviews Terraform changes, so a comment is
    judged as a comment. Directives that policy scanners read out of comments, such as
    checkov skips or tflint ignores, are out of scope and are treated as ordinary text.

    Conservative by construction. Anything that is not obviously a comment or blank
    counts as code, so the failure mode is a wasted review rather than a missed change.
    That includes an indentation-only edit, where both sides are code lines, and a pure
    rename, where there are no changed content lines at all and this returns False.
    """
    lines = changed_lines(diff_text)
    if not lines:
        return False
    for line in lines:
        body = line.strip()
        if not body:
            continue
        if not body.startswith(LINE_COMMENT_PREFIXES):
            return False
    return True

def old_side_paths(diff_text: str) -> dict[str, str]:
    """new-side path -> old-side path, for each file section.

    Verification matches a finding's claimed filename against either side, because a
    model looking at a renamed file may name it by the path it used to have.
    """
    out = {}
    for line in diff_text.splitlines():
        if not line.startswith(FILE_HEADER):
            continue
        fields = line.split()
        if len(fields) < 4:
            continue
        old = fields[-2]
        out[_new_side_path(line)] = old[2:] if old.startswith("a/") else old
    return out

