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
from ietf_llm.utils import Verbosity, get_wg_file_cache_dir

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
    assert seen and all(s.endswith("%") for s in seen)
