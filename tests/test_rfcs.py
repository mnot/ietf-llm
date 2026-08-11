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

from ietf_llm.paths import get_cache_dir
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


# --- Reader: get_rfc_info -------------------------------------------------------


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


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (8 * 86400, "8 days"),
        (86400, "1 day"),  # singular, not "1 days"
        (86400 + 3600, "1 day"),  # whole days only; no "1 day 1 hour"
        (3600, "1 hour"),  # sub-day: must not render "0 days"
        (2 * 3600, "2 hours"),
        (60, "1 hour"),  # never "0 hours"
    ],
)
def test_format_age(seconds: int, expected: str) -> None:
    # Unit-tested directly, not through render_rfc: `no_such_rfc` only calls
    # this past the TTL, so with today's 24h TTL the sub-day cases are
    # unreachable from there and a render-level test would pass on the days
    # branch without ever exercising them. The sub-day branch guards a TTL
    # lowered below a day, which would otherwise render "0 days ago".
    assert rfcs._format_age(seconds) == expected


def test_hit_on_stale_index_is_unaffected(isolated_home: Path) -> None:
    # Staleness only clouds a *miss*. A hit is a hit — no caveat, and the
    # reader must not go looking for one.
    rfc_dir = _seed(isolated_home)
    _age_mirror(rfc_dir, 30 * 86400)
    out = rfcs.render_rfc("200")
    assert "RFC200 — Quantum" in out
    assert "last refreshed" not in out


# --- Read-path revalidation (get_rfc_info on a stale miss) ----------------------

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


def test_revalidation_does_not_retry(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, gather_on: Any
) -> None:
    # The read path must ask for the non-retrying session. `timeout` is not a
    # deadline: the retrying adapter honours Retry-After, so a 429/503 from
    # rfc.fyi would sleep ~90s past a 5s timeout with a caller waiting on it.
    # A failed fetch has a correct fallback (the honest stale miss), so one
    # attempt then bail is the right trade.
    rfc_dir = _seed(isolated_home)
    _age_mirror(rfc_dir, 8 * 86400)
    seen: List[Any] = []

    def fake_get(url: str, **kwargs: Any) -> Any:
        seen.append(kwargs.get("retrying"))
        return _FakeResponse(503)

    monkeypatch.setattr(gather_rfcs, "governed_get", fake_get)
    out = mcp_rfcs._render_rfc_live("400")
    assert seen == [False]
    # ...and a 503 still lands on the honest message, not a bare negative.
    assert "No such RFC" in out and "last refreshed" in out


def test_gather_still_retries(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The trade above is read-path only. A gather is a background job with
    # nobody waiting, so riding out a blip still beats failing the stage.
    seen: List[Any] = []

    def fake_get(url: str, **kwargs: Any) -> Any:
        seen.append(kwargs.get("retrying"))
        return _FakeResponse(200, _body_for(url))

    monkeypatch.setattr(gather_rfcs, "governed_get", fake_get)
    gather_rfcs.ensure_rfc_index(force=True)
    assert seen and all(r is True for r in seen)


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
        url: str,
        headers: Optional[Dict[str, str]] = None,
        timeout: Any = None,
        **kwargs: Any,
    ):  # noqa: ANN202,ARG001
        calls.append(
            {
                "url": url,
                "headers": headers or {},
                "timeout": timeout,
                "retrying": kwargs.get("retrying"),
            }
        )
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


# --- Body availability (issue #218) ---------------------------------------
#
# `get_rfc_info` has never returned document text -- the `_rfc/` mirror is three
# JSON blobs of metadata. Left unsaid, its well-formed metadata response
# reads as "here is the RFC", and a caller that needed to quote a section
# reasons from memory instead. So every rendered entry ends with which of
# the two cases it is: body on disk (with the call that reads it), or no
# body reachable at all.


def test_rendered_entry_says_body_is_unavailable(isolated_home: Path) -> None:
    _seed(isolated_home)
    out = mcp_rfcs._render_rfc_live("200")
    assert "not reachable from here" in out
    assert "do not characterise what this RFC says" in out


def test_rendered_entry_points_at_a_cached_body(isolated_home: Path) -> None:
    from conftest import write_cache_file  # pylint: disable=import-outside-toplevel

    _seed(isolated_home)
    write_cache_file(isolated_home, "phone", "drafts/rfc200.txt", "the actual prose")
    out = mcp_rfcs._render_rfc_live("200")
    assert 'read_file_section("phone", "drafts/rfc200.txt")' in out
    assert "not reachable from here" not in out


