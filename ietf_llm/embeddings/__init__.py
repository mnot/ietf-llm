"""Local semantic search over a Working Group's gathered corpus.

This subpackage is split into four cohesive modules:

  chunking.py  — content-aware chunkers (per-message / per-issue / windowed)
  storage.py   — sqlite layout, vector packing, chunk lookup
  models.py    — embedding-model loading and process-level cache
  search.py    — build_index() and search() — the user-facing operations

For backward compatibility with callers that imported from the previous
single-file module (and to keep the public surface in one place), every
externally-used symbol is re-exported here.
"""

from __future__ import annotations

from .chunking import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    MAX_CHUNK_CHARS,
    Chunk,
    _chunk_file,
    _chunk_issues_file,
    _chunk_message_file,
    _chunk_windowed,
    _eligible_files,
)
from .models import DEFAULT_EMBED_MODEL, _MODEL_CACHE, _get_embed_model
from .search import Hit, build_index, search
from .storage import chunk_counts, find_chunks_by_url, get_chunk, get_messages

__all__ = [
    # Public surface
    "DEFAULT_EMBED_MODEL",
    "Chunk",
    "Hit",
    "build_index",
    "chunk_counts",
    "find_chunks_by_url",
    "get_chunk",
    "get_messages",
    "search",
    # Used by mcp_server.py for pre-warming
    "_get_embed_model",
    "_MODEL_CACHE",
    # Used by tests
    "CHUNK_OVERLAP",
    "CHUNK_SIZE",
    "MAX_CHUNK_CHARS",
    "_chunk_file",
    "_chunk_issues_file",
    "_chunk_message_file",
    "_chunk_windowed",
    "_eligible_files",
]
