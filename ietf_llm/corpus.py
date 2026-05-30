"""Corpus identity helpers shared by the CLI (`--list`) and the MCP
server (`list_corpora`).

A cached corpus is one of:

  - **group**     — backed by a Datatracker group (WG / RG / edwg / BoF).
                    Carries a status (active / concluded / bof / …).
  - **list**      — a mailing list gathered on its own (no WG).
  - **custom**    — explicit --draft / --github / mixed sources.
  - **synthetic** — an `x-` corpus (no Datatracker lookups at all).

`kind_status` classifies from on-disk artifacts only — no network — so
both `ietf-llm --list` and the MCP discovery tool can describe a corpus
cheaply and identically.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple

from . import config, paths
from .utils import DEFAULT_MONTHS, get_wg_file_cache_dir, is_synthetic_wg

# The gather CLI persists its config under this scope.
_GATHER_SCOPE = "gather"


def kind_status(wg: str) -> Tuple[str, str]:
    """Best-effort `(kind, status)` for a cached corpus.

    `status` is the cached group state (`active` / `concluded` / `bof`
    / …) for group corpora, empty otherwise. Reads `group.md`, then
    falls back to charter / meetings artifacts for caches predating
    `group.md`, then to the persisted source config.
    """
    if is_synthetic_wg(wg):
        return ("synthetic", "")
    cache = get_wg_file_cache_dir(wg)
    gpath = paths.group_path(cache)
    if os.path.isfile(gpath):
        return ("group", _group_status(gpath))
    # group.md absent on older caches; other Datatracker-sourced
    # artifacts still mark a group corpus (status unknown until the
    # next gather rewrites group.md).
    if os.path.isfile(paths.charter_path(cache)) or os.path.isdir(
        paths.meetings_dir(cache)
    ):
        return ("group", "")
    cfg = config.load(wg, _GATHER_SCOPE)
    if cfg.get("mailing_list") and not cfg.get("draft") and not cfg.get("github"):
        return ("list", "")
    return ("custom", "")


def _group_status(group_md_path: str) -> str:
    """Read the `**Status:** …` value from a group.md, or empty."""
    return _group_field(group_md_path, "**Status:**")


def _group_field(group_md_path: str, marker: str) -> str:
    """Read a `**Marker:** …` value from a group.md, or empty."""
    try:
        with open(group_md_path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(marker):
                    return line.split(marker, 1)[1].strip()
    except OSError:
        pass
    return ""


def describe(wg: str) -> str:
    """A brief, network-free 'what this corpus is about' line.

    Drawn from on-disk metadata and the persisted gather config, so the
    CLI (`--list`) and the MCP discovery tool can tell the consumer the
    *subject* of a corpus — the group's name, the list it follows, the
    person whose drafts it tracks — not just its kind. Degrades on older
    caches (a group with no stored name falls back to its shortname; an
    author corpus to the raw `--author` spec) until the next gather.
    """
    kind, _ = kind_status(wg)
    cfg = config.load(wg, _GATHER_SCOPE)
    if kind == "group":
        return _group_field(paths.group_path(get_wg_file_cache_dir(wg)), "**Name:**")
    return _source_subject(cfg)


def _source_subject(cfg: Dict[str, Any]) -> str:
    """Provenance line for a list / custom / synthetic corpus, from its
    persisted source flags. Empty when nothing was recorded."""
    parts: List[str] = []
    if cfg.get("author"):
        parts.append("author: " + str(cfg.get("author_name") or cfg["author"]))
    if cfg.get("new_drafts"):
        months = cfg.get("months") or DEFAULT_MONTHS
        parts.append(f"new Internet-Drafts (last {months} mo)")
    drafts = cfg.get("draft") or []
    if drafts:
        parts.append(_draft_phrase(drafts))
    lists = [_strip_domain(name) for name in (cfg.get("mailing_list") or [])]
    if lists:
        noun = "list" if len(lists) == 1 else "lists"
        parts.append(f"{noun} " + ", ".join(lists))
    repos = cfg.get("github") or []
    if repos:
        noun = "repo" if len(repos) == 1 else "repos"
        parts.append(f"{len(repos)} GitHub {noun}")
    return " · ".join(parts)


def _draft_phrase(drafts: List[str]) -> str:
    """`draft a` / `drafts a, b` / `N drafts (a, …)` — brief either way."""
    if len(drafts) == 1:
        return f"draft {drafts[0]}"
    if len(drafts) == 2:
        return "drafts " + ", ".join(drafts)
    return f"{len(drafts)} drafts ({drafts[0]}, …)"


def _strip_domain(name: str) -> str:
    """`agent2agent@ietf.org` -> `agent2agent`; leave bare names alone."""
    return name.split("@", 1)[0].strip()
