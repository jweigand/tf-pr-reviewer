"""The provider switch, and the Anthropic request shape.

No network. A stub is injected in place of the anthropic SDK, so these check what the
service sends and how it reads the reply, not whether Claude answers well.

What is NOT covered here: a real API call. That needs a key, and these tests are meant
to run on a machine that has neither a key nor a GPU.
"""

import json
import sys
import types

import pytest

from reviewer import config, model, prompts, schema

SCHEMA = schema.response_schema(prompts.IMPACT_CATEGORIES)
ANSWER = {"findings": [], "dismissed": [], "confidence": "high", "summary": "fine"}


class Block:
    def __init__(self, text):
        self.type, self.text = "text", text


class Usage:
    input_tokens, output_tokens = 1234, 56


class Response:
    def __init__(self, stop_reason="end_turn", text=None):
        self.stop_reason = stop_reason
        self.stop_details = None
        self.content = [Block(text if text is not None else json.dumps(ANSWER))]
        self.usage = Usage()


class Messages:
    """Captures the request instead of sending it."""

    def __init__(self, sent, response):
        self.sent, self.response = sent, response

    def create(self, **kwargs):
        self.sent.append(kwargs)
        return self.response


class FakeAnthropic:
    def __init__(self, sent, response):
        self.messages = Messages(sent, response)
        self.beta = types.SimpleNamespace(messages=Messages(sent, response))


@pytest.fixture
def anthropic_stub(monkeypatch):
    """Installs a fake `anthropic` module. model.py imports it lazily inside the
    function, which is what makes this substitution possible and is also why the
    default Ollama path never needs the SDK installed."""
    sent = []
    holder = {"response": Response()}

    def factory(**_kwargs):
        return FakeAnthropic(sent, holder["response"])

    monkeypatch.setitem(
        sys.modules, "anthropic", types.SimpleNamespace(Anthropic=factory)
    )
    return sent, holder


