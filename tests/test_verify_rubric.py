"""The riskiest logic in the service: grounding a finding, then colouring it.

Built on real recorded model output in fixtures/model/, so these run with no Ollama, no
GPU and no network. The central case is the one the brief asks for: a real finding is
verified, a fabricated one is rejected, and the colour follows.

Fixture #1 is padok-team/terraform-azurerm-postgresql-server#15, the pull request the
golden set labels impact=red, security=yellow. It removes two module input variables and
hardcodes a password in an example.
"""

import json
import pathlib

import pytest

from reviewer import diff, rubric, verify

ROOT = pathlib.Path(__file__).resolve().parent.parent
PRS = ROOT / "fixtures" / "prs"
MODEL = ROOT / "fixtures" / "model"

FIXTURE_1 = "padok-team_terraform-azurerm-postgresql-server_15"


def load(pr: str):
    """The .tf diff, split for verification, plus the recorded model answer."""
    tf = (PRS / pr / "tf.expected.diff").read_text(errors="surrogateescape")
    answer = json.loads((MODEL / f"{pr}.json").read_text())
    return diff.split_sections(tf), diff.old_side_paths(tf), answer


def judge_light(pr: str, light: str):
    sections, old_paths, answer = load(pr)
    findings = answer[light]["findings"]
    results = [verify.verify(f, sections, old_paths) for f in findings]
    judged = rubric.judge(findings, results)
    return judged, rubric.colour(judged, answer[light]["confidence"])


# ------------------------------------------------------------------ the real test

def test_a_real_finding_is_verified_and_makes_it_red():
    """Fixture #1 impact. The model quoted the removed variable block from the diff,
    the evidence is really there, and a verified high-severity interface break is red."""
    judged, colour = judge_light(FIXTURE_1, "impact")

    assert len(judged) == 1
    assert judged[0].status == "ok"
    assert judged[0].finding["category"] == "module_interface_change"
    assert judged[0].capped == rubric.HIGH
    assert colour == "red"


def test_a_fabricated_finding_is_rejected_and_cannot_reach_red():
    """The same pull request, same category and severity, evidence the model invented.

    This is the failure the grounding exists for. It must not reach red, and because an
    unverified finding is still treated cautiously, it lands on yellow rather than green."""
    sections, old_paths, answer = load(FIXTURE_1)
    fabricated = dict(answer["impact"]["findings"][0])
    fabricated["evidence"] = 'resource "azurerm_postgresql_server" "nonexistent" {'

    result = verify.verify(fabricated, sections, old_paths)
    assert result.status == "evidence_not_found"

    judged = rubric.judge([fabricated], [result])
    assert judged[0].verified is False
    assert rubric.colour(judged, "high") == "yellow"


def test_the_examples_cap_turns_a_real_finding_yellow_not_red():
    """Fixture #1 security: a literal password, genuinely in the diff, under examples/.

    Example code is not deployed, so it cannot carry a red on its own. The cap uses the
    path the evidence was found in, so mislabelling the file cannot dodge it."""
    judged, colour = judge_light(FIXTURE_1, "security")

    assert judged[0].status == "ok"
    assert "examples/" in judged[0].path
    assert judged[0].finding["severity"] == "medium"
    assert colour == "yellow"


# ------------------------------------------------------------------ verification

def test_evidence_from_the_wrong_file_is_rejected():
    sections, old_paths, answer = load(FIXTURE_1)
    finding = dict(answer["impact"]["findings"][0])
    finding["file"] = "outputs.tf"
    assert verify.verify(finding, sections, old_paths).status in (
        "file_not_in_diff",
        "evidence_not_found",
    )


def test_a_file_not_in_the_diff_at_all():
    sections, old_paths, _ = load(FIXTURE_1)
    finding = {"file": "does_not_exist.tf", "evidence": "anything", "category": "other"}
    assert verify.verify(finding, sections, old_paths).status == "file_not_in_diff"


def test_a_bare_filename_resolves_to_a_nested_path():
    """The model says "main.tf"; the diff has "modules/x/main.tf"."""
    sections = {"modules/postgresql/main.tf": "+ foo\n"}
    assert verify.candidate_paths("main.tf", sections, {}) == [
        "modules/postgresql/main.tf"
    ]
    assert verify.candidate_paths("postgresql/main.tf", sections, {}) == [
        "modules/postgresql/main.tf"
    ]


def test_a_suffix_must_land_on_a_directory_boundary():
    """"ain.tf" must not match "main.tf"."""
    assert verify.candidate_paths("ain.tf", {"main.tf": ""}, {}) == []


def test_empty_evidence_never_verifies():
    assert verify.evidence_in("+ anything at all\n", "") is False
    assert verify.evidence_in("+ anything at all\n", "   \n  \n") is False


