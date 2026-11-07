"""Pick N agents for a subtask by trust score. Owner: B.

Routing is one of the four change levers: a trust update is a versioned change.
"""

from __future__ import annotations

from core.llm.clients import provider_available, swarm_identities
from core.models import AgentIdentity, Subtask
from core.store.base import Store


def route(subtask: Subtask, store: Store, n: int) -> list[AgentIdentity]:
    # Skip providers with no API key so a missing key never produces an agent that dies at get_client.
    # With no keys at all (offline tests, fake swarm) keep the full roster.
    import os

    full = swarm_identities()
    # SWARM_AGENTS="gemini-t2,claude-t2,meta-spark-t5" pins the roster for a recorded run (order kept). It is
    # taken from the FULL roster: a pinned identity is the caller's explicit choice (fake swarms need no key).
    pinned = [s.strip() for s in os.environ.get("SWARM_AGENTS", "").split(",") if s.strip()]
    if pinned:
        by_id = {a.id: a for a in full}
        roster = [by_id[i] for i in pinned if i in by_id] or full
    elif os.environ.get("SWARM_FAKE") == "1":
        roster = full  # offline fake swarm: identities are labels, no client is ever built
    else:
        roster = [a for a in full if provider_available(a.provider)] or full
    scored = [(store.get_trust(a.id).score, a) for a in roster]
    scored.sort(key=lambda x: -x[0])
    chosen = [a for _, a in scored[:n]]
    # Keep at least two provider families for independence.
    families = {a.provider for a in chosen}
    if len(families) < 2:
        for _, a in scored[n:]:
            if a.provider not in families:
                chosen[-1] = a
                break
    return chosen
