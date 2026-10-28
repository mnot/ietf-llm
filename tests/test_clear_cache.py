"""`--clear-cache` (issue #260 follow-up): it must wipe everything a gather of the
corpus would otherwise reuse -- the workspace, the cached mailing-list mail
(which lives outside the workspace on purpose, so a plain re-gather keeps it),
the corpus's own ETag store, a split embedding index, and on the cloud backend
the copies held in the store, which would otherwise be restored (and the mail
folded back in) by the very next gather. Other corpora and the shared caches
stay untouched.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import pytest

from ietf_llm.gather import lifecycle, sequencer
from ietf_llm.gather.sources import cache_sync, datatracker, mbox
from ietf_llm.log import Verbosity
from ietf_llm.paths import get_cache_dir
from ietf_llm.store.cloud import CloudCorpusStore
from ietf_llm.store.blobs import FileBlobStore
from ietf_llm.store.control import KvControlPlane
from ietf_llm.store.corpus import LocalCorpusStore
from ietf_llm.store.kv import InMemoryKvStore


def _populate(corpus: str) -> None:
    """Everything one gathered corpus leaves on a host."""
    root = Path(get_cache_dir())
    (root / corpus / "files").mkdir(parents=True)
    (root / corpus / "files" / "group.md").write_text("# group\n")
    (root / corpus / "embeddings.db").write_text("index")
    mail = root / "imap-cache" / corpus / corpus
    (mail / "epoch-7").mkdir(parents=True)
    (mail / "1.eml").write_text("Message-Id: <1@x>\n\nbody\n")
    (mail / "epoch-7" / "9.eml").write_text("Message-Id: <9@x>\n\nbody\n")
    (mail / mbox.UIDVALIDITY_FILE).write_text("8")
    etag = Path(datatracker.http_cache_path(corpus))
    etag.parent.mkdir(parents=True, exist_ok=True)
    etag.write_text("{}")


def _left(corpus: str) -> List[str]:
    root = Path(get_cache_dir())
    return sorted(
        str(path.relative_to(root))
        for tree in (root / corpus, root / "imap-cache" / corpus)
        for path in tree.rglob("*")
        if path.is_file()
    )


# --- this host ---------------------------------------------------------------


def test_clear_local_state_wipes_the_corpus_and_only_the_corpus(isolated_home: Path) -> None:
    _populate("tls")
    _populate("httpbis")
    shared = Path(datatracker.http_cache_path())  # one store shared by all corpora
    shared.write_text("{}")

    lifecycle.clear_local_state("tls", str(Path(get_cache_dir()) / "tls"))

    assert _left("tls") == []
    assert not Path(datatracker.http_cache_path("tls")).exists()
    assert _left("httpbis") == [  # all of it still there
        "httpbis/embeddings.db",
        "httpbis/files/group.md",
        "imap-cache/httpbis/httpbis/.uidvalidity",
        "imap-cache/httpbis/httpbis/1.eml",
        "imap-cache/httpbis/httpbis/epoch-7/9.eml",
    ]
    assert Path(datatracker.http_cache_path("httpbis")).exists()
    assert shared.exists()


def test_clear_local_state_removes_a_split_index(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    index_root = tmp_path / "index"
    monkeypatch.setenv("IETF_LLM_INDEX_DIR", str(index_root))
    for corpus in ("tls", "httpbis"):
        (index_root / corpus).mkdir(parents=True)
        (index_root / corpus / "embeddings.db").write_text("index")
        (index_root / corpus / "topics.json").write_text("{}")
        (index_root / corpus / "unrelated.txt").write_text("not an index file")

    lifecycle.clear_local_state("tls", str(Path(get_cache_dir()) / "tls"))

    assert sorted(p.name for p in (index_root / "tls").iterdir()) == ["unrelated.txt"]
    assert len(list((index_root / "httpbis").iterdir())) == 3


def test_clear_local_state_on_a_clean_host_is_a_no_op(isolated_home: Path) -> None:
    lifecycle.clear_local_state("tls", str(Path(get_cache_dir()) / "tls"))
    lifecycle.clear_local_state("tls", str(Path(get_cache_dir()) / "tls"))


def test_the_sequencer_clears_through_the_same_helper(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wipe happens in `_gather_one` too, so a caller that doesn't go through
    the lifecycle still gets all of it -- not just the workspace."""
    from ietf_llm.gather.cli import build_parser

    class _Stop(Exception):
        pass

    def stop(*_a: object, **_k: object) -> None:
        raise _Stop

    _populate("tls")
    monkeypatch.setattr(sequencer, "_maybe_seed", stop)
    monkeypatch.setattr(sequencer, "_resolve_corpus_shape", lambda *a, **k: (False, True))
    args = build_parser().parse_args(["tls", "--clear-cache"])
    with pytest.raises(_Stop):
        sequencer._gather_one(args, Verbosity.QUIET)
    assert _left("tls") == []
    assert not (Path(get_cache_dir()) / "imap-cache" / "tls").exists()


