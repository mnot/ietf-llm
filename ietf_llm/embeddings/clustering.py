"""Mini-batch k-means over a corpus's vectors — the shared clustering
primitive behind the topic map (`overview`) and, later, centroid routing.

Hand-rolled in numpy on purpose: no scikit-learn, so the serve path stays
torch-free and dependency-light (issue #116). Vectors are assumed L2-
normalised (the index stores them that way; see `storage._pack`), so cosine
similarity is a dot product and squared Euclidean distance is monotone in
`1 - cosine` — we use the dot product for assignment and keep the centroids
renormalised so the same geometry holds for them.

Determinism is a requirement, not a nicety: an unchanged corpus must yield
identical centroids run to run, or the gather content-hash skip and the
cloud publish would churn. Every random choice is drawn from a seeded
`numpy.random.RandomState`, never the global RNG.
"""

from __future__ import annotations

from typing import Any, Tuple, cast

import numpy as np

#: Default mini-batch passes over the data. k-means converges fast on
#: normalised embeddings; a handful of epochs is plenty and keeps the
#: gather-time cost negligible against the embed itself.
_DEFAULT_EPOCHS = 10
#: Mini-batch size. Capped at the corpus size by the caller's loop.
_DEFAULT_BATCH = 256
#: Fixed seed so clustering is reproducible across gathers (see module docstring).
_SEED = 1729


def choose_k(n_docs: int) -> int:
    """Pick a cluster count for `n_docs` documents.

    Scales with sqrt(n) so a small WG gets a few themes and a broad one
    gets more, clamped to [4, 32] so a tiny corpus isn't over-split and a
    huge one stays legible in `overview`. The caller is responsible for
    not calling k-means at all when `n_docs` is below the floor.
    """
    if n_docs <= 4:
        return max(1, n_docs)
    return int(min(32, max(4, round((n_docs / 2) ** 0.5))))


def _normalize_rows(
    mat: "np.ndarray[Any, np.dtype[np.float32]]",
) -> "np.ndarray[Any, np.dtype[np.float32]]":
    """Return `mat` with each row scaled to unit L2 norm (zero rows left as-is)."""
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return cast(
        "np.ndarray[Any, np.dtype[np.float32]]", (mat / norms).astype(np.float32)
    )


def _kmeans_pp_init(
    mat: "np.ndarray[Any, np.dtype[np.float32]]", k: int, rng: np.random.RandomState
) -> "np.ndarray[Any, np.dtype[np.float32]]":
    """k-means++ seeding: pick `k` initial centres spread out across the
    data, so the result doesn't depend on a lucky random draw.

    Distances use squared Euclidean, which on unit vectors is `2 - 2·cos`,
    so a far-in-cosine point is far here too.
    """
    n_rows = mat.shape[0]
    first = rng.randint(n_rows)
    centres = [mat[first]]
    # Squared distance from each point to its nearest chosen centre so far.
    closest = _sq_dist(mat, centres[0])
    for _ in range(1, k):
        total = float(closest.sum())
        if total <= 0.0:
            # All remaining points coincide with a centre; pad with a repeat
            # (caller dedupes empty clusters). Deterministic index.
            centres.append(mat[int(np.argmax(closest)) if n_rows else 0])
            continue
        # Sample the next centre with probability proportional to D².
        probs = closest / total
        nxt = int(rng.choice(n_rows, p=probs))
        centres.append(mat[nxt])
        closest = np.minimum(closest, _sq_dist(mat, mat[nxt]))
    return np.asarray(centres, dtype=np.float32)


def _sq_dist(
    mat: "np.ndarray[Any, np.dtype[np.float32]]",
    point: "np.ndarray[Any, np.dtype[np.float32]]",
) -> "np.ndarray[Any, np.dtype[np.float32]]":
    """Squared Euclidean distance from every row of `mat` to `point`."""
    diff = mat - point
    return cast(
        "np.ndarray[Any, np.dtype[np.float32]]", np.einsum("ij,ij->i", diff, diff)
    )