def test_body_note_is_per_rfc_not_per_corpus(isolated_home: Path) -> None:
    # A corpus holding rfc200's body says nothing about rfc300's.
    from conftest import write_cache_file  # pylint: disable=import-outside-toplevel

    _seed(isolated_home)
    write_cache_file(isolated_home, "phone", "drafts/rfc200.txt", "the actual prose")
    assert "not reachable from here" in mcp_rfcs._render_rfc_live("300")


def test_misses_get_no_body_note(isolated_home: Path) -> None:
    # A miss is not a metadata response anyone could mistake for the
    # document, so it stays as terse as it was.
    _seed(isolated_home)
    out = mcp_rfcs._render_rfc_live("99999")
    assert "No such RFC" in out
    assert "Body:" not in out
    assert "has not been gathered" not in mcp_rfcs._render_rfc_live("200")


def test_body_lookup_stays_offline(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, gather_on: Any
) -> None:
    # The scan is a read-only walk of gathered corpora -- never a fetch.
    _seed(isolated_home)
    calls = _install_stub(monkeypatch, lambda url, hdrs: _FakeResponse(200, b"{}"))
    mcp_rfcs._render_rfc_live("200")
    assert calls == []


# --- Body lookup must not materialise the fleet ----------------------------
#
# On the cloud backend, resolving a corpus's files dir *materialises* that
# version's blobs onto scratch. `get_rfc_info` is a high-frequency metadata
# lookup, so sweeping every gathered corpus through the forcing resolver
# would download the whole fleet on one call. The sweep uses the
# non-forcing resolver; only the publishing WG's own corpus -- one corpus,
# the overwhelmingly likely holder -- is worth the materialisation.


class _RecordingStore:
    """Answers like the local backend, but records which corpora were
    resolved through the *materialising* path (`local_cache_dir`)."""

    def __init__(self, corpora: List[str]) -> None:
        self._corpora = corpora
        self.forced: List[str] = []

    def _files(self, corpus: str) -> Optional[str]:
        path = os.path.join(get_cache_dir(), corpus, "files")
        return path if os.path.isdir(path) else None

    def list_corpora(self) -> List[str]:
        return list(self._corpora)

    def local_cache_dir(self, corpus: str) -> Optional[str]:
        self.forced.append(corpus)
        return self._files(corpus)

    def materialised_cache_dir(self, corpus: str) -> Optional[str]:
        return self._files(corpus)


def _install_store(
    monkeypatch: pytest.MonkeyPatch, corpora: List[str]
) -> _RecordingStore:
    from ietf_llm.mcp import common  # pylint: disable=import-outside-toplevel

    store = _RecordingStore(corpora)
    monkeypatch.setattr(common, "get_corpus_store", lambda: store)
    return store


def test_working_group_accessor(isolated_home: Path) -> None:
    _seed(isolated_home)
    assert rfcs.working_group("200") == "phone"
    assert rfcs.working_group("RFC200") == "phone"
    # RFC300 carries an empty wg, as the legacy / independent streams do.
    assert rfcs.working_group("300") is None
    assert rfcs.working_group("99999") is None


