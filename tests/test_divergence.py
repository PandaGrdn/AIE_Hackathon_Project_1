import numpy as np

from core.models import AgentIdentity, AgentRun, ReasoningStep
from core.paths.divergence import find_divergence


def _run(vectors: list[list[float]], rid: str) -> AgentRun:
    agent = AgentIdentity(id=f"agent-{rid}", provider="meta", model="x")
    steps = [ReasoningStep(index=i, assumption=f"a{i}", action=f"do{i}", embedding=v) for i, v in enumerate(vectors)]
    return AgentRun(id=rid, subtask_id="s", agent=agent, sandbox_path="/tmp", steps=steps)


def test_divergence_found_at_outlier_step():
    rng = np.random.default_rng(0)
    base = [rng.standard_normal(8).tolist() for _ in range(4)]
    verified = [_run([(np.asarray(v) + rng.normal(0, 0.01, 8)).tolist() for v in base], f"v{i}") for i in range(5)]
    bad = [list(v) for v in base]
    bad[2] = (np.asarray(base[2]) + 5.0).tolist()  # step 2 goes far off
    failed = _run(bad, "f")
    div = find_divergence(failed, verified, sigma=2.0)
    assert div is not None and div.step_index == 2