def mini_batch_kmeans(
    matrix: "np.ndarray[Any, np.dtype[np.float32]]",
    k: int,
    *,
    epochs: int = _DEFAULT_EPOCHS,
    batch_size: int = _DEFAULT_BATCH,
    seed: int = _SEED,
) -> Tuple[
    "np.ndarray[Any, np.dtype[np.float32]]", "np.ndarray[Any, np.dtype[np.intp]]"
]:
    """Cluster the rows of `matrix` into `k` groups; return
    `(centroids, assignments)`.

    `centroids` is `(k', dim)` L2-normalised, where `k' = min(k, n_rows)`
    (a corpus with fewer rows than `k` can't fill `k` clusters).
    `assignments[i]` is the centroid index for row `i`.

    Sculley's mini-batch update with a per-centre learning rate of
    `1/count`; centroids are renormalised after the run so a dot product
    against them stays a cosine. An emptied centre is reseeded to the point
    furthest from its assigned centre, so `k'` distinct clusters survive.
    """
    mat = _normalize_rows(np.asarray(matrix, dtype=np.float32))
    n_rows = mat.shape[0]
    if n_rows == 0:
        return np.zeros((0, mat.shape[1] if mat.ndim == 2 else 0), dtype=np.float32), (
            np.zeros((0,), dtype=np.intp)
        )
    k = max(1, min(k, n_rows))
    rng = np.random.RandomState(seed)  # pylint: disable=no-member
    centres = _kmeans_pp_init(mat, k, rng)
    counts = np.zeros(k, dtype=np.float64)

    batch = min(batch_size, n_rows)
    for _ in range(epochs):
        idx = rng.permutation(n_rows)
        for start in range(0, n_rows, batch):
            rows = idx[start : start + batch]
            pts = mat[rows]
            # Assign each point to its nearest centre (max dot == max cosine).
            assign = np.argmax(pts @ centres.T, axis=1)
            # Per-point gradient step toward the centre, learning rate 1/count.
            for j in range(k):
                members = pts[assign == j]
                if members.shape[0] == 0:
                    continue
                for vec in members:
                    counts[j] += 1.0
                    centres[j] += (vec - centres[j]) / counts[j]
        centres = _normalize_rows(centres)

    # Final hard assignment + empty-cluster repair so every returned centroid
    # owns at least one point (a dead centroid would be a meaningless theme).
    assignments = np.argmax(mat @ centres.T, axis=1).astype(np.intp)
    centres, assignments = _repair_empty(mat, centres, assignments)
    centres = _normalize_rows(centres)
    return centres, assignments


def _repair_empty(
    mat: "np.ndarray[Any, np.dtype[np.float32]]",
    centres: "np.ndarray[Any, np.dtype[np.float32]]",
    assignments: "np.ndarray[Any, np.dtype[np.intp]]",
) -> Tuple[
    "np.ndarray[Any, np.dtype[np.float32]]", "np.ndarray[Any, np.dtype[np.intp]]"
]:
    """Reseed any centroid that ended up with no members to the worst-fit
    point of the largest cluster, then re-assign once. Bounded: at most `k`
    reseeds, so it always terminates."""
    k = centres.shape[0]
    for _ in range(k):
        sizes = np.bincount(assignments, minlength=k)
        empty = np.where(sizes == 0)[0]
        if empty.size == 0:
            break
        for j in empty:
            # Steal the point furthest from its own centre (biggest donor first).
            donor = int(np.argmax(sizes))
            members = np.where(assignments == donor)[0]
            if members.size <= 1:
                continue
            sims = mat[members] @ centres[donor]
            victim = int(members[int(np.argmin(sims))])
            centres[j] = mat[victim]
            assignments[victim] = j
            sizes = np.bincount(assignments, minlength=k)
    return centres, np.argmax(mat @ centres.T, axis=1).astype(np.intp)
