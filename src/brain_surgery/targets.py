"""Model targets: where a routed prompt actually runs.

Two kinds:

* ``openai``    -- any OpenAI-compatible chat endpoint (LM Studio, vLLM, llama.cpp,
                   LiteLLM, Ollama's /v1). Plain HTTP, no dependency.
* ``anthropic`` -- the Claude API through the official ``anthropic`` SDK.

Each call ends one of three ways:

* a :class:`Completion`;
* :class:`TargetUnavailable` -- the target could not be reached or served the call
  (connection error, rate limit, 5xx). The router may try the route's next target;
* :class:`TargetRefused` -- the model declined. That is final for this request: the
  router holds it for review and does not try other targets.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

DATA_TIERS = ("local-only", "cloud-allowed")

# Models that accept the server-side ``fallbacks: "default"`` parameter on the Claude API.
# On a decline the API re-runs the request on the model Anthropic designates for that
# refusal category; the router only sees a refusal if that whole chain declined.
SERVER_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class Completion:
    target: str
    model: str
    text: str
    stop_reason: str | None
    served_by: str | None = None      # differs from the requested model after a server-side fallback
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


class TargetUnavailable(Exception):
    pass


class TargetRefused(Exception):
    def __init__(self, target: str, model: str, category: str | None, detail: str = "") -> None:
        self.target, self.model, self.category, self.detail = target, model, category, detail
        super().__init__(f"{target} ({model}) declined" + (f": {category}" if category else ""))


class Target(Protocol):
    name: str
    model: str
    data_tier: str

    def complete(self, prompt: str, *, system: str | None = None,
                 max_tokens: int | None = None) -> Completion: ...


class OpenAICompatTarget:
    def __init__(self, name: str, base_url: str, model: str, *, data_tier: str = "local-only",
                 api_key_env: str | None = None, timeout_s: float = 300.0,
                 max_tokens: int = 4096, params: Mapping[str, Any] | None = None) -> None:
        self.name, self.model, self.data_tier = name, model, data_tier
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key_env = api_key_env
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        self.params = dict(params or {})

    def complete(self, prompt: str, *, system: str | None = None,
                 max_tokens: int | None = None) -> Completion:
        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": prompt}]
        body = {"model": self.model, "messages": messages,
                "max_tokens": max_tokens or self.max_tokens, **self.params}
        headers = {"Content-Type": "application/json"}
        if self.api_key_env and os.environ.get(self.api_key_env):
            headers["Authorization"] = f"Bearer {os.environ[self.api_key_env]}"
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise TargetUnavailable(f"{self.name}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TargetUnavailable(f"{self.name}: {exc}") from exc
        except ValueError as exc:
            raise TargetUnavailable(f"{self.name}: malformed JSON") from exc
        try:
            choice = data["choices"][0]
            finish = choice.get("finish_reason")
            text = (choice.get("message") or {}).get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise TargetUnavailable(f"{self.name}: reply has no choices") from exc
        if finish == "content_filter":
            raise TargetRefused(self.name, self.model, "content_filter")
        return Completion(target=self.name, model=self.model, text=text, stop_reason=finish,
                          served_by=data.get("model") or self.model, raw=data)


class AnthropicTarget:
    """Claude API target. ``client`` is injectable for tests; by default an
    ``anthropic.Anthropic`` client is built, reading the key from ``api_key_env``
    when given, else the SDK's normal credential chain."""

    def __init__(self, name: str, model: str, *, data_tier: str = "cloud-allowed",
                 api_key_env: str | None = None, server_fallback: str | None = None,
                 max_tokens: int = 16000, params: Mapping[str, Any] | None = None,
                 client: Any = None) -> None:
        self.name, self.model, self.data_tier = name, model, data_tier
        self.api_key_env = api_key_env
        if server_fallback is None:
            server_fallback = "default" if model in SERVER_FALLBACK_MODELS else "off"
        if server_fallback not in ("default", "off"):
            raise ValueError(f"target {name!r}: server_fallback must be 'default' or 'off'")
        self.server_fallback = server_fallback
        self.max_tokens = max_tokens
        self.params = dict(params or {})
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic  # optional dependency: pip install brain-surgery[anthropic]

            key = os.environ.get(self.api_key_env) if self.api_key_env else None
            self._client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        return self._client

    def request_kwargs(self, prompt: str, system: str | None, max_tokens: int | None) -> dict[str, Any]:
        kw: dict[str, Any] = {"model": self.model, "max_tokens": max_tokens or self.max_tokens,
                              "messages": [{"role": "user", "content": prompt}], **self.params}
        if system:
            kw["system"] = system
        if self.server_fallback == "default":
            kw["betas"] = [SERVER_FALLBACK_BETA]
            kw["fallbacks"] = "default"
        return kw

    def complete(self, prompt: str, *, system: str | None = None,
                 max_tokens: int | None = None) -> Completion:
        client = self._get_client()
        kw = self.request_kwargs(prompt, system, max_tokens)
        try:
            if "betas" in kw:
                resp = client.beta.messages.create(**kw)
            else:
                resp = client.messages.create(**kw)
        except Exception as exc:  # noqa: BLE001 - classified below
            raise _anthropic_unavailable(self.name, exc) from exc
        if getattr(resp, "stop_reason", None) == "refusal":
            details = getattr(resp, "stop_details", None)
            raise TargetRefused(self.name, self.model, getattr(details, "category", None),
                                getattr(details, "explanation", "") or "")
        text = "".join(getattr(b, "text", "") for b in (resp.content or [])
                       if getattr(b, "type", None) == "text")
        return Completion(target=self.name, model=self.model, text=text,
                          stop_reason=resp.stop_reason, served_by=getattr(resp, "model", None))


