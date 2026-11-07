"""Tests never call SubQ or any LLM. The SubQ judge is stubbed out unless SUBQ_LIVE_TESTS=1."""

import os

import pytest


@pytest.fixture(autouse=True)
def _no_live_subq(monkeypatch):
    if os.environ.get("SUBQ_LIVE_TESTS") == "1":
        return
    from core.harness import crosstest
    from core.reflect import reflector

    monkeypatch.setattr(crosstest, "default_judge", lambda: None)
    monkeypatch.setattr(reflector, "default_judge", lambda: None)
