"""Pick N agents for a subtask by trust score. Owner: B.

Routing is one of the four change levers: a trust update is a versioned change.
"""

from __future__ import annotations

from core.llm.clients import swarm_identities
from core.models import AgentIdentity, Subtask
from core.store.base import Store


def route(subtask: Subtask, store: Store, n: int) -> list[AgentIdentity]:
    roster = swarm_identities()
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