def _anthropic_unavailable(name: str, exc: Exception) -> TargetUnavailable:
    """Name the failure precisely; every API/transport error means 'try elsewhere'."""
    try:
        import anthropic
    except ImportError:
        return TargetUnavailable(f"{name}: {type(exc).__name__}: {exc}")
    if isinstance(exc, anthropic.RateLimitError):
        return TargetUnavailable(f"{name}: rate limited")
    if isinstance(exc, anthropic.AuthenticationError):
        return TargetUnavailable(f"{name}: authentication failed (check the key / workspace)")
    if isinstance(exc, anthropic.PermissionDeniedError):
        return TargetUnavailable(f"{name}: key lacks permission for this model")
    if isinstance(exc, anthropic.NotFoundError):
        return TargetUnavailable(f"{name}: model or endpoint not found")
    if isinstance(exc, anthropic.APIStatusError):
        return TargetUnavailable(f"{name}: HTTP {exc.status_code}")
    if isinstance(exc, anthropic.APIConnectionError):
        return TargetUnavailable(f"{name}: connection error")
    return TargetUnavailable(f"{name}: {type(exc).__name__}: {exc}")


def build_target(name: str, cfg: Mapping[str, Any]) -> Target:
    kind = cfg.get("kind")
    tier = cfg.get("data_tier", "local-only" if kind == "openai" else "cloud-allowed")
    if tier not in DATA_TIERS:
        raise ValueError(f"target {name!r}: data_tier must be one of {DATA_TIERS}")
    if kind == "openai":
        return OpenAICompatTarget(name, cfg["base_url"], cfg["model"], data_tier=tier,
                                  api_key_env=cfg.get("api_key_env"),
                                  timeout_s=float(cfg.get("timeout_s", 300)),
                                  max_tokens=int(cfg.get("max_tokens", 4096)),
                                  params=cfg.get("params"))
    if kind == "anthropic":
        if tier != "cloud-allowed":
            raise ValueError(f"target {name!r}: an anthropic target is cloud; data_tier must be cloud-allowed")
        return AnthropicTarget(name, cfg["model"], data_tier=tier,
                               api_key_env=cfg.get("api_key_env"),
                               server_fallback=cfg.get("server_fallback"),
                               max_tokens=int(cfg.get("max_tokens", 16000)),
                               params=cfg.get("params"))
    raise ValueError(f"target {name!r}: unknown kind {kind!r} (openai | anthropic)")
