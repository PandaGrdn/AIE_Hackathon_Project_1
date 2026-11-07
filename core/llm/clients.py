"""Thin chat clients for every provider. Owner: Lucas.

Meta exposes an OpenAI-compatible endpoint, so it uses the OpenAI-compatible adapter.
SubQ has no client here: it is a decision model reached only through the Decisions API in
`core/harness/subq_judge.py`, and it is never a swarm agent.
Every client has the same `chat(messages, tools=None) -> dict` surface so the
swarm runner and reflector don't care which provider is behind an identity.

Messages arrive OpenAI-style (roles system/user/assistant, string content);
tools arrive as OpenAI function-tool schemas (see core/swarm/tools.py).
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from typing import Any, Protocol

from core.config import settings
from core.models import AgentIdentity

ChatResponse = dict[str, Any]  # {"text", "tool_calls": [{"name", "arguments"}], "usage": {...}}


class LLMClient(Protocol):
    identity: AgentIdentity

    def chat(self, messages: list[dict[str, Any]], tools: list[dict] | None = None) -> ChatResponse:
        """Return {"text": str, "tool_calls": [{"name", "arguments"}], "usage": {"input_tokens", "output_tokens"}}."""
        ...


# ---------- retries ----------

RETRY_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504, 529}
_sleep: Callable[[float], None] = time.sleep  # monkeypatched in tests


def _status_of(exc: BaseException) -> int | None:
    for attr in ("status_code", "code", "status"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    resp = getattr(exc, "response", None)
    v = getattr(resp, "status_code", None)
    return v if isinstance(v, int) else None


def is_retryable(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name in {"APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError", "OverloadedError", "ServerError"}:
        return True
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return _status_of(exc) in RETRY_STATUSES


def _with_retries[T](fn: Callable[[], T], attempts: int = 4, base: float = 0.5) -> T:
    """Call `fn`; on a retryable error back off 0.5s, 1s, 2s, ... (+jitter) and try again."""
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            if i == attempts - 1 or not is_retryable(e):
                raise
            _sleep(base * 2**i + random.uniform(0, 0.25))
    raise RuntimeError("unreachable")


def _usage(inp: Any, out: Any) -> dict[str, int]:
    return {"input_tokens": int(inp or 0), "output_tokens": int(out or 0)}


# ---------- OpenAI-compatible ----------


class OpenAICompatClient:
    """Works for the Meta Model API and OpenAI itself."""

    def __init__(self, identity: AgentIdentity, api_key: str, base_url: str):
        from openai import OpenAI

        self.identity = identity
        self._client = OpenAI(api_key=api_key, base_url=base_url)

    def chat(self, messages, tools=None):
        resp = _with_retries(
            lambda: self._client.chat.completions.create(
                model=self.identity.model,
                messages=messages,
                temperature=self.identity.temperature,
                tools=tools or None,
            )
        )
        msg = resp.choices[0].message
        calls = []
        for tc in msg.tool_calls or []:
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            try:
                args = json.loads(fn.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw": fn.arguments}
            calls.append({"name": fn.name, "arguments": args if isinstance(args, dict) else {"_raw": args}})
        u = getattr(resp, "usage", None)
        return {
            "text": msg.content or "",
            "tool_calls": calls,
            "usage": _usage(getattr(u, "prompt_tokens", 0), getattr(u, "completion_tokens", 0)),
        }


# ---------- Gemini ----------

_GEMINI_SCHEMA_KEYS = {
    "type", "description", "properties", "required", "items", "enum", "format", "nullable",
    "minimum", "maximum", "minItems", "maxItems", "anyOf", "title",
}


def _gemini_schema(schema: dict | None) -> dict | None:
    """Drop JSON-schema keys the Gemini Schema type rejects; None for empty object schemas."""
    if not schema:
        return None
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k not in _GEMINI_SCHEMA_KEYS:
            continue
        if k == "properties" and isinstance(v, dict):
            v = {pk: _gemini_schema(pv) or {"type": "string"} for pk, pv in v.items()}
        elif k == "items" and isinstance(v, dict):
            v = _gemini_schema(v) or {"type": "string"}
        elif k == "anyOf" and isinstance(v, list):
            v = [_gemini_schema(x) or {"type": "string"} for x in v]
        out[k] = v
    if out.get("type") == "object" and not out.get("properties"):
        return None
    return out


def gemini_messages(messages: list[dict[str, Any]]) -> tuple[str | None, list]:
    from google.genai import types

    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    contents = [
        types.Content(role="model" if m["role"] == "assistant" else "user", parts=[types.Part.from_text(text=str(m["content"]))])
        for m in messages
        if m["role"] != "system"
    ]
    return (system or None), contents


def gemini_tools(tools: list[dict] | None) -> list | None:
    from google.genai import types

    if not tools:
        return None
    decls = []
    for t in tools:
        fn = t.get("function", t)
        kwargs: dict[str, Any] = {"name": fn["name"], "description": fn.get("description", "")}
        params = _gemini_schema(fn.get("parameters"))
        if params:
            kwargs["parameters"] = params
        decls.append(types.FunctionDeclaration(**kwargs))
    return [types.Tool(function_declarations=decls)]


class GeminiClient:
    def __init__(self, identity: AgentIdentity, api_key: str):
        from google import genai

        self.identity = identity
        self._client = genai.Client(api_key=api_key)

    def chat(self, messages, tools=None):
        from google.genai import types

        system, contents = gemini_messages(messages)
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=self.identity.temperature,
            tools=gemini_tools(tools),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        resp = _with_retries(
            lambda: self._client.models.generate_content(model=self.identity.model, contents=contents, config=config)
        )
        return parse_gemini_response(resp)


def parse_gemini_response(resp: Any) -> ChatResponse:
    text, calls = [], []
    for cand in getattr(resp, "candidates", None) or []:
        content = getattr(cand, "content", None)
        for part in (getattr(content, "parts", None) or []):
            fc = getattr(part, "function_call", None)
            if fc is not None and getattr(fc, "name", None):
                calls.append({"name": fc.name, "arguments": dict(fc.args or {})})
            elif getattr(part, "text", None) and not getattr(part, "thought", False):
                text.append(part.text)
    um = getattr(resp, "usage_metadata", None)
    return {
        "text": "".join(text),
        "tool_calls": calls,
        "usage": _usage(getattr(um, "prompt_token_count", 0), getattr(um, "candidates_token_count", 0)),
    }


# ---------- Anthropic ----------


def anthropic_tools(tools: list[dict] | None) -> list[dict] | None:
    if not tools:
        return None
    out = []
    for t in tools:
        fn = t.get("function", t)
        out.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def anthropic_messages(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, str]]]:
    """Split out system text and force the strict user/assistant alternation Anthropic requires."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    convo: list[dict[str, str]] = []
    for m in messages:
        if m["role"] == "system":
            continue
        role = "assistant" if m["role"] == "assistant" else "user"
        content = str(m["content"])
        if convo and convo[-1]["role"] == role:
            convo[-1]["content"] += "\n\n" + content
        else:
            convo.append({"role": role, "content": content})
    if not convo or convo[0]["role"] != "user":
        convo.insert(0, {"role": "user", "content": "(begin)"})
    return (system or None), convo


