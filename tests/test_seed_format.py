"""Tests for the seed-store on-disk format (`ietf_llm.seed.format`).

Pure/stdlib module, so these use `tmp_path` directly (no isolated_home). Covers:
- reading the compatibility tuple from an embeddings.db meta table, and its errors
- CompatTuple.matches (all fields; unknown vector_dim)
- bundle member selection (files/ minus raw/, index files from a split index dir)
- bundle build -> extract round-trip, sha256 verification, and tamper detection
- manifest/index JSON round-trip and format-version rejection
- extraction refusing a path-traversal member
"""

from __future__ import annotations

import os
import sqlite3
import tarfile

import pytest

from ietf_llm.seed import format as fmt


def _make_db(path, *, model="sentence-transformers/BAAI/bge-small-en-v1.5",
             schema="8", chunker="2", embed_dim="384", with_meta=True):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    if with_meta:
        rows = [("model", model), ("schema_version", schema),
                ("chunker_version", chunker)]
        if embed_dim is not None:
            rows.append(("embed_dim", embed_dim))
        conn.executemany("INSERT INTO meta VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


def test_read_compat_tuple_roundtrip(tmp_path):
    db = tmp_path / "embeddings.db"
    _make_db(db)
    ct = fmt.read_compat_tuple(str(db))
    assert ct.schema_version == 8
    assert ct.embedding_model.endswith("bge-small-en-v1.5")
    assert ct.chunker_version == "2"
    assert ct.vector_dim == 384


def test_read_compat_tuple_missing_file(tmp_path):
    with pytest.raises(fmt.SeedFormatError):
        fmt.read_compat_tuple(str(tmp_path / "nope.db"))


def test_read_compat_tuple_missing_keys(tmp_path):
    db = tmp_path / "empty.db"
    _make_db(db, with_meta=False)
    with pytest.raises(fmt.SeedFormatError):
        fmt.read_compat_tuple(str(db))


def test_read_compat_tuple_optional_embed_dim(tmp_path):
    db = tmp_path / "nodim.db"
    _make_db(db, embed_dim=None)
    assert fmt.read_compat_tuple(str(db)).vector_dim is None


def test_compat_matches():
    a = fmt.CompatTuple(8, "m", "2", 384)
    assert a.matches(fmt.CompatTuple(8, "m", "2", 384))
    assert not a.matches(fmt.CompatTuple(8, "other", "2", 384))
    assert not a.matches(fmt.CompatTuple(7, "m", "2", 384))
    assert not a.matches(fmt.CompatTuple(8, "m", "3", 384))
    assert not a.matches(fmt.CompatTuple(8, "m", "2", 512))
    # Unknown dim on either side does not veto.
    assert a.matches(fmt.CompatTuple(8, "m", "2", None))
    assert fmt.CompatTuple(8, "m", "2", None).matches(a)


def _seed_corpus(root):
    """A minimal corpus tree under root/<corpus>/ + index files; returns
    (corpus_dir, index_dir)."""
    corpus = os.path.join(root, "httpbis")
    files = os.path.join(corpus, "files")
    os.makedirs(os.path.join(files, "threads"))
    os.makedirs(os.path.join(files, "raw"))
    os.makedirs(os.path.join(files, "github"))
    with open(os.path.join(files, "charter.txt"), "w") as fh:
        fh.write("charter")
    with open(os.path.join(files, "threads", "t.md"), "w") as fh:
        fh.write("thread")
    with open(os.path.join(files, "github", "repo.json"), "w") as fh:
        fh.write("{}")
    with open(os.path.join(files, "raw", "mail-2026.txt"), "w") as fh:
        fh.write("raw dump")
    with open(os.path.join(corpus, "documents.json"), "w") as fh:
        fh.write("{}")
    with open(os.path.join(corpus, "last-gathered"), "w") as fh:
        fh.write("2026-07-01T00:00:00Z")
    with open(os.path.join(corpus, "gather-metrics.json"), "w") as fh:
        fh.write("{}")
    with open(os.path.join(corpus, "seed-source"), "w") as fh:
        fh.write("{}")  # a producer that itself seeds
    _make_db(os.path.join(corpus, "embeddings.db"))
    with open(os.path.join(corpus, "topics.json"), "w") as fh:
        fh.write("{}")
    return corpus, corpus  # index_dir == corpus_dir (default layout)


def test_iter_bundle_members_selection(tmp_path):
    corpus, index = _seed_corpus(str(tmp_path))
    arcs = [a for a, _ in fmt.iter_bundle_members(corpus, index)]
    assert "files/charter.txt" in arcs
    assert "files/threads/t.md" in arcs
    assert "files/github/repo.json" in arcs
    assert "documents.json" in arcs
    assert "last-gathered" in arcs
    assert "embeddings.db" in arcs
    assert "topics.json" in arcs
    # Excluded:
    assert not any(a.startswith("files/raw/") for a in arcs)
    assert "gather-metrics.json" not in arcs
    assert "seed-source" not in arcs


def test_iter_bundle_members_split_index(tmp_path):
    corpus, _ = _seed_corpus(str(tmp_path))
    # Move the index files to a separate dir (simulating IETF_LLM_INDEX_DIR).
    index = os.path.join(str(tmp_path), "idx", "httpbis")
    os.makedirs(index)
    for name in ("embeddings.db", "topics.json"):
        os.replace(os.path.join(corpus, name), os.path.join(index, name))
    members = dict(fmt.iter_bundle_members(corpus, index))
    assert members["embeddings.db"] == os.path.join(index, "embeddings.db")
    assert "topics.json" in members


def test_build_and_extract_roundtrip(tmp_path):
    corpus, index = _seed_corpus(str(tmp_path))
    members = fmt.iter_bundle_members(corpus, index)
    bundle = os.path.join(str(tmp_path), "out", "httpbis.tar.gz")
    digest, size = fmt.build_bundle(members, bundle)
    assert size == os.path.getsize(bundle)
    fmt.verify_sha256(bundle, digest)
    dest = os.path.join(str(tmp_path), "installed")
    fmt.extract_bundle(bundle, dest)
    assert open(os.path.join(dest, "files", "charter.txt")).read() == "charter"
    assert os.path.isfile(os.path.join(dest, "embeddings.db"))
    assert not os.path.exists(os.path.join(dest, "files", "raw"))


def test_verify_sha256_detects_tamper(tmp_path):
    corpus, index = _seed_corpus(str(tmp_path))
    bundle = os.path.join(str(tmp_path), "b.tar.gz")
    digest, _ = fmt.build_bundle(fmt.iter_bundle_members(corpus, index), bundle)
    with open(bundle, "ab") as fh:
        fh.write(b"tampered")
    with pytest.raises(fmt.SeedFormatError):
        fmt.verify_sha256(bundle, digest)


def test_manifest_roundtrip():
    m = fmt.Manifest(
        name="httpbis", version="20260701T000000Z",
        compat=fmt.CompatTuple(8, "m", "2", 384),
        window_months=12, gathered="2026-07-01T00:00:00Z",
        bundle="httpbis/httpbis-20260701T000000Z.tar.gz",
        bundle_sha256="abc", bundle_bytes=123)
    back = fmt.manifest_from_json(fmt.manifest_to_json(m))
    assert back == m


def test_index_roundtrip_and_lookup():
    idx = fmt.Index(
        generated="2026-07-01T00:00:00Z",
        compat=fmt.CompatTuple(8, "m", "2", 384),
        corpora=[fmt.IndexEntry("httpbis", "group", "HTTP WG", 12,
                                "2026-07-01T00:00:00Z", "20260701T000000Z",
                                "httpbis/manifest.json", 999)])
    back = fmt.Index.from_json(idx.to_json())
    assert back.compat == idx.compat
    assert back.entry("httpbis").version == "20260701T000000Z"
    assert back.entry("absent") is None


def test_index_rejects_unknown_format():
    with pytest.raises(fmt.SeedFormatError):
        fmt.Index.from_json('{"format": 999, "corpora": []}')


def test_extract_refuses_path_traversal(tmp_path):
    evil = os.path.join(str(tmp_path), "evil.tar.gz")
    victim = os.path.join(str(tmp_path), "payload.txt")
    with open(victim, "w") as fh:
        fh.write("x")
    with tarfile.open(evil, "w:gz") as tar:
        tar.add(victim, arcname="../escape.txt", recursive=False)
    with pytest.raises(fmt.SeedFormatError):
        fmt.extract_bundle(evil, os.path.join(str(tmp_path), "dest"))


def test_extract_refuses_symlink(tmp_path):
    # A symlink member could redirect a later write outside the destination; the
    # isfile/isdir guard must reject it (only path-traversal was covered before).
    evil = os.path.join(str(tmp_path), "evil.tar.gz")
    with tarfile.open(evil, "w:gz") as tar:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    with pytest.raises(fmt.SeedFormatError):
        fmt.extract_bundle(evil, os.path.join(str(tmp_path), "dest"))
