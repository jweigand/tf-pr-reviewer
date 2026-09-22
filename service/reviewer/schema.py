"""The JSON schema Ollama constrains the model's output with.

Field order is load-bearing. Constrained decoding emits fields in schema order, so the
order here is the order the model thinks in:

  findings before summary      a summary written first anchored a wrong conclusion in
                               the very first spike run, and the findings then bent to
                               agree with it
  explanation before category  the model reasons about what is wrong before it has to
                               pick a label, rather than picking a label and justifying
                               it afterwards

Python dicts preserve insertion order and json.dumps keeps it, so this works as long as
nobody serialises with sort_keys=True. That would silently undo both decisions, which is
why tests/test_schema.py asserts the order rather than trusting it.
"""


def response_schema(categories: list[str]) -> dict:
    """The schema for one light, parameterised by its category enum."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["findings", "dismissed", "confidence", "summary"],
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "file",
                        "evidence",
                        "explanation",
                        "category",
                        "severity",
                    ],
                    "properties": {
                        "file": {"type": "string"},
                        "evidence": {"type": "string"},
                        "explanation": {"type": "string"},
                        "category": {"type": "string", "enum": categories},
                        "severity": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                        },
                    },
                },
            },
            # Somewhere to put what the model considered and rejected, so it does not
            # pad findings to look useful.
            "dismissed": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["item", "reason"],
                    "properties": {
                        "item": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            },
            # An enum, not a number. A model asked for 0.0 to 1.0 produces confident
            # looking decimals it cannot justify.
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "summary": {"type": "string"},
        },
    }
