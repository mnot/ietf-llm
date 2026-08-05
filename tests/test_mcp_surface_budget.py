"""Budget gate on the MCP surface — the bytes every client pays for before it
asks a single question: the `instructions` field plus the serialized tool list
(name + description + inputSchema).

Line and word counts miss this. The instructions file is under 10% of the
surface; the tool docstrings are the rest, and they grow one reasonable-looking
paragraph at a time. So the measurement here is the payload a client actually
receives, built through `mcp.surface` (which goes through the production
`server.register_tools`, so a new module is measured for free).

The baseline in `mcp_surface_baseline.json` is a *ceiling*, ratchet-style:
shrinking is always fine, growing fails until someone edits the baseline in the
same commit — which is the point. The diff is the review signal.

Regenerate after a deliberate change:

    IETF_LLM_UPDATE_MCP_BASELINE=1 .venv/bin/python -m pytest \
        tests/test_mcp_surface_budget.py

To see *where* the weight is (and what to trim), run
`scripts/mcp_surface_report.py`.

Not measured: `_capability_footer` (machine-generated, and a version bump would
move it by a character), and per-parameter descriptions — today every parameter
carries none at all, so there is nothing to weigh. That is a live source of the
docstring bulk, not something this gate can ratchet.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable

import pytest

from ietf_llm.mcp.surface import SHAPES, build_surface, instructions_text

BASELINE = Path(__file__).parent / "mcp_surface_baseline.json"

#: How far below the ceiling the total may drift before we ask for a
#: re-baseline. Without this the ratchet only ever holds the line it was set
#: at: trim 30% of the surface and the old ceiling silently re-authorises the
#: bloat coming back.
SLACK = 0.90

_REGEN = "IETF_LLM_UPDATE_MCP_BASELINE"
_REGEN_HINT = (
    f"If the growth is deliberate, regenerate the ceiling in the same commit:\n"
    f"    {_REGEN}=1 .venv/bin/python -m pytest tests/test_mcp_surface_budget.py"
)


@pytest.fixture(autouse=True)
def _neutral_mode(monkeypatch: pytest.MonkeyPatch) -> Iterable[None]:
    """Drive the session mode from the shape under test, not from the ambient
    environment (`session_shape` restores the process globals itself)."""
    monkeypatch.delenv("IETF_LLM_ENABLE_GATHER", raising=False)
    monkeypatch.delenv("IETF_LLM_DEBUG_LOG", raising=False)
    yield


def _measure(shape: str) -> Dict[str, Any]:
    tools = build_surface(shape)
    weighed = {t.name: {"chars": t.chars, "params": len(t.params)} for t in tools}
    return {
        "instructions_chars": len(instructions_text(shape)),
        "total_chars": sum(t["chars"] for t in weighed.values()),
        "tools": weighed,
    }


def _regenerate() -> None:
    BASELINE.write_text(
        json.dumps(
            {
                "README": (
                    "Ceiling for the MCP surface a client receives. Regenerate "
                    "with IETF_LLM_UPDATE_MCP_BASELINE=1; see "
                    "tests/test_mcp_surface_budget.py. Growth here is a review "
                    "signal, not a formality."
                ),
                "shapes": {shape: _measure(shape) for shape in SHAPES},
            },
            indent=2,
            sort_keys=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _baseline(shape: str) -> Dict[str, Any]:
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    return data["shapes"][shape]


@pytest.mark.skipif(not os.environ.get(_REGEN), reason="regeneration not requested")
def test_regenerate_baseline() -> None:
    """Not a test — the regeneration entry point, so the ceiling is rewritten by
    the same code that measures it (a hand-edited number would be a guess)."""
    _regenerate()


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_tool_set_is_declared(shape: str) -> None:
    """A new tool is the most expensive thing you can add — it must land in the
    baseline deliberately, not ride in under a total that happened to fit."""
    measured = _measure(shape)
    base = _baseline(shape)
    added = sorted(set(measured["tools"]) - set(base["tools"]))
    removed = sorted(set(base["tools"]) - set(measured["tools"]))
    assert not added and not removed, (
        f"[{shape}] advertised tool set changed — added {added}, removed "
        f"{removed}.\n{_REGEN_HINT}"
    )


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_per_tool_budget(shape: str) -> None:
    """Per-tool ceilings, so one docstring can't quietly absorb the headroom
    every other tool gave back."""
    measured = _measure(shape)
    base = _baseline(shape)
    over = []
    for name, got in measured["tools"].items():
        want = base["tools"].get(name)
        if want is None:
            continue  # reported by test_tool_set_is_declared
        if got["chars"] > want["chars"]:
            delta = got["chars"] - want["chars"]
            over.append(
                f"  {name}: {got['chars']} chars (ceiling {want['chars']}, +{delta})"
            )
        if got["params"] > want["params"]:
            over.append(f"  {name}: {got['params']} params (ceiling {want['params']})")
    assert not over, (
        f"[{shape}] tools over budget:\n" + "\n".join(over) + f"\n{_REGEN_HINT}"
    )


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_total_surface_budget(shape: str) -> None:
    """The whole preamble: instructions plus every tool. Roughly chars/4 tokens
    — reported both ways because the ceiling is set in chars (exact) but the
    cost that matters is context the client can't spend on the user."""
    measured = _measure(shape)
    base = _baseline(shape)
    for key in ("instructions_chars", "total_chars"):
        got, want = measured[key], base[key]
        assert got <= want, (
            f"[{shape}] {key} {got} exceeds ceiling {want} "
            f"(+{got - want} chars, ~{(got - want) / 4:.0f} tokens).\n{_REGEN_HINT}"
        )


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_ratchet_is_current(shape: str) -> None:
    """Tighten the ceiling after a real trim, or the old number quietly
    re-authorises the bloat you just removed."""
    measured = _measure(shape)
    base = _baseline(shape)
    floor = base["total_chars"] * SLACK
    assert measured["total_chars"] >= floor, (
        f"[{shape}] the surface shrank to {measured['total_chars']} chars, well "
        f"under the {base['total_chars']} ceiling — tighten the ratchet.\n{_REGEN_HINT}"
    )
