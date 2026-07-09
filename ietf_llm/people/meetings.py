"""Attach meeting participation to the people Registry — link-only.

Two signals, both landing ONLY on a person the registry already knows from
mail / drafts / GitHub / roles (a meeting never mints a new actor — attendees
who never otherwise participated stay in the per-meeting roster artifact, not
the registry):

- `_ingest_attendance` — the Datatracker `attended` record, gathered per
  meeting into `attendance.json` (name + person uri). Linked by **person id**:
  we resolve the registry's own addresses to Datatracker person uris (the same
  spine `reconcile_mail_via_datatracker` uses) and match attendees against
  that. Collision-free.
- `_ingest_transcript_speakers` — the `**Name:**` speaker labels in each
  transcript. Linked by **exact canonical name** only (transcript labels carry
  no email); a last-resort match, so we decline anything but an exact hit.

Best-effort: a missing sidecar or a Datatracker outage during address
resolution leaves the registry as-is. Both passes run after the mail / GitHub /
role / draft ingest so they match against the complete registry.
"""

from __future__ import annotations

import json
import os
import re
from typing import TYPE_CHECKING, Dict, Set

from ..gather.sources.datatracker_people import resolve_addresses
from ..log import LogLevel, Verbosity, log
from ..paths import (
    attendance_data_path,
    get_wg_file_cache_dir,
    meetings_dir,
    transcripts_dir,
)

if TYPE_CHECKING:
    from . import Person, Registry

# `**Label:**` at line start = a transcript speaker cue.
_SPEAKER_RE = re.compile(r"^\*\*(?P<name>[^*\n:]+):\*\*", re.MULTILINE)

# The context header `enrich_transcripts` prepends uses the same `**Label:**`
# shape; these are not speakers.
_HEADER_LABELS = {"working group", "date / time", "meeting", "minutes"}


def ingest_meeting_participation(
    registry: "Registry",
    wg: str,
    verbose: Verbosity,
) -> None:
    """Attach attendance + transcript-speaking to known participants."""
    cache_dir = get_wg_file_cache_dir(wg)
    mdir = meetings_dir(cache_dir)
    if not os.path.isdir(mdir):
        return
    codes = sorted(
        name
        for name in os.listdir(mdir)
        if not name.startswith("_") and os.path.isdir(os.path.join(mdir, name))
    )
    if not codes:
        return
    _ingest_attendance(registry, cache_dir, codes, verbose)
    _ingest_transcript_speakers(registry, cache_dir, codes, verbose)


def _uri_to_person(registry: "Registry", verbose: Verbosity) -> "Dict[str, Person]":
    """Map each Datatracker person uri to its registry Person, via the
    person's known addresses. Reuses the cached address→uri resolution the
    mail-reconciliation pass already performed, so this makes no fresh
    network calls in a normal gather."""
    addresses = sorted({e for p in registry.persons for e in p.emails})
    if not addresses:
        return {}
    resolved = resolve_addresses(addresses, verbose=verbose)
    out: "Dict[str, Person]" = {}
    for addr, uri in resolved.items():
        person = registry.person_for_email(addr)
        if person is not None:
            out[uri.rstrip("/")] = person
    return out


def _ingest_attendance(
    registry: "Registry",
    cache_dir: str,
    codes: "list[str]",
    verbose: Verbosity,
) -> None:
    """Link `attendance.json` attendees to registry Persons by person id."""
    uri_to_person = _uri_to_person(registry, verbose)
    if not uri_to_person:
        return
    linked = 0
    unresolved = 0
    for code in codes:
        path = attendance_data_path(cache_dir, code)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                rows = json.load(fh)
        except (OSError, ValueError):
            continue
        for row in rows if isinstance(rows, list) else []:
            uri = str((row or {}).get("person") or "").rstrip("/")
            person = uri_to_person.get(uri)
            if person is not None:
                person.attended_sessions.add(code)
                linked += 1
            else:
                unresolved += 1
    if linked or unresolved:
        log(
            f"  attendance: linked {linked} attendee-session(s) to known "
            f"participants ({unresolved} attend-only, left to the roster)",
            verbose,
            level=LogLevel.STATUS,
        )


def _ingest_transcript_speakers(
    registry: "Registry",
    cache_dir: str,
    codes: "list[str]",
    verbose: Verbosity,
) -> None:
    """Link transcript `**Name:**` speakers to registry Persons by exact name."""
    linked = 0
    unresolved = 0
    for code in codes:
        tdir = transcripts_dir(cache_dir, code)
        if not os.path.isdir(tdir):
            continue
        speakers = _speakers_in(tdir)
        for name in speakers:
            person = registry.person_for_name(name)
            if person is not None:
                person.spoke_at_meetings.add(code)
                linked += 1
            else:
                unresolved += 1
    if linked or unresolved:
        log(
            f"  transcripts: linked {linked} speaker-meeting(s) to known "
            f"participants ({unresolved} unmatched name(s), left as text)",
            verbose,
            level=LogLevel.STATUS,
        )


def _speakers_in(tdir: str) -> "Set[str]":
    """Distinct speaker display names across a meeting's transcript files,
    excluding the context-header labels."""
    names: "Set[str]" = set()
    for fname in os.listdir(tdir):
        if not fname.endswith(".md"):
            continue
        try:
            with open(os.path.join(tdir, fname), "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        for match in _SPEAKER_RE.finditer(text):
            name = match.group("name").strip()
            if name and name.lower() not in _HEADER_LABELS:
                names.add(name)
    return names