# --- the store -----------------------------------------------------------------


def _persisted(kv: InMemoryKvStore, corpus: str, monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Persist a corpus with two lists, one of them with an archive, and an ETag
    store, from a throwaway host."""
    monkeypatch.setenv("IETF_LLM_CACHE_DIR", str(home))
    for name in ("moq", "moq-chairs"):
        mail = home / "imap-cache" / corpus / name
        mail.mkdir(parents=True, exist_ok=True)
        (mail / "1.eml").write_text("Message-Id: <1@x>\n\nbody\n")
        (mail / mbox.UIDVALIDITY_FILE).write_text("8")
    archive = home / "imap-cache" / corpus / "moq" / "epoch-7"
    archive.mkdir()
    (archive / "9.eml").write_text("Message-Id: <9@x>\n\nbody\n")
    datatracker.bind_gather_corpus(corpus)
    datatracker._active_http_cache().store("u", "etag", "{}")
    cache_sync.persist(kv, corpus)
    datatracker.bind_gather_corpus(None)


def test_clear_deletes_a_corporas_stored_state_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    kv = InMemoryKvStore()
    _persisted(kv, "moq", monkeypatch, tmp_path / "a")
    _persisted(kv, "tls", monkeypatch, tmp_path / "b")
    kv.put("fleet/gather-cache/github-users.json", b"{}")
    mine = {k for k in kv._data if k.startswith("corpora/moq/")}
    assert any("/imap/moq/epoch-7/index.json" in k for k in mine)  # the archive too
    assert "corpora/moq/gather-cache/http-cache.json" in mine
    others = {k for k in kv._data if not k.startswith("corpora/moq/")}

    cache_sync.clear(kv, "moq")

    assert not [k for k in kv._data if k.startswith("corpora/moq/")]
    assert {k for k in kv._data if not k.startswith("corpora/moq/")} == others


def test_clear_then_hydrate_restores_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    kv = InMemoryKvStore()
    _persisted(kv, "moq", monkeypatch, tmp_path / "a")
    cache_sync.clear(kv, "moq")
    cold = tmp_path / "cold"
    cold.mkdir()
    monkeypatch.setenv("IETF_LLM_CACHE_DIR", str(cold))
    assert cache_sync.hydrate(kv, "moq") == []
    assert not (cold / "imap-cache").exists()


@pytest.mark.parametrize("failing", ["delete", "list_children"])
def test_a_failed_clear_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failing: str
) -> None:
    kv = InMemoryKvStore()
    _persisted(kv, "moq", monkeypatch, tmp_path / "a")

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("bucket unreachable")

    monkeypatch.setattr(kv, failing, boom)
    with pytest.raises(OSError, match="unreachable"):
        cache_sync.clear(kv, "moq")


def _cloud(tmp_path: Path, kv: Optional[InMemoryKvStore]) -> CloudCorpusStore:
    return CloudCorpusStore(
        KvControlPlane(kv or InMemoryKvStore()),
        FileBlobStore(str(tmp_path / "bucket")),
        str(tmp_path / "scratch"),
        kv=kv,
    )


def test_the_cloud_store_delegates_and_the_others_do_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    kv = InMemoryKvStore()
    _persisted(kv, "moq", monkeypatch, tmp_path / "a")
    _cloud(tmp_path, kv).clear_gather_caches("moq")
    assert not [k for k in kv._data if k.startswith("corpora/moq/")]

    _cloud(tmp_path, None).clear_gather_caches("moq")  # no KvStore: nothing to clear
    LocalCorpusStore().clear_gather_caches("moq")