def test_blank_evidence_lines_are_skipped_not_failed():
    """A removed blank line is a lone "-" that normalizes to nothing. Requiring it to
    match would fail evidence that is otherwise honest."""
    section = '+resource "aws_s3_bucket" "b" {\n+  acl = "private"\n'
    assert verify.evidence_in(section, 'resource "aws_s3_bucket" "b" {\n\n  acl = "private"')


def test_normalize_strips_one_marker_then_whitespace():
    assert verify.normalize('+  acl = "private"  ') == 'acl = "private"'
    assert verify.normalize('-  acl = "private"') == 'acl = "private"'
    assert verify.normalize('   acl = "private"') == 'acl = "private"'


def test_resource_removal_must_cite_a_removed_block():
    """Small models stretch whatever category you give them, and resource_removal was
    the first to become a catch-all. Claiming it without pointing at a removed resource
    or module header is a category_mismatch."""
    section = '-resource "aws_s3_bucket" "old" {\n-  acl = "private"\n+  acl = "public"\n'
    sections, old_paths = {"main.tf": section}, {}

    honest = {
        "file": "main.tf",
        "evidence": 'resource "aws_s3_bucket" "old" {',
        "category": "resource_removal",
    }
    assert verify.verify(honest, sections, old_paths).status == "ok"

    mislabelled = {
        "file": "main.tf",
        "evidence": '  acl = "public"',
        "category": "resource_removal",
    }
    assert verify.verify(mislabelled, sections, old_paths).status == "category_mismatch"


def test_an_added_block_does_not_count_as_a_removed_one():
    section = '+resource "aws_s3_bucket" "new" {\n'
    finding = {
        "file": "main.tf",
        "evidence": 'resource "aws_s3_bucket" "new" {',
        "category": "rename_without_moved",
    }
    assert verify.verify(finding, {"main.tf": section}, {}).status == "category_mismatch"


# ------------------------------------------------------------------ rubric

@pytest.mark.parametrize(
    "category,path,severity,expected",
    [
        # A named category at high severity is the only way to reach red.
        ("resource_removal", "main.tf", "high", rubric.HIGH),
        ("resource_removal", "main.tf", "low", 0),
        # A diff cannot prove a replacement; only a plan can.
        ("forced_replacement", "main.tf", "high", rubric.MEDIUM),
        # "other" means the model could not classify it, which is where its reasoning
        # is weakest, so it cannot carry a red on its own either.
        ("other", "main.tf", "high", rubric.MEDIUM),
        # Example code is not deployed.
        ("resource_removal", "examples/simple/main.tf", "high", rubric.MEDIUM),
        ("resource_removal", "examples/main.tf", "high", rubric.MEDIUM),
        # "examples" as part of a longer name is not the examples directory.
        ("resource_removal", "my-examples-lib/main.tf", "high", rubric.HIGH),
        # An unknown severity is treated as the lowest, not the highest.
        ("resource_removal", "main.tf", "critical", 0),
    ],
)
def test_severity_caps(category, path, severity, expected):
    assert rubric.capped_severity(category, path, severity) == expected


def test_green_needs_no_findings_and_real_confidence():
    assert rubric.colour([], "high") == "green"


def test_low_confidence_alone_is_yellow():
    assert rubric.colour([], "low") == "yellow"


def test_an_unverified_high_severity_finding_is_yellow_not_red():
    """The cautious choice, and the one being made explicitly: the model saw something
    and the evidence did not land, which is a reason to look, not a reason to relax."""
    judged = rubric.judge(
        [{"category": "other", "severity": "high", "file": "main.tf"}],
        [verify.Result("evidence_not_found", None)],
    )
    assert rubric.colour(judged, "high") == "yellow"


# ------------------------------------------------------------------ whole corpus

@pytest.mark.parametrize("path", sorted(p.stem for p in MODEL.glob("*.json")))
def test_every_recorded_answer_produces_a_valid_colour(path):
    """No recorded response may crash the pipeline or produce a colour the schema's
    CHECK constraint would reject."""
    for light in ("impact", "security"):
        _, colour = judge_light(path, light)
        assert colour in ("green", "yellow", "red")


# ------------------------------------------- version constraints (fkc1e100#19)

# This pull request came back green for impact while loosening two provider
# constraints:
#
#     - version = "~> 5.0"      >= 5.0 and < 6.0, pinned to major 5
#     + version = ">= 5.0"      no upper bound at all
#     - version = "~> 7.43"     pinned to major 7
#     + version = ">= 5.0"      now also allows a downgrade
#
# The model had already noticed and dismissed it, citing the author's description:
# "the change restores a buildable state without changing which provider version is
# resolved". True of today's lockfile, irrelevant to the constraint. The miss was a
# missing category rather than a missing observation, which is why a
# version_constraint_change category fixed it and telling the model to distrust the
# description did not.
VERSION_CONSTRAINTS_PR = "fkc1e100_org-mono-repo_19"


