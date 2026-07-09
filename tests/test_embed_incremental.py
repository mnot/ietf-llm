"""The incremental embed skip keys on each file's content hash, not its
mtime. Two consequences this guards:

  - identical bytes with a *newer* mtime are still skipped — the cross-host
    case (a cloud replica materialises a published version onto fresh local
    files), where the old mtime rule would re-embed the whole corpus;
  - changed bytes with an *older* mtime are still re-embedded — where the old
    mtime rule (`recorded >= on-disk`) would wrongly skip them.

Also covers the one-time migration that sweeps an older index's stale
`mtime:<relpath>` rows.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Iterable, List

from ietf_llm import embeddings
from ietf_llm.embeddings.search import build_index
from ietf_llm.embeddings.storage import _db_path
from ietf_llm.log import Verbosity
from ietf_llm.paths import get_wg_file_cache_dir

from conftest import write_cache_file


class _StubModel:
    """Emits a fixed unit vector regardless of input."""

    def embed(self, _text: str) -> Iterable[float]:
        return [1.0, 0.0, 0.0, 0.0]

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


def _seed() -> None:
    embeddings._MODEL_CACHE["stub"] = _StubModel()  # noqa


def _meta_keys(wg: str, like: str) -> List[str]:
    conn = sqlite3.connect(_db_path(wg))
    try:
        return [
            r[0]
            for r in conn.execute(
                "SELECT key FROM meta WHERE key LIKE ?", (like,)
            ).fetchall()
        ]
    finally:
        conn.close()


def _build(wg: str) -> int:
    return build_index(
        wg, get_wg_file_cache_dir(wg), model_name="stub", verbose=Verbosity.QUIET
    )


class _CountingStub:
    """Like `_StubModel`, but records every text it is asked to embed, so a test
    can assert *which* chunks were (re-)embedded."""

    def __init__(self) -> None:
        self.embedded: List[str] = []

    def embed(self, text: str) -> Iterable[float]:
        self.embedded.append(text)
        return [1.0, 0.0, 0.0, 0.0]

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


_THREAD_HEAD = (
    "# Topic A\n\n**Span:** 2025-01-01 → 2025-01-03\n**Messages:** 3\n\n"
    "## Messages\n\n"
)
_MSG1 = "### [1] 2025-01-01 10:00 — Alice\n\nfirst message body\n\n"
_MSG2 = "### [2] 2025-01-02 11:00 — Bob\n\nsecond message body\n\n"
_MSG3 = "### [3] 2025-01-03 12:00 — Carol\n\nthird message body\n"


def _chunk_rows(wg: str, relpath: str) -> List[tuple]:
    conn = sqlite3.connect(_db_path(wg))
    try:
        return conn.execute(
            "SELECT chunk_idx, embedding, chunk_hash FROM chunks WHERE file=? "
            "ORDER BY chunk_idx, sub_idx",
            (relpath,),
        ).fetchall()
    finally:
        conn.close()


def test_append_reembeds_only_new_message(isolated_home: Path) -> None:
    # Issue #183: a thread that gains one message embeds only that message; the
    # earlier messages keep their stored vectors.
    stub = _CountingStub()
    embeddings._MODEL_CACHE["stub"] = stub  # noqa
    write_cache_file(isolated_home, "wg", "threads/t.md", _THREAD_HEAD + _MSG1 + _MSG2)
    assert _build("wg") > 0
    assert any("first message body" in t for t in stub.embedded)
    assert any("second message body" in t for t in stub.embedded)
    before = _chunk_rows("wg", "threads/t.md")
    assert all(h is not None for _, _, h in before)

    stub.embedded.clear()
    write_cache_file(
        isolated_home, "wg", "threads/t.md", _THREAD_HEAD + _MSG1 + _MSG2 + _MSG3
    )
    assert _build("wg") > 0
    # The whole point of #183: [1] and [2] were NOT re-embedded (their vectors
    # are reused); only the appended [3] was embedded. (A one-off "dimension
    # probe" embed is unrelated, so assert on the message texts.)
    assert not any(
        "first message body" in t or "second message body" in t
        for t in stub.embedded
    )
    assert sum("third message body" in t for t in stub.embedded) == 1
    # Exactly one new chunk; every prior chunk carried forward its exact stored
    # vector (reused, not re-embedded), and all chunks now carry a hash.
    after = _chunk_rows("wg", "threads/t.md")
    assert len(after) == len(before) + 1
    assert [row[1] for row in after[: len(before)]] == [row[1] for row in before]
    assert all(h is not None for _, _, h in after)


def test_build_index_returns_changed_flag(isolated_home: Path) -> None:
    # Issue #190: build_index reports whether the index changed, so the gather can
    # skip the topic-map recompute on a no-op re-gather.
    _seed()
    p = write_cache_file(isolated_home, "wg", "drafts/draft-x.txt", "body text\n")
    assert _build("wg") is True  # first build embeds → changed
    assert _build("wg") is False  # nothing changed → not changed
    p.write_text("different body\n")
    assert _build("wg") is True  # content changed → changed
    assert _build("wg") is False
    os.remove(p)
    assert _build("wg") is True  # file gone → chunks pruned → changed


def test_has_topics_reflects_sidecar(isolated_home: Path) -> None:
    from ietf_llm.embeddings import has_topics
    from ietf_llm.embeddings.storage import _topics_path

    assert has_topics("wg") is False
    path = _topics_path("wg", write=True)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()
    assert has_topics("wg") is True


def test_unchanged_file_skipped_despite_newer_mtime(isolated_home: Path) -> None:
    path = write_cache_file(isolated_home, "wg", "drafts/draft-x.txt", "body text\n")
    _seed()
    assert _build("wg") > 0  # first build embeds the file

    # Simulate a cross-host materialise: identical bytes, brand-new mtime.
    # The old mtime rule (recorded >= on-disk) would see the future mtime as
    # "advanced" and re-embed; the content hash is unchanged, so it must skip.
    os.utime(path, (1 << 31, 1 << 31))
    assert _build("wg") == 0


def test_changed_content_reembeds_even_with_older_mtime(isolated_home: Path) -> None:
    path = write_cache_file(isolated_home, "wg", "drafts/draft-x.txt", "body text\n")
    _seed()
    assert _build("wg") > 0

    # Change the bytes but backdate the mtime. The old rule (recorded >=
    # on-disk) would wrongly skip; the hash differs, so it must re-embed.
    path.write_text("different body text\n")
    os.utime(path, (1, 1))
    assert _build("wg") > 0


def test_stale_mtime_rows_migrated(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "drafts/draft-x.txt", "body text\n")
    _seed()
    _build("wg")

    # Inject a stale row as an older (mtime-keyed) index would have left it.
    conn = sqlite3.connect(_db_path("wg"))
    conn.execute("INSERT INTO meta(key, value) VALUES('mtime:drafts/draft-x.txt', '1')")
    conn.commit()
    conn.close()

    _build("wg")
    assert _meta_keys("wg", "mtime:%") == []
    assert _meta_keys("wg", "hash:%") == ["hash:drafts/draft-x.txt"]


def test_embed_feeds_percent_to_detail(isolated_home: Path, monkeypatch) -> None:
    # The on-device embed feeds a byte-weighted % into the stage `detail`
    # callback that gather_status surfaces. Force the progress throttle to fire
    # on every file so a fast stub build still emits at least once.
    import importlib

    # `embeddings.search` the attribute is the exported function, not the
    # module, so fetch the module object explicitly to patch its constant.
    search_module = importlib.import_module("ietf_llm.embeddings.search")
    monkeypatch.setattr(search_module, "_PROGRESS_SECS", 0)
    for i in range(3):
        write_cache_file(isolated_home, "wg", f"drafts/draft-{i}.txt", f"body {i}\n")
    _seed()
    seen: List[str] = []
    build_index(
        "wg",
        get_wg_file_cache_dir("wg"),
        model_name="stub",
        verbose=Verbosity.QUIET,
        detail=seen.append,
    )
    assert seen and all("%" in s and "embedded" in s for s in seen)


def test_embed_progress_eta_phrase() -> None:
    # The detail phrase is self-describing and carries a linear byte-ETA: 25%
    # of the bytes done after ~40s implies ~120s (3x) remaining.
    import time as _time

    from ietf_llm.embeddings.search import _embed_progress

    seen: List[str] = []
    _embed_progress(25, 100, _time.time() - 40, Verbosity.QUIET, seen.append)
    assert seen == ["25% embedded, ~2m00s left"]


def test_embed_progress_withholds_early_eta() -> None:
    # Below the threshold the estimate is withheld — an extrapolation from the
    # first few percent is noise. Just the percent, no "~... left".
    import time as _time

    from ietf_llm.embeddings.search import _embed_progress

    seen: List[str] = []
    _embed_progress(5, 100, _time.time() - 10, Verbosity.QUIET, seen.append)
    assert seen == ["5% embedded"]
