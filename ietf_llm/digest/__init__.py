"""Generate digest / index files for a Working Group's gathered corpus.

These files give an LLM consumer (Claude, GPT, etc) a low-context
overview of what's in the gathered corpus so that it can navigate to
specific files without having to read the whole tree. Three files are
produced:

  {wg}-_index.md    -- landing page: what's here, file inventory, usage hints
  {wg}-_issues.md   -- one row per GitHub issue (state, title, labels, etc.)
  {wg}-_threads.md  -- one row per mailing list thread (subject, n_msgs, span)

Digests are built deterministically from structured data already present
in the cache (GitHub JSON, .eml files, filenames). If an `llm` model is
supplied, one-line summaries are added inline for issues and threads.
See the `llm` package (https://llm.datasette.io/) for model configuration.

Implementation is split across cohesive submodules:

  helpers.py     — subject normalisation, date parsing, state case-folding
  summarizer.py  — the optional LLM-backed one-liner wrapper
  issues.py      — GitHub issues digest builder
  threads.py     — mailing list threads digest builder
  index.py       — top-level index + file categorisation

For backward compatibility (and for tests, which import the helpers
directly), every externally-used symbol is re-exported here.
"""

from __future__ import annotations

from typing import List, Optional

from ..utils import LogLevel, Verbosity, log
from .helpers import (
    _fmt_size,
    _normalize_subject,
    _parse_date,
    _short_addr,
    _state_is_open,
)
from .index import _build_index, _inventory
from .issues import _build_issues_digest
from .summarizer import _Summarizer, _llm_setup_help
from .threads import _build_threads_digest

__all__ = [
    "generate_digests",
    # Helpers re-exported for callers / tests
    "_fmt_size",
    "_normalize_subject",
    "_parse_date",
    "_short_addr",
    "_state_is_open",
    "_inventory",
    "_build_index",
    "_build_issues_digest",
    "_build_threads_digest",
    "_Summarizer",
    "_llm_setup_help",
]


def generate_digests(
    wg: str,
    cache_dir: str,
    summarize_model: Optional[str] = None,
    verbose: Verbosity = Verbosity.STATUS,
) -> List[str]:
    """Generate all digest files for the WG. Returns paths of generated files."""
    log("Generating digests...", verbose, level=LogLevel.STATUS)
    summarizer = _Summarizer(summarize_model, verbose)
    if summarize_model and not summarizer.active():
        log(
            "Continuing with deterministic digests only.",
            verbose,
            level=LogLevel.STATUS,
        )

    generated: List[str] = []

    issues_path = _build_issues_digest(cache_dir, wg, summarizer, verbose)
    if issues_path:
        generated.append(issues_path)

    threads_path = _build_threads_digest(wg, cache_dir, summarizer, verbose)
    if threads_path:
        generated.append(threads_path)

    # Index last so it can reference the others
    index_path = _build_index(
        wg,
        cache_dir,
        has_issues_digest=issues_path is not None,
        has_threads_digest=threads_path is not None,
        verbose=verbose,
    )
    generated.append(index_path)

    return generated
