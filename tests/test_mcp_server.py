"""Tests for the pure tool-function bodies in ietf_llm.mcp_server.

Only the no-network pieces:
- _safe_path: must reject path traversal and absolute paths
- tool_read_file_section: must enforce its line cap
- tool_list_corpora: only WGs with a files/ subdir
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ietf_llm import mcp_server

from conftest import write_cache_file


# --- _safe_path ------------------------------------------------------------


def test_safe_path_resolves_valid_file(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "ok.txt")
    resolved = mcp_server._safe_path("wg", "ok.txt")
    assert resolved is not None
    assert resolved.endswith("/ok.txt")


def test_safe_path_rejects_traversal(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "ok.txt")
    # Even though /etc/passwd exists on most systems, the function
    # should refuse to resolve outside the WG's files/ dir.
    assert mcp_server._safe_path("wg", "../../../etc/passwd") is None
    assert mcp_server._safe_path("wg", "../../other-wg/files/x.txt") is None


def test_safe_path_returns_none_for_missing(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "exists.txt")
    assert mcp_server._safe_path("wg", "missing.txt") is None


# --- tool_read_file_section -----------------------------------------------


def test_read_file_section_respects_max_lines(isolated_home: Path) -> None:
    content = "\n".join(f"line {i}" for i in range(1, 101))
    write_cache_file(isolated_home, "wg", "long.txt", content)
    out = mcp_server.tool_read_file_section("wg", "long.txt", start_line=1, max_lines=10)
    lines = out.splitlines()
    # 10 lines of content plus a "truncated" marker
    assert any(l.startswith("line 10") for l in lines)
    assert not any(l.startswith("line 11") for l in lines)
    assert any("truncated" in l for l in lines)


def test_read_file_section_rejects_oversized_request(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "ok.txt", "hi")
    out = mcp_server.tool_read_file_section("wg", "ok.txt", max_lines=99999)
    assert "exceeds hard cap" in out


def test_read_file_section_supports_start_line(isolated_home: Path) -> None:
    content = "\n".join(f"line {i}" for i in range(1, 21))
    write_cache_file(isolated_home, "wg", "long.txt", content)
    out = mcp_server.tool_read_file_section(
        "wg", "long.txt", start_line=10, max_lines=3
    )
    assert "line 10" in out
    assert "line 11" in out
    assert "line 12" in out
    assert "line 9" not in out


def test_read_file_section_rejects_path_traversal(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "ok.txt", "hi")
    out = mcp_server.tool_read_file_section("wg", "../../../etc/passwd")
    assert "not found" in out.lower()


# --- tool_list_corpora ---------------------------------------------


def test_list_corpora_only_wgs_with_files_dir(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg1", "x.txt")
    write_cache_file(isolated_home, "wg2", "x.txt")
    # A bare directory without files/ underneath shouldn't count.
    (isolated_home / ".cache" / "ietf-llm" / "stray").mkdir(parents=True)
    out = mcp_server.tool_list_corpora()
    assert "wg1" in out
    assert "wg2" in out
    assert "stray" not in out


def test_list_corpora_states_session_facts(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Deployment topology and gather availability must be read from the server,
    # not guessed from the transport: list_corpora states both, authoritatively.
    from ietf_llm import freshness

    write_cache_file(isolated_home, "wg1", "x.txt")
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", "1")
    monkeypatch.setattr(freshness, "_DEPLOYMENT_MODE", "stdio")
    on = mcp_server.tool_list_corpora()
    assert "single-user stdio server" in on.lower()
    assert "costs only you" in on.lower()  # no shared-cost hedging locally
    assert "in-session gather is available" in on.lower()
    assert "start_gather" in on
    # HTTP + read-only: the shared-cost caution is scoped on, gather off.
    monkeypatch.setattr(freshness, "_DEPLOYMENT_MODE", "http")
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", "0")
    off = mcp_server.tool_list_corpora()
    assert "shared http server" in off.lower()
    assert "read-only" in off.lower()
    assert "ietf-llm <name>" in off


def test_set_deployment_mode_normalises(monkeypatch: pytest.MonkeyPatch) -> None:
    from ietf_llm import freshness

    monkeypatch.setattr(freshness, "_DEPLOYMENT_MODE", "stdio")
    freshness.set_deployment_mode("http")
    assert freshness.deployment_mode() == "http"
    freshness.set_deployment_mode("streamable-anything-else")
    assert freshness.deployment_mode() == "stdio"  # anything but 'http'


def test_list_corpora_synthetic_status_disclaims_effort(isolated_home: Path) -> None:
    # A synthetic `x-` bundle must not read as a chartered effort: its
    # status cell, and the legend, say so explicitly.
    write_cache_file(isolated_home, "x-agent", "x.txt")
    out = mcp_server.tool_list_corpora()
    row = next(ln for ln in out.splitlines() if ln.startswith("x-agent"))
    assert "synthetic · local bundle, not an IETF effort" in row
    assert "not a chartered IETF effort" in out  # legend


def test_list_corpora_empty_message(isolated_home: Path) -> None:
    out = mcp_server.tool_list_corpora()
    # Corpus-oriented empty message points the user at the gather CLI.
    assert "no corpora" in out.lower()
    assert "ietf-llm" in out.lower()


# --- read_digest ----------------------------------------------------------


def test_read_digest_people_kind_is_valid(isolated_home: Path) -> None:
    # "people" is one of the recognised kinds. With no file present
    # we get the not-found message; with the file present we get content.
    write_cache_file(isolated_home, "wg", "digests/people.md", "# people\n")
    out = mcp_server.tool_read_digest("wg", "people")
    assert "# people" in out


def test_read_digest_rejects_unknown_kind(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "digests/index.md", "# x\n")
    out = mcp_server.tool_read_digest("wg", "nonsense")
    # An *unknown* kind is distinguished from an ungathered one.
    assert "Unknown digest kind 'nonsense'" in out
    assert "people" in out  # still lists the valid kinds
    assert "Valid kinds" in out


def test_read_digest_absent_kind_reports_what_corpus_has(isolated_home: Path) -> None:
    # `issues` is a valid kind, but this corpus gathered no GitHub repos.
    # The message should list what IS present, not the universal set, and
    # must not read as if `issues` were an invalid kind.
    write_cache_file(isolated_home, "wg", "digests/threads.md", "# threads\n")
    write_cache_file(isolated_home, "wg", "digests/people.md", "# people\n")
    out = mcp_server.tool_read_digest("wg", "issues")
    assert "no 'issues' digest" in out
    assert "This corpus has: threads, people" in out
    assert "Unknown digest kind" not in out  # not treated as invalid
    assert "GitHub" in out  # explains why issues are absent


def test_read_digest_sort_activity_wired(isolated_home: Path) -> None:
    # The sort="activity" heat ranking is reachable through the tool, not
    # just inside overview.
    write_cache_file(
        isolated_home, "wg", "digests/threads.md",
        "# wg: threads\n\n"
        "| Subject | Msgs | Participants | First | Last | File |\n"
        "|---|---|---|---|---|---|\n"
        "| Quiet | 2 | 1 | 2026-01-01 | 2026-01-02 | a.md |\n"
        "| Loud | 20 | 5 | 2026-01-01 | 2026-01-03 | b.md |\n",
    )
    out = mcp_server.tool_read_digest("wg", "threads", sort="activity")
    assert out.index("Loud") < out.index("Quiet")


def test_read_digest_exclude_mechanical_wired(isolated_home: Path) -> None:
    write_cache_file(
        isolated_home, "wg", "digests/timeline.md",
        "# wg: timeline\n\n## 2026\n\n"
        '- **2026-05-20** — WG Last Call thread: "x"\n'
        "- **2026-05-18** — `draft-x`: Alice → No Objection · ballots/x\n",
    )
    out = mcp_server.tool_read_digest("wg", "timeline", exclude_mechanical=True)
    assert "WG Last Call" in out
    assert "No Objection" not in out


def test_read_digest_no_digests_at_all(isolated_home: Path) -> None:
    # Corpus exists (has a files dir) but no digests were generated.
    write_cache_file(isolated_home, "wg", "charter.txt", "x")
    out = mcp_server.tool_read_digest("wg", "threads")
    assert "No digests for wg yet" in out


# --- systematic invalid-input handling -----------------------------------


def _nonexistent_corpus_calls(nx: str):
    """One lambda per wg-taking tool, invoked against a corpus that does
    not exist. New tools should be added here."""
    return {
        "overview": lambda: mcp_server.tool_overview(nx),
        "read_digest": lambda: mcp_server.tool_read_digest(nx, "threads"),
        "search": lambda: mcp_server.tool_search(nx, "q"),
        "list_labels": lambda: mcp_server.tool_list_labels(nx),
        "list_files": lambda: mcp_server.tool_list_files(nx),
        "read_topic": lambda: mcp_server.tool_read_topic(nx, "q"),
        "tally_positions": lambda: mcp_server.tool_tally_positions(nx, "threads/x.md"),
        "find_replies": lambda: mcp_server.tool_find_replies(nx, "threads/x.md", 1),
        "find_citations": lambda: mcp_server.tool_find_citations(nx, "draft-x"),
        "find_message_citations": lambda: mcp_server.tool_find_message_citations(
            nx, "threads/x.md"
        ),
        "get_chunk": lambda: mcp_server.tool_get_chunk(nx, "threads/x.md", 1),
        "get_chunks_batch": lambda: mcp_server.tool_get_chunks_batch(
            nx, [{"file": "threads/x.md", "chunk_idx": 1}]
        ),
        "get_by_url": lambda: mcp_server.tool_get_by_url(
            nx, "https://www.w3.org/mid/x"
        ),
        "read_file_section": lambda: mcp_server.tool_read_file_section(nx, "charter.txt"),
    }


def test_unknown_corpus_rejected_without_side_effects(isolated_home: Path) -> None:
    # Every wg-taking tool must reject a typo'd corpus with a clear message
    # — and must NOT create a junk cache directory just by being queried.
    nx = "typo-corpus-xyz"
    cache_dir = isolated_home / ".cache" / "ietf-llm" / nx
    for name, call in _nonexistent_corpus_calls(nx).items():
        out = call()
        assert f"Unknown corpus '{nx}'" in out, f"{name}: {out[:80]!r}"
        assert not cache_dir.exists(), f"{name} created a cache dir for a typo"


def test_invalid_date_rejected_not_silently_empty(isolated_home: Path) -> None:
    # A malformed or impossible date must fail loudly, not silently match
    # nothing (which reads as "no activity").
    write_cache_file(isolated_home, "wg", "digests/threads.md", "# threads\n")
    for bad in ("not-a-date", "2026-13-40", "05/01/2026"):
        assert "Invalid" in mcp_server.tool_read_digest("wg", "threads", since=bad)
        assert "Invalid" in mcp_server.tool_read_digest("wg", "threads", until=bad)
        assert "Invalid" in mcp_server.tool_search("wg", "q", since=bad)
    # A valid date is not rejected.
    assert "Invalid" not in mcp_server.tool_read_digest(
        "wg", "threads", since="2026-04-01"
    )


def test_search_k_clamped_no_crash(isolated_home: Path) -> None:
    # A huge or negative k must not crash or return an unbounded list.
    write_cache_file(
        isolated_home, "wg", "threads/2026-01-01-t.md",
        "# T\n\n## Messages\n\n### [1] 2026-01-01 09:00 — Alice\n\nbody.\n",
    )
    from test_search_filters import _build_with_stub  # noqa: F401

    _build_with_stub("wg", isolated_home)
    for bad_k in (100000, -5, 0):
        out = mcp_server.tool_search("wg", "anything", k=bad_k)
        assert isinstance(out, str)
        n_hits = sum(1 for ln in out.splitlines() if ln.startswith("[") and "file=" in ln)
        assert n_hits <= 100


def test_collapse_draft_versions() -> None:
    from ietf_llm.mcp_server import _collapse_draft_versions

    class _H:
        def __init__(self, file: str) -> None:
            self.file = file

    hits = [
        _H("drafts/draft-ietf-httpbis-rfc6265bis-04.txt"),
        _H("drafts/draft-ietf-httpbis-rfc6265bis-22.txt"),  # newest → kept
        _H("drafts/draft-ietf-httpbis-rfc6265bis-12.txt"),
        _H("drafts/rfc9110.txt"),  # RFC, no rev → kept
        _H("threads/2026-01-01-x.md"),  # non-draft → kept
        _H("drafts/draft-ietf-httpbis-other-01.txt"),  # different stem → kept
    ]
    kept, dropped = _collapse_draft_versions(hits)
    files = {h.file for h in kept}
    assert "drafts/draft-ietf-httpbis-rfc6265bis-22.txt" in files
    assert "drafts/draft-ietf-httpbis-rfc6265bis-04.txt" not in files
    assert "drafts/draft-ietf-httpbis-rfc6265bis-12.txt" not in files
    assert "drafts/rfc9110.txt" in files
    assert "threads/2026-01-01-x.md" in files
    assert "drafts/draft-ietf-httpbis-other-01.txt" in files
    assert dropped == 2


def test_offload_times_out_slow_calls(monkeypatch: object) -> None:
    # A stuck tool call returns a fast, retryable error rather than
    # hanging to the client's multi-minute ceiling. A fast call is
    # unaffected.
    import time

    import anyio

    monkeypatch.setenv("IETF_LLM_TOOL_TIMEOUT", "0.2")  # type: ignore[attr-defined]

    def fast() -> str:
        return "fast-result"

    def slow() -> str:
        time.sleep(1.0)
        return "unreached"

    async def run(fn: object) -> str:
        return await mcp_server._offload(fn)

    assert anyio.run(run, fast) == "fast-result"
    out = anyio.run(run, slow)
    assert "timed out" in out.lower()


def test_offload_timeout_disabled_by_zero(monkeypatch: object) -> None:
    import anyio

    monkeypatch.setenv("IETF_LLM_TOOL_TIMEOUT", "0")  # type: ignore[attr-defined]

    def quick() -> str:
        return "ran"

    async def run() -> str:
        return await mcp_server._offload(quick)

    assert anyio.run(run) == "ran"


def test_offload_emits_access_record_at_status(
    monkeypatch: object, capsys: object
) -> None:
    # On the HTTP serve path (default STATUS verbosity) every tool call leaves
    # a structured per-request line carrying tool / status / duration_ms.
    import json

    import anyio

    monkeypatch.setenv("IETF_LLM_MCP_TRANSPORT", "http")  # type: ignore[attr-defined]
    monkeypatch.setenv("IETF_LLM_LOG_FORMAT", "json")  # type: ignore[attr-defined]
    monkeypatch.delenv("IETF_LLM_LOG_LEVEL", raising=False)  # type: ignore[attr-defined]

    def tool_overview() -> str:
        return "ok"

    async def run() -> str:
        return await mcp_server._offload(tool_overview)

    assert anyio.run(run) == "ok"
    records = [
        json.loads(line)
        for line in capsys.readouterr().err.splitlines()  # type: ignore[attr-defined]
        if line.strip()
    ]
    access = [r for r in records if r.get("event") == "tool_call"]
    assert len(access) == 1
    assert access[0]["tool"] == "tool_overview"
    assert access[0]["status"] == "ok"
    assert isinstance(access[0]["duration_ms"], (int, float))


def test_offload_access_record_silent_on_stdio(
    monkeypatch: object, capsys: object
) -> None:
    # stdio/local default is QUIET, so an interactive session emits no
    # per-request access line.
    import anyio

    monkeypatch.delenv("IETF_LLM_MCP_TRANSPORT", raising=False)  # type: ignore[attr-defined]
    monkeypatch.delenv("IETF_LLM_LOG_LEVEL", raising=False)  # type: ignore[attr-defined]

    def tool_overview() -> str:
        return "ok"

    async def run() -> str:
        return await mcp_server._offload(tool_overview)

    assert anyio.run(run) == "ok"
    assert "tool_call" not in capsys.readouterr().err  # type: ignore[attr-defined]


def test_collapse_draft_versions_single_rev_kept() -> None:
    from ietf_llm.mcp_server import _collapse_draft_versions

    class _H:
        def __init__(self, file: str) -> None:
            self.file = file

    hits = [_H("drafts/draft-ietf-httpbis-only-03.txt")]
    kept, dropped = _collapse_draft_versions(hits)
    assert len(kept) == 1 and dropped == 0


def test_list_labels_prefix_example_is_templated(isolated_home: Path) -> None:
    # The subject-prefix example must use one of THIS corpus's prefixes,
    # not a hardcoded `[mlkem]` (which is a TLS prefix).
    write_cache_file(
        isolated_home,
        "wg",
        "threads/2026-01-01-x.md",
        "### [1] Re: [foo] hello\n\n_Subject:_ Re: [foo] hello\n\nbody\n",
    )
    out = mcp_server.tool_list_labels("wg")
    assert "[mlkem]" not in out
    if "subject=" in out:
        assert "[foo]" in out


def test_top_level_response_carries_freshness_line_when_fresh(
    isolated_home: Path,
) -> None:
    # _with_freshness now prepends the gather date to every top-level
    # response, not only when the cache is stale.
    from ietf_llm.freshness import record_gather

    write_cache_file(isolated_home, "wg", "digests/people.md", "# people\n")
    record_gather("wg")
    out = mcp_server.tool_read_digest("wg", "people")
    assert "gathered" in out.lower()
    assert "# people" in out


def test_top_level_response_flags_an_in_flight_refresh(
    isolated_home: Path,
) -> None:
    # A re-gather running in this process must not let the read path pass off
    # the prior snapshot as current: the freshness header keeps its (today)
    # stamp but gains a caveat that a refresh is running and the body predates
    # it. Regression guard for the "gathered today (mid-gather)" false-confidence
    # trap.
    from ietf_llm import gather_runner
    from ietf_llm.freshness import record_gather

    write_cache_file(isolated_home, "wg", "digests/people.md", "# people\n")
    record_gather("wg")
    with gather_runner._registry_lock:
        gather_runner._jobs["wg"] = "running"
    try:
        out = mcp_server.tool_read_digest("wg", "people")
    finally:
        with gather_runner._registry_lock:
            gather_runner._jobs.pop("wg", None)
    assert "gathered" in out.lower()  # the stamp is still there...
    assert "refresh is running" in out.lower()  # ...but now flagged as superseded
    assert "gather_status" in out
    # And once no gather is live, the caveat is gone.
    clean = mcp_server.tool_read_digest("wg", "people")
    assert "refresh is running" not in clean.lower()


def test_inflight_note_omits_zero_index_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `stage_index` is 0 until the first progress call; the note must not render
    # a meaningless "stage 0/7". It falls back to the stage name when present,
    # else a bare "in progress".
    from ietf_llm import gather_runner, mcp_server as srv

    monkeypatch.setattr(
        gather_runner,
        "local_inflight",
        lambda wg: {"corpus": wg, "state": "running", "stage_index": 0,
                    "stage_total": 7, "stage": "mailing list"},
    )
    note = srv._inflight_refresh_note("wg")
    assert note is not None
    assert "0/7" not in note
    assert "mailing list" in note

    monkeypatch.setattr(
        gather_runner, "local_inflight",
        lambda wg: {"corpus": wg, "state": "running"},
    )
    bare = srv._inflight_refresh_note("wg")
    assert bare is not None and "in progress" in bare


def test_stage_phrase_renders_or_none() -> None:
    from ietf_llm import mcp_server as srv

    assert srv._stage_phrase(None) is None
    assert srv._stage_phrase({"stage_index": 0, "stage_total": 19}) is None
    assert (
        srv._stage_phrase(
            {"stage_index": 18, "stage_total": 19, "stage": "embedding index"}
        )
        == "stage 18/19 (embedding index)"
    )
    assert srv._stage_phrase({"stage": "mailing list"}) == "stage: mailing list"


def test_first_gather_guard_refuses_when_never_gathered(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A first gather has no prior snapshot, so reads must refuse (naming the
    # stage and how many are left) rather than serve a half-built cache.
    from ietf_llm import gather_runner, mcp_server as srv

    monkeypatch.setattr(
        gather_runner, "local_inflight",
        lambda wg: {"corpus": wg, "state": "running", "stage_index": 5,
                    "stage_total": 19, "stage": "drafts"},
    )
    msg = srv._first_gather_guard("wg")
    assert msg is not None
    assert "first gather" in msg.lower()
    assert "5/19" in msg
    assert "14 stage" in msg  # 19 - 5 still to go
    assert "gather_status" in msg


def test_first_gather_guard_allows_regather_and_idle(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ietf_llm import gather_runner, mcp_server as srv
    from ietf_llm.freshness import record_gather

    # A completed version already exists -> a re-gather keeps serving it.
    record_gather("wg")
    monkeypatch.setattr(
        gather_runner, "local_inflight",
        lambda wg: {"corpus": wg, "state": "running", "stage_index": 5,
                    "stage_total": 19},
    )
    assert srv._first_gather_guard("wg") is None
    # No gather live -> no guard.
    monkeypatch.setattr(gather_runner, "local_inflight", lambda wg: None)
    assert srv._first_gather_guard("wg") is None


def test_timeout_note_names_running_gather(monkeypatch: pytest.MonkeyPatch) -> None:
    from ietf_llm import gather_runner, mcp_server as srv

    monkeypatch.setattr(
        gather_runner, "local_inflight",
        lambda wg: {"corpus": wg, "state": "running", "stage_index": 18,
                    "stage_total": 19, "stage": "embedding index"},
    )
    note = srv._timeout_inflight_note(srv.tool_search, ("wg", "query"))
    assert note is not None
    assert "still running" in note.lower()
    assert "18/19" in note and "embedding index" in note
    assert "not because the server is unresponsive" in note
    # A first arg that is not a valid corpus name (a query, a number) is ignored.
    assert srv._timeout_inflight_note(srv.tool_search, ("a query!", "q")) is None
    # Empty args (a no-corpus tool) -> no note.
    assert srv._timeout_inflight_note(srv.tool_search, ()) is None
    # No gather live -> no note.
    monkeypatch.setattr(gather_runner, "local_inflight", lambda wg: None)
    assert srv._timeout_inflight_note(srv.tool_search, ("wg", "query")) is None


def test_index_rebuilding_note(monkeypatch: pytest.MonkeyPatch) -> None:
    from ietf_llm import gather_runner, mcp_server as srv

    monkeypatch.setattr(
        gather_runner, "local_inflight",
        lambda wg: {"corpus": wg, "state": "running", "stage_index": 18,
                    "stage_total": 19, "stage": "embedding index"},
    )
    monkeypatch.setattr(srv, "probe_index", lambda wg: False)
    note = srv._index_rebuilding_note("wg")
    assert note is not None and "being rebuilt" in note and "18/19" in note
    # A servable index -> the empty result is real, no "not ready" caveat.
    monkeypatch.setattr(srv, "probe_index", lambda wg: True)
    assert srv._index_rebuilding_note("wg") is None
    # No gather live -> no note.
    monkeypatch.setattr(gather_runner, "local_inflight", lambda wg: None)
    assert srv._index_rebuilding_note("wg") is None


def test_top_level_response_silent_when_no_sentinel(isolated_home: Path) -> None:
    # Legacy cache with no sentinel: no freshness line, just the body.
    write_cache_file(isolated_home, "wg", "digests/people.md", "# people\n")
    out = mcp_server.tool_read_digest("wg", "people")
    assert "gathered" not in out.lower()


# --- get_chunk_text digest-file hint + chunk-not-found hints ---------------


def test_get_chunk_on_digest_file_redirects_to_read_digest(
    isolated_home: Path,
) -> None:
    # The consuming-LLM gap: get_chunk_text("aipref-_people.md", 0) used
    # to return an opaque "Chunk not found." Now it should name the
    # right tool to call.
    write_cache_file(isolated_home, "wg", "digests/people.md", "# people\n")
    out = mcp_server.tool_get_chunk("wg", "digests/people.md", 0)
    assert "digest" in out.lower()
    assert "read_digest" in out
    assert "kind='people'" in out


def test_get_chunk_unknown_file_explains_what_to_do(
    isolated_home: Path,
) -> None:
    write_cache_file(isolated_home, "wg", "x.txt", "hi")  # ensure cache exists
    out = mcp_server.tool_get_chunk("wg", "not-indexed.md", 0)
    # No DB / unindexed file → point at list_files or --embed, not silence.
    assert "list_files" in out or "--embed" in out


def test_read_file_section_on_digest_includes_hint(
    isolated_home: Path,
) -> None:
    write_cache_file(isolated_home, "wg", "digests/issues.md", "# issues\n\nbody\n")
    out = mcp_server.tool_read_file_section("wg", "digests/issues.md")
    assert "read_digest" in out
    assert "kind='issues'" in out
    # And it still serves the content (just prefixed with the hint).
    assert "# issues" in out


# --- list_files chunk counts + digest annotation --------------------------


def test_list_files_annotates_digest_files(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "digests/people.md", "x")
    write_cache_file(isolated_home, "wg", "digests/issues.md", "x")
    write_cache_file(isolated_home, "wg", "other.txt", "x")
    out = mcp_server.tool_list_files("wg")
    # Digest files should be flagged + redirect to read_digest.
    assert "digests/people.md" in out
    assert "read_digest" in out
    assert "kind='people'" in out
    assert "kind='issues'" in out


def test_list_files_distinguishes_not_indexed_from_no_chunks(
    isolated_home: Path,
) -> None:
    # Without an embedding index, files should show as "(not indexed)"
    # rather than "(no chunks)" — the latter implied the file was
    # genuinely empty when really the index just hadn't been built yet.
    write_cache_file(isolated_home, "wg", "drafts/draft-foo.txt", "body")
    out = mcp_server.tool_list_files("wg")
    assert "(not indexed)" in out
    assert "(no chunks)" not in out


def test_list_files_pattern_filter(isolated_home: Path) -> None:
    # The full inventory dump is unwieldy on large WGs; a glob filter
    # lets the consumer ask for just the slice they care about
    # without scrolling 600 lines of file names.
    write_cache_file(
        isolated_home, "wg", "threads/2026-04-01-mlkem-debate.md", "x",
    )
    write_cache_file(
        isolated_home, "wg", "threads/2026-04-02-mlkem-followup.md", "x",
    )
    write_cache_file(
        isolated_home, "wg", "threads/2026-05-01-unrelated.md", "x",
    )
    out = mcp_server.tool_list_files("wg", pattern="threads/*mlkem*")
    assert "mlkem-debate" in out
    assert "mlkem-followup" in out
    assert "unrelated" not in out


def test_list_files_pattern_no_match_gives_helpful_message(
    isolated_home: Path,
) -> None:
    write_cache_file(isolated_home, "wg", "x.txt", "x")
    out = mcp_server.tool_list_files("wg", pattern="threads/*mlkem*")
    assert "no files match" in out.lower()


# --- get_chunk_text range fetch ------------------------------------------


def test_get_chunk_range_rejects_inverted_bounds(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "x.txt", "hi")
    out = mcp_server.tool_get_chunk("wg", "x.txt", 5, end_chunk_idx=2)
    assert "less than" in out


def test_get_chunk_range_caps_size(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "x.txt", "hi")
    out = mcp_server.tool_get_chunk("wg", "x.txt", 0, end_chunk_idx=999)
    assert "max per call" in out


# --- freshness banner on top-level tools ----------------------------------


def _make_stale(wg: str, days: int) -> None:
    """Drop a backdated sentinel so staleness_warning fires."""
    from datetime import datetime, timedelta, timezone

    from ietf_llm.freshness import _sentinel_path

    when = datetime.now(timezone.utc) - timedelta(days=days)
    path = Path(_sentinel_path(wg))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(when.strftime("%Y-%m-%dT%H:%M:%SZ"))


def test_overview_prepends_staleness_warning_when_stale(
    isolated_home: Path,
) -> None:
    write_cache_file(isolated_home, "wg", "digests/index.md", "# wg index\n")
    _make_stale("wg", days=30)
    out = mcp_server.tool_overview("wg")
    assert out.startswith("⚠")
    assert "30 days ago" in out
    # And the actual overview body still follows it.
    assert "overview" in out.lower()


def test_overview_omits_banner_when_fresh(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "digests/index.md", "# wg index\n")
    from ietf_llm.freshness import record_gather

    record_gather("wg")
    out = mcp_server.tool_overview("wg")
    assert not out.startswith("⚠")


def test_read_digest_prepends_staleness_warning(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "digests/people.md", "# people\n")
    _make_stale("wg", days=14)
    out = mcp_server.tool_read_digest("wg", "people")
    assert out.startswith("⚠")
    assert "14 days ago" in out


def test_list_files_prepends_staleness_warning(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "anything.txt", "hi")
    _make_stale("wg", days=10)
    out = mcp_server.tool_list_files("wg")
    assert out.startswith("⚠")


# --- read_digest include_bodies (consumer feedback) ---------------------


def test_read_digest_include_bodies_appends_issue_files(
    isolated_home: Path,
) -> None:
    # The single biggest workflow win from the latest consumer trace:
    # include_bodies=True lets a `label="X"` query return the catalogue
    # AND the issues' opening descriptions in ONE call, replacing N
    # follow-up read_file_section calls.
    write_cache_file(
        isolated_home, "wg", "digests/issues.md",
        (
            "# wg: issues\n\n## org/repo\n\n"
            "| # | State | Title | Labels | Comments | Updated | "
            "Author | Participants | Dup-of | File |\n"
            "|---|-------|-------|--------|----------|---------|"
            "--------|--------------|--------|------|\n"
            "| 1 | OPEN | T1 | top-level | 0 | 2026-05-01 | "
            "Alice | | | `issues/org-repo/1.md` |\n"
            "| 2 | OPEN | T2 | top-level | 0 | 2026-05-02 | "
            "Bob | | | `issues/org-repo/2.md` |\n"
        ),
    )
    write_cache_file(
        isolated_home, "wg", "issues/org-repo/1.md",
        (
            "# Issue #1: T1\n\n"
            "**State:** OPEN  \n\n"
            "## Description\n\n"
            "### [1] 2026-05-01 10:00 — Alice _(opened issue)_\n\n"
            "Opening argument body.\n\n"
            "## Comments\n\n"
            "### [2] 2026-05-02 10:00 — Bob\n\n"
            "Some long comment thread we DON'T want pulled in.\n"
        ),
    )
    write_cache_file(
        isolated_home, "wg", "issues/org-repo/2.md",
        (
            "# Issue #2: T2\n\n"
            "**State:** OPEN  \n\n"
            "## Description\n\n"
            "### [1] 2026-05-02 10:00 — Bob _(opened issue)_\n\n"
            "Different argument.\n"
        ),
    )
    out = mcp_server.tool_read_digest(
        "wg", "issues", label="top-level", include_bodies=True,
    )
    # Both bodies appear under an "Issue bodies" section.
    assert "## Issue bodies" in out
    assert "Opening argument body." in out
    assert "Different argument." in out
    # Comment threads are deliberately NOT pulled in (size discipline).
    assert "DON'T want pulled in" not in out


def test_read_digest_include_bodies_only_for_issues_kind(
    isolated_home: Path,
) -> None:
    # `include_bodies` is a no-op on non-issues kinds — it doesn't
    # error, it just doesn't add a Bodies section.
    write_cache_file(
        isolated_home, "wg", "digests/threads.md", "# threads\n\nbody\n",
    )
    out = mcp_server.tool_read_digest(
        "wg", "threads", include_bodies=True,
    )
    assert "Issue bodies" not in out


def test_read_digest_without_include_bodies_unchanged(
    isolated_home: Path,
) -> None:
    # Default behaviour (include_bodies=False) is unchanged.
    write_cache_file(
        isolated_home, "wg", "digests/issues.md",
        "# wg: issues\n\n## org/repo\n\n"
        "| # | State | Title | File |\n|---|-------|-------|------|\n"
        "| 1 | OPEN | T | `issues/org-repo/1.md` |\n",
    )
    out = mcp_server.tool_read_digest("wg", "issues")
    assert "Issue bodies" not in out


# --- Next-step pointers on discovery tools ------------------------------


def test_list_labels_includes_next_call_signatures(
    isolated_home: Path,
) -> None:
    # Consumer feedback: discovery tools (list_labels in particular)
    # are useless without their companion tools, but the harness
    # lazy-loads. Embedding concrete next-call signatures in the
    # output gives the consuming LLM the names it needs for the next
    # tool_search query.
    write_cache_file(
        isolated_home, "wg", "digests/issues.md",
        (
            "# wg: issues\n\n## org/repo\n\n"
            "| # | State | Title | Labels | Comments | Updated | Author |\n"
            "|---|-------|-------|--------|----------|---------|--------|\n"
            "| 1 | OPEN | A | top-level | 1 | 2026-05-14 | Alice |\n"
        ),
    )
    out = mcp_server.tool_list_labels("wg")
    assert "read_digest" in out
    assert "include_bodies=True" in out  # the recommended new shape
    assert "search_corpus" in out


def test_list_files_includes_next_call_signatures(
    isolated_home: Path,
) -> None:
    write_cache_file(isolated_home, "wg", "anything.txt", "hi")
    out = mcp_server.tool_list_files("wg")
    assert "read_file_section" in out
    assert "get_chunk_text" in out


def test_list_corpora_includes_next_call_signatures(
    isolated_home: Path,
) -> None:
    write_cache_file(isolated_home, "wg", "x.txt", "hi")
    out = mcp_server.tool_list_corpora()
    assert "overview" in out
    assert "read_digest" in out
    assert "search_corpus" in out


# --- list_labels ----------------------------------------------------------


def test_list_labels_returns_frequencies(isolated_home: Path) -> None:
    # Consumer feedback: there was no list_labels tool, so the consumer
    # had to guess label="top-level". Expose the same data the overview
    # samples, but unbounded and as a dedicated tool.
    write_cache_file(
        isolated_home, "wg", "digests/issues.md",
        (
            "# wg: issues\n\n## org/repo\n\n"
            "| # | State | Title | Labels | Comments | Updated | Author |\n"
            "|---|-------|-------|--------|----------|---------|--------|\n"
            "| 1 | OPEN | A | top-level | 1 | 2026-05-14 | Alice |\n"
            "| 2 | OPEN | B | top-level, vocab | 1 | 2026-05-13 | Bob |\n"
            "| 3 | OPEN | C | vocab | 1 | 2026-05-12 | Carol |\n"
        ),
    )
    out = mcp_server.tool_list_labels("wg")
    # Both labels are listed with their counts.
    assert "`top-level` | 2" in out
    assert "`vocab` | 2" in out
    # Header records the total distinct count.
    assert "(2 distinct)" in out


def test_list_labels_handles_no_vocabulary(isolated_home: Path) -> None:
    # No issues digest AND no threads dir → friendly empty response,
    # not a crash. (The "no vocabulary" wording replaced the older
    # "no labels recorded" when the tool grew its subject-prefix
    # section.)
    write_cache_file(isolated_home, "wg", "digests/index.md", "# index\n")
    out = mcp_server.tool_list_labels("wg")
    assert "No curation vocabulary" in out


# --- GitHub URL surfacing on search hits ---------------------------------


def test_no_banner_when_sentinel_absent(isolated_home: Path) -> None:
    # Cache exists but freshness sentinel doesn't (legacy / pre-feature).
    # Per design we stay silent, not nag.
    write_cache_file(isolated_home, "wg", "digests/index.md", "# wg\n")
    out = mcp_server.tool_overview("wg")
    assert not out.startswith("⚠")


# --- get_by_url (consumer feedback #9) ---------------------------------


def test_get_by_url_returns_chunk_when_url_matches(
    isolated_home: Path,
) -> None:
    # Seed the chunks DB the long way: write a per-issue file with a
    # **URL:** line, then build the embedding index against it. The
    # chunker stamps the file-level URL onto every chunk; get_by_url
    # rounds-trips through that column.
    write_cache_file(
        isolated_home, "wg", "issues/org-repo/1.md",
        (
            "# Issue #1: T\n\n"
            "**Repository:** org/repo  \n"
            "**URL:** https://github.com/org/repo/issues/1  \n"
            "**State:** OPEN  \n\n"
            "## Description\n\n"
            "### [1] 2026-01-01 10:00 — Alice _(opened issue)_\n\n"
            "the actual chunk body here.\n"
        ),
    )
    # Build the index via the stub-model fixture used by search tests.
    from test_search_filters import _build_with_stub  # noqa: F401

    _build_with_stub("wg", isolated_home)
    out = mcp_server.tool_get_by_url(
        "wg", "https://github.com/org/repo/issues/1",
    )
    assert "the actual chunk body here" in out
    # Header carries enough metadata for the caller to pivot.
    assert "issues/org-repo/1.md" in out


def test_get_by_url_resolves_draft_datatracker_url(
    isolated_home: Path,
) -> None:
    # A draft's windowed chunks are stamped with the version-agnostic
    # Datatracker doc URL; get_by_url resolves that back to the draft.
    write_cache_file(
        isolated_home, "wg", "drafts/draft-ietf-wg-thing-02.txt",
        "Internet-Draft  Thing  March 2026\n\n"
        + "\n".join(f"draft body line {i}" for i in range(1, 120))
        + "\n",
    )
    from test_search_filters import _build_with_stub  # noqa: F401

    _build_with_stub("wg", isolated_home)
    out = mcp_server.tool_get_by_url(
        "wg", "https://datatracker.ietf.org/doc/draft-ietf-wg-thing/",
    )
    assert "draft body line" in out
    assert "drafts/draft-ietf-wg-thing-02.txt" in out


def test_get_by_url_resolves_w3_mid_archived_at(isolated_home: Path) -> None:
    # The Archived-At permalink stamped on every thread message is a
    # `www.w3.org/mid/<message-id>` URL — the form get_by_url must
    # resolve (not `mailarchive.ietf.org/...`).
    mid = "https://www.w3.org/mid/test-msgid-123@example.com"
    write_cache_file(
        isolated_home, "wg", "threads/2026-01-01-t.md",
        (
            "# Thread\n\n## Messages\n\n"
            "### [1] 2026-01-01 10:00 — Alice\n\n"
            "_Subject:_ Hello\n"
            f"_Archived-At:_ {mid}\n\n"
            "the message body to resolve.\n"
        ),
    )
    from test_search_filters import _build_with_stub  # noqa: F401

    _build_with_stub("wg", isolated_home)
    out = mcp_server.tool_get_by_url("wg", mid)
    assert "the message body to resolve" in out


def test_get_by_url_normalises_body_footnote_spelling(
    isolated_home: Path,
) -> None:
    # Regression: the Archived-At line is stored without a trailing slash,
    # but a message body cites the mailman permalink *with* one (and a mail
    # client may also vary scheme / www). Those incidental differences must
    # not make a gathered message read as "not in the corpus".
    stored = "https://mailarchive.ietf.org/arch/msg/wg/AbC123_tok"
    write_cache_file(
        isolated_home, "wg", "threads/2026-01-01-t.md",
        (
            "# Thread\n\n## Messages\n\n"
            "### [1] 2026-01-01 10:00 — Alice\n\n"
            "_Subject:_ Hello\n"
            f"_Archived-At:_ {stored}\n\n"
            "the footnote target body.\n"
        ),
    )
    from test_search_filters import _build_with_stub  # noqa: F401

    _build_with_stub("wg", isolated_home)
    # Each is the same message spelled differently from what's stored.
    for variant in (
        stored + "/",  # trailing slash (the reported failure)
        stored.replace("https://", "http://"),  # scheme
        stored.replace("mailarchive", "www.mailarchive"),  # leading www.
        f"<{stored}>",  # angle-bracket wrapped
        stored + "#anchor",  # trailing fragment
    ):
        out = mcp_server.tool_get_by_url("wg", variant)
        assert "the footnote target body" in out, variant


def test_find_chunks_by_url_tolerates_legacy_schemas(isolated_home: Path) -> None:
    # A read-only open can't migrate, so find_chunks_by_url must not crash
    # on an index that predates the `sub_idx` (v8) or `url` (v6) columns.
    # The UNIQUE(...sub_idx) constraint blocks DROP COLUMN, so hand-seed
    # minimal legacy tables that exercise each guard directly.
    import os
    import sqlite3

    from ietf_llm.embeddings import storage

    url = "https://mailarchive.ietf.org/arch/msg/wg/tok"

    def _seed(*, with_url: bool, with_sub_idx: bool) -> None:
        path = storage._db_path("wg")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            os.remove(path)
        cols = ["id INTEGER PRIMARY KEY", "file TEXT", "chunk_idx INTEGER"]
        fields = ["file", "chunk_idx"]
        values: list = ["threads/t.md", 1]
        if with_sub_idx:
            cols.append("sub_idx INTEGER DEFAULT 0")
            fields.append("sub_idx")
            values.append(0)
        cols += ["title TEXT", "text TEXT", "start_line INTEGER", "end_line INTEGER"]
        fields += ["title", "text", "start_line", "end_line"]
        values += ["T", "body", 1, 9]
        if with_url:
            cols.append("url TEXT")
            fields.append("url")
            values.append(url)
        conn = sqlite3.connect(path)
        conn.execute(f"CREATE TABLE chunks ({', '.join(cols)})")
        placeholders = ",".join("?" * len(values))
        conn.execute(
            f"INSERT INTO chunks ({', '.join(fields)}) VALUES ({placeholders})",
            values,
        )
        conn.commit()
        conn.close()

    # pre-v8: url present, sub_idx absent → still resolves (clause skipped),
    # and a trailing-slash variant normalises too.
    _seed(with_url=True, with_sub_idx=False)
    assert storage.find_chunks_by_url("wg", url + "/")

    # pre-v6: no url column at all → graceful empty, not OperationalError.
    _seed(with_url=False, with_sub_idx=False)
    assert storage.find_chunks_by_url("wg", url) == []


def test_get_by_url_returns_helpful_miss_message(
    isolated_home: Path,
) -> None:
    # Unknown URL → message that explains the supported forms, not silent
    # None — and names w3.org/mid so a consumer is not sent to mailarchive.
    write_cache_file(isolated_home, "wg", "x.txt", "hi")
    out = mcp_server.tool_get_by_url("wg", "https://mailarchive.ietf.org/arch/msg/x/y/")
    assert "No cached chunk" in out
    assert "w3.org/mid" in out
    assert "ietf-llm wg" in out  # the recovery hint


# --- get_chunks_batch (cross-file batch reads) ---------------------------


def test_get_chunks_batch_concatenates_multiple_files(
    isolated_home: Path,
) -> None:
    # Seed two per-issue files; verify a single batch call returns
    # chunks from both, separated by per-file headers.
    write_cache_file(
        isolated_home, "wg", "issues/org-repo/1.md",
        "# Issue #1: T1\n\n## Description\n\n"
        "### [1] 2026-01-01 10:00 — Alice _(opened issue)_\n\n"
        "first issue body.\n",
    )
    write_cache_file(
        isolated_home, "wg", "issues/org-repo/2.md",
        "# Issue #2: T2\n\n## Description\n\n"
        "### [1] 2026-01-02 10:00 — Bob _(opened issue)_\n\n"
        "second issue body.\n",
    )
    from test_search_filters import _build_with_stub  # noqa: F401

    _build_with_stub("wg", isolated_home)
    out = mcp_server.tool_get_chunks_batch("wg", [
        {"file": "issues/org-repo/1.md", "chunk_idx": 1},
        {"file": "issues/org-repo/2.md", "chunk_idx": 1},
    ])
    assert "first issue body" in out
    assert "second issue body" in out
    # Per-file headers so the consumer can tell hits apart.
    assert "issues/org-repo/1.md @ chunk 1" in out
    assert "issues/org-repo/2.md @ chunk 1" in out


def test_get_chunks_batch_caps_total_chunks(isolated_home: Path) -> None:
    # 21 chunks across requests exceeds the cap. The error message
    # names the cap so the consumer can split sensibly.
    write_cache_file(isolated_home, "wg", "x.txt", "hi")
    out = mcp_server.tool_get_chunks_batch("wg", [
        {"file": "f.md", "chunk_idx": 0, "end_chunk_idx": 20},
    ])
    assert "max per call is 20" in out


def test_get_chunks_batch_rejects_inverted_range(
    isolated_home: Path,
) -> None:
    write_cache_file(isolated_home, "wg", "x.txt", "hi")
    out = mcp_server.tool_get_chunks_batch("wg", [
        {"file": "f.md", "chunk_idx": 5, "end_chunk_idx": 2},
    ])
    assert "must be >= chunk_idx" in out


def test_get_chunks_batch_rejects_non_numeric_index(
    isolated_home: Path,
) -> None:
    # A non-numeric chunk_idx must return a clean message, not raise an opaque
    # protocol error out of the worker thread.
    write_cache_file(isolated_home, "wg", "x.txt", "hi")
    out = mcp_server.tool_get_chunks_batch("wg", [
        {"file": "f.md", "chunk_idx": "first"},
    ])
    assert "must be integers" in out
    # A non-dict entry is also handled gracefully.
    out2 = mcp_server.tool_get_chunks_batch("wg", ["not-a-dict"])
    assert "must be an object" in out2


def test_descendants_is_cycle_safe() -> None:
    from ietf_llm.mcp_server import _descendants  # pylint: disable=import-outside-toplevel

    # A malformed self-reply (graph[5] = [5]) must not list the root as its own
    # descendant, and a cycle must not loop forever.
    assert _descendants({5: [5]}, 5) == []
    assert sorted(_descendants({1: [2], 2: [1]}, 1)) == [2]
    # Normal trees still resolve in BFS order.
    assert _descendants({1: [2, 3], 2: [4]}, 1) == [2, 3, 4]


def test_get_chunks_batch_tolerates_single_dict_input(
    isolated_home: Path,
) -> None:
    # Defensive: if the MCP serialiser passes a lone dict instead of
    # a 1-element list, treat as length-1 batch rather than erroring.
    write_cache_file(
        isolated_home, "wg", "issues/org-repo/1.md",
        "# Issue #1: T\n\n## Description\n\n"
        "### [1] 2026-01-01 10:00 — Alice _(opened issue)_\n\nbody\n",
    )
    from test_search_filters import _build_with_stub  # noqa: F401

    _build_with_stub("wg", isolated_home)
    out = mcp_server.tool_get_chunks_batch(
        "wg",
        {"file": "issues/org-repo/1.md", "chunk_idx": 1},  # type: ignore[arg-type]
    )
    assert "body" in out


def test_read_file_window_past_eof(tmp_path) -> None:
    # Paging past the end returns a clear message, not a backwards "20-10 of 10"
    # header with an empty body.
    path = tmp_path / "f.txt"
    path.write_text("a\nb\nc\n", encoding="utf-8")
    out = mcp_server._read_file_window(str(path), start_line=20, max_lines=100)
    assert "past the end" in out and "3 lines" in out
    assert "20–10" not in out


# --- _load_server_instructions --------------------------------------------


def test_load_server_instructions_is_markdown_heading() -> None:
    # The instructions floor is a package-owned markdown string (no YAML
    # frontmatter): it opens with a heading and carries no `---` sentinel.
    out = mcp_server._load_server_instructions()  # pylint: disable=protected-access
    assert out is not None
    assert out.lstrip().startswith("#")
    assert "---" not in out.splitlines()[0]


def test_load_server_instructions_states_session_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The {{SESSION}} placeholder must be substituted with the mode-specific
    # facts (never served raw), so a client reads its deployment/capability
    # instead of inferring it.
    from ietf_llm import freshness

    monkeypatch.setattr(freshness, "_DEPLOYMENT_MODE", "stdio")
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", "1")
    out = mcp_server._load_server_instructions()  # pylint: disable=protected-access
    assert "{{SESSION}}" not in out  # placeholder resolved
    assert "## This session" in out
    assert "single-user stdio server" in out.lower()
    assert "available here" in out.lower()


def test_load_server_instructions_prepends_session_when_marker_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The fallback: if the bundled markdown ever loses the {{SESSION}} marker,
    # the mode-specific block must still appear (prepended), not silently vanish.
    # Drive it by stubbing the markdown to marker-less text.
    monkeypatch.setattr(
        mcp_server, "_strip_frontmatter", lambda _t: "# Routing\n\nbody, no marker"
    )
    out = mcp_server._load_server_instructions()  # pylint: disable=protected-access
    assert "{{SESSION}}" not in out
    assert "Fixed for this server's lifetime" in out  # the session section
    # Prepended, not appended, so it leads.
    assert out.index("Fixed for this server") < out.index("body, no marker")


def test_load_server_instructions_includes_load_bearing_content() -> None:
    # Spot-check that the routing rules the skill carries actually land
    # in the instructions string. If this regresses, the non-Claude
    # harnesses lose the guidance.
    out = mcp_server._load_server_instructions()  # pylint: disable=protected-access
    assert out is not None
    # Routing rules.
    assert "overview" in out
    assert "read_digest" in out
    assert "search_corpus" in out
    # IETF norms live in bundled docs, fetched on demand — the
    # instructions must point at the tools that return them (both the
    # reading side and the write/contribute side).
    assert "read_ietf_interpretation_norms" in out
    assert "read_ietf_participation_norms" in out


def test_read_ietf_interpretation_norms_returns_bundled_doc() -> None:
    # The interpretive norms (consensus, list-vs-meeting, attribution) are
    # the body of the `ietf-interpreting` skill, exposed via this tool
    # (frontmatter stripped). The load-bearing phrases must survive so
    # callers that pull the doc get the same guidance.
    out = mcp_server.tool_read_interpretation_norms()
    assert "chair-declared" in out
    assert "aggregate, don't attribute" in out
    assert "Unadopted drafts have no IETF status" in out
    assert "Independent Stream" in out
    # The skill's YAML frontmatter must not leak to MCP clients.
    assert not out.lstrip().startswith("---")
    assert "description:" not in out
    # These skills install into harnesses where the old norms files don't
    # exist — cross-refs must point at the sibling skill, not IETF.md /
    # PARTICIPATING.md.
    assert "IETF.md" not in out
    assert "PARTICIPATING.md" not in out


def test_read_ietf_participation_norms_returns_bundled_doc() -> None:
    # The participation norms (write side) are the body of the
    # `ietf-contributing` skill, exposed via this tool. Guard the
    # load-bearing phrases so the contribute-side guidance survives edits.
    out = mcp_server.tool_read_participation_norms()
    assert "You draft; the human sends" in out
    assert "Say it's AI-generated" in out
    assert "Engage with the group's existing work" in out
    # The skill's YAML frontmatter must not leak to MCP clients.
    assert not out.lstrip().startswith("---")
    assert "description:" not in out
    assert "IETF.md" not in out
    assert "PARTICIPATING.md" not in out
    assert "Where AI help is uncontroversial" in out
