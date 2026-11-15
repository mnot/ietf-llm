"""Cloud gather across the process boundary: hydrate -> a real gather child ->
persist, over an in-memory KvStore.

The in-session runner hydrates and persists the gather accelerator caches in the
server and runs the pipeline in `ietf_llm.gather.child`. State that only lives in
one process's memory is invisible to the other half (issue #279), and the rest of
the suite runs the pipeline in-process, where it never is. These tests spawn the
real child (`fake_gather_child.py`: the real `child.main` / `run_gather` /
`_gather_one`, with the network and stage bodies replaced) and look only at what
reached the store.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tarfile
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from ietf_llm.gather import pipeline as gather_pipeline
from ietf_llm.gather import runner as gather_runner
from ietf_llm.paths import get_cache_dir
from ietf_llm.store import corpus as corpus_store
from ietf_llm.store.blobs import FileBlobStore
from ietf_llm.store.cloud import CloudCorpusStore
from ietf_llm.store.control import KvControlPlane
from ietf_llm.store.kv import InMemoryKvStore

pytestmark = pytest.mark.usefixtures("real_gather_child")

_CHILD = Path(__file__).with_name("fake_gather_child.py")
_HTTP_KEY = "corpora/tls/gather-cache/http-cache.tar"
_IMAP_PREFIX = "corpora/tls/gather-cache/imap/tls/"
_ETAG_URL = "https://datatracker.ietf.org/api/v1/group/group/?acronym=tls&format=json"


@pytest.fixture
def cloud(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> Tuple[InMemoryKvStore, CloudCorpusStore]:
    kv = InMemoryKvStore()
    store = CloudCorpusStore(
        KvControlPlane(kv),
        FileBlobStore(str(isolated_home / "bucket")),
        str(isolated_home / "scratch"),
        kv=kv,
    )
    monkeypatch.setattr(corpus_store, "get_corpus_store", lambda: store)
    # The child reads the backend from its environment; it never reaches `kv`.
    monkeypatch.setenv("IETF_LLM_STORE_BACKEND", "cloud")
    monkeypatch.setenv("IETF_LLM_GATHER_MIN_INTERVAL", "0")
    monkeypatch.setenv("FAKE_LOG", str(isolated_home / "requests.jsonl"))
    monkeypatch.setattr(
        gather_pipeline,
        "_child_command",
        # -I: don't let the script's directory shadow the stdlib in the child.
        lambda spec: [sys.executable, "-I", str(_CHILD), *spec.to_argv()],
    )
    return kv, store


def _gather(corpus: str = "tls") -> Dict[str, Any]:
    assert gather_runner.start(gather_runner.GatherSpec(corpus=corpus))["started"]
    deadline = time.time() + 90
    while time.time() < deadline:
        status = gather_runner.read_status(corpus)
        if status and status.get("state") in ("done", "failed"):
            return status
        time.sleep(0.05)
    raise AssertionError(f"gather for {corpus} did not finish")


def _requests(home: Path) -> List[Dict[str, Any]]:
    log = home / "requests.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()]


def _stored(kv: InMemoryKvStore, key: str) -> Any:
    record = kv.get(key)
    assert record is not None, key
    return json.loads(record[0])


def _stored_etags(kv: InMemoryKvStore) -> Dict[str, str]:
    """The ETag store the child left in the store: URL -> ETag."""
    record = kv.get(_HTTP_KEY)
    assert record is not None
    with tarfile.open(fileobj=io.BytesIO(record[0]), mode="r:") as tar:
        handle = tar.extractfile("index.json")
        assert handle is not None
        entries = json.loads(handle.read())["entries"]
    return {url: entry["etag"] for url, entry in entries.items()}


def _a_fresh_container() -> None:
    """Drop everything a gather left on this host's disk; the store keeps it."""
    shutil.rmtree(get_cache_dir())


