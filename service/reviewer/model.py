"""Calling a model, and deciding whether what came back is usable.

Two providers behind one function. MODEL_PROVIDER picks between them; everything
downstream, verification and the rubric, sees the same Answer either way and does not
know or care which one produced it.

  ollama     the default. Local, free, no account. Constrained decoding via `format`.
  anthropic  hosted. Needs ANTHROPIC_API_KEY. Constrained decoding via
             output_config.format, which takes the same JSON schema unchanged.

The two are not parameterisations of each other, which is why the settings are separate
rather than shared: temperature and presence_penalty do not exist on current Claude
models and are rejected outright, and the context window differs by three orders of
magnitude.

On the Ollama path every option is set explicitly on every call. Its defaults are not
stable across models, and two of them actively worked against this task: qwen3.5:9b
thought by default despite published reviews saying otherwise, and it shipped with
presence_penalty 1.5, which penalises repeating tokens and so fights the one thing the
prompt demands, quoting a line of the diff back exactly as evidence.
"""

import json
import logging
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger("reviewer.model")


@dataclass
class Answer:
    """One light's worth of model output, and how it was produced."""

    content: dict | None  # parsed response, or None if nothing usable came back
    seconds: float
    done_reason: str | None  # "length" means the output was cut off mid-JSON
    retried: bool
    prompt_tokens: int | None
    eval_tokens: int | None

    @property
    def ok(self) -> bool:
        return self.content is not None


def _usable(content: str) -> dict | None:
    """Parse the response, and require the keys the rubric depends on.

    Constrained decoding makes malformed JSON rare but not impossible: a response cut
    off by num_predict is valid up to the truncation and invalid after it.
    """
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    if not all(k in parsed for k in ("findings", "dismissed", "confidence")):
        return None
    return parsed


def _request_body(cfg, system: str, user: str, response_schema: dict, think: str) -> dict:
    body = {
        "model": cfg.model,
        "stream": False,
        # Keeps the model resident between the two lights of one pull request, and
        # between pull requests. A reload costs 10 to 15 seconds. It also holds about
        # 6GB of wired memory for that long, which is the real price of this setting.
        "keep_alive": cfg.keep_alive,
        "format": response_schema,
        "options": {
            "num_ctx": cfg.num_ctx,
            "num_predict": cfg.num_predict,
            "temperature": cfg.temperature,
            "presence_penalty": cfg.presence_penalty,
        },
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    # "none" sends nothing, for models with no thinking support. Sending think to a
    # model that does not understand it is an error, not a no-op.
    if think in ("true", "false"):
        body["think"] = think == "true"
    return body


def _post(cfg, body: dict) -> tuple[dict, float]:
    with httpx.Client(timeout=cfg.model_timeout) as client:
        response = client.post(f"{cfg.ollama_url}/api/chat", json=body)
        response.raise_for_status()
    payload = response.json()
    return payload, payload.get("total_duration", 0) / 1e9


def ask(cfg, system: str, user: str, response_schema: dict) -> Answer:
    """One light, from whichever provider is configured."""
    if cfg.provider == "anthropic":
        return _ask_anthropic(cfg, system, user, response_schema)
    return _ask_ollama(cfg, system, user, response_schema)


def _ask_anthropic(cfg, system: str, user: str, response_schema: dict) -> Answer:
    """One light from Claude.

    The same schema object the Ollama path uses is passed straight through to
    output_config.format, so the field ordering that makes the model reason before it
    labels is preserved across both providers rather than reimplemented for each.

    Note what is absent: no temperature, no presence_penalty. Current Claude models
    reject both with a 400. Determinism therefore comes from the schema and the prompt
    rather than from a sampling setting, which is worth knowing when comparing a run
    here against a run on Ollama.
    """
    import anthropic  # imported lazily so the default path needs no API dependency

    client = anthropic.Anthropic(timeout=cfg.model_timeout)

    request = {
        "model": cfg.anthropic_model,
        "max_tokens": cfg.anthropic_max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": {
            "format": {"type": "json_schema", "schema": response_schema}
        },
    }
    if cfg.anthropic_effort:
        request["output_config"]["effort"] = cfg.anthropic_effort

    started = time.monotonic()
    if cfg.anthropic_fallbacks:
        # Safety classifiers can decline a request outright. Server-side fallbacks route
        # such a request to another model instead of returning nothing. Set
        # ANTHROPIC_FALLBACKS=false to send the plain request if this ever errors.
        response = client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **request
        )
    else:
        response = client.messages.create(**request)
    seconds = time.monotonic() - started

    content = None
    if response.stop_reason == "refusal":
        # Always checked before reading content: a refusal is an HTTP 200 with nothing
        # usable in it, and reading blindly would raise instead of recording the reason.
        log.warning("model declined the request: %s", response.stop_details)
    else:
        text = next((b.text for b in response.content if b.type == "text"), "")
        content = _usable(text)

    return Answer(
        content=content,
        seconds=round(seconds, 2),
        done_reason=response.stop_reason,
        retried=False,  # the SDK retries transport errors itself
        prompt_tokens=response.usage.input_tokens,
        eval_tokens=response.usage.output_tokens,
    )


def _ask_ollama(cfg, system: str, user: str, response_schema: dict) -> Answer:
    """One light. Retries once with thinking off if a thinking run returns nothing.

    Thinking is off by default. On the golden set it scored 8 of 12 in 4.5 minutes with
    thinking off against 7 of 12 in 22.5 minutes with it on, and twice the thinking used
    the entire output budget and returned no JSON at all. That failure is what this
    retry exists for, so it only fires when thinking was on to begin with.
    """
    payload, seconds = _post(cfg, _request_body(cfg, system, user, response_schema, cfg.think))
    content = _usable(payload.get("message", {}).get("content", ""))
    retried = False

    if content is None and cfg.think != "false":
        log.warning(
            "no usable JSON (done_reason=%s), retrying once with thinking off",
            payload.get("done_reason"),
        )
        retry, retry_seconds = _post(
            cfg, _request_body(cfg, system, user, response_schema, "false")
        )
        seconds += retry_seconds
        payload = retry
        content = _usable(payload.get("message", {}).get("content", ""))
        retried = True

    return Answer(
        content=content,
        seconds=round(seconds, 2),
        done_reason=payload.get("done_reason"),
        retried=retried,
        prompt_tokens=payload.get("prompt_eval_count"),
        eval_tokens=payload.get("eval_count"),
    )
