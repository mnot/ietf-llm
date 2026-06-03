"""Steer a *new* custom / synthetic corpus toward an existing one that
already covers the same sources, instead of minting a near-duplicate.

Custom and synthetic corpus names are free-form (`x-ai`, `x-llm-stuff`), so
two clients answering the same question invent different names over
overlapping draft / list / repo sets — duplicate embeds, no reuse, and
`list_corpora` clutter. Group and list corpora canonicalise on their
resolved name and don't need this; only custom + synthetic kinds do.

This is a read of existing cache metadata (each corpus's persisted gather
config); the only write is the gather the caller then (maybe) declines to
start. The freshness debounce (`freshness.cli_debounce_skip`) is the sibling
guard for an *already-cached* corpus; this one is for a not-yet-minted name.
"""

from __future__ import annotations

from typing import Any, List, Optional, Tuple

from . import config, freshness
from .utils import cached_wg_names, is_synthetic_wg

SCOPE = "gather"


def _norm_draft(name: str) -> str:
    # Lazy import keeps the gather pipeline's deps off the read-only path;
    # this module is only consulted at the gather entry point.
    from .gather.drafts import (  # pylint: disable=import-outside-toplevel
        normalize_draft_name,
    )

    return normalize_draft_name(name).lower()


def _norm_list(name: str) -> str:
    from .gather.mbox import (  # pylint: disable=import-outside-toplevel
        normalize_list_name,
    )

    return normalize_list_name(name).lower()


def source_signature(
    *,
    mailing_list: Optional[List[str]] = None,
    draft: Optional[List[str]] = None,
    github: Optional[List[str]] = None,
    author: Optional[str] = None,
    new_drafts: bool = False,
) -> "set[str]":
    """Typed, normalised tokens identifying a corpus's explicit sources.

    Drafts are stripped to their version-less base, list names to their
    bare form, repos lower-cased — so the same source written two ways
    (`draft-foo-07.txt` vs `draft-foo`, `tls@ietf.org` vs `tls`) matches.
    """
    tokens: "set[str]" = set()
    for lst in mailing_list or []:
        norm = _norm_list(lst)
        if norm:
            tokens.add(f"list:{norm}")
    for name in draft or []:
        norm = _norm_draft(name)
        if norm:
            tokens.add(f"draft:{norm}")
    for repo in github or []:
        slug = repo.strip().lower().removesuffix(".git").strip("/")
        if slug:
            tokens.add(f"github:{slug}")
    if author:
        tokens.add(f"author:{author.strip().lower()}")
    if new_drafts:
        tokens.add("new-drafts")
    return tokens


def find_overlapping_corpus(
    signature: "set[str]", *, exclude: str
) -> Optional[Tuple[str, List[str]]]:
    """The cached corpus sharing the most sources with `signature`, plus the
    shared tokens (sorted) — or None when nothing overlaps. `exclude` skips
    the corpus being gathered."""
    if not signature:
        return None
    best: Optional[Tuple[str, List[str]]] = None
    best_n = 0
    for name in cached_wg_names():
        if name == exclude:
            continue
        cfg = config.load(name, SCOPE)
        other = source_signature(
            mailing_list=cfg.get("mailing_list"),
            draft=cfg.get("draft"),
            github=cfg.get("github"),
            author=cfg.get("author"),
            new_drafts=cfg.get("new_drafts", False),
        )
        shared = signature & other
        if len(shared) > best_n:
            best = (name, sorted(shared))
            best_n = len(shared)
    return best


def _overlap_hint(corpus: str, existing: str, shared: List[str]) -> str:
    """One-line reuse hint naming the existing corpus and the shared sources."""
    return (
        f"'{corpus}' overlaps an existing corpus: '{existing}' already covers "
        f"{', '.join(shared)}. Prefer reusing '{existing}'; force to mint "
        f"'{corpus}' anyway."
    )


def canonicalize_skip(
    corpus: str,
    *,
    synthetic: bool,
    group_backed: bool,
    mailing_list: Optional[List[str]] = None,
    draft: Optional[List[str]] = None,
    github: Optional[List[str]] = None,
    author: Optional[str] = None,
    new_drafts: bool = False,
) -> Optional[str]:
    """Reuse hint if minting `corpus` (a *new* custom / synthetic corpus)
    would duplicate an existing corpus's sources, else None.

    None — i.e. go ahead and gather — when the corpus already exists (a
    re-gather, not a mint), when it is group-backed or a bare mailing list
    (those canonicalise on their name), or when no cached corpus shares a
    source. `--force` / `force=True` bypass this by not calling it.
    """
    if corpus in cached_wg_names():
        return None  # already cached: a re-gather, the freshness guard's job
    if group_backed:
        return None
    has_custom_sources = bool(draft or github or author or new_drafts)
    if not (synthetic or has_custom_sources):
        return None  # bare list / group-shaped: canonicalises on its name
    signature = source_signature(
        mailing_list=mailing_list,
        draft=draft,
        github=github,
        author=author,
        new_drafts=new_drafts,
    )
    match = find_overlapping_corpus(signature, exclude=corpus)
    if match is None:
        return None
    existing, shared = match
    return _overlap_hint(corpus, existing, shared)


def cli_gather_skip(args: Any, *, synthetic: bool, group_backed: bool) -> Optional[str]:
    """The skip message for a CLI gather invocation, or None to proceed.

    Combines the two entry-point guards: a reuse hint when a *new* custom /
    synthetic corpus would duplicate an existing one, and the freshness
    debounce for an already-cached corpus refreshed too soon. `--force`
    bypasses both.
    """
    if getattr(args, "force", False):
        return None
    hint = canonicalize_skip(
        args.wg,
        synthetic=synthetic,
        group_backed=group_backed,
        mailing_list=args.mailing_list,
        draft=args.draft,
        github=args.github,
        author=args.author,
        new_drafts=args.new_drafts,
    )
    if hint is not None:
        return hint
    return freshness.cli_debounce_skip(args)


def mcp_canonicalize_skip(spec: Any) -> Optional[str]:
    """Reuse hint for an MCP `start_gather` request, or None. Classifies the
    shape offline (synthetic by name; group-ness is unknown without the
    network, so left False — bare names are filtered out by having no custom
    sources, which keeps groups from being flagged)."""
    return canonicalize_skip(
        spec.corpus,
        synthetic=is_synthetic_wg(spec.corpus),
        group_backed=False,
        mailing_list=spec.mailing_list,
        draft=spec.draft,
        github=spec.github,
        author=spec.author,
        new_drafts=spec.new_drafts,
    )
