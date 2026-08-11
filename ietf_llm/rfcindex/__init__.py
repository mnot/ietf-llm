"""Reader for the semantic index rfc.fyi publishes over the RFC series.

This subpackage understands *another project's* on-disk format — the
`index.tar.gz` release asset built by rfc.fyi (mnot/rfc.fyi#56): int8
chunk vectors partitioned into IVF clusters, plus the per-RFC source
digests the builder uses to notice reissues. We consume it rather than
running a second embedding pipeline; see issue #230.

**Publisher-side only.** Nothing under `mcp/` may import this. The read
path reads our own `embeddings.db`; this module exists so the seed
publisher can turn rfc.fyi's artifact into one.
"""

from __future__ import annotations

from .fetch import IndexRelease, download_index, latest_release
from .format import (
    Cluster,
    ChunkMeta,
    IndexManifest,
    RfcIndexError,
    dequantise,
    iter_clusters,
    read_centroids,
    read_manifest,
    read_sources,
    verify,
)

__all__ = [
    "ChunkMeta",
    "Cluster",
    "IndexManifest",
    "IndexRelease",
    "RfcIndexError",
    "dequantise",
    "download_index",
    "iter_clusters",
    "latest_release",
    "read_centroids",
    "read_manifest",
    "read_sources",
    "verify",
]
