"""Tests for IETF session-polls discovery and rendering.

Polls are gathered via the Datatracker polls doctype. We stub
`iter_group_documents` (the API doc lister) and `fetch_resource` at the
`gather.sources.session_polls` module boundary so no HTTP is hit. Tests cover:

- The poll doc-name pattern matches `polls-<n>-<wg>-<dt>` and ignores
  slides / agenda names.
- The JSON poll payload renders to markdown (question + tallies), with
  a raw fallback for unexpected shapes.
- Already-cached polls files aren't re-downloaded.
- `discover_local_polls` parses filenames back to (wg, meeting, when).
- Timeline integration emits a `poll` event per cached file.
- Malformed filenames are skipped, not crashed on.
"""

from __future__ import annotations

import json
from datetime import timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from ietf_llm.gather.sources import session_polls
from ietf_llm.log import Verbosity
from ietf_llm.paths import get_wg_file_cache_dir


class _FakeResponse:
    """Minimal `requests.Response`-like object for stubbing."""

    def __init__(self, text: str, content_type: str = "application/json") -> None:
        self.text = text
        self.headers: Dict[str, str] = {"Content-Type": content_type}
        self.status_code = 200


#: A realistic poll payload as Datatracker serves it.
_POLL_JSON = json.dumps([
    {
        "start_time": "2022-07-28T13:30:00Z",
        "end_time": "2022-07-28T13:31:00Z",
        "text": "Adopt draft-foo?",
        "raise_hand": 28,
        "do_not_raise_hand": 4,
    },
])


def _stub(
    monkeypatch: pytest.MonkeyPatch,
    doc_names: List[str],
    content: Optional[_FakeResponse],
) -> List[str]:
    """Stub iter_group_documents to yield `doc_names` and fetch_resource
    to return `content`. Returns a list recording fetched URLs."""
    fetched: List[str] = []

    def fake_iter(wg: str, doc_type: str) -> Any:  # noqa: ARG001
        for name in doc_names:
            yield {"name": name}

    def fake_fetch(
        url: str, headers: Optional[Dict[str, str]] = None,  # noqa: ARG001
    ) -> Optional[_FakeResponse]:
        fetched.append(url)
        return content

    monkeypatch.setattr(session_polls, "iter_group_documents", fake_iter)
    monkeypatch.setattr(session_polls, "fetch_resource", fake_fetch)
    return fetched


# --- _POLLS_NAME_RE pattern -----------------------------------------------


def test_polls_name_pattern_matches_canonical_form() -> None:
    match = session_polls._POLLS_NAME_RE.match("polls-114-httpbis-202207281330")
    assert match is not None
    assert match.group(1) == "114"           # meeting number
    assert match.group(2) == "202207281330"  # session datetime


def test_polls_name_pattern_rejects_slide_or_agenda_names() -> None:
    assert session_polls._POLLS_NAME_RE.match("slides-114-httpbis-something") is None
    assert session_polls._POLLS_NAME_RE.match("agenda-114-httpbis") is None


# --- JSON rendering -------------------------------------------------------


def test_render_polls_body_formats_questions_and_tallies() -> None:
    body = session_polls._render_polls_body(_POLL_JSON)
    assert body is not None
    assert "### Poll 1: Adopt draft-foo?" in body
    assert "- Raise hand: 28" in body
    assert "- Do not raise hand: 4" in body
    # Timestamps are not surfaced as tallies.
    assert "start_time" not in body


def test_render_polls_body_falls_back_on_non_json() -> None:
    # An unexpected (non-JSON) payload is preserved verbatim rather
    # than dropped.
    assert session_polls._render_polls_body("plain text poll") == "plain text poll"
    assert session_polls._render_polls_body("") is None


# --- end-to-end gather ----------------------------------------------------