def test_the_etag_cache_reaches_the_store_and_the_next_child(
    cloud: Tuple[InMemoryKvStore, CloudCorpusStore],
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kv, _ = cloud
    monkeypatch.setenv("FAKE_UIDVALIDITY", "7")
    monkeypatch.setenv("FAKE_ETAG", "e1")
    assert _gather()["state"] == "done"
    first = kv.get(_HTTP_KEY)
    assert first is not None
    assert _stored_etags(kv)[_ETAG_URL] == "e1"

    _a_fresh_container()
    monkeypatch.setenv("FAKE_ETAG", "e2")
    assert _gather()["state"] == "done"

    # What hydrate restored is what the second child revalidated against...
    assert _requests(isolated_home)[1]["headers"]["If-None-Match"] == "e1"
    # ...and what the child learned came back out.
    second = kv.get(_HTTP_KEY)
    assert second is not None and second[0] != first[0]
    assert _stored_etags(kv)[_ETAG_URL] == "e2"


def test_a_renumbered_list_replaces_the_stored_epoch(
    cloud: Tuple[InMemoryKvStore, CloudCorpusStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    kv, _ = cloud
    monkeypatch.setenv("FAKE_UIDVALIDITY", "7")
    monkeypatch.setenv("FAKE_UIDS", "1,2,3")
    assert _gather()["state"] == "done"
    assert _stored(kv, _IMAP_PREFIX + "index.json")["uidvalidity"] == "7"

    # The server renumbers the list; the same host syncs it and confirms 9.
    monkeypatch.setenv("FAKE_UIDVALIDITY", "9")
    monkeypatch.setenv("FAKE_UIDS", "1,2")
    status = _gather()
    assert status["state"] == "done"
    assert not any("not confirmed" in note for note in status.get("notes", []))

    index = _stored(kv, _IMAP_PREFIX + "index.json")
    assert index["uidvalidity"] == "9"
    assert index["chunks"] == {"0": [1, 2]}
    # The old epoch's mail is kept as its own unit, not dropped.
    assert _stored(kv, _IMAP_PREFIX + "epoch-7/index.json")["chunks"] == {
        "0": [1, 2, 3]
    }


def test_a_skipped_child_publishes_nothing(
    cloud: Tuple[InMemoryKvStore, CloudCorpusStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    kv, store = cloud
    monkeypatch.setenv("FAKE_UIDVALIDITY", "7")
    assert _gather()["state"] == "done"
    version = store.resolve_current("tls")
    caches = {k: v for k, v in kv._data.items() if "gather-cache" in k}

    _a_fresh_container()
    monkeypatch.setenv("FAKE_OUTCOME", "skipped")
    status = _gather()

    assert status["state"] == "done"
    assert any("nothing published" in note for note in status["notes"])
    assert store.resolve_current("tls") == version
    assert len(store._list_versions("tls")) == 1
    assert {k: v for k, v in kv._data.items() if "gather-cache" in k} == caches


def test_a_crashing_child_leaves_its_error_in_the_status(
    cloud: Tuple[InMemoryKvStore, CloudCorpusStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store = cloud
    monkeypatch.setenv("FAKE_RAISE", "datatracker said no")
    status = _gather()
    assert status["state"] == "failed"
    assert "exited with code 1: ValueError: datatracker said no" in status["error"]
    assert store.resolve_current("tls") is None


def test_a_real_preflight_skip_reaches_the_parent_as_skipped(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exit code 4 the way production reaches it: the child's own preflight sees
    # a fresh last-gathered sentinel (what a seeded workspace carries) and stops.
    # A synthetic corpus needs no group lookup, and the local backend needs no
    # config-store round trip, so this stays off the network.
    from ietf_llm import freshness
    from ietf_llm.gather.lifecycle import GatherOutcome

    monkeypatch.delenv("IETF_LLM_STORE_BACKEND", raising=False)
    monkeypatch.setenv("FAKE_REAL_PREFLIGHT", "1")
    monkeypatch.setenv("FAKE_LOG", str(isolated_home / "requests.jsonl"))
    monkeypatch.setattr(
        gather_pipeline,
        "_child_command",
        lambda spec: [sys.executable, "-I", str(_CHILD), *spec.to_argv()],
    )
    freshness.record_gather("x-demo")

    outcome = gather_pipeline._run_pipeline_child(
        gather_runner.GatherSpec(corpus="x-demo"), lambda *a: None, lambda m: None, lambda: False
    )
    assert outcome is GatherOutcome.SKIPPED
    assert not (isolated_home / "requests.jsonl").exists()  # no stage ran
