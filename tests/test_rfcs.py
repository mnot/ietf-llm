"""Tests for the RFC-series index: reader (`ietf_llm.rfcs`) and the
rfc.fyi mirror writer (`ietf_llm.gather.sources.rfcs`).

The reader is exercised against a small seeded `_rfc/` cache; the writer
against a stubbed HTTP session so no HTTP is hit. The final block is a
writer->reader round-trip — drive `ensure_rfc_index` with canned
responses, then read the bytes back through `render_*` — which is the
durable guard against writer/reader format drift.

Data shapes mirror what rfc.fyi actually serves:
  rfcs.json : {"RFC9110": {title, status, stream, level, wg, area,
               keywords, obsoletes:[<RFC-name>, ...]}}
  refs.json : {"9110": {normative:[<bare-num>], informative:[<bare-num>]}}
  tags.json : {"collection": {...}}   (facets are derived by the reader)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from ietf_llm.singletons import rfcs
from ietf_llm.gather.sources import rfcs as gather_rfcs
from ietf_llm.mcp import rfcs as mcp_rfcs

# --- Synthetic dataset -----------------------------------------------------

# RFC200 obsoletes RFC100; RFC200 cites RFC100 normatively; RFC300 cites
# RFC100 informatively. That covers facet derivation, obsoletes
# inversion, and inbound/outbound reference counting in one small graph.
_RFCS: Dict[str, Dict[str, Any]] = {
    "RFC100": {
        "title": "Telephone Network Protocol",
        "status": "obsoleted",
        "stream": "ietf",
        "level": "std",
        "wg": "phone",
        "area": "art",
        "keywords": ["telephony", "switching"],
        "obsoletes": [],
    },
    "RFC200": {
        "title": "Quantum Telephone Protocol",
        "status": "current",
        "stream": "ietf",
        "level": "std",
        "wg": "phone",
        "area": "art",
        "keywords": ["telephony", "quantum"],
        "obsoletes": ["RFC100"],
    },
    "RFC300": {
        "title": "Survey of Messaging",
        "status": "current",
        "stream": "irtf",
        "level": "informational",
        "wg": "",
        "area": "irtf",
        "keywords": ["messaging"],
        "obsoletes": [],
    },
}

_REFS: Dict[str, Dict[str, List[str]]] = {
    "200": {"normative": ["100"], "informative": []},
    "300": {"normative": [], "informative": ["100"]},
}

_TAGS: Dict[str, Any] = {"collection": {"classic": {"rfcs": ["RFC100"]}}}


def _seed(home: Path) -> Path:
    """Write the three index blobs into the sandbox's `_rfc/` dir."""
    rfc_dir = home / ".cache" / "ietf-llm" / rfcs.RFC_DIR
    rfc_dir.mkdir(parents=True, exist_ok=True)
    (rfc_dir / "rfcs.json").write_text(json.dumps(_RFCS), encoding="utf-8")
    (rfc_dir / "refs.json").write_text(json.dumps(_REFS), encoding="utf-8")
    (rfc_dir / "tags.json").write_text(json.dumps(_TAGS), encoding="utf-8")
    return rfc_dir


@pytest.fixture(autouse=True)
def _reset_reader_cache() -> Any:
    """The reader memoises on mtime in a module global; reset it around
    every test so one test's parsed index can't leak into another."""
    rfcs._CACHE = None
    yield
    rfcs._CACHE = None


# --- Reader: search --------------------------------------------------------


def test_search_matches_title_word(isolated_home: Path) -> None:
    _seed(isolated_home)
    # "network" appears only in RFC100's title; "telephone" is in both
    # titles, so it's a poor discriminator — use the unique word.
    out = rfcs.render_search("network")
    assert "RFC100" in out and "RFC200" not in out


def test_search_prefix_matches_within_title(isolated_home: Path) -> None:
    _seed(isolated_home)
    # "quant" is a prefix of the title word "Quantum".
    out = rfcs.render_search("quant")
    assert "RFC200" in out and "RFC100" not in out


def test_search_matches_keyword(isolated_home: Path) -> None:
    _seed(isolated_home)
    out = rfcs.render_search("switching")  # only RFC100's keyword
    assert "RFC100" in out and "RFC200" not in out


