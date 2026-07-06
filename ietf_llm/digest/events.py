"""Shared event dataclass for the chronological digest.

Extracted from `digest.timeline` so that gather-side modules (notably
`gather.sources.datatracker_history`) can construct events without dragging in
the rendering machinery. Keeping this small and dependency-free avoids
the import cycle that would otherwise form between the gather and
digest layers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Event:
    """One dated event in the WG's chronological log."""

    when: datetime  # tz-aware UTC
    kind: str  # short slug: draft-published, issue-opened, …
    title: str  # one-line description for the digest row
    detail: Optional[str] = None  # optional second-line context
    link: Optional[str] = None  # filename to point readers at