def test_sweep_never_forces_materialisation(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import write_cache_file  # pylint: disable=import-outside-toplevel

    _seed(isolated_home)
    write_cache_file(isolated_home, "other", "drafts/rfc300.txt", "prose")
    store = _install_store(monkeypatch, ["other", "phone"])
    # RFC300 has no wg, so this is a pure sweep -- and it still finds the body.
    out = mcp_rfcs._render_rfc_live("300")
    assert 'read_file_section("other", "drafts/rfc300.txt")' in out
    assert store.forced == []


def test_owner_corpus_is_the_one_worth_materialising(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import write_cache_file  # pylint: disable=import-outside-toplevel

    _seed(isolated_home)
    write_cache_file(isolated_home, "phone", "drafts/rfc200.txt", "prose")
    store = _install_store(monkeypatch, ["other", "phone"])
    out = mcp_rfcs._render_rfc_live("200")
    assert 'read_file_section("phone", "drafts/rfc200.txt")' in out
    # Exactly one -- the publishing WG -- not every gathered corpus.
    assert store.forced == ["phone"]


def test_a_miss_forces_only_the_owner(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The common case. A miss must not escalate into resolving the fleet.
    _seed(isolated_home)
    store = _install_store(monkeypatch, ["other", "phone"])
    assert "not reachable from here" in mcp_rfcs._render_rfc_live("200")
    assert store.forced == ["phone"]


class _UnstagedStore(_RecordingStore):
    """A cloud replica with nothing staged: the non-forcing resolver finds
    nothing even though the bodies are on disk."""

    def materialised_cache_dir(self, corpus: str) -> Optional[str]:
        return None


def test_unstaged_corpus_reads_as_unreachable(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The behaviour change that will actually be hit on cloud. The body is
    # published, this replica just can't see it without materialising, so the
    # note claims unreachability rather than absence -- and still doesn't
    # download the fleet to find out.
    from conftest import write_cache_file  # pylint: disable=import-outside-toplevel

    _seed(isolated_home)
    write_cache_file(isolated_home, "other", "drafts/rfc300.txt", "prose")
    store = _UnstagedStore(["other", "phone"])
    from ietf_llm.mcp import common  # pylint: disable=import-outside-toplevel

    monkeypatch.setattr(common, "get_corpus_store", lambda: store)

    out = mcp_rfcs._render_rfc_live("300")
    assert "not reachable from here" in out
    assert store.forced == []


def test_owner_not_gathered_skips_the_probe(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The majority case: only ~4% of the series has a wg that is also a
    # gathered corpus, so usually there is no owner to probe at all.
    _seed(isolated_home)
    store = _install_store(monkeypatch, ["other"])  # 'phone' not gathered
    assert rfcs.working_group("200") == "phone"
    assert "not reachable from here" in mcp_rfcs._render_rfc_live("200")
    assert store.forced == []


def test_owner_wins_when_several_corpora_hold_the_body(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Precedence: previously first-alphabetical, now the publishing WG --
    # a better answer, since a body under an unrelated corpus is coincidence.
    from conftest import write_cache_file  # pylint: disable=import-outside-toplevel

    _seed(isolated_home)
    write_cache_file(isolated_home, "aaa", "drafts/rfc200.txt", "prose")
    write_cache_file(isolated_home, "phone", "drafts/rfc200.txt", "prose")
    _install_store(monkeypatch, ["aaa", "phone"])
    out = mcp_rfcs._render_rfc_live("200")
    assert 'read_file_section("phone", "drafts/rfc200.txt")' in out


def test_a_reference_written_as_a_name_still_resolves() -> None:
    """Observed live: RFC 9786's normative refs carry `"RFC9785"` where all
    9,797 other entries carry `"9785"`. `int()` on that raised, which aborted
    the whole index load and took every RFC tool with it."""
    assert rfcs.rfc_num_to_name("RFC9785") == "RFC9785"
    assert rfcs.rfc_num_to_name("9785") == "RFC9785"
    assert rfcs.rfc_name_to_num("RFC9785") == "9785"


@pytest.mark.parametrize("value", ["", "nonsense", "RFC", "9110.5", None])
def test_an_unparseable_reference_is_dropped_not_raised(value: Any) -> None:
    """One malformed field in someone else's data is not a reason to have no
    RFC metadata at all."""
    assert rfcs.rfc_num_to_name(value) == ""
    assert rfcs.rfc_name_to_num(value) == ""


def test_one_bad_reference_does_not_abort_the_index(isolated_home: Path) -> None:
    rfc_dir = isolated_home / ".cache" / "ietf-llm" / rfcs.RFC_DIR
    rfc_dir.mkdir(parents=True, exist_ok=True)
    (rfc_dir / "rfcs.json").write_text(
        json.dumps(
            {
                "RFC9785": {"title": "Cited", "status": "current"},
                "RFC9786": {"title": "Citing", "status": "current"},
            }
        ),
        encoding="utf-8",
    )
    (rfc_dir / "refs.json").write_text(
        json.dumps({"9786": {"normative": ["RFC9785", "junk"], "informative": []}}),
        encoding="utf-8",
    )
    (rfc_dir / "tags.json").write_text("{}", encoding="utf-8")
    rfcs._CACHE = None
    data = rfcs._load()
    assert data is not None, "one malformed reference aborted the load"
    out = data.outbound_refs("RFC9786")
    assert out["normative"] == ["RFC9785"]  # the junk is dropped, not blank
    assert len(data.inbound_refs("RFC9785")) == 1
