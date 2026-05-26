"""Tests for IETF session-polls discovery and rendering.

We stub `fetch_resource` at the module boundary in
`gather.session_polls` so no HTTP is hit. Tests cover:

- The materials-page link extractor picks up `polls-…` hrefs and
  ignores unrelated ones (slides, agendas, other WGs' polls).
- Already-cached polls files aren't re-downloaded.
- The on-disk format includes meeting / session-datetime / source URL.
- `discover_local_polls` parses filenames back to (wg, meeting, when).
- Timeline integration emits a `poll` event per cached file.
- Malformed filenames are skipped, not crashed on.
"""

from __future__ import annotations

from datetime import timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from ietf_llm.gather import session_polls
from ietf_llm.utils import Verbosity, get_wg_file_cache_dir


class _FakeResponse:
    """Minimal `requests.Response`-like object for stubbing."""

    def __init__(self, text: str, content_type: str = "text/html") -> None:
        self.text = text
        self.headers: Dict[str, str] = {"Content-Type": content_type}
        self.status_code = 200


def _stub_fetch(
    monkeypatch: pytest.MonkeyPatch,
    responses: Dict[str, Optional[_FakeResponse]],
) -> None:
    """Patch session_polls.fetch_resource with a URL-substring lookup."""
    def fake_fetch(
        url: str, headers: Optional[Dict[str, str]] = None,  # noqa: ARG001
    ) -> Optional[_FakeResponse]:
        for key, body in responses.items():
            if key in url:
                return body
        return None

    monkeypatch.setattr(session_polls, "fetch_resource", fake_fetch)


# --- _POLLS_HREF_RE pattern -----------------------------------------------


def test_polls_url_pattern_matches_canonical_form() -> None:
    # The URL the user pointed at:
    #   /meeting/114/materials/polls-114-httpbis-202207281330-00
    href = "/meeting/114/materials/polls-114-httpbis-202207281330-00"
    match = session_polls._POLLS_HREF_RE.search(href)
    assert match is not None
    assert match.group(1) == "114"   # meeting number
    assert match.group(2) == "httpbis"
    assert match.group(3) == "202207281330"
    assert match.group(4) == "00"    # version


def test_polls_url_pattern_rejects_slide_or_agenda_links() -> None:
    # The materials hub also lists slides and agendas; their hrefs
    # follow different patterns and must NOT be picked up as polls.
    assert session_polls._POLLS_HREF_RE.search(
        "/meeting/114/materials/slides-114-httpbis-something"
    ) is None
    assert session_polls._POLLS_HREF_RE.search(
        "/meeting/114/materials/agenda-114-httpbis"
    ) is None


# --- end-to-end download -------------------------------------------------


def _materials_hub_html(href: str) -> str:
    return f"""
    <html><body>
      <a href='/meeting/114/materials/slides-114-httpbis-foo'>Slides</a>
      <a href='{href}'>Polls</a>
      <a href='/meeting/114/materials/agenda-114-httpbis'>Agenda</a>
    </body></html>
    """


def _poll_doc_html(body_text: str) -> str:
    return f"""
    <html><body>
      <div class='card-body'>
        <h3>Adopt draft-foo?</h3>
        <p>Yes: 28 / No: 4 / Abstain: 9</p>
        <p>{body_text}</p>
      </div>
    </body></html>
    """


