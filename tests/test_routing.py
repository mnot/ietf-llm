"""Centroid routing — `route`, the fleet-key CAS-merge, and the
`which_corpus` tool (issue #116, item 2).

Uses the content-aware stub model (axis per theme keyword) so corpora are
genuinely separable in centroid space; the writer→reader path (build_index →
generate_topics → topics.json → load_routing_table → route) runs end to end
against real sidecars, not hand-built fixtures.
"""

from __future__ import annotations
from ietf_llm import mcp

from pathlib import Path
from typing import Iterable, List

from ietf_llm import embeddings
from ietf_llm.corpus import routing
from ietf_llm.embeddings.search import build_index
from ietf_llm.embeddings.topics import generate_topics, routing_projection
from ietf_llm.store.kv import InMemoryKvStore
from ietf_llm.mcp.corpus import tool_which_corpus
from ietf_llm.log import Verbosity
from ietf_llm.paths import get_wg_file_cache_dir

from conftest import write_cache_file

_AXES = {"quic": 0, "tls": 1, "http": 2, "dnssec": 3}


class _Stub:
    def embed(self, text: str) -> Iterable[float]:
        low = text.lower()
        vec = [0.0] * 8
        vec[_AXES[max(_AXES, key=low.count)]] = 1.0
        return vec

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


def _thread(subject: str, keyword: str) -> str:
    return (
        f"# {subject}\n\n**Span:** 2025-01-01 → 2025-03-01\n**Messages:** 1\n\n"
        f"## Messages\n\n### [1] 2025-03-01 10:00 — Alice\n\n{keyword} " * 1
        + f"{keyword} " * 30
        + "\n"
    )


def _seed_corpus(home: Path, wg: str, keyword: str, model: str = "stub") -> None:
    embeddings._MODEL_CACHE[model] = _Stub()  # pylint: disable=protected-access
    for n in range(8):  # over the topic-map floor
        write_cache_file(
            home, wg, f"threads/2025-{keyword}-{n}.md",
            _thread(f"{keyword} discussion item {n}", keyword),
        )
    build_index(wg, get_wg_file_cache_dir(wg), model_name=model, verbose=Verbosity.QUIET)
    assert generate_topics(wg, verbose=Verbosity.QUIET)


def test_route_picks_right_corpus(isolated_home: Path) -> None:
    _seed_corpus(isolated_home, "quicwg", "quic")
    _seed_corpus(isolated_home, "tlswg", "tls")

    res = routing.route("quic loss recovery tuning")
    assert res.matches
    assert res.matches[0].corpus == "quicwg"
    assert res.confident
    assert res.model_id == "stub"

    res2 = routing.route("tls handshake message")
    assert res2.matches[0].corpus == "tlswg"
    assert res2.confident


def test_route_abstains_on_absent_topic(isolated_home: Path) -> None:
    _seed_corpus(isolated_home, "quicwg", "quic")
    _seed_corpus(isolated_home, "tlswg", "tls")
    # Neither corpus is about dnssec — nothing should clear the floor.
    res = routing.route("dnssec zone validation")
    assert not res.confident


def test_route_reports_no_centroids(isolated_home: Path) -> None:
    _seed_corpus(isolated_home, "quicwg", "quic")
    _seed_corpus(isolated_home, "tlswg", "tls")
    # A corpus with an index but no topic map (too few docs to cluster).
    embeddings._MODEL_CACHE["stub"] = _Stub()  # pylint: disable=protected-access
    write_cache_file(isolated_home, "tiny", "threads/x.md", _thread("quic a", "quic"))
    build_index("tiny", get_wg_file_cache_dir("tiny"), model_name="stub", verbose=Verbosity.QUIET)
    res = routing.route("quic loss recovery")
    assert "tiny" in res.no_centroids
    assert res.matches[0].corpus == "quicwg"


def test_route_gates_on_model_id(isolated_home: Path) -> None:
    _seed_corpus(isolated_home, "quicwg", "quic")
    _seed_corpus(isolated_home, "tlswg", "tls")
    # A third corpus on a different embedding model is not score-comparable.
    _seed_corpus(isolated_home, "othermodel", "http", model="stub2")
    res = routing.route("quic loss recovery")
    # Majority model is "stub" (2 corpora); the stub2 corpus is reported skipped.
    assert res.model_id == "stub"
    assert "othermodel" in res.skipped_other_model


