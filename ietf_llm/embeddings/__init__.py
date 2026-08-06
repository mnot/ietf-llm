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
    EMBED_CHAR_BUDGET,
    EMBED_CHAR_OVERLAP,
    MAX_CHUNK_CHARS,
    Chunk,
    _chunk_file,
    _chunk_issues_file,
    _chunk_message_file,
    _chunk_windowed,
    _eligible_files,
    _window_text,
)
from .models import (
    _MODEL_CACHE,
    DEFAULT_EMBED_MODEL,
    _get_embed_model,
    is_remote_embed_model,
)
from .search import Hit, build_index, index_model, related, search
from .storage import (
    any_indexed_wg,
    chunk_counts,
    chunk_spans,
    find_chunks_by_url,
    get_chunk,
    get_messages,
    probe_index,
    read_topics,
)
from .topics import build_topics, generate_topics, has_topics

__all__ = [
    # Public surface
    "DEFAULT_EMBED_MODEL",
    "Chunk",
    "Hit",
    "any_indexed_wg",
    "build_index",
    "build_topics",
    "chunk_counts",
    "chunk_spans",
    "find_chunks_by_url",
    "generate_topics",
    "has_topics",
    "get_chunk",
    "get_messages",
    "index_model",
    "probe_index",
    "read_topics",
    "related",
    "search",
    # Used by ietf_llm/mcp/server.py for pre-warming
    "_get_embed_model",
    "_MODEL_CACHE",
    "is_remote_embed_model",
    # Used by tests
    "EMBED_CHAR_BUDGET",
    "EMBED_CHAR_OVERLAP",
    "MAX_CHUNK_CHARS",
    "_chunk_file",
    "_chunk_issues_file",
    "_chunk_message_file",
    "_chunk_windowed",
    "_eligible_files",
    "_window_text",
]