def test_the_fixture_really_does_loosen_the_constraints():
    """Guards the premise. If someone re-records this fixture and the diff changes,
    the tests below would start proving nothing."""
    tf = (PRS / VERSION_CONSTRAINTS_PR / "tf.expected.diff").read_text(
        errors="surrogateescape"
    )
    assert '-      version = "~> 5.0"' in tf
    assert '+      version = ">= 5.0"' in tf
    assert '-      version = "~> 7.43"' in tf


def test_loosened_version_constraints_are_not_green():
    """The regression this exists for. Green here means nobody is told that a future
    terraform init can now resolve a major version that was previously excluded."""
    judged, colour = judge_light(VERSION_CONSTRAINTS_PR, "impact")
    assert colour != "green"


def test_both_loosened_constraints_are_found_and_verified():
    judged, _ = judge_light(VERSION_CONSTRAINTS_PR, "impact")
    constraint_findings = [
        j for j in judged if j.finding["category"] == "version_constraint_change"
    ]
    assert len(constraint_findings) == 2
    for j in constraint_findings:
        assert j.status == "ok", "the quoted constraint must be in the diff"
        assert '">= 5.0"' in j.finding["evidence"]


def test_the_category_exists_in_the_prompt_and_the_schema():
    """The category and its enum have to stay in step, or constrained decoding rejects
    the label the task text asks for."""
    from reviewer import prompts, schema as schema_mod

    assert "version_constraint_change" in prompts.IMPACT_CATEGORIES
    assert "version_constraint_change:" in prompts.IMPACT_TASK
    enum = schema_mod.response_schema(prompts.IMPACT_CATEGORIES)["properties"][
        "findings"
    ]["items"]["properties"]["category"]["enum"]
    assert "version_constraint_change" in enum


def test_other_cannot_reach_red_on_its_own():
    """Red should require a named, checkable category.

    Measured on Federated-Engineers#123: the model leaked a security finding into the
    impact light, and whether that landed on red or yellow came down to whether it
    happened to label it forced_replacement or other. Capping other removes the luck."""
    assert rubric.capped_severity("other", "main.tf", "high") == rubric.MEDIUM
    judged = rubric.judge(
        [{"category": "other", "severity": "high", "file": "main.tf"}],
        [verify.Result("ok", "main.tf")],
    )
    assert rubric.colour(judged, "high") == "yellow"


# ------------------------------ the two lights disagreeing (Chas-Challenge-Team5#35)

# One changed line, two opposite verdicts, both correct:
#
#     -  default = ["0.0.0.0/0"]
#     +  default = ["35.235.240.0/20"]        Google Cloud IAP range
#
# For security this is an improvement, so it belongs in dismissed, not findings. For
# impact it is a breaking change: anyone who never set the variable was getting
# unrestricted SSH and now silently loses it on upgrade, even though their code still
# plans cleanly. That is exactly what default_behavior_change is for.
#
# It also happens to have a Swedish title, which is a useful reminder that nothing in
# the pipeline assumes English.
BOTH_LIGHTS_PR = "Chas-Challenge-Team5_itsx25-infra_35"


def test_a_tightened_default_is_red_for_impact():
    judged, colour = judge_light(BOTH_LIGHTS_PR, "impact")

    assert colour == "red"
    assert len(judged) == 1
    assert judged[0].status == "ok"
    assert judged[0].finding["category"] == "default_behavior_change"
    assert judged[0].capped == rubric.HIGH
    assert "35.235.240.0/20" in judged[0].finding["evidence"]


def test_the_same_change_is_green_for_security():
    """A change that improves security is not a security finding.

    The model has to put it in dismissed rather than reporting a narrowed CIDR as an
    exposure. Getting this wrong in the other direction, flagging every hardening
    change as a problem, would make the security light useless noise."""
    judged, colour = judge_light(BOTH_LIGHTS_PR, "security")

    assert colour == "green"
    assert judged == []


def test_the_improvement_was_considered_and_dismissed_not_missed():
    """Green here has to mean "looked at it and it is fine", not "never noticed it".
    The dismissed list is what distinguishes the two, which is why it is in the schema."""
    _, _, answer = load(BOTH_LIGHTS_PR)
    dismissed = answer["security"]["dismissed"]

    assert len(dismissed) >= 1
    text = " ".join(d["item"] + " " + d["reason"] for d in dismissed)
    assert "0.0.0.0/0" in text


def test_default_behavior_change_is_not_capped():
    """Unlike forced_replacement and other, this category can carry a red on its own.
    A default that changes under existing users is checkable from the diff alone."""
    assert (
        rubric.capped_severity("default_behavior_change", "variables.tf", "high")
        == rubric.HIGH
    )