def test_fleet_key_cas_merge_roundtrip() -> None:
    kv = InMemoryKvStore()
    assert routing.read_fleet_table(kv) == {}
    routing.persist_corpus_entry(kv, "tls", {"model_id": "m", "dim": 8, "centroids": ["AA=="]})
    routing.persist_corpus_entry(kv, "quic", {"model_id": "m", "dim": 8, "centroids": ["BB=="]})
    table = routing.read_fleet_table(kv)
    assert set(table) == {"tls", "quic"}  # second merge did not clobber the first
    # Re-publishing a corpus replaces only its own entry.
    routing.persist_corpus_entry(kv, "tls", {"model_id": "m", "dim": 8, "centroids": ["CC=="]})
    assert routing.read_fleet_table(kv)["tls"]["centroids"] == ["CC=="]
    assert "quic" in routing.read_fleet_table(kv)


def test_routing_projection_skips_unusable() -> None:
    assert routing_projection({"model_id": "", "clusters": []}) is None
    assert routing_projection({"model_id": "m", "clusters": []}) is None
    out = routing_projection(
        {"model_id": "m", "dim": 8, "clusters": [{"centroid": "AA=="}, {"size": 3}]}
    )
    assert out == {"model_id": "m", "dim": 8, "centroids": ["AA=="]}


def test_which_corpus_tool_renders(isolated_home: Path) -> None:
    _seed_corpus(isolated_home, "quicwg", "quic")
    _seed_corpus(isolated_home, "tlswg", "tls")
    out = tool_which_corpus("quic loss recovery")
    assert "**quicwg**" in out
    assert "search_corpus" in out
    # Abstention path mentions find_efforts as the fallback.
    assert "find_efforts" in tool_which_corpus("dnssec zone validation")


def test_which_corpus_empty_query() -> None:
    assert "needs a question" in tool_which_corpus("   ")


class _FakeStore:
    """Minimal store exposing only what `route` calls — to simulate a cloud
    fleet table holding a stale entry for a corpus no longer cached."""

    def __init__(self, table: dict, corpora: List[str]) -> None:
        self._table = table
        self._corpora = corpora

    def routing_fleet_table(self) -> dict:
        return self._table

    def list_corpora(self) -> List[str]:
        return self._corpora


def test_route_drops_stale_fleet_entry(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import numpy as np  # pylint: disable=import-outside-toplevel

    from ietf_llm.embeddings.storage import (  # pylint: disable=import-outside-toplevel
        encode_centroid,
    )

    embeddings._MODEL_CACHE["stub"] = _Stub()  # pylint: disable=protected-access
    eye = np.eye(8, dtype=np.float32)

    def entry(axis: int) -> dict:
        return {"model_id": "stub", "dim": 8, "centroids": [encode_centroid(eye[axis])]}

    # The fleet key still has "removed", but list_corpora no longer reports it.
    table = {"live": entry(0), "removed": entry(1)}
    store = _FakeStore(table, ["live"])
    monkeypatch.setattr("ietf_llm.store.corpus.get_corpus_store", lambda: store)

    res = routing.route("quic")
    assert "removed" not in {m.corpus for m in res.matches}
    assert "removed" not in res.no_centroids  # not "missing a topic map" — just gone


def _shared_plus_unique(n: int) -> dict:
    """`n` corpora that each carry a shared centroid (axis 0, the 'generic'
    theme) plus one unique centroid (axis i+1)."""
    import numpy as np  # pylint: disable=import-outside-toplevel

    from ietf_llm.embeddings.storage import (  # pylint: disable=import-outside-toplevel
        encode_centroid,
    )

    eye = np.eye(8, dtype=np.float32)
    return {
        f"c{i}": {
            "model_id": "m",
            "dim": 8,
            "centroids": [encode_centroid(eye[0]), encode_centroid(eye[i + 1])],
        }
        for i in range(n)
    }


def _patch_store(monkeypatch, raw: dict) -> None:  # type: ignore[no-untyped-def]
    store = _FakeStore(raw, list(raw))
    monkeypatch.setattr("ietf_llm.store.corpus.get_corpus_store", lambda: store)


def test_generic_theme_flags(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _patch_store(monkeypatch, _shared_plus_unique(5))
    # The shared axis-0 theme recurs across all corpora → generic; the unique
    # one does not.
    assert routing.generic_theme_flags("c0") == [True, False]


def test_generic_theme_flags_off_below_min_corpora(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Four corpora is below the activation floor — suppression stays off.
    _patch_store(monkeypatch, _shared_plus_unique(4))
    assert routing.generic_theme_flags("c0") is None
