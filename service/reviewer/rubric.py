"""Turning verified findings into a traffic light.

The model never picks the colour. It reports findings with evidence; this decides what
they add up to. That split is the whole design: grounding proves the model read the
diff, the rubric decides what the diff means, and only the second half is something a
reviewer can argue with line by line.

Ported from the jq rubric in spike/phase0.sh.
"""

import re
from dataclasses import dataclass

SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}
HIGH, MEDIUM = 2, 1

# Findings under examples/ are capped because example code is not deployed. The path
# used is the one the evidence was actually found in, not the one the model claimed,
# so a model that mislabels the file cannot dodge or trigger the cap.
EXAMPLES_PATH = re.compile(r"(^|/)examples/")

# Neither of these can carry a red on its own.
#
# forced_replacement: a diff cannot prove a replacement, only a plan can, and the model
# invented one more than once in the spike including claiming that scaling an EKS node
# group forces one.
#
# other: red should require a named, checkable category. Without this cap the model can
# reach red through the one label that means "I could not classify this", which is
# exactly where its reasoning is weakest. Measured: on Federated-Engineers#123 the model
# leaked a security finding into the impact light, and whether it landed on red or
# yellow came down to whether it happened to label it forced_replacement or other.
CANNOT_STAND_ALONE = ("forced_replacement", "other")


@dataclass(frozen=True)
class Judged:
    finding: dict
    status: str
    path: str
    capped: int

    @property
    def verified(self) -> bool:
        return self.status == "ok"


def capped_severity(category: str, path: str, severity: str) -> int:
    rank = SEVERITY_RANK.get((severity or "").lower(), 0)
    if category in CANNOT_STAND_ALONE or EXAMPLES_PATH.search(path or ""):
        return min(rank, MEDIUM)
    return rank


def judge(findings: list[dict], results: list) -> list[Judged]:
    """Pair each finding with its verification result and its capped severity."""
    judged = []
    for finding, result in zip(findings, results):
        path = result.path or finding.get("file", "")
        judged.append(
            Judged(
                finding=finding,
                status=result.status,
                path=path,
                capped=capped_severity(
                    finding.get("category", ""), path, finding.get("severity", "")
                ),
            )
        )
    return judged


def colour(judged: list[Judged], confidence: str) -> str:
    """red, yellow or green.

    red     a verified finding that is still high severity after the caps
    yellow  any finding at all, verified or not, or low confidence
    green   nothing found, and the model was not unsure

    An unverified finding still makes it yellow. That is deliberately cautious: the
    model saw something, and the evidence not landing means this cannot be trusted,
    not that the pull request is fine.
    """
    if any(j.capped == HIGH for j in judged if j.verified):
        return "red"
    if judged or (confidence or "").lower() == "low":
        return "yellow"
    return "green"
