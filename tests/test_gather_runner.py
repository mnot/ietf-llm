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
from ietf_llm import freshness, gather_runner, utils
from ietf_llm.gather_stages import stage_plan
from ietf_llm.utils import Verbosity


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
    monkeypatch.setattr(main_mod, "_download_github_archives", lambda *a, **k: [])
    monkeypatch.setattr(main_mod, "build_registry", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "scan_citations", lambda *a, **k: {})
    monkeypatch.setattr(main_mod, "validate_draft_names", lambda names, v: list(names))
    monkeypatch.setattr(main_mod, "validate_list_names", lambda names, v: list(names))


def _emitted(args: Any) -> List[str]:
    seen: List[str] = []
    main_mod._gather_one(
        args, Verbosity.QUIET, progress=lambda n, i, t: seen.append(n)
    )
    return seen


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
        if status and status.get("state") in ("done", "failed"):
            return status
        time.sleep(0.01)
    raise AssertionError(f"gather for {corpus} did not finish in {timeout}s")


def test_start_runs_to_done_with_progress(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(argv: List[str], verbosity: Any, progress: Any = None) -> bool:
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

    def blocking_run(argv: List[str], verbosity: Any, progress: Any = None) -> bool:
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


@pytest.mark.skipif(utils._fcntl is None, reason="needs flock")
def test_running_status_without_held_lock_reads_as_interrupted(
    isolated_home: Path,
) -> None:
    # No live gather holds the corpus lock, so a stuck `running` record is a
    # dead gather -> interrupted.
    _write_status("zombie", state="running", started="x")
    assert gather_runner.read_status("zombie")["state"] == "interrupted"


def test_running_status_with_held_lock_stays_running(isolated_home: Path) -> None:
    # While the gather lock is genuinely held, `running` is real.
    _write_status("live", state="running", started="x")
    with utils.file_lock(gather_runner._lock_path("live")):
        assert gather_runner.read_status("live")["state"] == "running"


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
