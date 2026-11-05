"""The break-it harness must never silently skip a stale mutation (follow-up 3)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


def load() -> ModuleType:
    path = Path(__file__).resolve().parent.parent / "scripts" / "break_it.py"
    spec = importlib.util.spec_from_file_location("break_it", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["break_it"] = module
    spec.loader.exec_module(module)
    return module


def test_every_mutation_target_matches_the_current_code_exactly_once() -> None:
    mod = load()
    assert mod.stale(mod.MUTATIONS) == []
    assert len({m.name for m in mod.MUTATIONS}) == len(mod.MUTATIONS)


def test_a_stale_mutation_makes_the_script_exit_2_with_a_message_and_run_nothing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = load()
    ran: list[int] = []

    def fake_run() -> list[str]:
        ran.append(1)
        return []

    monkeypatch.setattr(mod, "run_tests", fake_run)
    stale_one = mod.Mutation("stale_example", "donation_ledger/parsing.py", "NO SUCH TEXT", "x")
    ambiguous = mod.Mutation("ambiguous_example", "donation_ledger/parsing.py", "import", "x")
    monkeypatch.setattr(mod, "MUTATIONS", [mod.MUTATIONS[0], stale_one, ambiguous])
    assert mod.main([]) == 2
    out = capsys.readouterr().out
    assert "stale_example" in out and "ambiguous_example" in out and "Nothing was run" in out
    assert ran == []
