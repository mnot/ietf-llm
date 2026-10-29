"""Feature 6 capstone: an in-session gather publishes to the cloud backend, and
the published version is then served by the read path — gather to read, end to
end, over an S3-compatible object store (moto in-process)."""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Iterator

import boto3
import pytest
from moto import mock_aws

from ietf_llm.gather import sequencer as main_mod
from ietf_llm import corpus
from ietf_llm.gather import lifecycle
from ietf_llm.gather.lifecycle import GatherOutcome
from ietf_llm.gather import runner as gather_runner
from ietf_llm.store.corpus import get_corpus_store
from ietf_llm.store.cloud import _clear_resolve_cache
from ietf_llm.paths import get_cache_dir
from ietf_llm.store.kv_s3 import S3KvStore


def _wait_done(corpus: str, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = gather_runner.read_status(corpus)
        if status and status.get("state") in ("done", "failed"):
            assert status["state"] == "done", status
            return
        time.sleep(0.01)
    raise AssertionError(f"gather for {corpus} did not finish in {timeout}s")

_STORE_ENV = (
    "IETF_LLM_STORE_BACKEND",
    "IETF_LLM_STORE_URL",
    "IETF_LLM_SCRATCH_DIR",
)


@pytest.fixture(autouse=True)
def _clear_store_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _STORE_ENV:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def cloud_s3(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Select the cloud backend over an in-process moto S3 bucket that holds
    both the version content and the control-plane keys."""
    base = isolated_home / "store"
    monkeypatch.setenv("IETF_LLM_STORE_BACKEND", "cloud")
    monkeypatch.setenv("IETF_LLM_STORE_URL", "s3://test-bucket")
    monkeypatch.setenv("IETF_LLM_SCRATCH_DIR", str(base / "scratch"))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("IETF_LLM_STORE_ENDPOINT_URL", raising=False)
    _clear_resolve_cache()  # the resolve cache is keyed by store URL; isolate
    with mock_aws():
        boto3.client("s3").create_bucket(Bucket="test-bucket")
        yield
    _clear_resolve_cache()


def test_cloud_gather_publishes_and_serves(
    cloud_s3: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stub the pipeline: write a files/ tree into the local cache (the gather's
    # natural output, which on a cloud node is ephemeral scratch), report
    # success. The worker then publishes that tree to the cloud backend.
    def _fake_run_gather(argv: list[str], _verbosity: object, progress: object = None, note_fn: object = None) -> GatherOutcome:
        corpus = argv[0]
        digests = Path(get_cache_dir()) / corpus / "files" / "digests"
        digests.mkdir(parents=True, exist_ok=True)
        (digests / "index.md").write_text("# Overview\nbody\n")
        return GatherOutcome.GATHERED

    monkeypatch.setattr(main_mod, "run_gather", _fake_run_gather)

    assert gather_runner.start(gather_runner.GatherSpec(corpus="tls"))["started"]
    _wait_done("tls")

    # A fresh store (same config) resolves the published version and
    # materialises its files for the read path.
    store = get_corpus_store()
    assert store.resolve_current("tls") is not None
    cache = store.local_cache_dir("tls")
    assert cache is not None
    assert (Path(cache) / "digests" / "index.md").read_text() == "# Overview\nbody\n"


def test_listing_classification_degrades_then_reads_through_seam(
    cloud_s3: None, isolated_home: Path
) -> None:
    """`kind_status` / `describe` (the `list_corpora` classification) must go
    through the CorpusStore seam: never create a junk local cache dir and never
    download a corpus's blobs just to classify it. A corpus not yet staged on
    this replica degrades to the config-only path; once a real read stages it,
    classification reads `group.md` from the staged tree."""
    base = isolated_home / "store"
    # Publish a group corpus straight to the cloud store: a files/group.md
    # carrying a name and status.
    workspace = isolated_home / "ws"
    (workspace / "files").mkdir(parents=True)
    (workspace / "files" / "group.md").write_text(
        "# tls\n**Name:** Transport Layer Security\n**Status:** active\n"
    )
    store = get_corpus_store()
    store.publish("tls", str(workspace))

    # Before any read materialises tls, classification degrades to config-only
    # (no config -> custom / empty) WITHOUT downloading blobs or creating a dir.
    assert corpus.kind_status("tls") == ("custom", "")
    assert corpus.describe("tls") == ""
    assert not (Path(get_cache_dir()) / "tls" / "files").exists()
    assert not (base / "scratch" / "tls").exists()

    # A real read stages the version onto scratch (the seam materialises here).
    assert get_corpus_store().local_cache_dir("tls") is not None

    # Now the same classification reads group.md through the staged tree.
    assert corpus.kind_status("tls") == ("group", "active")
    assert corpus.describe("tls") == "Transport Layer Security"
    # Still no junk dir under the local cache root — the seam owns scratch.
    assert not (Path(get_cache_dir()) / "tls" / "files").exists()


def test_all_statuses_includes_control_plane_on_cloud(cloud_s3: None) -> None:
    # On the cloud backend, a no-corpus gather_status listing must see gathers
    # recorded in the control plane by other replicas, not only the corpora
    # cached on this host (which here is none).
    store = get_corpus_store()
    store.put_gather_status(
        "tls", {"corpus": "tls", "state": "running", "updated": "2026-06-06T00:00:00Z"}
    )
    store.put_gather_status(
        "quic", {"corpus": "quic", "state": "queued", "updated": "2026-06-06T00:01:00Z"}
    )
    corpora = {s.get("corpus") for s in gather_runner.all_statuses()}
    assert {"tls", "quic"} <= corpora


def test_fleet_slot_blocks_a_gather_until_released(
    cloud_s3: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IETF_LLM_GATHER_MAX_INFLIGHT", "1")
    monkeypatch.setattr(lifecycle, "SLOT_POLL_S", 0.02)

    def _fake_run_gather(argv: list[str], _v: object, progress: object = None, note_fn: object = None) -> GatherOutcome:
        digests = Path(get_cache_dir()) / argv[0] / "files" / "digests"
        digests.mkdir(parents=True, exist_ok=True)
        (digests / "index.md").write_text("ok\n")
        return GatherOutcome.GATHERED

    monkeypatch.setattr(main_mod, "run_gather", _fake_run_gather)

    # Another host already holds the one fleet slot.
    store = get_corpus_store()
    assert store.acquire_gather_slot("other-host", "x", 1000.0, 1) is True

    assert gather_runner.start(gather_runner.GatherSpec(corpus="tls"))["started"]
    # tls cannot get a slot, so it waits in `queued` (the worker is polling).
    deadline = time.time() + 2.0
    while time.time() < deadline:
        st = gather_runner.read_status("tls")
        if st and st.get("state") == "queued":
            break
        time.sleep(0.01)
    assert gather_runner.read_status("tls")["state"] == "queued"

    # Free the fleet slot; tls now acquires it, runs, and publishes.
    store.release_gather_slot("other-host")
    _wait_done("tls")
    assert get_corpus_store().resolve_current("tls") is not None


def _wait_state(corpus: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = gather_runner.read_status(corpus)
        if status and status.get("state") in ("done", "failed"):
            return status
        time.sleep(0.01)
    raise AssertionError(f"gather for {corpus} did not finish in {timeout}s")


def _imap_dir(corpus: str) -> Path:
    return Path(get_cache_dir()) / "imap-cache" / corpus / corpus


def test_cloud_gather_restores_the_mail_cache_on_a_fresh_container(
    cloud_s3: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Issue #260: the second gather runs on a host whose imap-cache is empty,
    # and must find the first gather's mail restored before any stage runs.
    monkeypatch.setenv("IETF_LLM_GATHER_MIN_INTERVAL", "0")  # re-gather at once
    seen: list[list[str]] = []

    def _fake_run_gather(argv: list[str], _verbosity: object, progress: object = None, note_fn: object = None) -> GatherOutcome:
        corpus = argv[0]
        mail = _imap_dir(corpus)
        if not seen:
            mail.mkdir(parents=True, exist_ok=True)
            for uid in (1, 2, 700):
                (mail / f"{uid}.eml").write_text(f"Message-Id: <{uid}@x>\n\nbody\n")
            (mail / ".uidvalidity").write_text("7")
        seen.append(sorted(p.name for p in mail.glob("*.eml")))
        digests = Path(get_cache_dir()) / corpus / "files" / "digests"
        digests.mkdir(parents=True, exist_ok=True)
        (digests / "index.md").write_text("# Overview\n")
        return GatherOutcome.GATHERED

    monkeypatch.setattr(main_mod, "run_gather", _fake_run_gather)

    assert gather_runner.start(gather_runner.GatherSpec(corpus="tls"))["started"]
    assert _wait_state("tls")["state"] == "done"
    shutil.rmtree(Path(get_cache_dir()) / "imap-cache")  # a fresh container

    assert gather_runner.start(gather_runner.GatherSpec(corpus="tls"))["started"]
    assert _wait_state("tls")["state"] == "done"
    assert seen[1] == ["1.eml", "2.eml", "700.eml"]


def test_cloud_gather_fails_before_any_stage_when_stored_mail_cannot_be_fetched(
    cloud_s3: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IETF_LLM_GATHER_MIN_INTERVAL", "0")  # re-gather at once
    ran: list[str] = []

    def _fake_run_gather(argv: list[str], _verbosity: object, progress: object = None, note_fn: object = None) -> GatherOutcome:
        ran.append(argv[0])
        mail = _imap_dir(argv[0])
        mail.mkdir(parents=True, exist_ok=True)
        (mail / "1.eml").write_text("Message-Id: <1@x>\n\nbody\n")
        digests = Path(get_cache_dir()) / argv[0] / "files" / "digests"
        digests.mkdir(parents=True, exist_ok=True)
        (digests / "index.md").write_text("# Overview\n")
        return GatherOutcome.GATHERED

    monkeypatch.setattr(main_mod, "run_gather", _fake_run_gather)
    assert gather_runner.start(gather_runner.GatherSpec(corpus="tls"))["started"]
    assert _wait_state("tls")["state"] == "done"
    version = get_corpus_store().resolve_current("tls")
    shutil.rmtree(Path(get_cache_dir()) / "imap-cache")

    real_get = S3KvStore.get

    def _get(self: S3KvStore, key: str):  # type: ignore[no-untyped-def]
        if "gather-cache/imap/" in key and key.endswith(".tar.gz"):
            raise OSError("bucket unreachable")
        return real_get(self, key)

    monkeypatch.setattr(S3KvStore, "get", _get)
    assert gather_runner.start(gather_runner.GatherSpec(corpus="tls"))["started"]
    status = _wait_state("tls")
    assert status["state"] == "failed"
    assert "GatherCacheRestoreError" in status["error"] and "'tls'" in status["error"]
    assert ran == ["tls"]  # the second gather never reached a stage
    assert get_corpus_store().resolve_current("tls") == version  # nothing published

# --- the CLI on the cloud backend (issue #258) ----------------------------


def _quiet_cli_housekeeping(monkeypatch: pytest.MonkeyPatch) -> None:
    from ietf_llm.cli import main as cli_main

    for name in (
        "ensure_rfc_index",
        "ensure_rfc_corpus",
        "ensure_catalog_index",
        "ensure_groups_index",
        "sync_if_pristine",
    ):
        monkeypatch.setattr(cli_main, name, lambda *a, **k: None)


def test_cli_gather_publishes_to_the_cloud_store(
    cloud_s3: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ietf-llm <corpus>` with the cloud backend selected must publish what it
    gathered — before #258 it populated local scratch and stopped, leaving the
    bucket untouched — and record the gather fleet-visibly as the CLI's."""
    from ietf_llm.cli import main as cli_main
    from ietf_llm.gather.lifecycle import GatherOutcome

    _quiet_cli_housekeeping(monkeypatch)

    def _fake_gather_one(args: object, _v: object, **_kw: object) -> GatherOutcome:
        digests = Path(get_cache_dir()) / "tls" / "files" / "digests"
        digests.mkdir(parents=True, exist_ok=True)
        (digests / "index.md").write_text("# Overview\nfrom the CLI\n")
        return GatherOutcome.GATHERED

    monkeypatch.setattr(cli_main, "_gather_one", _fake_gather_one)
    monkeypatch.setattr(cli_main.sys, "argv", ["ietf-llm", "tls"])
    cli_main.main()

    store = get_corpus_store()
    assert store.resolve_current("tls") is not None
    cache = store.local_cache_dir("tls")
    assert cache is not None
    assert (Path(cache) / "digests" / "index.md").read_text() == "# Overview\nfrom the CLI\n"
    status = store.get_gather_status("tls")
    assert status is not None
    assert status["state"] == "done" and status["driver"] == "cli"
    # Lease and slot were released: another gather can start at once.
    assert store.acquire_lease("tls", "someone-else", 10.0) is True


def test_cli_gather_refused_while_another_host_holds_the_lease(
    cloud_s3: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ietf_llm.cli import main as cli_main

    _quiet_cli_housekeeping(monkeypatch)
    store = get_corpus_store()
    assert store.acquire_lease("tls", "other-host:1:abcd", 1000.0) is True
    monkeypatch.setattr(
        cli_main,
        "_gather_one",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not gather")),
    )
    monkeypatch.setattr(cli_main.sys, "argv", ["ietf-llm", "tls"])
    with pytest.raises(SystemExit) as exc:
        cli_main.main()
    assert exc.value.code == 1
    assert store.resolve_current("tls") is None


def test_cli_gather_of_a_fresh_corpus_downloads_nothing(
    cloud_s3: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sweep over a corpus that is not due must cost one small read: the
    debounce answers from the published version's own timestamp, before the
    workspace is seeded (which downloads the whole version)."""
    from ietf_llm.cli import main as cli_main

    _quiet_cli_housekeeping(monkeypatch)
    store = get_corpus_store()
    digests = Path(get_cache_dir()) / "tls" / "files" / "digests"
    digests.mkdir(parents=True, exist_ok=True)
    (digests / "index.md").write_text("# Overview\n")
    first = store.publish("tls", str(Path(get_cache_dir()) / "tls"))
    assert store.resolve_current("tls") == first

    def forbidden(*_a: object, **_k: object) -> object:
        raise AssertionError("a corpus that is not due must not be seeded")

    monkeypatch.setattr(type(store), "seed_workspace", forbidden)
    monkeypatch.setattr(cli_main, "_gather_one", forbidden)
    monkeypatch.setattr(main_mod, "_resolve_corpus_shape", lambda *a, **k: (False, True))
    monkeypatch.setattr(cli_main.sys, "argv", ["ietf-llm", "tls"])
    cli_main.main()
    assert store.resolve_current("tls") == first
    assert store.get_gather_status("tls") is None


def test_cli_clear_cache_wipes_the_stores_copy_of_the_mail_too(
    cloud_s3: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ietf-llm tls --clear-cache` on the cloud backend: the old mail is gone
    from this host and from the store, so neither this gather nor a later fresh
    container gets it back, and what the gather fetched afresh is what persists."""
    from ietf_llm.cli import main as cli_main
    from ietf_llm.gather.lifecycle import GatherOutcome
    from ietf_llm.gather.sources import cache_sync

    _quiet_cli_housekeeping(monkeypatch)

    def _gather_one(args: object, _v: object, **_kw: object) -> GatherOutcome:
        mail = _imap_dir("tls")
        mail.mkdir(parents=True, exist_ok=True)
        for uid in fresh_uids:
            (mail / f"{uid}.eml").write_text(f"Message-Id: <{uid}@x>\n\nbody\n")
        (mail / ".uidvalidity").write_text("7")
        digests = Path(get_cache_dir()) / "tls" / "files" / "digests"
        digests.mkdir(parents=True, exist_ok=True)
        (digests / "index.md").write_text("# Overview\n")
        return GatherOutcome.GATHERED

    monkeypatch.setattr(cli_main, "_gather_one", _gather_one)
    monkeypatch.setattr(cli_main.sys, "argv", ["ietf-llm", "tls"])
    fresh_uids = (1, 2, 700)
    seen: list[list[str]] = []
    cli_main.main()  # the first gather: mail now stored

    kv = get_corpus_store()._kv  # type: ignore[attr-defined]
    assert any("gather-cache/imap/tls/" in key for key in _stored_keys(kv))

    def _gather_after_clear(args: object, _v: object, **_kw: object) -> GatherOutcome:
        mail = _imap_dir("tls")
        seen.append(sorted(p.name for p in mail.glob("*.eml")) if mail.exists() else [])
        assert not (Path(get_cache_dir()) / "tls" / "files" / "digests").exists()
        mail.mkdir(parents=True, exist_ok=True)
        (mail / "5.eml").write_text("Message-Id: <5@x>\n\nbody\n")
        (mail / ".uidvalidity").write_text("7")
        digests = Path(get_cache_dir()) / "tls" / "files" / "digests"
        digests.mkdir(parents=True, exist_ok=True)
        (digests / "index.md").write_text("# Overview\n")
        return GatherOutcome.GATHERED

    monkeypatch.setattr(cli_main, "_gather_one", _gather_after_clear)
    monkeypatch.setattr(cli_main.sys, "argv", ["ietf-llm", "tls", "--clear-cache"])
    cli_main.main()
    assert seen[-1] == []  # nothing from before was restored

    shutil.rmtree(Path(get_cache_dir()) / "imap-cache")  # a fresh container
    assert cache_sync.hydrate(kv, "tls") == []
    assert sorted(p.name for p in _imap_dir("tls").glob("*.eml")) == ["5.eml"]


def _stored_keys(kv: object) -> list[str]:
    from ietf_llm.store.kv import KvStore

    assert isinstance(kv, KvStore)
    found: list[str] = []

    def walk(prefix: str) -> None:
        for name in kv.list_children(prefix):
            if kv.get(prefix + name) is not None:
                found.append(prefix + name)
            walk(f"{prefix}{name}/")

    walk("corpora/tls/")
    return found


def _publish_tree(corpus: str, root: Path) -> str:
    """Publish a minimal corpus tree straight to the cloud store."""
    (root / "files").mkdir(parents=True)
    (root / "files" / "group.md").write_text("# tls\n**Status:** active\n")
    return get_corpus_store().publish(corpus, str(root))


def test_start_on_a_fresh_replica_debounces_on_the_published_gather_time(
    cloud_s3: None, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Issue #290: this replica has no local last-gathered sentinel, but the
    # published version is minutes old. The debounce must read the store, or it
    # admits the request and downloads the version only to find it fresh.
    version = _publish_tree("tls", isolated_home / "ws")
    assert not (Path(get_cache_dir()) / "tls").exists()

    def _unreachable(*_a: object, **_k: object) -> GatherOutcome:
        raise AssertionError("a fresh corpus must not be gathered")

    monkeypatch.setattr(main_mod, "run_gather", _unreachable)
    result = gather_runner.start(gather_runner.GatherSpec(corpus="tls"))
    assert result["started"] is False and result["reason"] == "fresh"
    assert get_corpus_store().resolve_current("tls") == version


def test_a_skipped_gather_publishes_nothing(
    cloud_s3: None, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Issue #290: a pipeline that found nothing to do left the seeded workspace
    # untouched; publishing it would upload an identical version.
    version = _publish_tree("tls", isolated_home / "ws")
    monkeypatch.setattr(
        main_mod, "run_gather", lambda *a, **k: GatherOutcome.SKIPPED
    )

    assert gather_runner.start(gather_runner.GatherSpec(corpus="tls", force=True))[
        "started"
    ]
    status = _wait_state("tls")
    assert status["state"] == "done"
    assert any("nothing published" in note for note in status["notes"])
    assert get_corpus_store().resolve_current("tls") == version
    assert len(get_corpus_store()._list_versions("tls")) == 1  # pylint: disable=protected-access
