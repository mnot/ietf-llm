"""Tests for the background gather runner (ietf_llm.gather_runner) and the
writer-side guard that `_gather_one`'s inline stage sequence matches
`stage_plan` for each corpus shape.

The runner spawns a daemon thread that calls `__main__.run_gather`; tests
stub that out so no network/pipeline runs, and poll the on-disk status file
the runner writes under the sandboxed HOME.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Tuple

import pytest

from ietf_llm import __main__ as main_mod
from ietf_llm import config, freshness, gather_runner, utils
from ietf_llm.gather_stages import stage_plan
from ietf_llm.utils import Verbosity, get_wg_file_cache_dir


# --- to_argv suppression flags --------------------------------------------


def test_to_argv_always_suppresses_raw_and_pdf() -> None:
    # An MCP gather is the only caller of to_argv; it never wants the local
    # grep/NotebookLM dumps or the slide .pdf sources.
    argv = gather_runner.GatherSpec(corpus="httpbis").to_argv()
    assert "--no-raw" in argv
    assert "--no-pdf" in argv


# --- writer-side drift guard ----------------------------------------------


def _stub_pipeline(
    monkeypatch: pytest.MonkeyPatch, shape: Tuple[bool, bool]
) -> None:
    """No-op every gather worker so `_gather_one` runs its stage skeleton
    only, and force the resolved shape."""
    monkeypatch.setattr(main_mod, "_resolve_corpus_shape", lambda a, p, v: shape)
    for name in (
        "process_charter", "write_group_info", "sync_mailing_list",
        "process_transcripts", "enrich_transcripts", "process_documents",
        "process_extra_drafts", "_gather_dynamic_drafts", "extract_all_pdfs",
        "process_github_issues", "write_issue_files", "write_thread_files",
        "write_citations_digest", "_gather_mentioned_drafts",
        "write_people_digest", "write_timeline_digest", "generate_digests",
        "build_index", "record_gather",
    ):
        monkeypatch.setattr(main_mod, name, lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "process_meetings", lambda *a, **k: [])
    monkeypatch.setattr(main_mod, "download_github_archives", lambda *a, **k: [])
    monkeypatch.setattr(main_mod, "build_registry", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "scan_citations", lambda *a, **k: {})
    monkeypatch.setattr(main_mod, "validate_draft_names", lambda names, v: list(names))
    monkeypatch.setattr(main_mod, "validate_list_names", lambda names, v: list(names))
    monkeypatch.setattr(
        main_mod, "validate_github_repos", lambda names, verbose=None: list(names)
    )


def _emitted(args: Any) -> List[str]:
    seen: List[str] = []
    main_mod._gather_one(
        args, Verbosity.QUIET, progress=lambda n, i, t, d: seen.append(n)
    )
    return seen


def test_stored_zero_months_degrades_without_force(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A prior forced `--months 0` persists 0; an unforced refresh must not
    # silently inherit all-history — it degrades to the default window.
    _stub_pipeline(monkeypatch, (False, True))
    config.save("myorg", main_mod.SCOPE, {"months": 0})
    args = main_mod.build_parser().parse_args(["myorg"])
    main_mod._gather_one(args, Verbosity.QUIET)
    assert args.months == utils.DEFAULT_MONTHS


def test_stored_zero_months_kept_with_force(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pipeline(monkeypatch, (False, True))
    config.save("myorg", main_mod.SCOPE, {"months": 0})
    args = main_mod.build_parser().parse_args(["myorg", "--force"])
    main_mod._gather_one(args, Verbosity.QUIET)
    assert args.months == 0  # all-history honoured for this run


def test_emitted_stages_match_plan_group(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pipeline(monkeypatch, (False, True))
    args = main_mod.build_parser().parse_args(["myorg"])
    assert _emitted(args) == stage_plan(args, group_backed=True)


def test_emitted_stages_match_plan_custom(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pipeline(monkeypatch, (False, False))
    args = main_mod.build_parser().parse_args(["mylist"])
    assert _emitted(args) == stage_plan(args, group_backed=False)


def test_emitted_stages_match_plan_custom_with_sources(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pipeline(monkeypatch, (False, False))
    args = main_mod.build_parser().parse_args(
        ["myc", "--github", "o/r", "--draft", "draft-x", "--no-embed"]
    )
    emitted = _emitted(args)
    assert emitted == stage_plan(args, group_backed=False)
    assert "github archives" in emitted and "github issues" in emitted
    assert "drafts" in emitted
    assert "embedding index" not in emitted


# --- suppression threading: flags + cloud auto-trip -----------------------


def _capture_suppress_pdf(
    monkeypatch: pytest.MonkeyPatch, shape: Tuple[bool, bool]
) -> dict:
    """Stub the pipeline and capture the suppress_pdf boolean extract_all_pdfs
    receives, so we can assert the flag/cloud plumbing."""
    _stub_pipeline(monkeypatch, shape)
    seen: dict = {}

    def grab_pdf(*_a: Any, **k: Any) -> list:
        seen["pdf"] = k.get("suppress_pdf")
        return []

    monkeypatch.setattr(main_mod, "extract_all_pdfs", grab_pdf)
    return seen


def test_suppress_pdf_off_by_default_local(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_mod.service_config, "store_backend", lambda: "local")
    seen = _capture_suppress_pdf(monkeypatch, (False, True))
    args = main_mod.build_parser().parse_args(["myorg"])
    main_mod._gather_one(args, Verbosity.QUIET)
    assert seen == {"pdf": False}


def test_suppress_pdf_follows_cli_flag(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_mod.service_config, "store_backend", lambda: "local")
    seen = _capture_suppress_pdf(monkeypatch, (False, True))
    args = main_mod.build_parser().parse_args(["myorg", "--no-pdf"])
    main_mod._gather_one(args, Verbosity.QUIET)
    assert seen == {"pdf": True}


def test_cloud_backend_trips_pdf_without_flag(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A CLI gather against a cloud backend suppresses even with no flags.
    monkeypatch.setattr(main_mod.service_config, "store_backend", lambda: "cloud")
    seen = _capture_suppress_pdf(monkeypatch, (False, True))
    args = main_mod.build_parser().parse_args(["myorg"])
    main_mod._gather_one(args, Verbosity.QUIET)
    assert seen == {"pdf": True}


def test_gather_one_returns_false_on_unusable_name(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_mod, "_resolve_corpus_shape", lambda a, p, v: None)
    args = main_mod.build_parser().parse_args(["typo"])
    assert main_mod._gather_one(args, Verbosity.QUIET) is False


# --- runner ---------------------------------------------------------------


def _wait_terminal(corpus: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = gather_runner.read_status(corpus)
        if status and status.get("state") in ("done", "failed", "cancelled"):
            return status
        time.sleep(0.01)
    raise AssertionError(f"gather for {corpus} did not finish in {timeout}s")


def test_start_runs_to_done_with_progress(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(argv: List[str], verbosity: Any, progress: Any = None, note_fn: Any = None) -> bool:
        progress("mailing list", 1, 2)
        progress("digests", 2, 2)
        return True

    monkeypatch.setattr(main_mod, "run_gather", fake_run)
    result = gather_runner.start(gather_runner.GatherSpec(corpus="tls"))
    assert result["started"] is True
    status = _wait_terminal("tls")
    assert status["state"] == "done"
    assert status["stage"] == "digests"
    assert status["stage_index"] == 2 and status["stage_total"] == 2
    assert status["started"] and status["finished"] and status["error"] is None


def test_progress_detail_lands_in_status(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A mid-stage detail call (4th arg) must surface in the published status so
    # gather_status can show a long stage moving.
    def fake_run(
        argv: List[str], verbosity: Any, progress: Any = None, note_fn: Any = None
    ) -> bool:
        progress("mailing list", 1, 2)
        progress("mailing list", 1, 2, "ietf-http-wg: 5/10 messages downloaded")
        return True

    monkeypatch.setattr(main_mod, "run_gather", fake_run)
    gather_runner.start(gather_runner.GatherSpec(corpus="tls"))
    status = _wait_terminal("tls")
    assert status["stage"] == "mailing list"
    assert status["stage_detail"] == "ietf-http-wg: 5/10 messages downloaded"


def test_pipeline_notes_land_in_status(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(
        argv: List[str], verbosity: Any, progress: Any = None, note_fn: Any = None
    ) -> bool:
        note_fn("Auto-tracked 1 GitHub repo(s): httpwg/http-extensions.")
        return True

    monkeypatch.setattr(main_mod, "run_gather", fake_run)
    gather_runner.start(gather_runner.GatherSpec(corpus="httpbis"))
    status = _wait_terminal("httpbis")
    assert status["notes"] == ["Auto-tracked 1 GitHub repo(s): httpwg/http-extensions."]


def test_start_returns_a_cancel_token(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_mod, "run_gather", lambda *a, **k: True)
    result = gather_runner.start(gather_runner.GatherSpec(corpus="tls"))
    assert isinstance(result.get("cancel_token"), str) and result["cancel_token"]
    _wait_terminal("tls")


def test_request_stop_cancels_running_gather(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    proceed = threading.Event()

    def fake_run(
        argv: List[str], verbosity: Any, progress: Any = None, note_fn: Any = None
    ) -> bool:
        progress("mailing list", 1, 2)  # running; first cancel poll (none yet)
        started.set()
        proceed.wait(timeout=5.0)
        progress("digests", 2, 2)  # stage transition: polls, sees stop, raises
        return True

    monkeypatch.setattr(main_mod, "run_gather", fake_run)
    result = gather_runner.start(gather_runner.GatherSpec(corpus="tls"))
    token = result["cancel_token"]
    assert started.wait(timeout=5.0)
    stop_result = gather_runner.request_stop("tls", token)
    assert stop_result["stopped"] is True
    proceed.set()
    status = _wait_terminal("tls")
    assert status["state"] == "cancelled"
    assert status["cancel_requested"] is True


def test_request_stop_rejects_bad_token(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = _blocking_gather(monkeypatch)
    gather_runner.start(gather_runner.GatherSpec(corpus="httpbis"))
    try:
        # The token hash is set at enqueue, so a wrong token is refused even
        # before the worker reaches "running".
        result = gather_runner.request_stop("httpbis", "not-the-token")
        assert result["stopped"] is False
        assert result["reason"] == "bad token"
    finally:
        release.set()
    assert _wait_terminal("httpbis")["state"] == "done"  # not cancelled


def test_request_stop_no_active_gather(isolated_home: Path) -> None:
    result = gather_runner.request_stop("ghost", "tok")
    assert result["stopped"] is False
    assert result["reason"] == "not running"


def test_start_records_failed_on_unusable_name(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_mod, "run_gather", lambda *a, **k: False)
    gather_runner.start(gather_runner.GatherSpec(corpus="typo"))
    status = _wait_terminal("typo")
    assert status["state"] == "failed"
    assert "not a recognized" in status["error"]


def test_start_records_failed_on_exception(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_k: Any) -> bool:
        raise RuntimeError("network down")

    monkeypatch.setattr(main_mod, "run_gather", boom)
    gather_runner.start(gather_runner.GatherSpec(corpus="quic"))
    status = _wait_terminal("quic")
    assert status["state"] == "failed"
    assert "RuntimeError: network down" in status["error"]


def test_second_start_reports_already_running(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()

    def blocking_run(argv: List[str], verbosity: Any, progress: Any = None, note_fn: Any = None) -> bool:
        progress("mailing list", 1, 1)
        release.wait(timeout=5.0)
        return True

    monkeypatch.setattr(main_mod, "run_gather", blocking_run)
    first = gather_runner.start(gather_runner.GatherSpec(corpus="httpbis"))
    assert first["started"] is True
    try:
        second = gather_runner.start(gather_runner.GatherSpec(corpus="httpbis"))
        assert second["started"] is False
        assert second["reason"] == "already running"
    finally:
        release.set()
    _wait_terminal("httpbis")


def _blocking_gather(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """Patch run_gather to block on the returned event, so a gather stays in
    flight while the test inspects the queue."""
    release = threading.Event()

    def blocking_run(argv: List[str], verbosity: Any, progress: Any = None, note_fn: Any = None) -> bool:
        progress("mailing list", 1, 1)
        release.wait(timeout=5.0)
        return True

    monkeypatch.setattr(main_mod, "run_gather", blocking_run)
    return release


def test_two_corpora_run_concurrently(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The default cap (3) lets a host run several gathers at once, so a second
    # client's gather does not wait behind the first.
    both_running = threading.Event()
    release = threading.Event()
    started: set[str] = set()
    lock = threading.Lock()

    def blocking_run(argv: List[str], verbosity: Any, progress: Any = None, note_fn: Any = None) -> bool:
        progress("mailing list", 1, 1)
        with lock:
            started.add(argv[0])
            if len(started) >= 2:
                both_running.set()
        release.wait(timeout=5.0)
        return True

    monkeypatch.setattr(main_mod, "run_gather", blocking_run)
    first = gather_runner.start(gather_runner.GatherSpec(corpus="tls"))
    second = gather_runner.start(gather_runner.GatherSpec(corpus="quic"))
    assert first["started"] and first["queued_behind"] == 0
    # ahead=1 < cap, so quic starts at once (does not wait behind tls).
    assert second["started"] and second["queued_behind"] == 0
    try:
        assert both_running.wait(timeout=3.0), "gathers did not run concurrently"
    finally:
        release.set()
    _wait_terminal("tls")
    _wait_terminal("quic")


def test_queued_behind_reported_at_cap(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With the cap at 1, the first gather fills it; the second is reported as
    # waiting behind 1 (the arithmetic the tool turns into a "queued" message).
    monkeypatch.setenv("IETF_LLM_GATHER_MAX_INFLIGHT", "1")
    release = _blocking_gather(monkeypatch)
    first = gather_runner.start(gather_runner.GatherSpec(corpus="tls"))
    try:
        second = gather_runner.start(gather_runner.GatherSpec(corpus="quic"))
        assert first["queued_behind"] == 0
        assert second["queued_behind"] == 1
    finally:
        release.set()
    _wait_terminal("tls")
    _wait_terminal("quic")


def test_queue_full_is_refused(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IETF_LLM_GATHER_QUEUE_MAX", "1")
    release = _blocking_gather(monkeypatch)
    first = gather_runner.start(gather_runner.GatherSpec(corpus="tls"))
    assert first["started"] is True
    try:
        # The backlog bound (1) is already reached by tls, so quic is refused.
        refused = gather_runner.start(gather_runner.GatherSpec(corpus="quic"))
        assert refused["started"] is False
        assert refused["reason"] == "queue full"
    finally:
        release.set()
    _wait_terminal("tls")


# --- cross-host gather visibility (cloud backend) -------------------------


def _patch_store(monkeypatch: pytest.MonkeyPatch, store: Any) -> None:
    from ietf_llm import corpus_store

    monkeypatch.setattr(corpus_store, "get_corpus_store", lambda: store)


def test_start_refuses_when_another_host_is_running(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gather running on another host is fleet-visible through the control
    plane: `start()` must report it as already running and NOT spawn a thread
    that would clobber the holder's shared status."""
    from ietf_llm.corpus_store import LocalCorpusStore

    class _FleetRunning(LocalCorpusStore):
        def get_gather_status(self, corpus: str) -> Any:
            return {"corpus": corpus, "state": "running", "stage": "drafts"}

    _patch_store(monkeypatch, _FleetRunning())
    monkeypatch.setattr(
        main_mod,
        "run_gather",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    result = gather_runner.start(gather_runner.GatherSpec(corpus="httpbis"))
    assert result["started"] is False
    assert result["reason"] == "already running"


def test_start_refuses_even_with_force_when_running(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`force` overrides the freshness debounce only — never a live gather."""
    from ietf_llm.corpus_store import LocalCorpusStore

    class _FleetRunning(LocalCorpusStore):
        def get_gather_status(self, corpus: str) -> Any:
            return {"corpus": corpus, "state": "running"}

    _patch_store(monkeypatch, _FleetRunning())
    result = gather_runner.start(
        gather_runner.GatherSpec(corpus="httpbis", force=True)
    )
    assert result["started"] is False
    assert result["reason"] == "already running"


def test_lease_denied_at_enqueue_refuses_without_clobbering(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-corpus lease is taken at enqueue, so if another host owns the
    corpus, `start()` refuses synchronously (already running) without spawning
    a worker or writing any status over the holder's record."""
    from ietf_llm.corpus_store import LocalCorpusStore

    class _LeaseDenied(LocalCorpusStore):
        def acquire_lease(self, corpus: str, owner: str, ttl: float) -> bool:
            return False

    _patch_store(monkeypatch, _LeaseDenied())
    monkeypatch.setattr(
        main_mod,
        "run_gather",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("lease denied")),
    )
    result = gather_runner.start(gather_runner.GatherSpec(corpus="quic"))
    assert result["started"] is False
    assert result["reason"] == "already running"
    # No status persisted: the holder's record (None here) is left untouched.
    assert gather_runner.read_status("quic") is None


def test_all_statuses_and_read_status(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_mod, "run_gather", lambda *a, **k: True)
    assert gather_runner.read_status("nope") is None
    assert gather_runner.all_statuses() == []
    gather_runner.start(gather_runner.GatherSpec(corpus="cfrg"))
    _wait_terminal("cfrg")
    assert gather_runner.read_status("cfrg")["state"] == "done"
    names = [s["corpus"] for s in gather_runner.all_statuses()]
    assert "cfrg" in names


# --- freshness debounce at the start() entry point ------------------------


def _backdate_hours(corpus: str, hours: float) -> None:
    when = datetime.now(timezone.utc) - timedelta(hours=hours)
    Path(freshness._sentinel_path(corpus)).write_text(
        when.strftime("%Y-%m-%dT%H:%M:%SZ"), encoding="utf-8"
    )


def test_start_debounces_a_freshly_gathered_corpus(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_k: Any) -> bool:
        raise AssertionError("run_gather must not be reached for a fresh corpus")

    monkeypatch.setattr(main_mod, "run_gather", boom)
    freshness.record_gather("tls")  # just now -> inside the default 6h window
    result = gather_runner.start(gather_runner.GatherSpec(corpus="tls"))
    assert result["started"] is False
    assert result["reason"] == "fresh"
    assert "skipped" in result["detail"]


def test_start_force_bypasses_debounce(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_mod, "run_gather", lambda *a, **k: True)
    freshness.record_gather("tls")
    result = gather_runner.start(gather_runner.GatherSpec(corpus="tls", force=True))
    assert result["started"] is True
    _wait_terminal("tls")


def test_start_source_change_bypasses_debounce(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A request that supplies sources isn't a plain refresh -> not debounced.
    monkeypatch.setattr(main_mod, "run_gather", lambda *a, **k: True)
    freshness.record_gather("tls")
    result = gather_runner.start(
        gather_runner.GatherSpec(corpus="tls", draft=["draft-x"])
    )
    assert result["started"] is True
    _wait_terminal("tls")


def test_start_not_debounced_once_stale(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_mod, "run_gather", lambda *a, **k: True)
    freshness.record_gather("tls")
    _backdate_hours("tls", freshness.GATHER_MIN_INTERVAL_DEFAULT_HOURS + 1)
    result = gather_runner.start(gather_runner.GatherSpec(corpus="tls"))
    assert result["started"] is True
    _wait_terminal("tls")


def test_has_sources_flags_only_real_sources() -> None:
    assert gather_runner.GatherSpec(corpus="tls").has_sources() is False
    assert gather_runner.GatherSpec(corpus="tls", force=True).has_sources() is False
    assert gather_runner.GatherSpec(corpus="tls", months=6).has_sources() is False
    assert gather_runner.GatherSpec(corpus="x", draft=["d"]).has_sources() is True
    assert gather_runner.GatherSpec(corpus="x", new_drafts=True).has_sources() is True


def test_force_is_rendered_to_argv() -> None:
    # force propagates to the background thread's _gather_one (which
    # re-checks the debounce), and parses cleanly through the CLI parser.
    argv = gather_runner.GatherSpec(corpus="tls", force=True).to_argv()
    assert "--force" in argv
    assert main_mod.build_parser().parse_args(argv).force is True
    # Absent by default.
    assert "--force" not in gather_runner.GatherSpec(corpus="tls").to_argv()


# --- freshness debounce at the CLI entry point (_gather_one) ---------------
#
# The debounce lives inside `_gather_one` (on raw CLI args, before merge),
# so drive it directly via the stubbed pipeline and observe whether any
# stage runs: a debounced gather returns True and emits nothing.


def _ran_stages(
    monkeypatch: pytest.MonkeyPatch, argv: List[str], shape: Tuple[bool, bool]
) -> bool:
    _stub_pipeline(monkeypatch, shape)
    args = main_mod.build_parser().parse_args(argv)
    seen = _emitted(args)
    return bool(seen)


def test_cli_debounces_fresh_corpus(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    freshness.record_gather("tls")
    assert _ran_stages(monkeypatch, ["tls"], (False, True)) is False  # skipped


def test_cli_force_bypasses_debounce(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    freshness.record_gather("tls")
    assert _ran_stages(monkeypatch, ["tls", "--force"], (False, True)) is True


def test_cli_source_flag_bypasses_debounce(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    freshness.record_gather("tls")
    assert _ran_stages(monkeypatch, ["tls", "--draft", "draft-x"], (False, True))


def test_cli_gathers_when_stale(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    freshness.record_gather("tls")
    _backdate_hours("tls", freshness.GATHER_MIN_INTERVAL_DEFAULT_HOURS + 1)
    assert _ran_stages(monkeypatch, ["tls"], (False, True)) is True


def test_cli_first_gather_never_debounced(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No sentinel yet -> the gather proceeds.
    assert _ran_stages(monkeypatch, ["new-corpus"], (False, True)) is True


# --- custom-corpus canonicalisation at the start() entry point ------------


def _seed_corpus(corpus: str, **sources: Any) -> None:
    get_wg_file_cache_dir(corpus)
    config.save(corpus, "gather", dict(sources))


def test_start_steers_new_overlapping_corpus_to_reuse(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_k: Any) -> bool:
        raise AssertionError("run_gather must not run for a duplicate corpus")

    monkeypatch.setattr(main_mod, "run_gather", boom)
    _seed_corpus("x-existing", draft=["draft-foo-bar"])
    result = gather_runner.start(
        gather_runner.GatherSpec(corpus="x-new", draft=["draft-foo-bar"])
    )
    assert result["started"] is False
    assert result["reason"] == "similar exists"
    assert "x-existing" in result["detail"]


def test_start_force_bypasses_canonicalisation(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_mod, "run_gather", lambda *a, **k: True)
    _seed_corpus("x-existing", draft=["draft-foo-bar"])
    result = gather_runner.start(
        gather_runner.GatherSpec(corpus="x-new", draft=["draft-foo-bar"], force=True)
    )
    assert result["started"] is True
    _wait_terminal("x-new")


def test_start_no_overlap_gathers(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_mod, "run_gather", lambda *a, **k: True)
    _seed_corpus("x-existing", draft=["draft-foo-bar"])
    result = gather_runner.start(
        gather_runner.GatherSpec(corpus="x-new", draft=["draft-distinct"])
    )
    assert result["started"] is True
    _wait_terminal("x-new")


# --- corpus-name validation (path-traversal guard) ------------------------


@pytest.mark.parametrize(
    "name", ["tls", "httpbis", "last-call", "x-webbotauth", "draft.foo_bar", "a"]
)
def test_valid_corpus_name_accepts_real_names(name: str) -> None:
    assert gather_runner.valid_corpus_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "", "..", "../evil", "../../etc/passwd", "/abs", "a/b", "a\\b",
        ".hidden", "-flag", "has space", "x" * 129,
    ],
)
def test_valid_corpus_name_rejects_unsafe(name: str) -> None:
    assert gather_runner.valid_corpus_name(name) is False


def test_start_rejects_traversal_without_writing(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = {"ran": False}
    monkeypatch.setattr(
        main_mod, "run_gather", lambda *a, **k: called.__setitem__("ran", True)
    )
    result = gather_runner.start(gather_runner.GatherSpec(corpus="../evil"))
    assert result["started"] is False
    assert result["reason"] == "invalid name"
    # Nothing ran, and no directory was materialised inside or outside cache.
    time.sleep(0.05)
    assert called["ran"] is False
    cache = isolated_home / ".cache" / "ietf-llm"
    assert not (cache.parent / "evil").exists()
    assert not (cache / ".." / "evil").exists()


# --- interrupted-gather (zombie status) detection -------------------------


def _write_status(corpus: str, **fields: Any) -> None:
    path = gather_runner._status_path(corpus)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"corpus": corpus, **fields}, handle)


def test_untracked_nonterminal_status_reads_as_interrupted(
    isolated_home: Path,
) -> None:
    # No live job in this process's registry, so a stuck non-terminal record is
    # a dead gather -> interrupted (covers both queued and running).
    _write_status("zombie", state="running", started="x")
    assert gather_runner.read_status("zombie")["state"] == "interrupted"
    _write_status("waiting", state="queued", started="x")
    assert gather_runner.read_status("waiting")["state"] == "interrupted"


def test_tracked_nonterminal_status_stays(isolated_home: Path) -> None:
    # While the corpus is tracked in this process's registry, its state is real.
    _write_status("live", state="running", started="x")
    _write_status("pend", state="queued", started="x")
    with gather_runner._registry_lock:
        gather_runner._jobs["live"] = "running"
        gather_runner._jobs["pend"] = "queued"
    try:
        assert gather_runner.read_status("live")["state"] == "running"
        assert gather_runner.read_status("pend")["state"] == "queued"
    finally:
        with gather_runner._registry_lock:
            gather_runner._jobs.pop("live", None)
            gather_runner._jobs.pop("pend", None)


def test_done_status_never_relabelled(isolated_home: Path) -> None:
    _write_status("fin", state="done", started="x")
    assert gather_runner.read_status("fin")["state"] == "done"


def test_read_status_rejects_unsafe_name(isolated_home: Path) -> None:
    assert gather_runner.read_status("../etc/passwd") is None


def test_spec_to_argv_round_trips_sources() -> None:
    spec = gather_runner.GatherSpec(
        corpus="x-foo",
        mailing_list=["a@ietf.org"],
        draft=["draft-x"],
        github=["o/r"],
        author="mnot@mnot.net",
        new_drafts=True,
        months=6,
    )
    argv = spec.to_argv()
    assert argv[0] == "x-foo"
    assert "--mailing-list" in argv and "a@ietf.org" in argv
    assert "--author" in argv and "mnot@mnot.net" in argv
    assert "--new-drafts" in argv
    assert "--months" in argv and "6" in argv
    # Parses cleanly through the real CLI parser.
    parsed = main_mod.build_parser().parse_args(argv)
    assert parsed.wg == "x-foo"
    assert parsed.months == 6
