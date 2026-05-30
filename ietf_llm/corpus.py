"""Corpus identity helpers shared by the CLI (`--list`) and the MCP
server (`list_working_groups`).

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
from typing import Tuple

from . import config, paths
from .utils import get_wg_file_cache_dir, is_synthetic_wg

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
    try:
        with open(group_md_path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("**Status:**"):
                    return line.split("**Status:**", 1)[1].strip()
    except OSError:
        pass
    return ""
