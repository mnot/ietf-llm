"""The `--author` gather pass drives reviews as well as drafts.

`sources.reviews` is unit-tested separately; this covers the sequencer
wiring — that a resolved `--author` reaches `gather_reviews` with the
person id and canonical name, and that nothing fires when there is no
`--author` or the spec doesn't resolve.
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, List, Optional, Tuple

import pytest

from ietf_llm.gather import sequencer
from ietf_llm.log import Verbosity

Q = Verbosity.QUIET


def _args(**kw: Any) -> argparse.Namespace:
    base: Dict[str, Any] = {
        "wg": "mnot",
        "author": None,
        "new_drafts": False,
        "draft": None,
        "months": 12,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    resolved: Optional[Tuple[int, str]],
) -> List[Tuple[str, int, str]]:
    """Stub the author + reviews seams; return the recorded review calls."""
    calls: List[Tuple[str, int, str]] = []

    monkeypatch.setattr(
        sequencer, "resolve_person", lambda spec, verbose=None: resolved
    )
    monkeypatch.setattr(
        sequencer, "fetch_author_draft_names", lambda pid, verbose=None: ["draft-x-00"]
    )
    monkeypatch.setattr(
        sequencer, "process_extra_drafts", lambda names, cache, verbose=None: None
    )
    monkeypatch.setattr(sequencer, "_persist_author_name", lambda wg, name: None)
    monkeypatch.setattr(
        sequencer,
        "gather_reviews",
        lambda cache, pid, name, verbose=None: calls.append((cache, pid, name)) or [],
    )
    return calls


def test_author_gather_fetches_reviews(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch(monkeypatch, resolved=(103881, "Mark Nottingham"))
    sequencer._gather_dynamic_drafts(
        _args(author="mnot@mnot.net"), "/cache/mnot", {}, Q
    )
    assert calls == [("/cache/mnot", 103881, "Mark Nottingham")]


def test_unresolved_author_fetches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unresolvable --author spec already skips the draft pass; the
    review pass must not fire on the back of it either."""
    calls = _patch(monkeypatch, resolved=None)
    sequencer._gather_dynamic_drafts(_args(author="nobody@example.com"), "/c", {}, Q)
    assert calls == []


def test_no_author_flag_fetches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch(monkeypatch, resolved=(1, "Someone"))
    sequencer._gather_dynamic_drafts(_args(), "/c", {}, Q)
    assert calls == []
