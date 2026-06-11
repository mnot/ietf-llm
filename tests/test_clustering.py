"""Unit tests for the numpy mini-batch k-means primitive (issue #116).

Operates on synthetic matrices, no embedding backend — the primitive is
pure numpy and must be deterministic.
"""

from __future__ import annotations

import numpy as np

from ietf_llm.embeddings.clustering import choose_k, mini_batch_kmeans


def _blobs() -> "np.ndarray":
    """Three tight, well-separated clusters on the unit sphere (8-dim)."""
    rng = np.random.RandomState(0)
    centres = np.eye(3, 8, dtype=np.float32)  # e0, e1, e2 — orthogonal
    rows = []
    for c in centres:
        for _ in range(20):
            rows.append(c + 0.01 * rng.randn(8).astype(np.float32))
    return np.asarray(rows, dtype=np.float32)


def test_recovers_separated_clusters() -> None:
    mat = _blobs()
    centres, assign = mini_batch_kmeans(mat, 3)
    assert centres.shape == (3, 8)
    # Each of the three input blobs lands in a single cluster.
    blocks = [set(assign[0:20]), set(assign[20:40]), set(assign[40:60])]
    assert all(len(b) == 1 for b in blocks)
    # …and the three blobs map to three distinct clusters.
    assert len({next(iter(b)) for b in blocks}) == 3


def test_centroids_are_unit_norm() -> None:
    centres, _ = mini_batch_kmeans(_blobs(), 3)
    norms = np.linalg.norm(centres, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)


def test_deterministic() -> None:
    mat = _blobs()
    c1, a1 = mini_batch_kmeans(mat, 3)
    c2, a2 = mini_batch_kmeans(mat, 3)
    assert np.array_equal(a1, a2)
    assert np.allclose(c1, c2)


def test_k_capped_at_n_rows() -> None:
    mat = _blobs()[:2]
    centres, assign = mini_batch_kmeans(mat, 8)
    assert centres.shape[0] == 2
    assert assign.shape == (2,)


def test_empty_matrix() -> None:
    centres, assign = mini_batch_kmeans(np.zeros((0, 8), dtype=np.float32), 3)
    assert centres.shape[0] == 0
    assert assign.shape == (0,)


def test_no_empty_clusters() -> None:
    # Even with k larger than the natural cluster count, every returned
    # centroid owns at least one point.
    centres, assign = mini_batch_kmeans(_blobs(), 6)
    sizes = np.bincount(assign, minlength=centres.shape[0])
    assert (sizes > 0).all()


def test_choose_k() -> None:
    assert choose_k(0) == 1
    assert choose_k(3) == 3
    assert choose_k(8) == 4  # floor
    assert choose_k(200) == 10
    assert choose_k(100_000) == 32  # ceiling
