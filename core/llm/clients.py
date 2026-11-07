"""Thin chat clients for every provider. Owner: B.

SubQ and Meta expose OpenAI-compatible endpoints, so they share one adapter.
Every client has the same `chat(messages, tools=None) -> str` surface so the
swarm runner and reflector don't care which provider is behind an identity.
"""

from __future__ import annotations

from typing import Any, Protocol

from core.config import settings
from core.models import AgentIdentity


class LLMClient(Protocol):
    identity: AgentIdentity

    def chat(self, messages: list[dict[str, Any]], tools: list[dict] | None = None) -> dict[str, Any]:
        """Return {"text": str, "tool_calls": [{"name": str, "arguments": dict}]}."""
        ...


class OpenAICompatClient:
    """Works for SubQ, Meta Model API and OpenAI itself."""

    def __init__(self, identity: AgentIdentity, api_key: str, base_url: str):
        from openai import OpenAI

        self.identity = identity
        self._client = OpenAI(api_key=api_key, base_url=base_url)

    def chat(self, messages, tools=None):
        resp = self._client.chat.completions.create(
            model=self.identity.model,
            messages=messages,
            temperature=self.identity.temperature,
            tools=tools or None,
        )
        msg = resp.choices[0].message
        calls = []
        for tc in msg.tool_calls or []:
            import json

            calls.append({"name": tc.function.name, "arguments": json.loads(tc.function.arguments or "{}")})
        return {"text": msg.content or "", "tool_calls": calls}


class GeminiClient:
    def __init__(self, identity: AgentIdentity, api_key: str):
        from google import genai

        self.identity = identity
        self._client = genai.Client(api_key=api_key)

    def chat(self, messages, tools=None):
        # TODO(B): map OpenAI-style messages/tools to genai contents + function declarations
        prompt = "\n\n".join(f"{m['role'].upper()}: {m['content']}" for m in messages)
        resp = self._client.models.generate_content(model=self.identity.model, contents=prompt)
        return {"text": resp.text or "", "tool_calls": []}


class AnthropicClient:
    def __init__(self, identity: AgentIdentity, api_key: str):
        import anthropic

        self.identity = identity
        self._client = anthropic.Anthropic(api_key=api_key)

    def chat(self, messages, tools=None):
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        convo = [m for m in messages if m["role"] != "system"]
        resp = self._client.messages.create(
            model=self.identity.model,
            max_tokens=8000,
            system=system or None,
            messages=convo,
            temperature=self.identity.temperature,
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        # TODO(B): surface tool_use blocks as tool_calls
        return {"text": text, "tool_calls": []}


def get_client(identity: AgentIdentity) -> LLMClient:
    p = identity.provider
    if p == "subq":
        return OpenAICompatClient(identity, settings.subq_api_key, settings.subq_base_url)
    if p == "meta":
        return OpenAICompatClient(identity, settings.meta_api_key, settings.meta_base_url)
    if p == "gemini":
        return GeminiClient(identity, settings.gemini_api_key)
    if p == "anthropic":
        return AnthropicClient(identity, settings.anthropic_api_key)
    raise ValueError(f"unknown provider {p}")


def reflector_identity() -> AgentIdentity:
    return AgentIdentity(id="reflector-subq", provider="subq", model=settings.subq_model, temperature=0.0)


def swarm_identities() -> list[AgentIdentity]:
    """The default swarm roster. Vary temperature/prompt so identities differ."""
    return [
        AgentIdentity(id="meta-code-t2", provider="meta", model=settings.meta_model_code, temperature=0.2),
        AgentIdentity(id="meta-spark-t5", provider="meta", model=settings.meta_model_spark, temperature=0.5),
        AgentIdentity(id="gemini-t2", provider="gemini", model=settings.gemini_model, temperature=0.2),
        AgentIdentity(id="gemini-t7-alt", provider="gemini", model=settings.gemini_model, temperature=0.7, prompt_variant="alt"),
        AgentIdentity(id="claude-t2", provider="anthropic", model=settings.anthropic_model, temperature=0.2),
        AgentIdentity(id="subq-t3", provider="subq", model=settings.subq_model, temperature=0.3),
    ]
