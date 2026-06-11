"""Topic-map writer→reader round-trip (issue #116, item 1).

Drives the real writer (`generate_topics` over a stub-embedded index) and
reads its `topics.json` back through `read_topics` and the `overview`
renderer — the durable guard the project asks for, rather than asserting
against a hand-built sidecar.

The stub embedding model is *content-aware*: it points a vector at one of
four orthogonal axes chosen by which theme keyword dominates the text, so
distinct themes genuinely cluster apart (a constant-vector stub would
collapse everything into one cluster and test nothing).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List

import numpy as np

from ietf_llm import embeddings
from ietf_llm.digest.overview import build_overview
from ietf_llm.embeddings.search import build_index
from ietf_llm.embeddings.storage import decode_centroid, read_topics
from ietf_llm.embeddings.topics import generate_topics
from ietf_llm.utils import Verbosity, get_wg_file_cache_dir

from conftest import write_cache_file

_THEMES = {"quic": 0, "tls": 1, "http": 2, "dnssec": 3}


class _TopicStub:
    """8-dim unit vector aimed at the axis of the dominant theme keyword."""

    def _axis(self, text: str) -> int:
        low = text.lower()
        return _THEMES[max(_THEMES, key=low.count)]

    def embed(self, text: str) -> Iterable[float]:
        vec = [0.0] * 8
        vec[self._axis(text)] = 1.0
        return vec

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


def _thread(subject: str, keyword: str) -> str:
    body = f"{keyword} " * 30
    return (
        f"# {subject}\n\n"
        "**Span:** 2025-01-01 → 2025-03-01\n"
        "**Messages:** 1\n\n"
        "## Messages\n\n"
        f"### [1] 2025-03-01 10:00 — Alice\n\n"
        f"{body}\n"
    )


def _seed(home: Path, wg: str) -> None:
    embeddings._MODEL_CACHE["stub"] = _TopicStub()  # pylint: disable=protected-access
    # Three threads per theme — 12 docs, comfortably over the topic-map floor.
    for theme in _THEMES:
        for n in range(3):
            write_cache_file(
                home,
                wg,
                f"threads/2025-{theme}-{n}.md",
                _thread(f"{theme} discussion item {n}", theme),
            )
    cache = get_wg_file_cache_dir(wg)
    build_index(wg, cache, model_name="stub", verbose=Verbosity.QUIET)


def test_topics_round_trip(isolated_home: Path) -> None:
    _seed(isolated_home, "wg")
    assert generate_topics("wg", verbose=Verbosity.QUIET) is True

    topics = read_topics("wg")
    assert topics is not None
    assert topics["model_id"] == "stub"
    assert topics["dim"] == 8
    assert topics["doc_unit"] == "file"
    assert topics["n_docs"] == 12

    clusters = topics["clusters"]
    assert clusters
    # All 12 documents are accounted for, no double-counting.
    assert sum(c["size"] for c in clusters) == 12
    # Largest-first ordering.
    sizes = [c["size"] for c in clusters]
    assert sizes == sorted(sizes, reverse=True)

    # Every theme keyword surfaces as a distinguishing term somewhere.
    all_terms = {t for c in clusters for t in c["terms"]}
    for keyword in _THEMES:
        assert keyword in all_terms, keyword

    # Centroids decode to unit-norm vectors of the right width.
    for c in clusters:
        vec = decode_centroid(c["centroid"])
        assert vec.shape == (8,)
        assert np.isclose(np.linalg.norm(vec), 1.0, atol=1e-5)
        assert c["last_active"] == "2025-03-01T10:00:00Z" or c["last_active"] is None
        assert c["exemplars"]  # concrete anchor titles


def test_overview_renders_themes(isolated_home: Path) -> None:
    _seed(isolated_home, "wg")
    generate_topics("wg", verbose=Verbosity.QUIET)
    body = build_overview("wg", get_wg_file_cache_dir("wg"))
    assert "## Main discussion themes" in body


def test_overview_demotes_generic_themes(isolated_home: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from ietf_llm.digest import overview as ov  # pylint: disable=import-outside-toplevel
    from ietf_llm.embeddings.storage import (  # pylint: disable=import-outside-toplevel
        write_topics,
    )

    write_topics(
        "wg",
        {
            "version": 1, "model_id": "stub", "dim": 8, "doc_unit": "file", "n_docs": 60,
            "clusters": [
                {"centroid": "AA==", "size": 50, "label": "agenda, minutes",
                 "terms": [], "exemplars": [], "last_active": None},
                {"centroid": "BB==", "size": 10, "label": "pqc key exchange",
                 "terms": [], "exemplars": [], "last_active": None},
            ],
        },
    )
    # First cluster (biggest) is generic; the smaller one is distinctive.
    monkeypatch.setattr(ov, "generic_theme_flags", lambda wg: [True, False])
    body = "\n".join(ov._themes_section("wg"))  # pylint: disable=protected-access
    assert "common across WGs" in body
    # The distinctive theme is promoted above the bigger-but-generic one.
    assert body.index("pqc key exchange") < body.index("agenda, minutes")


def test_overview_themes_fallback_on_flag_length_mismatch(
    isolated_home: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    from ietf_llm.digest import overview as ov  # pylint: disable=import-outside-toplevel
    from ietf_llm.embeddings.storage import (  # pylint: disable=import-outside-toplevel
        write_topics,
    )

    write_topics(
        "wg",
        {
            "version": 1, "model_id": "stub", "dim": 8, "doc_unit": "file", "n_docs": 60,
            "clusters": [
                {"centroid": "AA==", "size": 50, "label": "agenda, minutes",
                 "terms": [], "exemplars": [], "last_active": None},
                {"centroid": "BB==", "size": 10, "label": "pqc key exchange",
                 "terms": [], "exemplars": [], "last_active": None},
            ],
        },
    )
    # Flags shorter than clusters (the projection-divergence case) → no
    # demotion or tagging; the sidecar order is rendered untouched.
    monkeypatch.setattr(ov, "generic_theme_flags", lambda wg: [True])
    body = "\n".join(ov._themes_section("wg"))  # pylint: disable=protected-access
    assert "common across WGs" not in body
    assert body.index("agenda, minutes") < body.index("pqc key exchange")


def test_too_few_documents_skips(isolated_home: Path) -> None:
    embeddings._MODEL_CACHE["stub"] = _TopicStub()  # pylint: disable=protected-access
    write_cache_file(
        isolated_home, "tiny", "threads/2025-quic-0.md", _thread("quic a", "quic")
    )
    build_index("tiny", get_wg_file_cache_dir("tiny"), model_name="stub", verbose=Verbosity.QUIET)
    assert generate_topics("tiny", verbose=Verbosity.QUIET) is False
    assert read_topics("tiny") is None
    # Overview still renders, just without a themes section.
    assert "## Main discussion themes" not in build_overview(
        "tiny", get_wg_file_cache_dir("tiny")
    )
