"""The prompts, copied verbatim from spike/phase0.sh (PROMPT_VERSION v3).

Not reworded during the port, on purpose. These exact strings are what scored 8 of 12
on the golden set and 12 of 14 on the held-out set. Changing a word here invalidates
those numbers, so any edit should come with a re-run of scripts/eval.py rather than a
feeling that the wording reads better.

Prompt changes must stay general. Wording aimed at a specific test pull request tunes
the evaluation set instead of the reviewer.

Changed since v3: version_constraint_change was added after fkc1e100/org-mono-repo#19
came back green while loosening "~> 5.0" to ">= 5.0" and "~> 7.43" to ">= 5.0". The model
had already spotted the change and talked itself out of it, so the miss was a missing
place to put the finding rather than a missing observation. Measured on the labelled set:
the new category was used 0 times on the six pull requests that do not change a
constraint, and twice on the one that does.
"""

SYSTEM = """You are reviewing a Terraform pull request. You only see the diff, not the whole repository.
Report only real problems the diff itself shows, at most 3. Each finding must name the file and quote one line copied exactly from that file in the diff as evidence.
Anything you considered but decided is not a problem goes in dismissed, with a one-line reason. An empty findings list is a normal, good answer.
Keep every explanation to one sentence. Set confidence to low if you would need files outside the diff to be sure."""

IMPACT_TASK = """TASK: assess IMPACT: could this change break or disrupt existing infrastructure, the code that depends on it, or how it behaves for existing users?
Categories:
- resource_removal: a resource or module block is deleted.
- rename_without_moved: a resource or module block is removed and a near-identical one is added under a different name, with no moved block.
- forced_replacement: an attribute change you believe makes Terraform destroy and recreate the resource. Name the attribute. Only a plan can confirm this.
- module_interface_change: a module input variable or output is removed, renamed, or retyped, or a module directory is renamed or moved, so existing callers break.
- output_change: an output of a root configuration is removed or retyped.
- lifecycle_change: lifecycle settings such as prevent_destroy or ignore_changes are removed or weakened.
- default_behavior_change: a default value or built-in setting changes, so existing users who never set it get different behavior after upgrading, even though their code still plans cleanly. Example: a variable default flips from true to false.
- version_constraint_change: a provider, module or required_version constraint is loosened, widened or raised, so a future terraform init can resolve a version that was previously excluded. Changing "~> 5.0" to ">= 5.0" removes the major-version ceiling. A lockfile pinning today's version does not make this safe, because the constraint is what governs the next upgrade.
- other: a real impact that fits none of the above.
Out of scope: security exposure (open CIDRs, IAM, encryption, secrets) is assessed separately. Do not report it here.
Not impact: moving a block within a file; changing how a value is referenced when the value stays the same; changes under examples/ only affect the examples."""

IMPACT_CATEGORIES = [
    "resource_removal",
    "rename_without_moved",
    "forced_replacement",
    "module_interface_change",
    "output_change",
    "lifecycle_change",
    "default_behavior_change",
    "version_constraint_change",
    "other",
]

SECURITY_TASK = """TASK: assess SECURITY. Look for: network exposure (ingress from 0.0.0.0/0 or ::/0, public access enabled, public IPs);
IAM or role grants broader than needed (wildcards, admin roles); encryption disabled or weakened; secrets or passwords written as literal values;
logging or auditing disabled. Judge severity by context: a literal password under examples/ is lower severity than in production code;
0.0.0.0/0 on port 443 of a load balancer is lower than on a database port. A change that improves security is not a finding; put it in dismissed."""

SECURITY_CATEGORIES = [
    "network_exposure",
    "iam_privilege",
    "encryption",
    "secret_exposure",
    "logging_or_audit",
    "other",
]

# Impact first: it is the light the rubric is most likely to turn red, and running it
# first means a timeout costs the cheaper answer rather than the more valuable one.
LIGHTS = (
    ("impact", IMPACT_TASK, IMPACT_CATEGORIES),
    ("security", SECURITY_TASK, SECURITY_CATEGORIES),
)


def user_message(title: str, body: str | None, tf_diff: str, task: str) -> str:
    """Diff first, task last.

    Every light for one pull request shares an identical prefix up to the task, so
    Ollama reuses the processed prompt for the second call. Measured in the spike:
    prompt evaluation dropped from 13.1s to 0.7s. Putting the task first would cost
    that on every light after the first.
    """
    return (
        f"Pull request title: {title}\n"
        f"Pull request description: {body or '(no description)'}\n\n"
        f"Diff (.tf files only):\n{tf_diff}\n\n"
        f"{task}"
    )
