"""Field order in the response schema is a decision, so it gets a test.

Constrained decoding emits fields in schema order. If someone reorders the dict, or
serialises it with sort_keys=True, the model starts labelling before it reasons and
summarising before it finds anything, and nothing else in the system would notice.
"""

import json

from reviewer import prompts, schema


def test_findings_come_before_summary():
    keys = list(schema.response_schema(prompts.IMPACT_CATEGORIES)["properties"])
    assert keys.index("findings") < keys.index("summary")


def test_explanation_comes_before_category_and_severity():
    finding = schema.response_schema(prompts.IMPACT_CATEGORIES)["properties"][
        "findings"
    ]["items"]["properties"]
    keys = list(finding)
    assert keys.index("explanation") < keys.index("category")
    assert keys.index("explanation") < keys.index("severity")


def test_serialising_preserves_order():
    """json.dumps must not be given sort_keys=True anywhere on this path."""
    body = json.dumps(schema.response_schema(prompts.IMPACT_CATEGORIES))
    assert body.index('"findings"') < body.index('"summary"')
    assert body.index('"explanation"') < body.index('"category"')


def test_sort_keys_would_break_the_finding_field_order():
    """States the failure mode explicitly, so the test above is not mistaken for a
    tautology about dict ordering.

    Only one of the two decisions is actually protected by being explicit here.
    Alphabetically "category" precedes "explanation", so sorting reverses that pair and
    the model would label before it reasons. "findings" happens to precede "summary"
    alphabetically as well, so that ordering would survive sorting by coincidence
    rather than by design. Worth knowing which of the two is load-bearing if the field
    names ever change."""
    body = json.dumps(schema.response_schema(prompts.IMPACT_CATEGORIES), sort_keys=True)
    assert body.index('"category"') < body.index('"explanation"')
    assert body.index('"findings"') < body.index('"summary"')


def test_every_field_is_required():
    s = schema.response_schema(prompts.SECURITY_CATEGORIES)
    assert set(s["required"]) == set(s["properties"])
    finding = s["properties"]["findings"]["items"]
    assert set(finding["required"]) == set(finding["properties"])


def test_categories_are_constrained_to_the_light():
    impact = schema.response_schema(prompts.IMPACT_CATEGORIES)
    enum = impact["properties"]["findings"]["items"]["properties"]["category"]["enum"]
    assert enum == prompts.IMPACT_CATEGORIES
    assert "network_exposure" not in enum
