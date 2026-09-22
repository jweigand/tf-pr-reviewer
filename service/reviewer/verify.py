"""Checking a finding against the diff it claims to come from.

The model is asked to quote a line of the diff as evidence. This is where that claim is
checked, in code, rather than taken on trust. A small model will invent evidence: in the
spike, qwen2.5-coder:7b produced five fabricated security findings on one pull request
and every one of them was caught here.

What this cannot do is catch bad judgment. A finding whose evidence really is in the
diff and whose conclusion is wrong passes verification cleanly. That is the reason the
rubric lives in code too: grounding proves the model read the diff, not that it was
right about it.

Ported from verify_finding in spike/phase0.sh.
"""

import re
from dataclasses import dataclass

# A finding in one of these categories has to point at a resource or module block that
# the diff actually removes. Without the check, "resource_removal" became a catch-all
# label the model reached for whenever something looked alarming.
CATEGORIES_NEEDING_A_REMOVED_BLOCK = ("resource_removal", "rename_without_moved")

BLOCK_HEADER = re.compile(r'^(resource|module)\s+"')


@dataclass(frozen=True)
class Result:
    status: str  # ok | file_not_in_diff | evidence_not_found | category_mismatch
    path: str | None  # the diff path the evidence was actually found in

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def normalize(line: str) -> str:
    """Strip one leading diff marker, then surrounding whitespace.

    The model quotes evidence with or without the +/- it saw, and with whatever
    indentation the file had. Both sides of the comparison go through this.
    """
    if line[:1] in ("+", "-", " "):
        line = line[1:]
    return line.strip()


def _strip_prefix(claimed: str) -> str:
    for prefix in ("./", "a/", "b/"):
        if claimed.startswith(prefix):
            return claimed[len(prefix) :]
    return claimed


def candidate_paths(claimed: str, sections: dict, old_paths: dict) -> list[str]:
    """Diff paths that the finding's filename could be referring to.

    Exact match, or a path suffix on a directory boundary, so a model that says
    "main.tf" resolves to "modules/postgresql/main.tf". A bare filename can match more
    than one file, which is why this returns a list and the caller tries each.
    """
    claimed = _strip_prefix(claimed)
    matches = []
    for new_path in sections:
        old_path = old_paths.get(new_path, new_path)
        if any(
            p == claimed or p.endswith("/" + claimed) for p in (new_path, old_path)
        ):
            matches.append(new_path)
    return matches


def evidence_in(section: str, evidence: str) -> bool:
    """Every non-blank evidence line must appear somewhere in this file's section.

    Blank lines are skipped rather than failed: a removed blank line is a lone "-" that
    normalizes to nothing, and requiring it to match would fail honest evidence. At
    least one line has to be checked, so empty evidence cannot pass by default.
    """
    checked = 0
    for line in evidence.splitlines():
        text = normalize(line)
        if not text:
            continue
        checked += 1
        if text not in section:
            return False
    return checked > 0


def cites_removed_block(section: str, evidence: str) -> bool:
    """True if some evidence line is a resource or module header the diff removes."""
    removed = "\n".join(l for l in section.splitlines() if l.startswith("-"))
    for line in evidence.splitlines():
        text = normalize(line)
        if BLOCK_HEADER.match(text) and text in removed:
            return True
    return False


def verify(finding: dict, sections: dict, old_paths: dict) -> Result:
    """Check one finding. See Result.status for the vocabulary."""
    paths = candidate_paths(finding.get("file", ""), sections, old_paths)
    if not paths:
        return Result("file_not_in_diff", None)

    for path in paths:
        if evidence_in(sections[path], finding.get("evidence", "")):
            if finding.get("category") in CATEGORIES_NEEDING_A_REMOVED_BLOCK:
                if not cites_removed_block(sections[path], finding["evidence"]):
                    return Result("category_mismatch", path)
            return Result("ok", path)

    return Result("evidence_not_found", None)
