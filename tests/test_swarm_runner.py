"""Swarm runner tests: scripted client, in-memory sandbox, no network."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from core.harness.spec_tests import SpecTests
from core.llm.embeddings import fake_embed
from core.models import AgentIdentity, FunctionSig, Outcome, Subtask, TestCase
from core.store.memory import MemoryStore
from core.swarm import runner


class FakeClient:
    def __init__(self, responses: list[dict], usage: dict | None = None):
        self.responses = list(responses)
        self.usage = usage
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        if not self.responses:
            return {"text": "", "tool_calls": [{"name": "done", "arguments": {}}], "usage": self.usage}
        resp = dict(self.responses.pop(0))
        if self.usage is not None:
            resp["usage"] = self.usage
        return resp


class FakeSandbox:
    created: ClassVar[list[FakeSandbox]] = []

    def __init__(self, repo: Path, run_id: str, backend: str | None = None):
        self.repo, self.run_id = repo, run_id
        self.path = repo / "sb" / run_id
        self.files: dict[str, str] = {"README.md": "hi", "pkg/mod.py": "x = 1"}
        self.module_results = {"test_spec::test_a": True, "test_spec::test_b": False}
        self.run_tests_calls: list = []
        FakeSandbox.created.append(self)

    def __enter__(self):
        self.path.mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, *exc):
        return None

    def resolve(self, rel: str) -> Path:
        if ".." in Path(rel).parts:
            raise ValueError(f"path escapes sandbox: {rel}")
        return self.path / rel

    def write_file(self, rel: str, content: str) -> None:
        self.resolve(rel)
        self.files[rel] = content

    def read_file(self, rel: str, max_bytes: int = 20_000) -> str:
        self.resolve(rel)
        if rel not in self.files:
            raise FileNotFoundError(rel)
        return self.files[rel][:max_bytes]

    def list_files(self, pattern: str = "**/*", max_entries: int = 200) -> list[str]:
        return sorted(self.files)[:max_entries]

    def run_tests(self, path: str | None = None, timeout: int = 120) -> str:
        self.run_tests_calls.append(path)
        return f"1 passed ({path or 'all'})"

    def run_pytest_modules(self, modules: dict[str, str], timeout: int = 120) -> dict[str, bool]:
        self.modules_seen = modules
        return dict(self.module_results)

    def diff(self) -> str:
        return "+fake diff"


def tc(name, args, **kw):
    return {"name": name, "arguments": {**args, **kw}}


def step(i: int):
    return tc("log_step", {"assumption": f"a{i}", "action": f"act{i}", "files_touched": ["pkg/mod.py"]})


@pytest.fixture
def env(tmp_path, monkeypatch):
    FakeSandbox.created.clear()
    runner.META.clear()
    clients: dict[str, FakeClient] = {}

    def fake_get_client(identity):
        c = clients[identity.id]
        if isinstance(c, Exception):
            raise c
        return c

    monkeypatch.setattr(runner, "get_client", fake_get_client)
    monkeypatch.setattr(runner, "embed_texts", fake_embed)
    monkeypatch.setattr(runner, "Sandbox", FakeSandbox)
    ns = SimpleNamespace(agent_timeout_s=60, runs_dir=tmp_path / "runs", sandboxes_dir=tmp_path / "sb", sandbox_backend="copy")
    monkeypatch.setattr(runner, "settings", ns)
    repo = tmp_path / "repo"
    repo.mkdir()
    return SimpleNamespace(clients=clients, settings=ns, repo=repo, store=MemoryStore())


SUBTASK = Subtask(index=0, title="Tier for", spec="tier", interface=[FunctionSig(name="tier_for", file="pkg/mod.py", params=["amount: float"], returns="str")], files=["pkg/mod.py"])
AGENT = AgentIdentity(id="a", provider="meta", model="m")


def run(env, responses, agent=AGENT, spec=None, usage=None):
    env.clients[agent.id] = FakeClient(responses, usage=usage)
    return runner.run_one(SUBTASK, agent, env.repo, "", [], env.store, spec=spec)


def test_happy_path_writes_meta(env):
    run_ = run(env, [
        {"text": "", "tool_calls": [step(0), tc("write_file", {"path": "pkg/mod.py", "content": "def tier_for(a): return 'x'"})]},
        {"text": "", "tool_calls": [step(1), tc("run_tests", {"path": "tests/test_x.py"}), tc("done", {})]},
    ])
    assert run_.error is None
    assert len(run_.steps) == 2
    assert run_.diff == "+fake diff"
    sb = FakeSandbox.created[0]
    assert sb.files["pkg/mod.py"].startswith("def tier_for")
    assert sb.run_tests_calls == ["tests/test_x.py"]
    meta_path = env.settings.runs_dir / run_.id / "meta.json"
    meta = json.loads(meta_path.read_text())
    assert meta["agent_id"] == "a" and meta["steps"] == 2 and meta["turns"] == 2 and meta["error"] is None
    assert runner.run_meta(run_) == meta
    assert run_.id in env.store.runs and len(env.store.steps) == 2


def test_write_without_log_step_rejected_then_accepted(env):
    client = FakeClient([
        {"text": "", "tool_calls": [tc("write_file", {"path": "pkg/mod.py", "content": "bad"})]},
        {"text": "", "tool_calls": [step(0), tc("write_file", {"path": "pkg/mod.py", "content": "good"}), tc("write_file", {"path": "pkg/mod.py", "content": "bad2"})]},
        {"text": "", "tool_calls": [tc("done", {})]},
    ])
    env.clients["a"] = client
    run_ = runner.run_one(SUBTASK, AGENT, env.repo, "", [], env.store)
    sb = FakeSandbox.created[0]
    assert sb.files["pkg/mod.py"] == "good"
    assert run_.error is None
    assert runner.run_meta(run_)["rejections"] == 2


def test_explore_tools_and_errors_need_no_log_step(env):
    responses = [
        {"text": "", "tool_calls": [tc("list_files", {"pattern": "**/*.py"}), tc("read_file", {"path": "pkg/mod.py"}), tc("read_file", {"path": "nope.py"}), tc("read_file", {"path": "../etc"}), tc("bogus", {})]},
        {"text": "", "tool_calls": [tc("done", {})]},
    ]
    client = FakeClient(responses)
    env.clients["a"] = client
    seen = []
    orig = client.chat

    def chat(messages, tools=None):
        seen.append(list(messages))
        return orig(messages, tools)

    client.chat = chat
    run_ = runner.run_one(SUBTASK, AGENT, env.repo, "", [], env.store)
    assert run_.error is None
    results = [m["content"] for m in seen[-1] if m["content"].startswith("[")]
    assert any("pkg/mod.py" in r and r.startswith("[list_files result]") for r in results)
    assert any("x = 1" in r for r in results)
    assert any("FileNotFoundError" in r for r in results)
    assert any("ValueError" in r for r in results)
    assert any("unknown tool bogus" in r for r in results)


def _spec():
    c1 = TestCase(id="c1", function="tier_for", args=[10.0], source="spec")
    c2 = TestCase(id="c2", function="tier_for", args=[-1.0], source="spec")
    c3 = TestCase(id="c3", function="tier_for", args=[99.0], source="spec")
    return SpecTests(subtask_id=SUBTASK.id, cases=[c1, c2, c3], expected={"c1": "gold", "c2": {"raises": "ValueError"}, "c3": "hidden"}, properties=[], visible_ids=["c1", "c2"])


def test_run_spec_tests_function_cases(env, monkeypatch):
    outcomes = {10.0: Outcome(ok=True, value="silver"), -1.0: Outcome(ok=False, exception="ValueError")}
    monkeypatch.setattr(runner, "_call", lambda impl_dir, sig, case: outcomes[case.args[0]])
    captured = []
    client = FakeClient([
        {"text": "", "tool_calls": [step(0), tc("run_spec_tests", {})]},
        {"text": "", "tool_calls": [tc("done", {})]},
    ])
    orig = client.chat

    def chat(messages, tools=None):
        captured.append(list(messages))
        return orig(messages, tools)

    client.chat = chat
    env.clients["a"] = client
    run_ = runner.run_one(SUBTASK, AGENT, env.repo, "", [], env.store, spec=_spec())
    assert run_.error is None
    assert run_.steps[-1].live_results == {"c1": False, "c2": True}
    report = captured[-1][-1]["content"]
    assert "1/2 visible spec tests passed" in report
    assert "FAIL tier_for(10.0): expected 'gold' got 'silver'" in report
    assert "PASS tier_for(-1.0)" in report
    assert "99.0" not in report and "hidden" not in report


def test_run_spec_tests_modules(env):
    spec = SimpleNamespace(visible_modules={"test_spec": "def test_a(): pass"}, visible=[], expected={})
    run_ = run(env, [
        {"text": "", "tool_calls": [step(0), tc("run_spec_tests", {}), tc("done", {})]},
    ], spec=spec)
    assert run_.steps[-1].live_results == {"test_spec::test_a": True, "test_spec::test_b": False}
    assert FakeSandbox.created[0].modules_seen == spec.visible_modules


def test_no_spec_message(env):
    client = FakeClient([{"text": "", "tool_calls": [step(0), tc("run_spec_tests", {}), tc("done", {})]}])
    env.clients["a"] = client
    run_ = runner.run_one(SUBTASK, AGENT, env.repo, "", [], env.store)
    assert run_.steps[-1].live_results is None and run_.error is None


def test_timeout(env):
    env.settings.agent_timeout_s = 0
    run_ = run(env, [{"text": "", "tool_calls": [tc("done", {})]}])
    assert run_.error and run_.error.startswith("timeout")
    assert env.clients["a"].calls == 0
    assert run_.diff == "+fake diff"


def test_token_budget(env):
    run_ = run(env, [{"text": "", "tool_calls": [step(0)]}] * 5, usage={"input_tokens": 300_000, "output_tokens": 200_000})
    assert run_.error and "budget" in run_.error
    assert env.clients["a"].calls == 1
    assert runner.run_meta(run_)["input_tokens"] == 300_000


def test_run_swarm_survives_crash(env):
    a, b = AgentIdentity(id="a", provider="meta", model="m"), AgentIdentity(id="b", provider="meta", model="m")
    env.clients["a"] = FakeClient([{"text": "", "tool_calls": [tc("done", {})]}])
    env.clients["b"] = RuntimeError("boom")
    runs = asyncio.run(runner.run_swarm(SUBTASK, [a, b], env.repo, [], env.store))
    assert [r.agent.id for r in runs] == ["a", "b"]
    assert runs[0].error is None
    assert runs[1].error == "RuntimeError: boom"