def test_search_short_term_never_matches(isolated_home: Path) -> None:
    _seed(isolated_home)
    # Two-char term is below PREFIX_LEN; matches nothing even though
    # "te" prefixes "telephony"/"Telephone".
    assert "No RFCs match" in rfcs.render_search("te")


def test_search_multi_term_is_intersection(isolated_home: Path) -> None:
    _seed(isolated_home)
    # "telephony" (kw, both) AND "quantum" (kw, only RFC200) -> RFC200.
    out = rfcs.render_search("telephony quantum")
    assert "RFC200" in out and "RFC100" not in out


def test_search_bare_number_short_circuits(isolated_home: Path) -> None:
    _seed(isolated_home)
    out = rfcs.render_search("100")
    assert "RFC100" in out and "RFC200" not in out and "RFC300" not in out


def test_search_unknown_number_falls_through_to_text(isolated_home: Path) -> None:
    _seed(isolated_home)
    # 999 isn't an RFC, so it's treated as a normal (non-matching) term.
    assert "No RFCs match" in rfcs.render_search("999")


def test_search_status_filter(isolated_home: Path) -> None:
    _seed(isolated_home)
    out = rfcs.render_search("telephony", status="current")
    assert "RFC200" in out and "RFC100" not in out


def test_search_stream_and_level_filters(isolated_home: Path) -> None:
    _seed(isolated_home)
    out = rfcs.render_search("messaging", stream="irtf", level="informational")
    assert "RFC300" in out


def test_search_wg_filter(isolated_home: Path) -> None:
    _seed(isolated_home)
    out = rfcs.render_search("telephony", group="phone")
    assert "RFC100" in out and "RFC200" in out


def test_search_impossible_filter_reports_no_match(isolated_home: Path) -> None:
    _seed(isolated_home)
    out = rfcs.render_search("telephony", stream="iab")
    assert "No RFCs match" in out


def test_search_limit_truncates_and_notes_total(isolated_home: Path) -> None:
    _seed(isolated_home)
    out = rfcs.render_search("telephony", limit=1)
    # Both RFC100 and RFC200 match; only one is shown but the total stands.
    assert "2 RFCs" in out and "showing 1" in out


def test_search_not_gathered_message(isolated_home: Path) -> None:
    # No _seed: the index doesn't exist.
    assert "has not been gathered" in rfcs.render_search("anything")


# --- Reader: get_rfc -------------------------------------------------------


def test_render_rfc_core_metadata(isolated_home: Path) -> None:
    _seed(isolated_home)
    out = rfcs.render_rfc("200")
    assert "RFC200 — Quantum Telephone Protocol" in out
    assert "Status: current" in out
    assert "Stream: ietf" in out
    assert "Level: std" in out
    assert "Working group: phone" in out
    assert "telephony, quantum" in out  # keywords


def test_render_rfc_obsoletes_and_obsoleted_by(isolated_home: Path) -> None:
    _seed(isolated_home)
    out200 = rfcs.render_rfc("200")
    assert "Obsoletes: RFC100" in out200
    out100 = rfcs.render_rfc("100")
    assert "Obsoleted by: RFC200" in out100  # inversion


def test_render_rfc_reference_counts(isolated_home: Path) -> None:
    _seed(isolated_home)
    out = rfcs.render_rfc("100")
    # RFC200 cites it normatively, RFC300 informatively.
    assert "Cited by: 1 normative, 1 informative" in out


def test_render_rfc_outbound_refs(isolated_home: Path) -> None:
    _seed(isolated_home)
    out = rfcs.render_rfc("200")
    assert "References (normative): RFC100" in out


def test_render_rfc_text_links(isolated_home: Path) -> None:
    _seed(isolated_home)
    out = rfcs.render_rfc("200")
    assert "rfc-editor.org/rfc/rfc200.txt" in out
    assert "rfc-editor.org/info/rfc200" in out


def test_render_rfc_accepts_name_form(isolated_home: Path) -> None:
    _seed(isolated_home)
    assert "RFC200 — Quantum" in rfcs.render_rfc("RFC200")


def test_render_rfc_unknown_number(isolated_home: Path) -> None:
    _seed(isolated_home)
    assert "No such RFC" in rfcs.render_rfc("99999")


