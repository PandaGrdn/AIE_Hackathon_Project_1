"""Offline tests for core.llm.clients: no network, SDK objects are faked."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from core.llm import clients
from core.llm.clients import (
    AnthropicClient,
    GeminiClient,
    OpenAICompatClient,
    _with_retries,
    anthropic_messages,
    anthropic_tools,
    gemini_messages,
    gemini_tools,
    is_retryable,
    swarm_identities,
)
from core.models import AgentIdentity
from core.swarm.tools import AGENT_TOOLS

IDENT = AgentIdentity(id="t", provider="x", model="m", temperature=0.3)
MESSAGES = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "[log_step] {}"},
    {"role": "user", "content": "[log_step result]\nlogged"},
]


def _bare(cls, fake_sdk):
    c = object.__new__(cls)
    c.identity = IDENT
    c._client = fake_sdk
    return c


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(clients, "_sleep", lambda s: None)


# ---------- OpenAI-compatible ----------


def test_openai_compat_parses_tool_calls_and_usage():
    seen = {}

    def create(**kw):
        seen.update(kw)
        msg = NS(content=None, tool_calls=[NS(function=NS(name="log_step", arguments='{"assumption": "a", "action": "b"}'))])
        return NS(choices=[NS(message=msg)], usage=NS(prompt_tokens=11, completion_tokens=5))

    c = _bare(OpenAICompatClient, NS(chat=NS(completions=NS(create=create))))
    out = c.chat(MESSAGES, tools=AGENT_TOOLS)
    assert out["text"] == ""
    assert out["tool_calls"] == [{"name": "log_step", "arguments": {"assumption": "a", "action": "b"}}]
    assert out["usage"] == {"input_tokens": 11, "output_tokens": 5}
    assert seen["tools"] is AGENT_TOOLS and seen["temperature"] == 0.3


# ---------- Anthropic ----------


def test_anthropic_tool_conversion():
    out = anthropic_tools(AGENT_TOOLS)
    names = [t["name"] for t in out]
    assert "log_step" in names and "done" in names
    ls = next(t for t in out if t["name"] == "log_step")
    assert set(ls) == {"name", "description", "input_schema"}
    assert ls["input_schema"]["required"] == ["assumption", "action"]


def test_anthropic_message_alternation():
    system, convo = anthropic_messages(
        [
            {"role": "system", "content": "s1"},
            {"role": "system", "content": "s2"},
            {"role": "assistant", "content": "first"},
            {"role": "user", "content": "u1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a1"},
            {"role": "assistant", "content": "a2"},
        ]
    )
    assert system == "s1\n\ns2"
    assert [m["role"] for m in convo] == ["user", "assistant", "user", "assistant"]
    assert convo[0]["content"] == "(begin)"
    assert convo[2]["content"] == "u1\n\nu2" and convo[3]["content"] == "a1\n\na2"


def test_anthropic_chat_parses_tool_use():
    seen = {}

    def create(**kw):
        seen.update(kw)
        return NS(
            content=[NS(type="text", text="ok "), NS(type="tool_use", name="write_file", input={"path": "a.py", "content": "x"}, id="tu")],
            usage=NS(input_tokens=7, output_tokens=3),
        )

    c = _bare(AnthropicClient, NS(messages=NS(create=create)))
    out = c.chat(MESSAGES, tools=AGENT_TOOLS)
    assert out["text"] == "ok "
    assert out["tool_calls"] == [{"name": "write_file", "arguments": {"path": "a.py", "content": "x"}}]
    assert out["usage"] == {"input_tokens": 7, "output_tokens": 3}
    assert seen["system"] == "sys" and seen["max_tokens"] == 8000
    assert [m["role"] for m in seen["messages"]] == ["user", "assistant", "user"]
    assert seen["tools"][0]["input_schema"]["type"] == "object"


# ---------- Gemini ----------


def test_gemini_message_and_tool_conversion():
    system, contents = gemini_messages(MESSAGES)
    assert system == "sys"
    assert [c.role for c in contents] == ["user", "model", "user"]
    assert contents[1].parts[0].text == "[log_step] {}"
    tools = gemini_tools(AGENT_TOOLS)
    decls = tools[0].function_declarations
    by_name = {d.name: d for d in decls}
    assert set(by_name) == {t["function"]["name"] for t in AGENT_TOOLS}
    assert "assumption" in by_name["log_step"].parameters.properties
    empty = {"type": "function", "function": {"name": "noop", "description": "n", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}
    assert gemini_tools([empty])[0].function_declarations[0].parameters is None  # empty object schema dropped
    assert gemini_tools(None) is None


def test_gemini_chat_parses_function_calls():
    seen = {}

    def generate_content(**kw):
        seen.update(kw)
        parts = [NS(text="thinking", function_call=None, thought=False), NS(text=None, function_call=NS(name="log_step", args={"assumption": "a", "action": "b"}), thought=False)]
        return NS(candidates=[NS(content=NS(parts=parts))], usage_metadata=NS(prompt_token_count=20, candidates_token_count=4))

    c = _bare(GeminiClient, NS(models=NS(generate_content=generate_content)))
    out = c.chat(MESSAGES, tools=AGENT_TOOLS)
    assert out["text"] == "thinking"
    assert out["tool_calls"] == [{"name": "log_step", "arguments": {"assumption": "a", "action": "b"}}]
    assert out["usage"] == {"input_tokens": 20, "output_tokens": 4}
    cfg = seen["config"]
    assert cfg.system_instruction == "sys" and cfg.temperature == 0.3
    assert cfg.automatic_function_calling.disable is True
    assert len(cfg.tools[0].function_declarations) == len(AGENT_TOOLS)
    assert seen["model"] == "m" and [c.role for c in seen["contents"]] == ["user", "model", "user"]


# ---------- retries ----------


class Flaky(Exception):
    status_code = 503


def test_retry_then_success(monkeypatch):
    sleeps = []
    monkeypatch.setattr(clients, "_sleep", sleeps.append)
    n = {"calls": 0}

    def fn():
        n["calls"] += 1
        if n["calls"] < 3:
            raise Flaky("overloaded")
        return "ok"

    assert _with_retries(fn, attempts=4) == "ok"
    assert n["calls"] == 3 and len(sleeps) == 2
    assert 0.5 <= sleeps[0] < 1.0 and 1.0 <= sleeps[1] < 1.5


def test_retry_gives_up():
    n = {"calls": 0}

    def fn():
        n["calls"] += 1
        raise Flaky("still down")

    with pytest.raises(Flaky):
        _with_retries(fn, attempts=4)
    assert n["calls"] == 4


def test_non_retryable_raises_immediately():
    n = {"calls": 0}

    def fn():
        n["calls"] += 1
        raise ValueError("bad request")

    with pytest.raises(ValueError):
        _with_retries(fn, attempts=4)
    assert n["calls"] == 1


def test_is_retryable_recognises_sdk_errors():
    import anthropic
    import httpx
    import openai
    from google.genai import errors as gerr

    req = httpx.Request("POST", "https://x")
    resp429 = httpx.Response(429, request=req)
    resp400 = httpx.Response(400, request=req)
    assert is_retryable(openai.RateLimitError("rl", response=resp429, body=None))
    assert is_retryable(openai.APIConnectionError(request=req))
    assert is_retryable(anthropic.APITimeoutError(request=req))
    assert is_retryable(anthropic.InternalServerError("ise", response=httpx.Response(500, request=req), body=None))
    assert is_retryable(gerr.ClientError(429, {"error": {"message": "quota"}}))
    assert is_retryable(gerr.ServerError(503, {"error": {"message": "unavailable"}}))
    assert not is_retryable(openai.BadRequestError("bad", response=resp400, body=None))
    assert not is_retryable(gerr.ClientError(400, {"error": {"message": "bad"}}))
    assert not is_retryable(KeyError("x"))


# ---------- roster ----------


def test_roster_shape():
    roster = swarm_identities()
    assert len(roster) == 11  # 6 gemini (a Gemini-only swarm still fills 6 seats), 2 anthropic, 1 openai, 2 meta
    assert len({i.id for i in roster}) == 11
    assert sum(i.provider == "gemini" for i in roster) == 6
    assert sum(i.provider == "openai" for i in roster) == 1
    providers = {i.provider for i in roster}
    assert "subq" not in providers
    assert len(providers) >= 2
    assert sum(i.provider == "anthropic" for i in roster) == 2
    # SubQ Snap is a decisions API (no text), so text roles run on the configured text provider.
    assert clients.reflector_identity().provider != "subq"
    assert clients.reflector_identity().provider == clients.settings.text_provider


def test_provider_available(monkeypatch):
    monkeypatch.setattr(clients, "settings", NS(anthropic_api_key="k", gemini_api_key=""))
    assert clients.provider_available("anthropic")
    assert not clients.provider_available("gemini")
    assert not clients.provider_available("nope")
