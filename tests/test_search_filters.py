"""Tests for faceted search and chunk_date extraction.

Index-side tests use a stub embedding model so they're fast and
deterministic — no HuggingFace, no API calls. The stub just returns
a constant 8-dim vector for every text, so cosine similarity is 1.0
between any two embeddings; that means the model can't *rank* hits,
but it's still useful for exercising the filter SQL: every chunk
either matches the WHERE clause or doesn't, and the test asserts on
which ones come back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List

from ietf_llm import embeddings
from ietf_llm.embeddings.chunking import _normalize_to_utc_iso
from ietf_llm.embeddings.search import build_index, search
from ietf_llm.utils import Verbosity, get_wg_file_cache_dir

from conftest import write_cache_file


# --- _normalize_to_utc_iso -------------------------------------------------


def test_normalize_rfc5322_utc() -> None:
    assert _normalize_to_utc_iso("Mon, 01 Jan 2025 10:00:00 +0000") == "2025-01-01T10:00:00Z"


def test_normalize_rfc5322_other_tz_converts_to_utc() -> None:
    # 2025-01-01 10:00:00 +0500 should become 05:00:00 UTC.
    assert _normalize_to_utc_iso("Mon, 01 Jan 2025 10:00:00 +0500") == "2025-01-01T05:00:00Z"


def test_normalize_rfc5322_naive_assumed_utc() -> None:
    assert _normalize_to_utc_iso("Mon, 01 Jan 2025 10:00:00") == "2025-01-01T10:00:00Z"


def test_normalize_github_format() -> None:
    # github.py's format_date produces "YYYY-MM-DD HH:MM:SS UTC".
    assert _normalize_to_utc_iso("2025-01-01 10:00:00 UTC") == "2025-01-01T10:00:00Z"


def test_normalize_malformed_returns_none() -> None:
    assert _normalize_to_utc_iso("not a date") is None
    assert _normalize_to_utc_iso("") is None


# --- Index-with-stub-model fixtures ----------------------------------------


class _StubModel:
    """Returns a fixed unit vector for any text. Lets search() run
    end-to-end without a real embedding backend."""

    def embed(self, _text: str) -> Iterable[float]:  # llm.EmbeddingModel API
        return [1.0] + [0.0] * 7

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


def _seed_stub_model(model_name: str = "stub") -> None:
    """Inject the stub into the process-level model cache so build_index
    and search both skip the real loader."""
    embeddings._MODEL_CACHE[model_name] = _StubModel()  # pylint: disable=protected-access


def _build_with_stub(wg: str, isolated_home: Path) -> None:
    cache = get_wg_file_cache_dir(wg)
    _seed_stub_model("stub")
    build_index(wg, cache, model_name="stub", verbose=Verbosity.QUIET)


# --- chunk_date in indexed rows --------------------------------------------


def test_thread_chunks_get_chunk_date(isolated_home: Path) -> None:
    # Thread-file message sections carry per-message chunk_date.
    write_cache_file(
        isolated_home, "wg", "wg-thread-2025-01-01-topic-a.md",
        (
            "# Topic A\n\n"
            "**Span:** 2025-01-01 → 2025-01-01\n"
            "**Messages:** 1\n\n"
            "## Messages\n\n"
            "### [1] 2025-01-01 10:00 — Alice\n\n"
            "body\n"
        ),
    )
    _build_with_stub("wg", isolated_home)
    hits = search("wg", "anything", k=10, verbose=Verbosity.QUIET)
    hits_in_range = search(
        "wg", "anything", k=10,
        since="2024-01-01T00:00:00Z", until="2026-01-01T00:00:00Z",
        verbose=Verbosity.QUIET,
    )
    # Header chunk + one message chunk; only the message chunk is dated.
    assert len(hits) >= 1
    assert len(hits_in_range) >= 1


def test_windowed_chunks_have_null_chunk_date_and_are_filtered_out(
    isolated_home: Path,
) -> None:
    write_cache_file(
        isolated_home, "wg", "draft-foo-00.txt", "Body of the draft. " * 100,
    )
    _build_with_stub("wg", isolated_home)
    # No date filter: chunks show up.
    assert search("wg", "x", k=5, verbose=Verbosity.QUIET)
    # With a date filter: chunks with NULL chunk_date are excluded.
    assert (
        search(
            "wg", "x", k=5, since="2024-01-01T00:00:00Z",
            verbose=Verbosity.QUIET,
        )
        == []
    )


# --- Filter behaviour -------------------------------------------------------


def _seed_two_files(isolated_home: Path) -> None:
    """Seed a thread file (dated 2025-01-01) and a per-issue file
    (dated 2026-06-02). The legacy `<wg>-github-<repo>.txt` blob is
    no longer indexed — per-issue .md files cover its content with
    proper per-message chunking."""
    write_cache_file(
        isolated_home, "wg", "wg-thread-2025-01-01-mail-topic.md",
        (
            "# Mail topic\n\n"
            "**Span:** 2025-01-01 → 2025-01-01\n\n"
            "## Messages\n\n"
            "### [1] 2025-01-01 10:00 — Alice\n\nfoo body\n"
        ),
    )
    write_cache_file(
        isolated_home, "wg", "wg-issue-org-repo-1.md",
        (
            "# Issue #1: Issue topic\n\n"
            "**Repository:** org/repo  \n"
            "**State:** OPEN  \n"
            "**Labels:** Vocabulary, Top-Level, ready to close  \n\n"
            "## Description\n\n"
            "### [1] 2026-06-02 10:00 — Alice _(opened issue)_\n\nbody\n"
        ),
    )


def test_file_pattern_filter_restricts_to_thread_files(
    isolated_home: Path,
) -> None:
    _seed_two_files(isolated_home)
    _build_with_stub("wg", isolated_home)
    hits = search(
        "wg", "x", k=10, file_pattern="%-thread-%", verbose=Verbosity.QUIET,
    )
    assert all("-thread-" in h.file for h in hits)
    assert len(hits) >= 1


def test_file_pattern_filter_restricts_to_github(isolated_home: Path) -> None:
    _seed_two_files(isolated_home)
    _build_with_stub("wg", isolated_home)
    hits = search(
        "wg", "x", k=10, file_pattern="%-issue-%", verbose=Verbosity.QUIET,
    )
    assert all("-issue-" in h.file for h in hits)
    assert hits


def test_since_filter_includes_only_newer_chunks(isolated_home: Path) -> None:
    _seed_two_files(isolated_home)
    _build_with_stub("wg", isolated_home)
    # Thread message dated 2025-01-01; issue 2026-06-02. since=2026
    # should leave only the issue chunk.
    hits = search(
        "wg", "x", k=10, since="2026-01-01T00:00:00Z", verbose=Verbosity.QUIET,
    )
    assert all("-issue-" in h.file for h in hits)
    assert hits


def test_until_filter_includes_only_older_chunks(isolated_home: Path) -> None:
    _seed_two_files(isolated_home)
    _build_with_stub("wg", isolated_home)
    hits = search(
        "wg", "x", k=10, until="2026-01-01T00:00:00Z", verbose=Verbosity.QUIET,
    )
    assert all("-thread-" in h.file for h in hits)
    assert hits


def test_combined_filters_can_return_empty(isolated_home: Path) -> None:
    _seed_two_files(isolated_home)
    _build_with_stub("wg", isolated_home)
    # 2030+ contains nothing.
    hits = search(
        "wg", "x", k=10, since="2030-01-01T00:00:00Z", verbose=Verbosity.QUIET,
    )
    assert hits == []


# --- label filter (per-issue file labels in the chunks index) -------------


def test_issue_chunks_carry_lowercased_labels(isolated_home: Path) -> None:
    # The seed file uses mixed case in the `**Labels:**` header line;
    # the chunker lowercases + comma-normalises for predictable LIKE
    # filtering at search time.
    _seed_two_files(isolated_home)
    _build_with_stub("wg", isolated_home)
    hits = search(
        "wg", "x", k=10, file_pattern="%-issue-%", verbose=Verbosity.QUIET,
    )
    # Every chunk in the issue file inherits the same file-level labels.
    assert hits
    for hit in hits:
        assert hit.labels == "vocabulary,top-level,ready to close"


def test_thread_chunks_have_no_labels(isolated_home: Path) -> None:
    _seed_two_files(isolated_home)
    _build_with_stub("wg", isolated_home)
    hits = search(
        "wg", "x", k=10, file_pattern="%-thread-%", verbose=Verbosity.QUIET,
    )
    assert hits
    assert all(h.labels is None for h in hits)


def test_label_filter_matches_substring_case_insensitively(
    isolated_home: Path,
) -> None:
    _seed_two_files(isolated_home)
    _build_with_stub("wg", isolated_home)
    # Caller can pass any case; matched against the normalised column.
    hits = search(
        "wg", "x", k=10, label="Top-Level", verbose=Verbosity.QUIET,
    )
    assert hits
    assert all("top-level" in (h.labels or "") for h in hits)
    # Thread file (no labels at all) must NOT come back.
    assert all("-thread-" not in h.file for h in hits)


def test_label_filter_excludes_unlabelled_chunks(isolated_home: Path) -> None:
    # A label that isn't on the seeded issue should return nothing
    # (and definitely shouldn't surface thread/draft chunks, which
    # have labels=NULL).
    _seed_two_files(isolated_home)
    _build_with_stub("wg", isolated_home)
    hits = search(
        "wg", "x", k=10, label="nonexistent", verbose=Verbosity.QUIET,
    )
    assert hits == []


def test_issue_file_without_labels_line_gets_null_labels(
    isolated_home: Path,
) -> None:
    write_cache_file(
        isolated_home, "wg", "wg-issue-org-repo-2.md",
        (
            "# Issue #2: No labels here\n\n"
            "**Repository:** org/repo  \n"
            "**State:** OPEN  \n\n"
            "## Description\n\n"
            "### [1] 2026-06-02 10:00 — Alice _(opened issue)_\n\nbody\n"
        ),
    )
    _build_with_stub("wg", isolated_home)
    hits = search(
        "wg", "x", k=10, file_pattern="%-issue-%", verbose=Verbosity.QUIET,
    )
    assert hits
    assert all(h.labels is None for h in hits)


# --- mail-archive year-dump exclusion (consumer feedback #7) --------------


def test_mail_archive_year_dump_is_not_indexed(isolated_home: Path) -> None:
    # The legacy `<wg>-mail-archive-YYYY.txt` blob duplicates content
    # already covered by per-thread .md files. It must be excluded
    # from indexing so search hits are de-duplicated.
    write_cache_file(
        isolated_home, "wg", "wg-mail-archive-2025.txt",
        "Subject: legacy\nFrom: a\nDate: 2025-01-01\n\nbody\n",
    )
    write_cache_file(
        isolated_home, "wg", "wg-thread-2025-01-01-topic.md",
        "# T\n\n### [1] 2025-01-01 10:00 — Alice\n\nbody\n",
    )
    _build_with_stub("wg", isolated_home)
    hits = search("wg", "anything", k=20, verbose=Verbosity.QUIET)
    assert hits
    assert all("mail-archive" not in h.file for h in hits)