def test_render_rfc_not_gathered_message(isolated_home: Path) -> None:
    assert "has not been gathered" in rfcs.render_rfc("100")


def _age_mirror(rfc_dir: Path, seconds: float) -> None:
    """Backdate the mirror's mtime — the reader's staleness signal."""
    path = rfc_dir / "rfcs.json"
    old = time.time() - seconds
    os.utime(path, (old, old))


def test_miss_on_fresh_index_is_a_bare_negative(isolated_home: Path) -> None:
    # A just-written mirror can't be hiding a recent RFC, so the miss is
    # authoritative and must not be hedged with a staleness caveat.
    _seed(isolated_home)
    out = rfcs.render_rfc("99999")
    assert "No such RFC" in out
    assert "last refreshed" not in out


def test_miss_on_stale_index_admits_it_may_be_stale(isolated_home: Path) -> None:
    # The RFC9846 case: past the TTL a miss is indistinguishable from
    # "published since we mirrored", so it must say so rather than assert
    # a negative the caller would read as authoritative.
    rfc_dir = _seed(isolated_home)
    _age_mirror(rfc_dir, rfcs.RFC_TTL_SECONDS + 60)
    out = rfcs.render_rfc("99999")
    assert "No such RFC" in out
    assert "last refreshed" in out


def test_stale_miss_reports_the_age_in_days(isolated_home: Path) -> None:
    rfc_dir = _seed(isolated_home)
    _age_mirror(rfc_dir, 8 * 86400)
    assert "8 days ago" in rfcs.render_rfc("99999")


def test_stale_miss_reports_hours_under_a_day(isolated_home: Path) -> None:
    # Just past a 24h TTL there are no whole days to report; the message
    # must not read "0 days ago".
    rfc_dir = _seed(isolated_home)
    _age_mirror(rfc_dir, rfcs.RFC_TTL_SECONDS + 3600)
    out = rfcs.render_rfc("99999")
    assert "25 hours ago" in out or "1 day ago" in out
    assert "0 days" not in out


def test_hit_on_stale_index_is_unaffected(isolated_home: Path) -> None:
    # Staleness only clouds a *miss*. A hit is a hit — no caveat, and the
    # reader must not go looking for one.
    rfc_dir = _seed(isolated_home)
    _age_mirror(rfc_dir, 30 * 86400)
    out = rfcs.render_rfc("200")
    assert "RFC200 — Quantum" in out
    assert "last refreshed" not in out


# --- Read-path revalidation (get_rfc on a stale miss) ----------------------

# A newcomer, absent from the seeded mirror: the RFC9846 shape — published
# after the last gather, so only a live revalidation can resolve it.
_RFC400 = {
    "title": "Recently Published Protocol",
    "status": "current",
    "stream": "ietf",
    "level": "std",
    "wg": "new",
    "area": "art",
    "keywords": [],
    "obsoletes": [],
}


def _grown_body(url: str) -> bytes:
    """Upstream as it looks once RFC400 has been published."""
    if url.endswith("rfcs.json"):
        return json.dumps({**_RFCS, "RFC400": _RFC400}).encode("utf-8")
    return _body_for(url)


@pytest.fixture(autouse=True)
def _reset_revalidate_state() -> Any:
    """The revalidation throttle lives in module globals; reset it around
    every test so one test's attempt can't throttle the next."""
    gather_rfcs.reset_state()
    yield
    gather_rfcs.reset_state()