def test_polls_downloaded_to_named_file(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    href = "/meeting/114/materials/polls-114-httpbis-202207281330-00"
    _stub_fetch(monkeypatch, {
        "materials.html": _FakeResponse(_materials_hub_html(href)),
        "polls-114-httpbis": _FakeResponse(_poll_doc_html("Discussion.")),
    })
    cache = Path(get_wg_file_cache_dir("httpbis"))
    cache.mkdir(parents=True, exist_ok=True)
    written = session_polls.fetch_polls_from_materials_page(
        "https://datatracker.ietf.org/meeting/114/materials.html",
        str(cache),
        "httpbis",
        verbose=Verbosity.QUIET,
    )
    assert len(written) == 1
    out = Path(written[0])
    assert out.name == "httpbis-polls-114-202207281330.md"
    text = out.read_text()
    # Header carries the load-bearing context.
    assert "IETF 114" in text
    assert "2022-07-28 13:30 UTC" in text
    assert "polls-114-httpbis-202207281330-00" in text  # source URL
    # Body content survives the HTML cleaning.
    assert "Adopt draft-foo?" in text


def test_polls_skip_when_already_cached(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Re-running gather mustn't re-fetch a polls document we already
    # have. Saves bandwidth and matches existing minutes behaviour.
    href = "/meeting/114/materials/polls-114-httpbis-202207281330-00"
    cache = Path(get_wg_file_cache_dir("httpbis"))
    cache.mkdir(parents=True, exist_ok=True)
    pre_existing = cache / "httpbis-polls-114-202207281330.md"
    pre_existing.write_text("# pre-existing content\n")

    called: list[str] = []

    def tracking_fetch(
        url: str, headers: Optional[Dict[str, str]] = None,  # noqa: ARG001
    ) -> Optional[_FakeResponse]:
        called.append(url)
        if "materials.html" in url:
            return _FakeResponse(_materials_hub_html(href))
        return _FakeResponse(_poll_doc_html("fresh content"))

    monkeypatch.setattr(session_polls, "fetch_resource", tracking_fetch)
    written = session_polls.fetch_polls_from_materials_page(
        "https://datatracker.ietf.org/meeting/114/materials.html",
        str(cache),
        "httpbis",
        verbose=Verbosity.QUIET,
    )
    # Pre-existing file unchanged, no new files reported, and the
    # poll-doc URL was never fetched.
    assert written == []
    assert pre_existing.read_text() == "# pre-existing content\n"
    assert not any("polls-114-httpbis" in u for u in called)


def test_polls_ignores_other_wgs_on_same_hub(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A joint or back-to-back session might render polls for a
    # different WG on the same materials hub. We mustn't pull those.
    other_href = "/meeting/114/materials/polls-114-tls-202207281530-00"
    _stub_fetch(monkeypatch, {
        "materials.html": _FakeResponse(_materials_hub_html(other_href)),
        "polls-114-tls": _FakeResponse(_poll_doc_html("tls content")),
    })
    cache = Path(get_wg_file_cache_dir("httpbis"))
    cache.mkdir(parents=True, exist_ok=True)
    written = session_polls.fetch_polls_from_materials_page(
        "https://datatracker.ietf.org/meeting/114/materials.html",
        str(cache),
        "httpbis",
        verbose=Verbosity.QUIET,
    )
    assert written == []
    assert not (cache / "httpbis-polls-114-202207281530.md").exists()


def test_polls_version_dedupe(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If the hub lists -00 and -01 for the same session, we take one
    # (the first encountered) — not both, which would duplicate.
    html = """
    <html><body>
      <a href='/meeting/114/materials/polls-114-httpbis-202207281330-00'>v0</a>
      <a href='/meeting/114/materials/polls-114-httpbis-202207281330-01'>v1</a>
    </body></html>
    """
    _stub_fetch(monkeypatch, {
        "materials.html": _FakeResponse(html),
        "polls-114-httpbis": _FakeResponse(_poll_doc_html("body")),
    })
    cache = Path(get_wg_file_cache_dir("httpbis"))
    cache.mkdir(parents=True, exist_ok=True)
    written = session_polls.fetch_polls_from_materials_page(
        "https://datatracker.ietf.org/meeting/114/materials.html",
        str(cache),
        "httpbis",
        verbose=Verbosity.QUIET,
    )
    assert len(written) == 1


def test_polls_no_links_no_files(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Hub page has no polls links → no writes, no errors.
    _stub_fetch(monkeypatch, {
        "materials.html": _FakeResponse(
            "<html><body><a href='/foo'>Foo</a></body></html>"
        ),
    })
    cache = Path(get_wg_file_cache_dir("httpbis"))
    cache.mkdir(parents=True, exist_ok=True)
    written = session_polls.fetch_polls_from_materials_page(
        "https://datatracker.ietf.org/meeting/114/materials.html",
        str(cache),
        "httpbis",
        verbose=Verbosity.QUIET,
    )
    assert written == []


def test_polls_fetch_failure_is_swallowed(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Materials hub unreachable → empty result, no crash. Same
    # philosophy as the rest of the Datatracker integration.
    monkeypatch.setattr(
        session_polls, "fetch_resource",
        lambda url, headers=None: None,
    )
    cache = Path(get_wg_file_cache_dir("httpbis"))
    cache.mkdir(parents=True, exist_ok=True)
    assert session_polls.fetch_polls_from_materials_page(
        "https://datatracker.ietf.org/meeting/114/materials.html",
        str(cache),
        "httpbis",
        verbose=Verbosity.QUIET,
    ) == []


# --- discover_local_polls -------------------------------------------------


def test_discover_local_polls_parses_filename(
    isolated_home: Path,
) -> None:
    cache = Path(get_wg_file_cache_dir("httpbis"))
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "httpbis-polls-114-202207281330.md").write_text("# polls\n")
    (cache / "httpbis-polls-115-202211090830.md").write_text("# polls\n")
    # An unrelated file in the same dir mustn't show up.
    (cache / "httpbis-thread-2022-07-28-topic.md").write_text("# t\n")
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
    cache.mkdir(parents=True, exist_ok=True)
    # Filename matches the prefix but the date token is garbage.
    (cache / "httpbis-polls-114-NOTADATE.md").write_text("# x\n")
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
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "httpbis-polls-114-202207281330.md").write_text(
        "# httpbis session polls — IETF 114\n\nbody\n"
    )
    events = build_events(
        "httpbis", str(cache), Registry(),
        verbose=Verbosity.QUIET,
    )
    poll_events = [e for e in events if e.kind == "poll"]
    assert len(poll_events) == 1
    assert "IETF 114" in poll_events[0].title
    # Link uses backticks so the existing timeline renderer doesn't
    # need a special case.
    assert "httpbis-polls-114-202207281330.md" in (poll_events[0].link or "")
