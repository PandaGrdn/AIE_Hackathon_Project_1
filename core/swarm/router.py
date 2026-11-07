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
    roster = [a for a in swarm_identities() if provider_available(a.provider)] or swarm_identities()
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