@pytest.fixture(name="gather_on")
def _gather_on(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Enable the gather gate — the sanctioned networked-read exception."""
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", "1")


def test_stale_miss_revalidates_and_resolves(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, gather_on: Any
) -> None:
    # The whole point: a number published since the last gather resolves
    # rather than coming back as "No such RFC".
    rfc_dir = _seed(isolated_home)
    _age_mirror(rfc_dir, 8 * 86400)
    _install_stub(monkeypatch, lambda url, hdrs: _FakeResponse(200, _grown_body(url)))
    out = mcp_rfcs._render_rfc_live("400")
    assert "RFC400 — Recently Published Protocol" in out
    assert "No such RFC" not in out


def test_stale_miss_fetches_only_the_existence_file(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, gather_on: Any
) -> None:
    # refs/tags are the reference graph — not needed to answer existence,
    # and they lag upstream anyway. Don't pull ~1.6MB to say yes or no.
    rfc_dir = _seed(isolated_home)
    _age_mirror(rfc_dir, 8 * 86400)
    calls = _install_stub(
        monkeypatch, lambda url, hdrs: _FakeResponse(200, _grown_body(url))
    )
    mcp_rfcs._render_rfc_live("400")
    assert calls and all(c["url"].endswith("rfcs.json") for c in calls)


def test_hit_never_touches_the_network(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, gather_on: Any
) -> None:
    # The common path stays offline even when the mirror is ancient.
    rfc_dir = _seed(isolated_home)
    _age_mirror(rfc_dir, 30 * 86400)
    calls = _install_stub(monkeypatch, lambda url, hdrs: _FakeResponse(200, b"{}"))
    assert "RFC200 — Quantum" in mcp_rfcs._render_rfc_live("200")
    assert calls == []


def test_fresh_miss_never_touches_the_network(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, gather_on: Any
) -> None:
    # Inside the TTL a miss is authoritative; fetching would be pointless.
    _seed(isolated_home)
    calls = _install_stub(monkeypatch, lambda url, hdrs: _FakeResponse(200, b"{}"))
    assert "No such RFC" in mcp_rfcs._render_rfc_live("99999")
    assert calls == []


def test_revalidation_is_gated_off_without_gather(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A read-only HTTP replica keeps its offline boundary: no fetch, and the
    # stale miss is still reported honestly rather than as a bare negative.
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", "0")
    rfc_dir = _seed(isolated_home)
    _age_mirror(rfc_dir, 8 * 86400)
    calls = _install_stub(monkeypatch, lambda url, hdrs: _FakeResponse(200, b"{}"))
    out = mcp_rfcs._render_rfc_live("400")
    assert calls == []
    assert "No such RFC" in out and "last refreshed" in out


def test_failed_revalidation_falls_back_to_the_honest_miss(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, gather_on: Any
) -> None:
    # rfc.fyi down: never raise into the tool, and don't let the failure
    # turn the stale miss back into a confident negative.
    rfc_dir = _seed(isolated_home)
    _age_mirror(rfc_dir, 8 * 86400)

    def _boom(url: str, hdrs: Dict[str, str]) -> Any:
        import requests  # pylint: disable=import-outside-toplevel

        raise requests.ConnectionError("rfc.fyi unreachable")

    _install_stub(monkeypatch, _boom)
    out = mcp_rfcs._render_rfc_live("400")
    assert "No such RFC" in out and "last refreshed" in out


def test_revalidation_is_throttled_across_misses(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, gather_on: Any
) -> None:
    # A burst of misses against a down host must not hammer rfc.fyi: one
    # attempt per backoff window, the rest fall back to the stale answer.
    rfc_dir = _seed(isolated_home)
    _age_mirror(rfc_dir, 8 * 86400)

    def _boom(url: str, hdrs: Dict[str, str]) -> Any:
        import requests  # pylint: disable=import-outside-toplevel

        raise requests.ConnectionError("rfc.fyi unreachable")

    calls = _install_stub(monkeypatch, _boom)
    for _ in range(5):
        mcp_rfcs._render_rfc_live("400")
    assert len(calls) == 1


def test_revalidation_sends_the_stored_etag(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, gather_on: Any
) -> None:
    # A 304 is the common case (the series changes slowly), and it both
    # costs no body and restarts the TTL — so the next miss is authoritative
    # and doesn't re-fetch.
    rfc_dir = _seed(isolated_home)
    (rfc_dir / "rfcs.json.etag").write_text('W/"v1"', encoding="utf-8")
    _age_mirror(rfc_dir, 8 * 86400)
    calls = _install_stub(monkeypatch, lambda url, hdrs: _FakeResponse(304))
    out = mcp_rfcs._render_rfc_live("99999")
    assert calls and calls[0]["headers"].get("If-None-Match") == 'W/"v1"'
    # 304 touched the mirror, so the miss is now inside the TTL: no caveat.
    assert "No such RFC" in out and "last refreshed" not in out


def test_no_revalidation_without_a_mirror(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, gather_on: Any
) -> None:
    # Nothing mirrored yet is a gather, not a revalidation; the tool says so.
    calls = _install_stub(monkeypatch, lambda url, hdrs: _FakeResponse(200, b"{}"))
    assert "has not been gathered" in mcp_rfcs._render_rfc_live("400")
    assert calls == []


def test_revalidate_never_materialises_the_mirror(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, gather_on: Any
) -> None:
    # Called with nothing mirrored, revalidation must decline *before*
    # claiming an attempt: marking one writes a throttle marker, and that
    # would makedirs `_rfc/` into existence. Reads never materialise cache.
    # (`_render_rfc_live` can't reach this — `is_stale_miss` is False with no
    # mirror — so guard the entry point directly.)
    calls = _install_stub(monkeypatch, lambda url, hdrs: _FakeResponse(200, b"{}"))
    gather_rfcs.revalidate_index()
    assert calls == []
    assert not (isolated_home / ".cache" / "ietf-llm" / rfcs.RFC_DIR).exists()


# --- Writer: ensure_rfc_index ---------------------------------------------


class _FakeResponse:
    """Minimal `requests.Response`-like object for stubbing."""

    def __init__(
        self,
        status_code: int = 200,
        body: bytes = b"{}",
        etag: Optional[str] = None,
    ) -> None:
        self.status_code = status_code
        self.content = body
        self.headers: Dict[str, str] = {}
        if etag is not None:
            self.headers["ETag"] = etag

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests  # pylint: disable=import-outside-toplevel

            raise requests.HTTPError(f"status {self.status_code}")


def _install_stub(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> List[Dict[str, Any]]:
    """Replace the shared HTTP session's get with `handler`, recording
    each call's url + headers. Returns the call log."""
    calls: List[Dict[str, Any]] = []

    def fake_get(
        url: str, headers: Optional[Dict[str, str]] = None, timeout: Any = None
    ):  # noqa: ANN202,ARG001
        calls.append({"url": url, "headers": headers or {}})
        return handler(url, headers or {})

    monkeypatch.setattr(gather_rfcs, "governed_get", fake_get)
    return calls


def _body_for(url: str) -> bytes:
    if url.endswith("rfcs.json"):
        return json.dumps(_RFCS).encode("utf-8")
    if url.endswith("refs.json"):
        return json.dumps(_REFS).encode("utf-8")
    return json.dumps(_TAGS).encode("utf-8")


def test_writer_200_writes_body_and_sidecar(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_stub(
        monkeypatch,
        lambda url, hdrs: _FakeResponse(200, _body_for(url), etag='W/"v1"'),
    )
    gather_rfcs.ensure_rfc_index(force=True)
    rfc_dir = isolated_home / ".cache" / "ietf-llm" / rfcs.RFC_DIR
    files = set(os.listdir(rfc_dir))
    for name in rfcs.RFC_FILES:
        assert name in files
        assert f"{name}.etag" in files
        assert (rfc_dir / f"{name}.etag").read_text() == 'W/"v1"'
    assert not any(f.endswith(".tmp") for f in files)  # atomic, no leftovers


def test_writer_first_fetch_sends_no_conditional_header(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_stub(
        monkeypatch,
        lambda url, hdrs: _FakeResponse(200, _body_for(url), etag='W/"v1"'),
    )
    gather_rfcs.ensure_rfc_index(force=True)
    assert all("If-None-Match" not in c["headers"] for c in calls)


def test_writer_revalidates_with_if_none_match(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # First pass writes bodies + etags.
    _install_stub(
        monkeypatch,
        lambda url, hdrs: _FakeResponse(200, _body_for(url), etag='W/"v1"'),
    )
    gather_rfcs.ensure_rfc_index(force=True)
    # Second pass (forced past the TTL) must echo the stored etag back.
    calls = _install_stub(monkeypatch, lambda url, hdrs: _FakeResponse(304))
    gather_rfcs.ensure_rfc_index(force=True)
    assert calls and all(c["headers"].get("If-None-Match") == 'W/"v1"' for c in calls)


def test_writer_304_keeps_body_and_etag_touches_mtime(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_stub(
        monkeypatch,
        lambda url, hdrs: _FakeResponse(200, _body_for(url), etag='W/"v1"'),
    )
    gather_rfcs.ensure_rfc_index(force=True)
    rfc_dir = isolated_home / ".cache" / "ietf-llm" / rfcs.RFC_DIR
    body = rfc_dir / "rfcs.json"
    etag = rfc_dir / "rfcs.json.etag"
    original = body.read_bytes()
    old_mtime = body.stat().st_mtime
    os.utime(body, (old_mtime - 1000, old_mtime - 1000))  # age it
    # 304: body content unchanged, etag preserved, mtime refreshed.
    _install_stub(monkeypatch, lambda url, hdrs: _FakeResponse(304))
    gather_rfcs.ensure_rfc_index(force=True)
    assert body.read_bytes() == original
    assert etag.read_text() == 'W/"v1"'
    assert body.stat().st_mtime > old_mtime - 1000


def test_writer_200_without_etag_drops_stale_sidecar(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_stub(
        monkeypatch,
        lambda url, hdrs: _FakeResponse(200, _body_for(url), etag='W/"v1"'),
    )
    gather_rfcs.ensure_rfc_index(force=True)
    rfc_dir = isolated_home / ".cache" / "ietf-llm" / rfcs.RFC_DIR
    assert (rfc_dir / "rfcs.json.etag").exists()
    # A later 200 with no ETag header must remove the now-mismatched sidecar.
    _install_stub(
        monkeypatch, lambda url, hdrs: _FakeResponse(200, _body_for(url), etag=None)
    )
    gather_rfcs.ensure_rfc_index(force=True)
    assert not (rfc_dir / "rfcs.json.etag").exists()


def test_writer_ttl_guard_skips_network_when_fresh(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_stub(
        monkeypatch,
        lambda url, hdrs: _FakeResponse(200, _body_for(url), etag='W/"v1"'),
    )
    gather_rfcs.ensure_rfc_index(force=True)  # files now fresh
    calls = _install_stub(monkeypatch, lambda url, hdrs: _FakeResponse(304))
    gather_rfcs.ensure_rfc_index(force=False)  # within TTL -> no fetch
    assert calls == []


def test_writer_network_error_preserves_cache_and_does_not_raise(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_stub(
        monkeypatch,
        lambda url, hdrs: _FakeResponse(200, _body_for(url), etag='W/"v1"'),
    )
    gather_rfcs.ensure_rfc_index(force=True)
    rfc_dir = isolated_home / ".cache" / "ietf-llm" / rfcs.RFC_DIR
    original = (rfc_dir / "rfcs.json").read_bytes()

    import requests  # pylint: disable=import-outside-toplevel

    def boom(url: str, hdrs: Dict[str, str]) -> _FakeResponse:
        raise requests.ConnectionError("network down")

    _install_stub(monkeypatch, boom)
    gather_rfcs.ensure_rfc_index(force=True)  # must not raise
    assert (rfc_dir / "rfcs.json").read_bytes() == original  # untouched


def test_writer_http_error_preserves_cache(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_stub(
        monkeypatch,
        lambda url, hdrs: _FakeResponse(200, _body_for(url), etag='W/"v1"'),
    )
    gather_rfcs.ensure_rfc_index(force=True)
    rfc_dir = isolated_home / ".cache" / "ietf-llm" / rfcs.RFC_DIR
    original = (rfc_dir / "rfcs.json").read_bytes()
    _install_stub(monkeypatch, lambda url, hdrs: _FakeResponse(500, b""))
    gather_rfcs.ensure_rfc_index(force=True)  # 5xx -> raise_for_status, swallowed
    assert (rfc_dir / "rfcs.json").read_bytes() == original


# --- Writer -> reader round-trip ------------------------------------------


def test_round_trip_writer_to_reader(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the writer with canned responses, then read the bytes it
    wrote back through the reader. Guards against writer/reader drift."""
    _install_stub(
        monkeypatch,
        lambda url, hdrs: _FakeResponse(200, _body_for(url), etag='W/"v1"'),
    )
    gather_rfcs.ensure_rfc_index(force=True)
    # Reader sees exactly what the writer produced.
    search = rfcs.render_search("quantum")
    assert "RFC200" in search
    detail = rfcs.render_rfc("100")
    assert "Obsoleted by: RFC200" in detail
    assert "Cited by: 1 normative, 1 informative" in detail