def cfg(**overrides):
    base = dict(
        provider="anthropic", anthropic_model="claude-opus-5",
        anthropic_max_tokens=16000, anthropic_effort="", anthropic_fallbacks=False,
        model_timeout=900.0,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


# ------------------------------------------------------------------ routing

def test_the_switch_sends_ollama_traffic_to_ollama(monkeypatch):
    """The default provider must not reach for the SDK at all."""
    called = []
    monkeypatch.setattr(model, "_ask_ollama", lambda *a: called.append(a) or "ollama")
    assert model.ask(cfg(provider="ollama"), "sys", "user", SCHEMA) == "ollama"
    assert len(called) == 1


def test_an_unknown_provider_falls_back_to_ollama(monkeypatch):
    """A typo in MODEL_PROVIDER should degrade to the default, not crash mid-review."""
    monkeypatch.setattr(model, "_ask_ollama", lambda *a: "ollama")
    assert model.ask(cfg(provider="typo"), "sys", "user", SCHEMA) == "ollama"


# ------------------------------------------------------------------ request shape

def test_the_schema_is_passed_through_unchanged(anthropic_stub):
    """The field ordering that makes the model reason before it labels has to survive
    the trip to a different provider. Same object, not a reimplementation."""
    sent, _ = anthropic_stub
    model.ask(cfg(), "sys", "user", SCHEMA)

    fmt = sent[0]["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"] is SCHEMA

    finding = fmt["schema"]["properties"]["findings"]["items"]["properties"]
    keys = list(finding)
    assert keys.index("explanation") < keys.index("category")


def test_sampling_parameters_are_never_sent(anthropic_stub):
    """Current Claude models reject temperature and presence_penalty with a 400. The
    Ollama path pins both, so the risk is someone copying those settings across."""
    sent, _ = anthropic_stub
    model.ask(cfg(), "sys", "user", SCHEMA)

    for forbidden in ("temperature", "presence_penalty", "top_p", "top_k", "think"):
        assert forbidden not in sent[0], forbidden


def test_the_prompt_is_split_into_system_and_user(anthropic_stub):
    sent, _ = anthropic_stub
    model.ask(cfg(), "SYSTEM TEXT", "USER TEXT", SCHEMA)

    assert sent[0]["system"] == "SYSTEM TEXT"
    assert sent[0]["messages"] == [{"role": "user", "content": "USER TEXT"}]


def test_effort_is_sent_only_when_set(anthropic_stub):
    sent, _ = anthropic_stub
    model.ask(cfg(), "sys", "user", SCHEMA)
    assert "effort" not in sent[0]["output_config"]

    model.ask(cfg(anthropic_effort="low"), "sys", "user", SCHEMA)
    assert sent[1]["output_config"]["effort"] == "low"


def test_fallbacks_are_opt_outable(anthropic_stub):
    """Enabled by default, but one env var turns it off without touching code, because
    it is the part of this path that cannot be verified without a key."""
    sent, _ = anthropic_stub
    model.ask(cfg(anthropic_fallbacks=True), "sys", "user", SCHEMA)
    assert sent[0]["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in sent[0]["betas"]

    model.ask(cfg(anthropic_fallbacks=False), "sys", "user", SCHEMA)
    assert "fallbacks" not in sent[1]


# ------------------------------------------------------------------ reading the reply

def test_a_good_reply_becomes_an_answer(anthropic_stub):
    _, holder = anthropic_stub
    answer = model.ask(cfg(), "sys", "user", SCHEMA)

    assert answer.ok
    assert answer.content == ANSWER
    assert answer.done_reason == "end_turn"
    assert answer.prompt_tokens == 1234
    assert answer.eval_tokens == 56
    assert answer.retried is False


def test_a_refusal_is_recorded_rather_than_raised(anthropic_stub):
    """A refusal is an HTTP 200 with nothing usable in it. Reading content blindly
    would raise; the review has to come back as unusable and carry the reason."""
    _, holder = anthropic_stub
    holder["response"] = Response(stop_reason="refusal", text="")

    answer = model.ask(cfg(), "sys", "user", SCHEMA)
    assert answer.ok is False
    assert answer.done_reason == "refusal"


def test_a_truncated_reply_is_not_usable(anthropic_stub):
    """max_tokens cut the JSON off mid-object: valid up to the cut, invalid after."""
    _, holder = anthropic_stub
    holder["response"] = Response(stop_reason="max_tokens", text='{"findings": [')

    answer = model.ask(cfg(), "sys", "user", SCHEMA)
    assert answer.ok is False
    assert answer.done_reason == "max_tokens"


# ------------------------------------------------------------------ config

def test_the_default_provider_is_ollama(monkeypatch):
    """A fresh clone must run with no account and no key."""
    for key in ("MODEL_PROVIDER", "OLLAMA_NUM_CTX"):
        monkeypatch.delenv(key, raising=False)
    assert config.from_env().provider == "ollama"


def test_the_context_budget_widens_for_the_hosted_provider(monkeypatch):
    """The 8192 ceiling exists because Ollama truncates silently. Carrying it over
    would skip pull requests Claude could review comfortably.

    OLLAMA_NUM_CTX is set here on purpose. docker-compose.yml always sets it, so the
    first version of this test, which deleted it, passed while the running service
    stayed pinned at 8192. The budget has to come from the active provider's own
    variable, not from whichever one happens to be in the environment."""
    monkeypatch.setenv("OLLAMA_NUM_CTX", "8192")
    monkeypatch.setenv("OLLAMA_NUM_PREDICT", "3000")
    monkeypatch.delenv("ANTHROPIC_CONTEXT_LIMIT", raising=False)

    monkeypatch.setenv("MODEL_PROVIDER", "ollama")
    assert config.from_env().num_ctx == 8192

    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    assert config.from_env().num_ctx > 100_000


def test_the_output_reserve_follows_the_provider_too(monkeypatch):
    """review.py subtracts num_predict from num_ctx before deciding too_large. On the
    hosted path that reserve is max_tokens, not Ollama's num_predict."""
    monkeypatch.setenv("OLLAMA_NUM_PREDICT", "3000")
    monkeypatch.setenv("ANTHROPIC_MAX_TOKENS", "16000")

    monkeypatch.setenv("MODEL_PROVIDER", "ollama")
    assert config.from_env().num_predict == 3000

    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    assert config.from_env().num_predict == 16000


def test_an_explicit_hosted_context_setting_still_wins(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_CONTEXT_LIMIT", "4096")
    assert config.from_env().num_ctx == 4096


def test_the_record_names_the_model_that_actually_answered(monkeypatch):
    """A review produced by Claude must not be recorded as qwen3.5:9b. The first real
    API call is what caught this."""
    monkeypatch.setenv("MODEL", "qwen3.5:9b")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-5")

    monkeypatch.setenv("MODEL_PROVIDER", "ollama")
    assert config.from_env().active_model == "qwen3.5:9b"

    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    assert config.from_env().active_model == "claude-opus-5"
