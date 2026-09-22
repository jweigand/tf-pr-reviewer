"""Every setting the service has, read from the environment once at startup.

Kept in one place so there is a single answer to "what is this configured with", and
read once so a running review cannot change behaviour halfway through.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    brokers: str
    candidates_topic: str
    reviews_topic: str
    consumer_group: str

    # Enrichment: where the PR detail and diff come from.
    ingest_mode: str
    fixtures_dir: str
    github_token: str

    # Read by the service only to ask whether a commit already has a verdict. Connect
    # still does all the writing.
    postgres_dsn: str

    # Context budget. Ollama truncates a prompt longer than num_ctx without saying so,
    # so the service checks before calling rather than trusting the model to complain.
    num_ctx: int
    num_predict: int

    # Which provider answers the two model calls. "ollama" is the default so a fresh
    # clone runs with no account and no key. "anthropic" is the hosted alternative.
    provider: str

    # The model. Every one of these is pinned on every call; see model.py for why.
    ollama_url: str
    model: str
    think: str  # "true" | "false" | "none" (none sends nothing)
    temperature: float
    presence_penalty: float
    keep_alive: str
    model_timeout: float

    # Anthropic. Deliberately separate from the Ollama settings rather than shared:
    # temperature and presence_penalty do not exist on this path (current models reject
    # them), and the context window is three orders of magnitude larger.
    anthropic_model: str
    anthropic_max_tokens: int
    anthropic_effort: str
    anthropic_fallbacks: bool

    @property
    def active_model(self) -> str:
        """The model that will actually answer, whichever provider is configured.

        The record stores this, so a row says what produced it rather than whatever the
        unused provider happens to be set to.
        """
        return self.anthropic_model if self.provider == "anthropic" else self.model

    def describe(self) -> str:
        return (
            f"brokers={self.brokers} "
            f"consume={self.candidates_topic} "
            f"produce={self.reviews_topic} "
            f"group={self.consumer_group} "
            f"mode={self.ingest_mode} "
            f"provider={self.provider} "
            f"model={self.active_model} "
            f"num_ctx={self.num_ctx} num_predict={self.num_predict} "
            f"think={self.think}"
        )


def from_env() -> Config:
    provider = os.environ.get("MODEL_PROVIDER", "ollama").strip().lower()
    anthropic_max_tokens = int(os.environ.get("ANTHROPIC_MAX_TOKENS", "16000"))

    # num_ctx and num_predict are the *effective* budget for whichever provider is
    # active, read from that provider's own variables. They are deliberately not
    # shared: the OLLAMA_ ones are always set by docker-compose.yml, so reading them
    # on the hosted path would silently pin Claude to an 8192-token window and skip
    # pull requests it could review comfortably.
    if provider == "anthropic":
        num_ctx = int(os.environ.get("ANTHROPIC_CONTEXT_LIMIT", "200000"))
        num_predict = anthropic_max_tokens
    else:
        num_ctx = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))
        num_predict = int(os.environ.get("OLLAMA_NUM_PREDICT", "3000"))

    return Config(
        provider=provider,
        brokers=os.environ.get("KAFKA_BROKERS", "redpanda:9092"),
        candidates_topic=os.environ.get("CANDIDATES_TOPIC", "pr.candidates"),
        reviews_topic=os.environ.get("REVIEWS_TOPIC", "pr.reviews"),
        consumer_group=os.environ.get("CONSUMER_GROUP", "tf-pr-reviewer"),
        ingest_mode=os.environ.get("INGEST_MODE", "replay"),
        fixtures_dir=os.environ.get("FIXTURES_DIR", "/fixtures"),
        github_token=os.environ.get("GITHUB_TOKEN", ""),
        postgres_dsn=os.environ.get(
            "POSTGRES_DSN",
            "postgres://{u}:{p}@postgres:5432/{d}".format(
                u=os.environ.get("POSTGRES_USER", "tfpr"),
                p=os.environ.get("POSTGRES_PASSWORD", "tfpr"),
                d=os.environ.get("POSTGRES_DB", "tfpr"),
            ),
        ),
        num_ctx=num_ctx,
        num_predict=num_predict,
        ollama_url=os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434").rstrip("/"),
        model=os.environ.get("MODEL", "qwen3.5:9b"),
        think=os.environ.get("OLLAMA_THINK", "false"),
        temperature=float(os.environ.get("OLLAMA_TEMPERATURE", "0")),
        presence_penalty=float(os.environ.get("OLLAMA_PRESENCE_PENALTY", "0")),
        keep_alive=os.environ.get("OLLAMA_KEEP_ALIVE", "30m"),
        model_timeout=float(os.environ.get("MODEL_TIMEOUT_SECONDS", "900")),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-5"),
        anthropic_max_tokens=anthropic_max_tokens,
        anthropic_effort=os.environ.get("ANTHROPIC_EFFORT", "").strip().lower(),
        anthropic_fallbacks=os.environ.get("ANTHROPIC_FALLBACKS", "true").lower()
        not in ("0", "false", "no"),
    )
