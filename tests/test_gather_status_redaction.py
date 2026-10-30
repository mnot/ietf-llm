"""The fleet-visible gather status never carries credentials embedded in a URL,
whichever route the text takes to it: the in-process CLI driver, the MCP
runner's worker, the cache and seed notes, and the child's note records. The
choke point is `lifecycle.write_status`; `error_text` redacts before the length
cut so a cut inside ``user:token@host`` cannot leave a fragment."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from ietf_llm.gather import lifecycle, pipeline as gather_pipeline, runner as gather_runner, sequencer
from ietf_llm.gather.lifecycle import GatherOutcome, StatusRecorder
from ietf_llm.log import Verbosity
from ietf_llm.redact import redact_userinfo, redact_value
from ietf_llm.store.corpus import LocalCorpusStore

SECRET_URL = "https://svc:s3cret-token@embed.example/v1/embeddings"
CLEAN_URL = "https://***@embed.example/v1/embeddings"


def _assert_clean(*records: Any) -> None:
    blob = json.dumps(records)
    for fragment in ("s3c", "svc:", "svc@"):
        assert fragment not in blob, fragment


class _Store(LocalCorpusStore):
    """A local store with no real lease, slot or publish, which records every
    status record put to it and can be made to fail in the places a real
    object store does."""

    def __init__(self) -> None:
        self.statuses: List[Dict[str, Any]] = []
        self.seed_error: Optional[BaseException] = None
        self.hydrate_problems: List[str] = []
        self.publish_error: Optional[BaseException] = None
        self.persist_error: Optional[BaseException] = None

    def acquire_lease(self, corpus: str, owner: str, ttl: float) -> bool:
        return True

    def renew_lease(self, corpus: str, owner: str, ttl: float) -> bool:
        return True

    def release_lease(self, corpus: str, owner: str) -> None:
        pass

    def acquire_gather_slot(self, owner: str, corpus: str, ttl: float, max_inflight: int) -> bool:
        return True

    def release_gather_slot(self, owner: str) -> None:
        pass

    def seed_workspace(self, corpus: str, dest_root: str) -> Optional[str]:
        if self.seed_error:
            raise self.seed_error
        return None

    def hydrate_gather_caches(self, corpus: str) -> List[str]:
        return self.hydrate_problems

    def publish(self, corpus: str, workspace: str, *, extra_files: Any = None, lease_owner: Optional[str] = None) -> str:
        if self.publish_error:
            raise self.publish_error
        return "v1"

    def persist_gather_caches(self, corpus: str) -> List[str]:
        if self.persist_error:
            raise self.persist_error
        return []

    def put_gather_status(self, corpus: str, status: Dict[str, Any]) -> None:
        self.statuses.append(json.loads(json.dumps(status)))


# --- the helper ------------------------------------------------------------


def test_redact_userinfo_without_a_scheme() -> None:
    # What requests quotes for a base URL missing "https://".
    msg = "No connection adapters were found for 'svc:s3cret@host.example/v1/embeddings'"
    assert redact_userinfo(msg) == (
        "No connection adapters were found for '***@host.example/v1/embeddings'"
    )
    assert "s3cret" not in redact_userinfo("svc:p@ss@host.example/v1")


@pytest.mark.parametrize(
    "plain",
    [
        "write to a@b.example about it",
        "Contact: alice@example.org/ietf",
        "mailto:alice@example.org/x",
        "git@github.com:org/repo",
        "clone git@github.com:org/repo.git/",
        "https://host.example/a:b@c/x",
        "Message-ID:<a@b.example>",
        "workspace seed skipped (OSError: boom)",
    ],
)
def test_redact_userinfo_leaves_ordinary_text_alone(plain: str) -> None:
    assert redact_userinfo(plain) == plain


@pytest.mark.parametrize(
    "leaky",
    [
        "//svc:s3cret@host.example/v1",
        "see '//svc:s3cret@host.example/v1'",
        "https:/svc:s3cret@host.example/v1",
        "svc:s3cret@host.example:8443/v1",
    ],
)
def test_redact_userinfo_malformed_scheme_forms(leaky: str) -> None:
    out = redact_userinfo(leaky)
    _assert_clean(out)
    assert "@host.example" in out  # the host is kept for diagnosis


def test_redact_userinfo_time_is_bounded_on_adversarial_input() -> None:
    # Unbounded repeats in the scheme-less pattern made these quadratic: seconds
    # at 40 KB, never finishing at 1 MB. 100 KB keeps a regression a failure
    # (about 10 s) rather than a hang; the bounded pattern takes well under 0.2 s.
    shapes = ["a:!", "1:2,", "color:red;margin:0;", "k:v|", "@", ":", "a:@", "//a:", "://"]
    for unit in shapes:
        text = unit * (100_000 // len(unit))
        started = time.perf_counter()
        redact_userinfo(text)
        assert time.perf_counter() - started < 2.0, unit


def test_redact_userinfo_time_scales_linearly() -> None:
    def took(size: int) -> float:
        text = "a:!" * (size // 3)
        started = time.perf_counter()
        redact_userinfo(text)
        return time.perf_counter() - started

    small, large = took(100_000), took(400_000)
    # 4x the input: about 4x if linear, 16x if quadratic. The floor keeps a fast
    # small run from turning scheduler noise into a failure.
    assert large < 10 * max(small, 0.02)


def test_redact_value_walks_nested_strings_and_copies() -> None:
    original = {"a": [SECRET_URL, {"b": SECRET_URL}], "n": 3, "none": None}
    assert redact_value(original) == {"a": [CLEAN_URL, {"b": CLEAN_URL}], "n": 3, "none": None}
    assert original["a"][0] == SECRET_URL  # the live record is not rewritten


def test_error_text_redacts_before_cutting() -> None:
    for pad in range(lifecycle.STATUS_ERROR_MAX - 40, lifecycle.STATUS_ERROR_MAX):
        text = lifecycle.error_text("x" * pad + " " + SECRET_URL)
        assert len(text) <= lifecycle.STATUS_ERROR_MAX
        _assert_clean(text)


# --- the in-process / CLI path --------------------------------------------


def _cli(store: _Store, pipeline: Any) -> GatherOutcome:
    return lifecycle.gather_and_publish(
        store, "tls", pipeline, spec={"corpus": "tls"}, verbosity=Verbosity.QUIET
    )


def test_cli_pipeline_error_never_puts_credentials_in_the_status(isolated_home: Path) -> None:
    store = _Store()

    def boom(progress: Any, note: Any) -> GatherOutcome:
        raise ConnectionError(f"POST {SECRET_URL} failed")

    with pytest.raises(ConnectionError):
        _cli(store, boom)
    final = store.statuses[-1]
    assert final["error"] == "ConnectionError: POST https://***@embed.example/v1/embeddings failed"
    _assert_clean(store.statuses, Path(lifecycle.status_path("tls")).read_text())


def test_cli_error_cut_inside_the_credentials_leaves_no_fragment(isolated_home: Path) -> None:
    prefix = len("ConnectionError: ")
    for pad in range(lifecycle.STATUS_ERROR_MAX - prefix - 30, lifecycle.STATUS_ERROR_MAX - prefix):
        store = _Store()

        def boom(progress: Any, note: Any, pad: int = pad) -> GatherOutcome:
            raise ConnectionError("x" * pad + " " + SECRET_URL)

        with pytest.raises(ConnectionError):
            _cli(store, boom)
        assert len(store.statuses[-1]["error"]) <= lifecycle.STATUS_ERROR_MAX
        _assert_clean(store.statuses)


def test_cli_publish_error_and_cache_notes_are_redacted(isolated_home: Path) -> None:
    store = _Store()
    store.publish_error = OSError(f"PUT {SECRET_URL} refused")
    store.seed_error = OSError(f"GET {SECRET_URL} refused")
    store.hydrate_problems = [f"corpus cache at {SECRET_URL} unreadable"]
    with pytest.raises(OSError):
        _cli(store, lambda progress, note: GatherOutcome.GATHERED)
    final = store.statuses[-1]
    assert final["state"] == "failed"
    assert any("workspace seed skipped" in n for n in final["notes"])
    _assert_clean(store.statuses)


def test_cli_cache_persist_note_is_redacted(isolated_home: Path) -> None:
    store = _Store()
    store.persist_error = OSError(f"PUT {SECRET_URL} refused")
    _cli(store, lambda progress, note: GatherOutcome.GATHERED)
    final = store.statuses[-1]
    assert final["state"] == "done"
    assert any("persist skipped" in n for n in final["notes"])
    _assert_clean(store.statuses)


def test_a_pipeline_note_and_detail_quoting_credentials_are_redacted(isolated_home: Path) -> None:
    store = _Store()

    def pipeline(progress: Any, note: Any) -> GatherOutcome:
        note(f"fetching from {SECRET_URL}")
        progress("drafts", 1, 2, f"from {SECRET_URL}")
        return GatherOutcome.SKIPPED

    _cli(store, pipeline)
    _assert_clean(store.statuses, Path(lifecycle.status_path("tls")).read_text())


# --- the child's note records, through the parent --------------------------


def test_a_child_note_record_is_redacted_on_its_way_to_the_status(isolated_home: Path) -> None:
    store = _Store()
    recorder = StatusRecorder(store, lifecycle.new_status("tls", {}, "running", driver="mcp"))
    gather_pipeline._apply_record(
        {"t": "note", "msg": f"seed {SECRET_URL} unreachable"}, lambda *a, **k: None, recorder.note
    )
    assert store.statuses[-1]["notes"] == [f"seed {CLEAN_URL} unreachable"]
    _assert_clean(store.statuses)


# --- the MCP runner's worker -----------------------------------------------


def _patch_store(monkeypatch: pytest.MonkeyPatch, store: Any) -> None:
    from ietf_llm.store import corpus as corpus_store

    monkeypatch.setattr(corpus_store, "get_corpus_store", lambda: store)


def _wait_terminal(corpus: str, timeout: float = 5.0) -> Dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = gather_runner.read_status(corpus)
        if status and status.get("state") in ("done", "failed", "cancelled"):
            return status
        time.sleep(0.01)
    raise AssertionError(f"gather for {corpus} did not finish in {timeout}s")


@pytest.mark.usefixtures("gather_in_process")
def test_runner_pipeline_error_is_redacted(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store()
    _patch_store(monkeypatch, store)

    def run(argv: List[str], verbosity: Any, progress: Any = None, note_fn: Any = None) -> GatherOutcome:
        raise ConnectionError(f"POST {SECRET_URL} failed")

    monkeypatch.setattr(sequencer, "run_gather", run)
    assert gather_runner.start(gather_runner.GatherSpec(corpus="tls"))["started"]
    status = _wait_terminal("tls")
    assert status["state"] == "failed"
    assert status["error"] == "ConnectionError: POST https://***@embed.example/v1/embeddings failed"
    _assert_clean(status, store.statuses)


@pytest.mark.usefixtures("gather_in_process")
def test_runner_error_cut_inside_the_credentials_leaves_no_fragment(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _Store()
    _patch_store(monkeypatch, store)
    # The URL's userinfo spans offsets 8..32; move the 500-character cut across it.
    for offset in range(8, 33, 3):
        pad = lifecycle.STATUS_ERROR_MAX - len("ConnectionError: ") - 1 - offset

        def run(
            argv: List[str], verbosity: Any, progress: Any = None, note_fn: Any = None, pad: int = pad
        ) -> GatherOutcome:
            raise ConnectionError("x" * pad + " " + SECRET_URL)

        monkeypatch.setattr(sequencer, "run_gather", run)
        corpus = f"wg{offset}"
        gather_runner.start(gather_runner.GatherSpec(corpus=corpus))
        status = _wait_terminal(corpus)
        assert status["state"] == "failed"
        _assert_clean(status, store.statuses)


@pytest.mark.usefixtures("gather_in_process")
def test_runner_seed_and_publish_errors_are_redacted(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _Store()
    store.seed_error = OSError(f"GET {SECRET_URL} refused")
    store.publish_error = OSError(f"PUT {SECRET_URL} refused")
    _patch_store(monkeypatch, store)
    monkeypatch.setattr(sequencer, "run_gather", lambda *a, **k: GatherOutcome.GATHERED)
    gather_runner.start(gather_runner.GatherSpec(corpus="tls"))
    status = _wait_terminal("tls")
    assert status["state"] == "failed" and "OSError" in status["error"]
    assert any("workspace seed skipped" in n for n in status["notes"])
    _assert_clean(status, store.statuses)
