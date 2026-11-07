from pathlib import Path

from core.decompose.interfaces import diff_interface, extract_interface
from core.harness.strategies import generate_cases
from core.models import FunctionSig


def test_extract_interface(tmp_path: Path):
    (tmp_path / "m.py").write_text("def tier_for(amount: float) -> str:\n    return 'basic'\n\ndef _private(): ...\n")
    sigs = extract_interface(tmp_path, ["m.py"])
    assert [s.name for s in sigs] == ["tier_for"]
    assert sigs[0].params == ["amount: float"] and sigs[0].returns == "str"
    assert diff_interface([FunctionSig(name="other", file="m.py", params=["x: int"])], sigs) == ["m.py:other/1"]


def test_generate_cases_has_boundaries_and_fuzz():
    sig = FunctionSig(name="tier_for", file="m.py", params=["amount: float"], returns="str")
    cases = generate_cases(sig, n_fuzz=20)
    seeds = [c for c in cases if c.source == "seed"]
    assert any(c.args == [100.0] for c in seeds)
    assert len([c for c in cases if c.source == "fuzz"]) > 0
