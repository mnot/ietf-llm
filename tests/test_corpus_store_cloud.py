"""Tests for the cloud CorpusStore backend (publish protocol + read path)."""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

import pytest

from ietf_llm.store.blobs import FileBlobStore
from ietf_llm.store.corpus import VersionVanished, pin_corpus_version
from ietf_llm.store.cloud import CloudCorpusStore, _clear_resolve_cache
from ietf_llm.store.control import KvControlPlane
from ietf_llm.store.kv import InMemoryKvStore


def _store(tmp_path: Path) -> Tuple[CloudCorpusStore, KvControlPlane]:
    control = KvControlPlane(InMemoryKvStore())
    blobs = FileBlobStore(str(tmp_path / "bucket"))
    store = CloudCorpusStore(control, blobs, str(tmp_path / "scratch"))
    return store, control


def _workspace(tmp_path: Path, name: str, index_body: str) -> str:
    ws = tmp_path / name
    (ws / "files" / "digests").mkdir(parents=True)
    (ws / "files" / "digests" / "index.md").write_text(index_body)
    (ws / "last-gathered").write_text("2026-06-04T00:00:00Z")
    return str(ws)


def test_publish_then_read_roundtrip(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    ws = _workspace(tmp_path, "ws", "hello")
    assert store.publish("tls", ws, version="v1") == "v1"
    assert store.resolve_current("tls") == "v1"
    assert store.list_corpora() == ["tls"]
    assert store.corpus_exists("tls") is True
    cache = store.local_cache_dir("tls")
    assert cache is not None
    assert (Path(cache) / "digests" / "index.md").read_text() == "hello"


def test_manifest_is_a_blob_but_stripped_from_the_served_tree(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    store.publish("tls", _workspace(tmp_path, "ws", "hi"), version="v1")
    cache = store.local_cache_dir("tls")
    assert cache is not None
    version_root = Path(cache).parent  # scratch/tls/v1
    # The manifest is persisted as a blob in the version prefix...
    blob = tmp_path / "bucket" / "corpora" / "tls" / "versions" / "v1"
    assert (blob / "manifest.json").exists()
    # ...but stripped from the materialised tree, so a re-gather workspace seeded
    # from it never re-uploads it as content.
    assert not (version_root / "manifest.json").exists()
    assert not (Path(cache) / "manifest.json").exists()


def test_local_cache_dir_absent_corpus(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    assert store.resolve_current("ghost") is None
    assert store.corpus_exists("ghost") is False
    assert store.local_cache_dir("ghost") is None


def test_second_publish_moves_pointer(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    store.publish("tls", _workspace(tmp_path, "ws1", "first"), version="v1")
    store.publish("tls", _workspace(tmp_path, "ws2", "second"), version="v2")
    assert store.resolve_current("tls") == "v2"
    cache = store.local_cache_dir("tls")
    assert cache is not None
    assert (Path(cache) / "digests" / "index.md").read_text() == "second"


def test_abandoned_publish_leaves_prior_version(tmp_path: Path) -> None:
    store, control = _store(tmp_path)
    store.publish("tls", _workspace(tmp_path, "ws1", "first"), version="v1")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("pointer flip failed")

    # Simulate a crash after blobs are staged but before the pointer flips.
    control.set_current = _boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        store.publish("tls", _workspace(tmp_path, "ws2", "second"), version="v2")

    # The prior version is still current; the staged v2 blobs are never seen.
    assert store.resolve_current("tls") == "v1"
    cache = store.local_cache_dir("tls")
    assert cache is not None
    assert (Path(cache) / "digests" / "index.md").read_text() == "first"


# --- GC: reap superseded version blobs on publish (keep current + previous) ---


def test_publish_reaps_old_versions_keeping_current_and_previous(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    store.publish("tls", _workspace(tmp_path, "w1", "1"), version="v1")
    store.publish("tls", _workspace(tmp_path, "w2", "2"), version="v2")
    store.publish("tls", _workspace(tmp_path, "w3", "3"), version="v3")
    # Default retain=2 → keep v3 (current) + v2 (previous); v1 is reaped.
    assert store._list_versions("tls") == ["v2", "v3"]
    # The current version is still fully readable after the reap.
    cache = store.local_cache_dir("tls")
    assert cache is not None
    assert (Path(cache) / "digests" / "index.md").read_text() == "3"


def test_retain_versions_knob_keeps_more(tmp_path: Path) -> None:
    control = KvControlPlane(InMemoryKvStore())
    blobs = FileBlobStore(str(tmp_path / "bucket"))
    store = CloudCorpusStore(
        control, blobs, str(tmp_path / "scratch"), retain_versions=3
    )
    for n in range(1, 5):
        store.publish("tls", _workspace(tmp_path, f"w{n}", str(n)), version=f"v{n}")
    # retain=3 → keep v4, v3, v2; only v1 reaped.
    assert store._list_versions("tls") == ["v2", "v3", "v4"]


def test_retain_versions_floor_is_one(tmp_path: Path) -> None:
    control = KvControlPlane(InMemoryKvStore())
    blobs = FileBlobStore(str(tmp_path / "bucket"))
    # A sub-1 value is floored to 1 (the current version is never reaped).
    store = CloudCorpusStore(
        control, blobs, str(tmp_path / "scratch"), retain_versions=0
    )
    store.publish("tls", _workspace(tmp_path, "w1", "1"), version="v1")
    store.publish("tls", _workspace(tmp_path, "w2", "2"), version="v2")
    assert store._list_versions("tls") == ["v2"]


def test_reap_ranks_current_off_pointer_not_id_age(tmp_path: Path) -> None:
    # An old-id version that is *still current* must survive even though a
    # higher-id prefix exists — the keep-set ranks current off the pointer, not
    # the version id's embedded timestamp (issue #87 trap 1).
    store, control = _store(tmp_path)
    store._retain_versions = 1
    store._blobs.put("corpora/tls/versions/2020-current/manifest.json", b"{}")
    store._blobs.put("corpora/tls/versions/2026-orphan/manifest.json", b"{}")
    control.set_current("tls", "2020-current")
    store._reap_versions("tls", "2020-current")
    assert store._list_versions("tls") == ["2020-current"]


def test_publish_reaps_failed_publish_orphan(tmp_path: Path) -> None:
    control = KvControlPlane(InMemoryKvStore())
    blobs = FileBlobStore(str(tmp_path / "bucket"))
    store = CloudCorpusStore(
        control, blobs, str(tmp_path / "scratch"), retain_versions=1
    )
    store.publish("tls", _workspace(tmp_path, "w1", "1"), version="v1")
    # A prefix staged by a publish whose pointer flip never landed: it carries a
    # version id but is referenced by nothing.
    store._blobs.put("corpora/tls/versions/orphan/manifest.json", b"{}")
    store._blobs.put("corpora/tls/versions/orphan/files/x.md", b"x")
    store.publish("tls", _workspace(tmp_path, "w2", "2"), version="v2")
    # retain=1 keeps only the current version; v1 and the orphan are both gone.
    assert store._list_versions("tls") == ["v2"]


def test_reap_failure_never_fails_an_already_succeeded_publish(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    store.publish("tls", _workspace(tmp_path, "w1", "1"), version="v1")

    def _boom(_prefix: str) -> None:
        raise RuntimeError("delete failed")

    store._blobs.delete_prefix = _boom  # type: ignore[method-assign]
    # The pointer still flips and publish returns normally despite the reap error.
    assert store.publish("tls", _workspace(tmp_path, "w2", "2"), version="v2") == "v2"
    assert store.resolve_current("tls") == "v2"


# --- vanished-version recovery on the read path (couples with version GC) -----


def _two_replicas(
    tmp_path: Path,
    *,
    reader_ttl: float = 0.0,
) -> Tuple[CloudCorpusStore, CloudCorpusStore, KvControlPlane]:
    """A reader and a writer replica over one shared control plane + blob store,
    each with its own scratch and resolve-cache scope — so a publish by the
    writer reaps blobs the reader may still believe are current."""
    _clear_resolve_cache()
    control = KvControlPlane(InMemoryKvStore())
    blobs = FileBlobStore(str(tmp_path / "bucket"))
    reader = CloudCorpusStore(
        control, blobs, str(tmp_path / "scratch-r"), resolve_ttl=reader_ttl, cache_key="r"
    )
    writer = CloudCorpusStore(
        control, blobs, str(tmp_path / "scratch-w"), retain_versions=1, cache_key="w"
    )
    return reader, writer, control


def test_unpinned_read_recovers_from_reaped_version(tmp_path: Path) -> None:
    # The reader caches v1 as current (TTL 100s), then the writer publishes v2
    # and reaps v1's blobs. An unpinned read on the reader hits the dead v1,
    # re-resolves the pointer cache-bypassing, and retries on v2 transparently.
    reader, writer, _ = _two_replicas(tmp_path, reader_ttl=100.0)
    reader.publish("tls", _workspace(tmp_path, "w1", "first"), version="v1")
    writer.publish("tls", _workspace(tmp_path, "w2", "second"), version="v2")
    cache = reader.local_cache_dir("tls")
    assert cache is not None
    assert (Path(cache) / "digests" / "index.md").read_text() == "second"


def test_pinned_read_raises_version_vanished(tmp_path: Path) -> None:
    # A request pinned to v1 cannot silently swap versions; the reaped v1 surfaces
    # as a typed VersionVanished naming the now-current v2.
    reader, writer, _ = _two_replicas(tmp_path)
    reader.publish("tls", _workspace(tmp_path, "w1", "first"), version="v1")
    writer.publish("tls", _workspace(tmp_path, "w2", "second"), version="v2")
    with pin_corpus_version("tls", "v1"):
        with pytest.raises(VersionVanished) as exc:
            reader.local_cache_dir("tls")
    assert exc.value.old_version == "v1"
    assert exc.value.new_version == "v2"
    # It stays a FileNotFoundError, so unaware callers still catch it.
    assert isinstance(exc.value, FileNotFoundError)


def test_genuine_loss_of_live_version_reraises_plain(tmp_path: Path) -> None:
    # The live version's own blobs are gone but the pointer still names it: this
    # is real data loss, not supersession, so it re-raises a plain
    # FileNotFoundError rather than a VersionVanished.
    store, _ = _store(tmp_path)
    store.publish("tls", _workspace(tmp_path, "w1", "first"), version="v1")
    store._blobs.delete_prefix("corpora/tls/versions/v1/")
    with pytest.raises(FileNotFoundError) as exc:
        store.local_cache_dir("tls")
    assert not isinstance(exc.value, VersionVanished)


# --- seed_workspace: pre-populate a gather workspace from the current version


def _versioned_workspace(tmp_path: Path, name: str, draft: str, db: str) -> str:
    """A workspace shaped like a real published version: a `files/` tree plus a
    top-level `embeddings.db` (the index)."""
    ws = tmp_path / name
    (ws / "files" / "drafts").mkdir(parents=True)
    (ws / "files" / "drafts" / "d.txt").write_text(draft)
    (ws / "embeddings.db").write_text(db)
    return str(ws)


def test_seed_workspace_default_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = _store(tmp_path)
    store.publish("tls", _versioned_workspace(tmp_path, "src", "draft", "IDX"), "v1")

    # Index dir == workspace parent: the default layout, where embeddings.db
    # belongs inside the swapped workspace.
    monkeypatch.setenv("IETF_LLM_INDEX_DIR", str(tmp_path / "cache"))
    dest = tmp_path / "cache" / "tls"
    assert store.seed_workspace("tls", str(dest)) == "v1"
    assert (dest / "files" / "drafts" / "d.txt").read_text() == "draft"
    assert (dest / "embeddings.db").read_text() == "IDX"


def test_seed_workspace_split_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = _store(tmp_path)
    store.publish("tls", _versioned_workspace(tmp_path, "src", "draft", "IDX"), "v1")

    # Split index (IETF_LLM_INDEX_DIR off the cache): the DB must land where
    # build_index reads it, not in the workspace.
    monkeypatch.setenv("IETF_LLM_INDEX_DIR", str(tmp_path / "index"))
    dest = tmp_path / "cache" / "tls"
    assert store.seed_workspace("tls", str(dest)) == "v1"
    assert (dest / "files" / "drafts" / "d.txt").read_text() == "draft"
    assert not (dest / "embeddings.db").exists()
    assert (tmp_path / "index" / "tls" / "embeddings.db").read_text() == "IDX"


def test_seed_workspace_no_published_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = _store(tmp_path)
    monkeypatch.setenv("IETF_LLM_INDEX_DIR", str(tmp_path / "cache"))
    dest = tmp_path / "cache" / "ghost"
    assert store.seed_workspace("ghost", str(dest)) is None
    assert not dest.exists()


def test_seed_workspace_replaces_stale_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = _store(tmp_path)
    store.publish("tls", _versioned_workspace(tmp_path, "src", "v1draft", "IDX"), "v1")
    monkeypatch.setenv("IETF_LLM_INDEX_DIR", str(tmp_path / "cache"))
    dest = tmp_path / "cache" / "tls"
    # A stale workspace from an older gather: a file absent in v1.
    (dest / "files" / "drafts").mkdir(parents=True)
    (dest / "files" / "drafts" / "old.txt").write_text("stale")
    assert store.seed_workspace("tls", str(dest)) == "v1"
    assert (dest / "files" / "drafts" / "d.txt").read_text() == "v1draft"
    assert not (dest / "files" / "drafts" / "old.txt").exists()


# --- read-path access marker + gather time (cloud backend) ----------------


def test_cloud_record_access_round_trips_via_control_plane(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    store, control = _store(tmp_path)
    assert store.last_accessed("tls") is None
    store.record_access("tls")
    # It landed under the corpus's control-plane access key.
    assert control.get_access("tls") is not None
    when = store.last_accessed("tls")
    assert when is not None
    assert abs((datetime.now(timezone.utc) - when).total_seconds()) < 60


def test_cloud_gathered_at_parses_version_timestamp(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    ws = _workspace(tmp_path, "ws", "hello")
    # A real (_new_version) token, so gathered_at exercises the actual format.
    version = store.publish("tls", ws)
    when = store.gathered_at("tls")
    assert when is not None
    assert when.strftime("%Y%m%dT%H%M%SZ") == version.split("-", 1)[0]


def test_cloud_gathered_at_none_for_absent_corpus(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    assert store.gathered_at("ghost") is None