def test_polls_downloaded_to_named_file(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(monkeypatch, ["polls-114-httpbis-202207281330"], _FakeResponse(_POLL_JSON))
    cache = Path(get_wg_file_cache_dir("httpbis"))
    cache.mkdir(parents=True, exist_ok=True)
    written = session_polls.process_session_polls(
        "httpbis", str(cache), verbose=Verbosity.QUIET,
    )
    assert len(written) == 1
    out = Path(written[0])
    # Post-reorg: under meetings/ietf114/polls/.
    assert out.name == "202207281330.md"
    assert "meetings/ietf114/polls" in str(out)
    text = out.read_text()
    # Header carries the load-bearing context.
    assert "IETF 114" in text
    assert "2022-07-28 13:30 UTC" in text
    assert "polls-114-httpbis-202207281330" in text  # source URL
    # Rendered tallies survive.
    assert "Adopt draft-foo?" in text
    assert "Raise hand: 28" in text


def test_polls_skip_when_already_cached(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Re-running gather mustn't re-fetch a polls document we already
    # have. Saves bandwidth and matches existing minutes behaviour.
    cache = Path(get_wg_file_cache_dir("httpbis"))
    cache.mkdir(parents=True, exist_ok=True)
    polls_subdir = cache / "meetings" / "ietf114" / "polls"
    polls_subdir.mkdir(parents=True, exist_ok=True)
    pre_existing = polls_subdir / "202207281330.md"
    pre_existing.write_text("# pre-existing content\n")

    fetched = _stub(
        monkeypatch, ["polls-114-httpbis-202207281330"], _FakeResponse(_POLL_JSON),
    )
    written = session_polls.process_session_polls(
        "httpbis", str(cache), verbose=Verbosity.QUIET,
    )
    # Pre-existing file unchanged, no new files reported, and the
    # poll content URL was never fetched.
    assert written == []
    assert pre_existing.read_text() == "# pre-existing content\n"
    assert fetched == []


def test_polls_skips_unparseable_doc_names(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A polls doctype doc whose name doesn't match the canonical shape
    # (e.g. an interim, which the numbered-meeting regex skips) is
    # ignored without error.
    _stub(
        monkeypatch,
        ["polls-interim-2023-httpbis-01-httpbis-202301011200"],
        _FakeResponse(_POLL_JSON),
    )
    cache = Path(get_wg_file_cache_dir("httpbis"))
    cache.mkdir(parents=True, exist_ok=True)
    assert session_polls.process_session_polls(
        "httpbis", str(cache), verbose=Verbosity.QUIET,
    ) == []


def test_polls_no_docs_no_files(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No polls docs for the group → no writes, no errors.
    _stub(monkeypatch, [], None)
    cache = Path(get_wg_file_cache_dir("httpbis"))
    cache.mkdir(parents=True, exist_ok=True)
    assert session_polls.process_session_polls(
        "httpbis", str(cache), verbose=Verbosity.QUIET,
    ) == []


def test_polls_fetch_failure_is_swallowed(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Poll content unreachable → empty result, no crash. Same
    # philosophy as the rest of the Datatracker integration.
    _stub(monkeypatch, ["polls-114-httpbis-202207281330"], None)
    cache = Path(get_wg_file_cache_dir("httpbis"))
    cache.mkdir(parents=True, exist_ok=True)
    assert session_polls.process_session_polls(
        "httpbis", str(cache), verbose=Verbosity.QUIET,
    ) == []


# --- discover_local_polls -------------------------------------------------


def test_discover_local_polls_parses_filename(
    isolated_home: Path,
) -> None:
    cache = Path(get_wg_file_cache_dir("httpbis"))
    for code, dt in [("ietf114", "202207281330"), ("ietf115", "202211090830")]:
        d = cache / "meetings" / code / "polls"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{dt}.md").write_text("# polls\n")
    # An unrelated thread file mustn't show up.
    threads_dir = cache / "threads"
    threads_dir.mkdir(parents=True, exist_ok=True)
    (threads_dir / "2022-07-28-topic.md").write_text("# t\n")
    polls = session_polls.discover_local_polls(str(cache))
    assert len(polls) == 2
    by_meeting = {p.meeting: p for p in polls}
    assert by_meeting["114"].when.year == 2022
    assert by_meeting["114"].when.month == 7
    assert by_meeting["114"].when.tzinfo is not None
    assert by_meeting["115"].meeting == "115"


def test_discover_local_polls_skips_malformed_filenames(
    isolated_home: Path,
) -> None:
    cache = Path(get_wg_file_cache_dir("httpbis"))
    polls_dir = cache / "meetings" / "ietf114" / "polls"
    polls_dir.mkdir(parents=True, exist_ok=True)
    # Filename matches the prefix but the date token is garbage.
    (polls_dir / "NOTADATE.md").write_text("# x\n")
    # No exception; the bad row is silently skipped.
    assert session_polls.discover_local_polls(str(cache)) == []


def test_discover_local_polls_handles_missing_dir(tmp_path: Path) -> None:
    assert session_polls.discover_local_polls(
        str(tmp_path / "nope")
    ) == []


# --- timeline integration -------------------------------------------------


def test_timeline_emits_poll_event_per_cached_file(
    isolated_home: Path,
) -> None:
    # Consumer-facing payoff: a cached polls file becomes a `poll`
    # event in the timeline, linkable from a `read_digest` call with
    # event_kind="poll".
    from ietf_llm.digest.timeline import build_events
    from ietf_llm.people import Registry

    cache = Path(get_wg_file_cache_dir("httpbis"))
    polls_dir = cache / "meetings" / "ietf114" / "polls"
    polls_dir.mkdir(parents=True, exist_ok=True)
    (polls_dir / "202207281330.md").write_text(
        "# httpbis session polls — IETF 114\n\nbody\n"
    )
    events = build_events(
        "httpbis", str(cache), Registry(),
        verbose=Verbosity.QUIET,
    )
    poll_events = [e for e in events if e.kind == "poll"]
    assert len(poll_events) == 1
    assert "IETF 114" in poll_events[0].title
    # Link is the relative path to the cached file.
    assert "meetings/ietf114/polls/202207281330.md" in (
        poll_events[0].link or ""
    )