class AnthropicClient:
    def __init__(self, identity: AgentIdentity, api_key: str):
        import anthropic

        self.identity = identity
        self._client = anthropic.Anthropic(api_key=api_key)

    def chat(self, messages, tools=None):
        system, convo = anthropic_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self.identity.model,
            "max_tokens": 8000,
            "messages": convo,
            "temperature": self.identity.temperature,
        }
        if system:
            kwargs["system"] = system
        atools = anthropic_tools(tools)
        if atools:
            kwargs["tools"] = atools
        resp = _with_retries(lambda: self._client.messages.create(**kwargs))
        return parse_anthropic_response(resp)


def parse_anthropic_response(resp: Any) -> ChatResponse:
    text, calls = [], []
    for b in getattr(resp, "content", None) or []:
        kind = getattr(b, "type", "")
        if kind == "text":
            text.append(b.text)
        elif kind == "tool_use":
            inp = b.input
            calls.append({"name": b.name, "arguments": dict(inp) if isinstance(inp, dict) else {"_raw": inp}})
    u = getattr(resp, "usage", None)
    return {
        "text": "".join(text),
        "tool_calls": calls,
        "usage": _usage(getattr(u, "input_tokens", 0), getattr(u, "output_tokens", 0)),
    }


# ---------- factory + roster ----------

_PROVIDER_KEYS = {
    "meta": "meta_api_key",
    "gemini": "gemini_api_key",
    "anthropic": "anthropic_api_key",
}


def provider_available(provider: str) -> bool:
    """True when the API key for `provider` is set in settings."""
    attr = _PROVIDER_KEYS.get(provider)
    return bool(attr and getattr(settings, attr, ""))


def get_client(identity: AgentIdentity) -> LLMClient:
    p = identity.provider
    if p == "meta":
        return OpenAICompatClient(identity, settings.meta_api_key, settings.meta_base_url)
    if p == "gemini":
        return GeminiClient(identity, settings.gemini_api_key)
    if p == "anthropic":
        return AnthropicClient(identity, settings.anthropic_api_key)
    raise ValueError(f"unknown provider {p}")


def reflector_identity() -> AgentIdentity:
    """Text-generating roles (decomposer, spec-test author, reflector lesson text).

    SubQ Snap cannot generate text (Decisions API only, see core/llm/decisions.py), so these run on
    the configured text provider (Gemini by default).
    """
    if settings.text_provider == "gemini":
        return AgentIdentity(id="text-gemini", provider="gemini", model=settings.gemini_model, temperature=0.0)
    if settings.text_provider == "anthropic":
        return AgentIdentity(id="text-anthropic", provider="anthropic", model=settings.anthropic_model, temperature=0.0)
    return AgentIdentity(id="text-meta", provider="meta", model=settings.meta_model_spark, temperature=0.0)


def swarm_identities() -> list[AgentIdentity]:
    """The default swarm roster (6: 2 meta, 2 gemini, 2 anthropic). Vary temperature/prompt so identities differ.

    SubQ is deliberately absent: it is the verdict judge (Decisions API, no chat endpoint), and a
    judge must not also implement. Gemini writes the spec tests and lessons.
    The full roster is returned regardless of configured keys; the router filters with
    `provider_available`.
    """
    return [
        AgentIdentity(id="meta-code-t2", provider="meta", model=settings.meta_model_code, temperature=0.2),
        AgentIdentity(id="meta-spark-t5", provider="meta", model=settings.meta_model_spark, temperature=0.5),
        AgentIdentity(id="gemini-t2", provider="gemini", model=settings.gemini_model, temperature=0.2),
        AgentIdentity(id="gemini-t7-alt", provider="gemini", model=settings.gemini_model, temperature=0.7, prompt_variant="alt"),
        AgentIdentity(id="claude-t2", provider="anthropic", model=settings.anthropic_model, temperature=0.2),
        AgentIdentity(id="claude-t7-alt", provider="anthropic", model=settings.anthropic_model, temperature=0.7, prompt_variant="alt"),
    ]
