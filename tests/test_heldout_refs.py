"""Held-out reference implementations match their pinned interfaces and their specs' boundaries."""

from __future__ import annotations

import importlib.util
import inspect
import json
from pathlib import Path
from types import ModuleType

import pytest

from core.models import FunctionSig
from eval.heldout_runner import HELDOUT_DIR, load_heldout

SUBTASKS = ["letter_grades", "library_fines", "parking_billing", "shipping_bands"]


def _interface(name: str) -> list[FunctionSig]:
    return [FunctionSig(**f) for f in json.loads((HELDOUT_DIR / name / "interface.json").read_text())]


def _module(name: str, file: str) -> ModuleType:
    path = HELDOUT_DIR / name / "reference" / file
    spec = importlib.util.spec_from_file_location(f"heldout_ref_{name}", path)
    assert spec and spec.loader, path
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fn(name: str, func: str):
    sig = next(s for s in _interface(name) if s.name == func)
    return getattr(_module(name, sig.file), func)


@pytest.mark.parametrize("name", SUBTASKS)
def test_reference_matches_interface(name: str):
    d = HELDOUT_DIR / name
    assert (d / "spec.md").read_text().strip()
    iface = _interface(name)
    assert 1 <= len(iface) <= 2
    for sig in iface:
        fn = getattr(_module(name, sig.file), sig.name, None)
        assert callable(fn), f"{name}: {sig.file} lacks {sig.name}"
        params = list(inspect.signature(fn).parameters)
        assert len(params) == len(sig.params), f"{name}.{sig.name} arity"
        assert params == [p.split(":", 1)[0].strip() for p in sig.params]


def test_load_heldout_returns_all_subtasks():
    items = load_heldout()
    assert sorted(st.title for st, _ in items) == SUBTASKS
    for st, ref in items:
        assert st.interface and st.files and st.spec.strip()
        assert all((ref / f).is_file() for f in st.files)


# ---------- shipping_bands ----------


def test_shipping_fee_boundaries():
    fee = _fn("shipping_bands", "shipping_fee")
    assert fee(0.0) == 5.0  # zero is valid
    assert fee(1.0) == 5.0 and fee(1.001) == 9.0  # 1 kg inclusive in first band
    assert fee(5.0) == 9.0 and fee(5.001) == 15.0  # 5 kg inclusive in second band
    assert fee(19.999) == 15.0 and fee(20.0) == 20.0  # "less than 20" vs "20 or more"
    assert fee(20.5) == 20.12  # 20.125 rounds half to even
    assert fee(1000.0) == 265.0
    with pytest.raises(ValueError):
        fee(-0.01)
    with pytest.raises(ValueError):
        fee(1000.01)


# ---------- letter_grades ----------


def test_letter_grade_boundaries():
    grade = _fn("letter_grades", "letter_grade")
    assert [grade(s) for s in (100, 90, 89, 80, 79, 70, 69, 60, 59, 0)] == list("AABBCCDDFF")
    with pytest.raises(ValueError):
        grade(-1)
    with pytest.raises(ValueError):
        grade(101)


def test_final_score_rounding_empty_negative():
    final = _fn("letter_grades", "final_score")
    assert final([88, 89]) == 88  # 88.5 -> 88 (half to even, not half up)
    assert final([89, 90]) == 90  # 89.5 -> 90 (not truncated)
    assert final([86, 87]) == 86
    assert final([88, 89, 89]) == 89  # 88.67 -> nearest
    assert final([]) is None
    with pytest.raises(ValueError):
        final([90, -5])
    with pytest.raises(ValueError):
        final([101])


# ---------- parking_billing ----------


def test_parking_charge_boundaries():
    charge = _fn("parking_billing", "parking_charge")
    assert charge(0) == 0.0 and charge(15) == 0.0  # grace inclusive
    assert charge(16) == 2.5 and charge(60) == 2.5  # one started hour
    assert charge(61) == 5.0
    assert charge(420) == 17.5 and charge(421) == 18.0  # cap
    assert charge(1440) == 18.0
    with pytest.raises(ValueError):
        charge(-1)
    with pytest.raises(ValueError):
        charge(1441)


# ---------- library_fines ----------


def test_late_fine_boundaries():
    fine = _fn("library_fines", "late_fine")
    assert fine(0, "book") == 0.0
    assert fine(3, "book") == 0.3  # rounded, not 0.30000000000000004
    assert fine(50, "book") == 5.0 and fine(51, "book") == 5.0  # cap inclusive
    assert fine(9, "dvd") == 9.0 and fine(10, "dvd") == 10.0 and fine(11, "dvd") == 10.0
    assert fine(10, "laptop") == 50.0 and fine(100, "laptop") == 50.0
    for bad in ("Book", "", " book"):
        with pytest.raises(ValueError):
            fine(1, bad)
    with pytest.raises(ValueError):
        fine(-1, "book")


def test_top_fined_ordering_ties_empty():
    top = _fn("library_fines", "top_fined")
    fines = {"cara": 2.0, "abe": 3.5, "bo": 2.0, "dan": 0.0, "eve": 0.01}
    assert top(fines, 10) == ["abe", "bo", "cara", "eve"]  # ties by name, 0 excluded
    assert top(fines, 2) == ["abe", "bo"]
    assert top(fines, 0) == []
    assert top({}, 3) == []
    with pytest.raises(ValueError):
        top(fines, -1)
    with pytest.raises(ValueError):
        top({"x": -1.0}, 1)
