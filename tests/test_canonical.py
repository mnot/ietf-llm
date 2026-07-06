"""Tests for custom/synthetic-corpus canonicalisation (ietf_llm.corpus.canonical):
a new corpus whose explicit sources overlap an already-cached one is steered
toward reuse rather than minted afresh. Group / list corpora and re-gathers
of an existing corpus are out of scope.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ietf_llm import config, freshness
from ietf_llm.corpus import canonical
from ietf_llm.paths import get_wg_file_cache_dir


def _seed(corpus: str, **sources: object) -> None:
    """Materialise a cached corpus: a `files/` dir (so it's discoverable) plus
    a persisted gather config carrying its sources."""
    get_wg_file_cache_dir(corpus)  # creates ~/.cache/ietf-llm/<corpus>/files/
    config.save(corpus, "gather", dict(sources))


def _args(wg: str, **over: object) -> argparse.Namespace:
    base = dict(
        wg=wg,
        mailing_list=None,
        draft=None,
        github=None,
        author=None,
        new_drafts=False,
        force=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


# --- source_signature normalisation ---------------------------------------


def test_signature_normalises_draft_list_repo() -> None:
    sig = canonical.source_signature(
        mailing_list=["TLS@ietf.org"],
        draft=["draft-foo-bar-07.txt"],
        github=["Owner/Repo.git"],
    )
    assert sig == {"list:tls", "draft:draft-foo-bar", "github:owner/repo"}


def test_signature_author_and_new_drafts() -> None:
    sig = canonical.source_signature(author="MNot@mnot.net", new_drafts=True)
    assert sig == {"author:mnot@mnot.net", "new-drafts"}


def test_signature_empty_is_empty() -> None:
    assert canonical.source_signature() == set()


# --- find_overlapping_corpus ----------------------------------------------


def test_find_overlap_matches_shared_draft(isolated_home: Path) -> None:
    _seed("x-existing", draft=["draft-foo-bar"])
    sig = canonical.source_signature(draft=["draft-foo-bar-03"])
    match = canonical.find_overlapping_corpus(sig, exclude="x-new")
    assert match is not None
    name, shared = match
    assert name == "x-existing"
    assert shared == ["draft:draft-foo-bar"]


def test_find_overlap_none_when_disjoint(isolated_home: Path) -> None:
    _seed("x-existing", draft=["draft-foo-bar"])
    sig = canonical.source_signature(draft=["draft-unrelated"])
    assert canonical.find_overlapping_corpus(sig, exclude="x-new") is None


def test_find_overlap_excludes_self(isolated_home: Path) -> None:
    _seed("x-me", draft=["draft-foo-bar"])
    sig = canonical.source_signature(draft=["draft-foo-bar"])
    assert canonical.find_overlapping_corpus(sig, exclude="x-me") is None


def test_find_overlap_prefers_most_shared(isolated_home: Path) -> None:
    _seed("x-one", draft=["draft-a"])
    _seed("x-two", draft=["draft-a"], github=["o/r"])
    sig = canonical.source_signature(draft=["draft-a"], github=["o/r"])
    name, shared = canonical.find_overlapping_corpus(sig, exclude="x-new")
    assert name == "x-two"
    assert len(shared) == 2


# --- canonicalize_skip policy ---------------------------------------------


def test_skip_hint_for_new_overlapping_synthetic(isolated_home: Path) -> None:
    _seed("x-existing", draft=["draft-foo-bar"])
    hint = canonical.canonicalize_skip(
        "x-new", synthetic=True, group_backed=False, draft=["draft-foo-bar"]
    )
    assert hint is not None
    assert "x-existing" in hint and "draft:draft-foo-bar" in hint


def test_skip_none_when_corpus_already_cached(isolated_home: Path) -> None:
    # A re-gather of an existing corpus is the freshness guard's job, not this.
    _seed("x-existing", draft=["draft-foo-bar"])
    _seed("x-dup", draft=["draft-foo-bar"])
    assert (
        canonical.canonicalize_skip(
            "x-dup", synthetic=True, group_backed=False, draft=["draft-foo-bar"]
        )
        is None
    )


def test_skip_none_for_group_backed(isolated_home: Path) -> None:
    _seed("x-existing", draft=["draft-foo-bar"])
    assert (
        canonical.canonicalize_skip(
            "tls", synthetic=False, group_backed=True, draft=["draft-foo-bar"]
        )
        is None
    )


def test_skip_none_for_bare_name(isolated_home: Path) -> None:
    # No synthetic prefix and no custom sources -> a group/list name that
    # canonicalises on its own; not our concern even if a list overlaps.
    _seed("x-existing", mailing_list=["tls"])
    assert (
        canonical.canonicalize_skip(
            "last-call", synthetic=False, group_backed=False, mailing_list=["tls"]
        )
        is None
    )


def test_skip_none_when_no_overlap(isolated_home: Path) -> None:
    _seed("x-existing", draft=["draft-foo-bar"])
    assert (
        canonical.canonicalize_skip(
            "x-new", synthetic=True, group_backed=False, draft=["draft-other"]
        )
        is None
    )


# --- cli_gather_skip: combines canonicalisation + freshness debounce -------


def test_cli_gather_skip_force_bypasses_both(isolated_home: Path) -> None:
    _seed("x-existing", draft=["draft-foo-bar"])
    args = _args("x-new", draft=["draft-foo-bar"], force=True)
    assert canonical.cli_gather_skip(args, synthetic=True, group_backed=False) is None


def test_cli_gather_skip_returns_reuse_hint(isolated_home: Path) -> None:
    _seed("x-existing", draft=["draft-foo-bar"])
    args = _args("x-new", draft=["draft-foo-bar"])
    out = canonical.cli_gather_skip(args, synthetic=True, group_backed=False)
    assert out is not None and "x-existing" in out


def test_cli_gather_skip_falls_through_to_debounce(isolated_home: Path) -> None:
    # No overlap, but the corpus is freshly gathered -> debounce message.
    freshness.record_gather("tls")
    args = _args("tls")
    out = canonical.cli_gather_skip(args, synthetic=False, group_backed=True)
    assert out is not None and "freshness window" in out


def test_cli_gather_skip_none_when_clear(isolated_home: Path) -> None:
    # New, non-overlapping, never gathered -> proceed.
    args = _args("x-brand-new", draft=["draft-unique"])
    assert canonical.cli_gather_skip(args, synthetic=True, group_backed=False) is None
